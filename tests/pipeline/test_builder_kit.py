"""scripts/build_builder_kit.py: the tag check, the refusals, and the pin it writes. The wheel build itself runs in
test_builder_kit_docker.py, because it needs setuptools, which this venv doesn't have."""

import hashlib
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from builder import VERSION
from contracts.schema import validate

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "ghcr.io/example-org/golden-road-builder@sha256:" + "c" * 64


def configured_platform():
    """platform/platform.json as configure.py leaves it."""
    platform = json.loads((ROOT / "platform/platform.json").read_text())
    platform.update(org="example-org", model_gateway_hosts=["llm.example.org"], identity_header="X-Roles-Test",
                    edge_ingress={"namespace": "edge-ns", "pod_labels": {"app": "edge"}})
    return (json.dumps(platform, indent=2) + "\n").encode()


def copy_tree(dest):
    """What the script reads, from this checkout, with a configured platform config."""
    for rel in ("scripts/build_builder_kit.py", "pyproject.toml", "images/builder/requirements.txt"):
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dest / rel)
    shutil.copytree(ROOT / "src", dest / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    (dest / "platform").mkdir()
    (dest / "platform/platform.json").write_bytes(configured_platform())
    return dest


@pytest.fixture
def tree(tmp_path):
    return copy_tree(tmp_path / "platform-pipeline")


def kit(tree, monkeypatch):
    spec = importlib.util.spec_from_file_location("build_builder_kit_under_test", tree / "scripts/build_builder_kit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def fake_wheel(version, out, epoch):
        wheel = out / f"golden_road-{version}-py3-none-any.whl"
        wheel.write_bytes(f"wheel built at {epoch}".encode())
        return wheel

    monkeypatch.setattr(module, "build_wheel", fake_wheel)
    return module


def test_writes_a_valid_pin_for_the_wheel_the_requirements_and_the_platform(tree, tmp_path, monkeypatch):
    out = tmp_path / "dist"
    assert kit(tree, monkeypatch).main(["--tag", f"builder-v{VERSION}", "--image", IMAGE, "--out", str(out), "--source-date-epoch", "7"]) == 0
    pin = json.loads((out / "platform-pin.json").read_text())
    assert validate("platform-pin.v1", pin) == []
    assert pin["builder_version"] == VERSION and pin["image"] == IMAGE
    assert pin["wheel"] == f"golden_road-{VERSION}-py3-none-any.whl" and pin["wheel_sha256"] == hashlib.sha256(b"wheel built at 7").hexdigest()
    assert pin["requirements"] == f"golden_road-{VERSION}-requirements.txt"
    assert (out / pin["requirements"]).read_bytes() == (ROOT / "images/builder/requirements.txt").read_bytes()
    assert pin["requirements_sha256"] == hashlib.sha256((ROOT / "images/builder/requirements.txt").read_bytes()).hexdigest()
    assert pin["platform_config_sha256"] == hashlib.sha256(configured_platform()).hexdigest()
    assert sorted(p.name for p in out.iterdir()) == sorted(["platform-pin.json", pin["wheel"], pin["requirements"]])


@pytest.mark.parametrize("tag", ["builder-v0.0.1", f"v{VERSION}", VERSION, f"builder-v{VERSION}-rc1"])
def test_refuses_a_tag_that_is_not_builder_v_version(tree, tmp_path, monkeypatch, tag):
    with pytest.raises(SystemExit) as exc:
        kit(tree, monkeypatch).main(["--tag", tag, "--image", IMAGE, "--out", str(tmp_path / "dist")])
    assert "builder.VERSION" in str(exc.value) and not (tmp_path / "dist").exists()


@pytest.mark.parametrize("change", ["placeholder", "no-org"])
def test_refuses_an_unconfigured_platform(tree, tmp_path, monkeypatch, change):
    platform = json.loads(configured_platform())
    if change == "placeholder":
        platform["model_gateway_hosts"] = ["llm.replace-" "me.invalid"]
    else:
        del platform["org"]
    (tree / "platform/platform.json").write_text(json.dumps(platform))
    with pytest.raises(SystemExit) as exc:
        kit(tree, monkeypatch).main(["--tag", f"builder-v{VERSION}", "--image", IMAGE, "--out", str(tmp_path / "dist")])
    assert "configure.py" in str(exc.value) and not (tmp_path / "dist").exists()


@pytest.mark.parametrize("image", ["ghcr.io/o/b:latest", "ghcr.io/o/b@sha256:" + "c" * 63, "GHCR.io/o/b@sha256:" + "c" * 64, IMAGE + "\n"])
def test_refuses_an_image_that_is_not_pinned_by_digest(tree, tmp_path, monkeypatch, image):
    with pytest.raises(SystemExit) as exc:
        kit(tree, monkeypatch).main(["--tag", f"builder-v{VERSION}", "--image", image, "--out", str(tmp_path / "dist")])
    assert "digest" in str(exc.value)


def test_refuses_an_output_folder_that_is_not_empty(tree, tmp_path, monkeypatch):
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist/old.whl").write_text("x")
    with pytest.raises(SystemExit):
        kit(tree, monkeypatch).main(["--tag", f"builder-v{VERSION}", "--image", IMAGE, "--out", str(tmp_path / "dist")])

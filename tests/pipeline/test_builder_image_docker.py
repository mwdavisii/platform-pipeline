"""Build the builder image and run it. Opt-in: pytest -m docker"""

import hashlib
import json
import shutil
import os
import uuid
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.docker, pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")]

ROOT = Path(__file__).resolve().parents[2]


def run(image, *args):
    return subprocess.run(["docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp", image, *args],
                          capture_output=True, text=True)


def test_entry_points(builder_image):
    assert run(builder_image, "builder", "--help").returncode == 0
    assert run(builder_image, "golden-road", "--help").returncode == 0


def test_baked_config(builder_image):
    script = ("import hashlib,os,sys;"
              "[print(hashlib.sha256(open(os.environ[v],'rb').read()).hexdigest()) for v in ('BUILDER_PLATFORM_CONFIG','GOLDEN_ROAD_REQUIRED_CHECKS')];"
              "print(os.path.isdir(os.environ['GOLDEN_ROAD_OVERRIDES']))")
    out = run(builder_image, "python", "-c", script)
    assert out.returncode == 0, out.stderr
    platform_sha, required_sha, overrides_is_dir = out.stdout.split()
    assert platform_sha == hashlib.sha256((ROOT / "platform/platform.json").read_bytes()).hexdigest()
    assert required_sha == hashlib.sha256((ROOT / "platform/required-checks.json").read_bytes()).hexdigest()
    assert overrides_is_dir == "True"


def test_non_root(builder_image):
    assert run(builder_image, "id", "-u").stdout.strip() == "10001"


def build_context(tmp_path, required_text):
    ctx = tmp_path / "ctx"
    for name in ("pyproject.toml",):
        ctx.mkdir(exist_ok=True)
        shutil.copy(ROOT / name, ctx / name)
    for name in ("src", "images", "platform"):
        shutil.copytree(ROOT / name, ctx / name)
    (ctx / "platform/required-checks.json").write_text(required_text)
    return ctx


@pytest.mark.parametrize("bad", ['{"required": "nope"}', "not json", '{"required": [{"name": "x"}]}', '{"required": [], "extra": 1}'])
def test_malformed_required_checks_fail_the_build(tmp_path, bad):
    ctx = build_context(tmp_path, bad)
    tag = f"golden-road-builder-badchecks:{uuid.uuid4().hex[:8]}"
    cmd = ["docker", "build", "-q", "-f", "images/builder/Dockerfile", "-t", tag]
    if os.environ.get("GOLDEN_ROAD_DOCKER_BUILD_NETWORK"):
        cmd += ["--network", os.environ["GOLDEN_ROAD_DOCKER_BUILD_NETWORK"]]
    try:
        out = subprocess.run([*cmd, "."], cwd=ctx, capture_output=True, text=True)
        assert out.returncode != 0, "a malformed required-checks.json must not produce an image"
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)


def test_package_version_guide_and_doctor(builder_image):
    """pyproject takes its version from builder.VERSION, so the installed package carries it; guide and doctor ship."""
    out = run(builder_image, "python", "-c", "import builder, importlib.metadata as m; print(m.version('golden-road'), builder.VERSION)")
    assert out.returncode == 0, out.stderr
    installed, constant = out.stdout.split()
    assert installed == constant
    guide = run(builder_image, "builder", "guide")
    assert guide.returncode == 0 and json.loads(guide.stdout)["builder_version"] == constant
    assert run(builder_image, "golden-road", "doctor", "--help").returncode == 0

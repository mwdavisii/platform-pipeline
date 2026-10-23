"""Probe real images: the generated hello app passes; a build whose guard lets readers write fails. Opt-in: pytest -m docker"""

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

pytestmark = [pytest.mark.docker, pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")]
ROOT = Path(__file__).resolve().parents[2]
NETWORK = os.environ.get("GOLDEN_ROAD_DOCKER_BUILD_NETWORK")


def sh(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True)


def hello_tree(tmp_path):
    root = tmp_path / "app"
    shutil.copytree(ROOT / "bootstrap/hello-app", root)
    return root


def app_tree(tmp_path, break_guard=False, hello=False, portal=False):
    if portal:
        root = tmp_path / "app"
        shutil.copytree(ROOT / "bootstrap/golden-road-portal", root, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        return root
    if hello:
        return hello_tree(tmp_path)
    root = tmp_path / "app"
    shutil.copytree(ROOT / "tests/builder/golden/python-fastapi", root)
    shutil.copy(ROOT / "tests/builder/fixtures/python-fastapi/app.py", root / "app.py")
    shutil.copy(ROOT / "tests/builder/fixtures/python-fastapi/requirements.txt", root / "requirements.txt")
    policy_path = root / "_platform/authz-policy.json"
    policy = json.loads(policy_path.read_text())
    # the probe reads the identity header from the baked platform config, so make the golden tree agree with it
    # (it is a placeholder before configure.py and the deploy owners' header after)
    policy["identity_header"] = json.loads((ROOT / "platform/platform.json").read_text())["identity_header"]
    policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n")
    if break_guard:
        for rule in policy["rules"]:
            rule["roles"] = sorted(set(rule["roles"]) | {"reader"})
        policy_path.write_text(json.dumps(policy, indent=2, sort_keys=True) + "\n")
    return root


def probe(builder_image, tmp_path, break_guard, hello=False, portal=False):
    root = app_tree(tmp_path, break_guard, hello, portal)
    tag = f"golden-road-probe-test:{'broken' if break_guard else 'good'}-{uuid.uuid4().hex[:8]}"
    out = tmp_path / "out"
    out.mkdir()
    cid = None
    try:
        build = ["docker", "build", "-q", "-t", tag] + (["--network", NETWORK] if NETWORK else [])
        sh(*build, str(root))
        cid = sh("docker", "run", "-d", "--network", "none", "--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL",
                 "--security-opt", "no-new-privileges", tag).stdout.strip()
        result = sh("docker", "run", "--rm", "--network", f"container:{cid}", "--read-only", "--tmpfs", "/tmp",
                    "--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{root}:/src:ro", "-v", f"{out}:/out", builder_image,
                    "golden-road", "authz-probe", "--repo-root", "/src", "--base-url", "http://127.0.0.1:8080",
                    "--output", "/out/authz.json", check=False)
        report = out / "authz.json"
        assert report.is_file(), f"probe wrote no result (exit {result.returncode}): {result.stderr}"
        return result.returncode, json.loads(report.read_text())
    finally:
        if cid:
            sh("docker", "rm", "-f", cid, check=False)
        sh("docker", "rmi", "-f", tag, check=False)


def test_generated_app_passes(builder_image, tmp_path):
    code, result = probe(builder_image, tmp_path, break_guard=False)
    assert code == 0 and result["status"] == "pass" and result["denied"] > 0


def test_real_hello_app_passes(builder_image, tmp_path):
    code, result = probe(builder_image, tmp_path, break_guard=False, hello=True)
    assert code == 0 and result["status"] == "pass" and result["denied"] > 0


def test_reader_write_hole_fails(builder_image, tmp_path):
    code, result = probe(builder_image, tmp_path, break_guard=True)
    assert code == 1 and result["status"] == "fail"
    assert any(f["method"] == "POST" and f["roles"] == "reader" for f in result["failures"])


def test_real_portal_with_no_environment_passes(builder_image, tmp_path):
    """The preview's exact run: the portal image with no environment starts in unconfigured mode and the probe passes."""
    code, result = probe(builder_image, tmp_path, break_guard=False, portal=True)
    assert code == 0 and result["status"] == "pass" and result["denied"] > 0

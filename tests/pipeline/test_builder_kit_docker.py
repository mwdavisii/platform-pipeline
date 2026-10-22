"""The builder kit built for real in the builder image's Python base: the wheel is reproducible, the pin is valid, and the
kit installs the way the golden-road skill installs it (hashes required, the wheel without dependencies)."""

import hashlib
import json
import os
import re
import shutil
import subprocess

import pytest

from builder import VERSION
from contracts.schema import validate
from test_builder_kit import IMAGE, ROOT, configured_platform, copy_tree

pytestmark = [pytest.mark.docker, pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")]


def base_image():
    """The builder image's own Python base, by digest (images/builder/Dockerfile)."""
    return re.search(r"(?m)^FROM (docker\.io/library/python:3\.12-slim-bookworm@sha256:[0-9a-f]{64})$", (ROOT / "images/builder/Dockerfile").read_text()).group(1)


def test_the_kit_builds_reproducibly_and_installs_like_the_skill_installs_it(tmp_path):
    work = copy_tree(tmp_path / "work")
    script = (
        "set -eu; python -m venv /tmp/kit; "
        "/tmp/kit/bin/pip install -q --disable-pip-version-check --require-hashes --only-binary=:all: -r images/builder/requirements.txt; "
        f"for out in dist1 dist2; do /tmp/kit/bin/python -I scripts/build_builder_kit.py --tag builder-v{VERSION} --image {IMAGE} "
        "--source-date-epoch 1700000000 --out $out --self-test > /dev/null; done"
    )
    cmd = ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp", "-v", f"{work}:/work", "-w", "/work"]
    if os.environ.get("GOLDEN_ROAD_DOCKER_BUILD_NETWORK"):
        cmd += ["--network", os.environ["GOLDEN_ROAD_DOCKER_BUILD_NETWORK"]]
    out = subprocess.run([*cmd, base_image(), "sh", "-c", script], capture_output=True, text=True, timeout=1200)
    assert out.returncode == 0, out.stdout + out.stderr
    one, two = (json.loads((work / d / "platform-pin.json").read_text()) for d in ("dist1", "dist2"))
    assert one == two and validate("platform-pin.v1", one) == [] and one["builder_version"] == VERSION
    assert (work / "dist1" / one["wheel"]).read_bytes() == (work / "dist2" / one["wheel"]).read_bytes()
    assert one["platform_config_sha256"] == hashlib.sha256(configured_platform()).hexdigest()

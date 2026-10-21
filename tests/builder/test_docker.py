"""Build and run the generated images. Opt-in: pytest -m docker

Apps run with --network none (no egress, like production). A probe container
joins the app's network namespace and sends the requests over loopback, so the
tests don't depend on Docker bridge networking or published ports.

Set GOLDEN_ROAD_DOCKER_BUILD_NETWORK=host if `docker build` can't use the bridge
network (for example, the veth kernel module isn't loaded).
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from builder.config import load_base_images
from builder.generate import generate

pytestmark = [pytest.mark.docker, pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")]
FIXTURES = Path(__file__).parent / "fixtures"
BUILD_NETWORK = os.environ.get("GOLDEN_ROAD_DOCKER_BUILD_NETWORK")
PROBE_IMAGE = load_base_images()["python"]["runtime"]

PROBE = r'''
import json, sys, time, urllib.error, urllib.request

base = "http://127.0.0.1:8080"
cases, health = json.loads(sys.argv[1]), sys.argv[2]
deadline = time.time() + 60
while True:
    try:
        urllib.request.urlopen(base + health, timeout=2)
        break
    except urllib.error.HTTPError:
        break
    except OSError:
        if time.time() > deadline:
            sys.exit("app did not start")
        time.sleep(0.5)
out = []
for method, path, roles, body in cases:
    headers = {"content-type": "application/json"}
    if roles:
        headers["X-Platform-Roles"] = roles
    req = urllib.request.Request(base + path, method=method, data=body.encode() if body else None, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            out.append(resp.status)
    except urllib.error.HTTPError as exc:
        out.append(exc.code)
print(json.dumps(out))
'''


def sh(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True)


@pytest.fixture
def running(request, tmp_path):
    name = request.param
    root = tmp_path / name
    shutil.copytree(FIXTURES / name, root, ignore=shutil.ignore_patterns("node_modules", "__pycache__"))
    spec = json.loads((root / "builder-spec.json").read_text())
    generate(root, spec)
    tag = f"golden-road-test/{name}:latest"
    build = ["docker", "build", "-q", "-t", tag]
    if BUILD_NETWORK:
        build += ["--network", BUILD_NETWORK]
    sh(*build, str(root))
    container = sh("docker", "run", "-d", "--network", "none", "--read-only", "--tmpfs", "/tmp",
                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges", tag).stdout.strip()

    def probe(cases):
        proc = sh("docker", "run", "--rm", "--network", f"container:{container}", PROBE_IMAGE,
                  "python", "-c", PROBE, json.dumps(cases), spec.get("health_path", "/"), check=False)
        if proc.returncode != 0:
            logs = sh("docker", "logs", container, check=False)
            pytest.fail(f"probe failed: {proc.stderr}\napp logs:\n{logs.stdout}{logs.stderr}")
        return json.loads(proc.stdout)

    try:
        yield tag, probe
    finally:
        sh("docker", "rm", "-f", container, check=False)


@pytest.mark.parametrize("running", ["python-fastapi", "node-express", "static-site"], indirect=True)
def test_image_runs_as_non_root_under_restricted_flags(running):
    tag, probe = running
    user = sh("docker", "image", "inspect", "--format", "{{.Config.User}}", tag).stdout.strip()
    assert user and user.split(":")[0] not in ("", "0", "root")
    [status] = probe([["GET", "/", None, None]])
    assert status in (200, 403)


@pytest.mark.parametrize("running", ["python-fastapi", "node-express"], indirect=True)
def test_reader_cannot_write(running):
    _, probe = running
    note = json.dumps({"text": "hi"})
    assert probe([
        ["GET", "/api/notes", None, None],
        ["GET", "/api/notes", "reader", None],
        ["POST", "/api/notes", "reader", note],
        ["POST", "/api/notes", "writer", note],
    ]) == [403, 200, 403, 200]

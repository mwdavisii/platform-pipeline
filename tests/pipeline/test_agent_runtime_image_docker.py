"""Build the agent runtime and run it against the mock gateway (spec 2026-11-17 Tests, "the image starts with a mock gateway"). Opt-in:
pytest -m docker. Both containers share one network namespace with no network, as tests/builder/test_docker.py runs apps, and run as
the cluster runs them: a read-only root, no capabilities, no new privileges. The probe's wait is the test's, not the runtime's."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.docker, pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")]
ROOT = Path(__file__).resolve().parents[2]
TAG = "golden-road-test/agent-runtime:latest"
BUILD_NETWORK = os.environ.get("GOLDEN_ROAD_DOCKER_BUILD_NETWORK")
AGENT = "schema: agent/v1\ndisplay_name: Calendar helper\nmodel: chat-default\ninstructions: Help.\nstarters:\n  - When is everyone free?\n"
HARDENED = ["--read-only", "--tmpfs", "/tmp", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
PROBE = r'''
import json, sys, time, urllib.request
deadline = time.time() + 60
while True:
    try:
        health = json.loads(urllib.request.urlopen("http://127.0.0.1:8080/healthz", timeout=2).read()); break
    except OSError:
        if time.time() > deadline: sys.exit("the runtime did not start")
        time.sleep(0.5)
page = urllib.request.urlopen("http://127.0.0.1:8080/", timeout=10)
req = urllib.request.Request("http://127.0.0.1:8080/chat", method="POST", headers={"content-type": "application/json"},
                             data=json.dumps({"messages": [{"role": "user", "content": sys.argv[1]}]}).encode())
print(json.dumps({"health": health, "csp": page.headers["content-security-policy"], "page": page.read().decode(),
                  "chat": json.loads(urllib.request.urlopen(req, timeout=10).read())}))
'''


def sh(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True)


def build_runtime():
    """Build the runtime image as TAG (the template's end-to-end test builds FROM it)."""
    command = ["docker", "build", "-q", "-t", TAG, "-f", str(ROOT / "images/agent-runtime/Dockerfile")]
    if BUILD_NETWORK:
        command += ["--network", BUILD_NETWORK]
    sh(*command, str(ROOT / "images/agent-runtime"))
    return TAG


@pytest.fixture(scope="module")
def image():
    return build_runtime()


def run_with_mock_gateway(image, agent_dir, question):
    """The mock gateway in its own container (no network), the agent in its namespace; the probe asks one question. With agent_dir None
    nothing is mounted: the image's own /app/agent.yaml runs."""
    mock = sh("docker", "run", "-d", "--network", "none", *HARDENED, image,
              "python", "-m", "uvicorn", "agent_runtime.mock_gateway:app", "--host", "127.0.0.1", "--port", "9090").stdout.strip()
    agent = None
    try:
        mount = [] if agent_dir is None else ["-v", f"{agent_dir / 'agent.yaml'}:/app/agent.yaml:ro"]
        agent = sh("docker", "run", "-d", "--network", f"container:{mock}", *HARDENED, *mount,
                   "-e", "AGENT_GATEWAY_URL=http://127.0.0.1:9090/v1", "-e", "AGENT_GATEWAY_KEY=mock-key", image).stdout.strip()
        out = sh("docker", "run", "--rm", "--network", f"container:{mock}", image, "python", "-c", PROBE, question, check=False)
        logs = sh("docker", "logs", agent, check=False)
        assert out.returncode == 0, out.stderr + logs.stdout + logs.stderr
        return json.loads(out.stdout), logs.stdout + logs.stderr
    finally:
        sh("docker", "rm", "-f", *[c for c in (mock, agent) if c], check=False)


def test_the_runtime_answers_through_the_mock_gateway(image, tmp_path):
    (tmp_path / "agent.yaml").write_text(AGENT)
    tmp_path.chmod(0o755)
    (tmp_path / "agent.yaml").chmod(0o644)
    answer, logs = run_with_mock_gateway(image, tmp_path, "When is everyone free?")
    assert answer["health"] == {"ok": True} and "<h1>Calendar helper</h1>" in answer["page"] and "script-src 'self'" in answer["csp"]
    chat = answer["chat"]
    assert chat["reply"] == "Hello from the mock model. You asked: When is everyone free?" and chat["tool_calls"] == []
    assert "agent-runtime: ready with 0 tools" in logs and "mock-key" not in logs and "When is everyone free" not in logs
    assert f"request={chat['request_id']} person=- model=chat-default tools=- outcome=ok" in logs


def test_it_runs_as_the_agent_user(image):
    assert sh("docker", "run", "--rm", "--network", "none", image, "id", "-u").stdout.strip() == "10001"


def test_without_the_key_it_exits_naming_the_setting(image, tmp_path):
    (tmp_path / "agent.yaml").write_text(AGENT)
    (tmp_path / "agent.yaml").chmod(0o644)
    out = sh("docker", "run", "--rm", "--network", "none", *HARDENED, "-v", f"{tmp_path / 'agent.yaml'}:/app/agent.yaml:ro",
             "-e", "AGENT_GATEWAY_URL=http://127.0.0.1:9090/v1", image, check=False)
    assert out.returncode == 2 and "agent-runtime: AGENT_GATEWAY_KEY is not set" in out.stderr


def test_without_agent_yaml_it_exits_naming_the_file(image):
    out = sh("docker", "run", "--rm", "--network", "none", *HARDENED, "-e", "AGENT_GATEWAY_URL=http://127.0.0.1:9090/v1",
             "-e", "AGENT_GATEWAY_KEY=mock-key", image, check=False)
    assert out.returncode == 2 and "agent-runtime: /app/agent.yaml can't be read" in out.stderr

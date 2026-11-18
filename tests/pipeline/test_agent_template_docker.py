"""End to end (spec 2026-11-17 Tests): the template repo's tree builds with the builder, the image starts with a mock gateway, and
/chat answers a starter. Opt-in: pytest -m docker. The generated Dockerfile is checked exactly; to build it, its FROM (the platform's
pinned digest, which no local build has) is swapped for the runtime built here, and nothing else."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from helpers import builder_cmd, builder_env
from test_agent_runtime_image_docker import TAG, build_runtime, run_with_mock_gateway, sh

from builder.config import load_platform
from builder.generate import generate

pytestmark = [pytest.mark.docker, pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")]
ROOT = Path(__file__).resolve().parents[2]
AGENT_TAG = "golden-road-test/agent-calendar:latest"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """The template's tree, generated and verified, built FROM the runtime built here as AGENT_TAG: (the tree, the generated Dockerfile)."""
    saved = os.environ.pop("BUILDER_PLATFORM_CONFIG", None)  # the builder's test config: a non-zero runtime digest
    try:
        yield _build(tmp_path_factory.mktemp("template"))
    finally:
        if saved is not None:
            os.environ["BUILDER_PLATFORM_CONFIG"] = saved
        sh("docker", "rmi", "-f", AGENT_TAG, check=False)


def _build(tmp_path):
    root = tmp_path / "agent-calendar"
    shutil.copytree(ROOT / "bootstrap/golden-road-agent-template", root)
    generate(root, json.loads((root / "builder-spec.json").read_text()))
    runtime = load_platform()["images"]["agent"]["runtime"]
    dockerfile = (root / "Dockerfile").read_text()
    assert dockerfile.splitlines()[1:] == [f"FROM {runtime}", "COPY agent.yaml /app/agent.yaml"]
    env = {k: v for k, v in {**os.environ, **builder_env()}.items() if k != "BUILDER_PLATFORM_CONFIG"}
    verify = subprocess.run([*builder_cmd(), "verify", str(root)], capture_output=True, text=True, env=env)
    assert verify.returncode == 0 and json.loads(verify.stdout)["status"] == "ok", verify.stdout + verify.stderr
    build_runtime()
    (root / "Dockerfile.local").write_text(dockerfile.replace(f"FROM {runtime}", f"FROM {TAG}"))
    sh("docker", "build", "-q", "-t", AGENT_TAG, "-f", str(root / "Dockerfile.local"), str(root))
    return root, dockerfile


def test_the_template_answers_its_starter(built):
    files = sh("docker", "run", "--rm", "--network", "none", "--entrypoint", "ls", AGENT_TAG, "/app").stdout.split()
    assert "agent.yaml" in files and "Dockerfile.local" not in files and "README.md" not in files
    answer, logs = run_with_mock_gateway(AGENT_TAG, None, "What can you do?")
    assert answer["chat"]["reply"] == "Hello from the mock model. You asked: What can you do?"
    assert "<h1>My agent</h1>" in answer["page"] and "What can you do?" in answer["page"]
    assert "agent-runtime: ready with 0 tools" in logs


# The probe, run as the cluster's probe runs it: golden_road.authz_probe from this tree, in the started container's namespace.
PROBE_CALL = "import json; from golden_road.authz_probe import probe; print(json.dumps(probe('/src', 'http://127.0.0.1:8080', ready_timeout=60)))"


def _workflow_start(step_job):
    """The probe step's own lines, from golden-road.yml, that start the app: probe_env and the docker run -d."""
    import yaml
    doc = yaml.safe_load((ROOT / ".github/workflows/golden-road.yml").read_text())
    [step] = [s for s in doc["jobs"][step_job]["steps"] if s.get("name") == "Container authz probe"]
    lines = step["run"].splitlines()
    first = next(i for i, l in enumerate(lines) if l.strip().startswith("probe_env=()"))
    last = next(i for i, l in enumerate(lines) if l.strip().startswith("cid=$(docker run -d"))
    return "\n".join(lines[first:last + 1])


@pytest.mark.parametrize("step_job, image_word", [("preview", "golden-road-preview:pr"), ("release", '"$REF"')])
def test_the_workflow_s_probe_starts_the_agent_and_it_answers(built, tmp_path, step_job, image_word):
    """C1 (final review): the probe steps' own docker run, with no network, keeps an agent's runtime up and the probe gets its answer."""
    root, _ = built
    (tmp_path / "src").mkdir()
    shutil.copy(root / "builder-spec.json", tmp_path / "src/builder-spec.json")
    start = _workflow_start(step_job)
    assert image_word in start
    script = "set -euo pipefail\n" + start.replace(image_word, AGENT_TAG) + '\necho "$cid"\n'
    out = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    cid = out.stdout.strip()
    try:
        answer = sh("docker", "run", "--rm", "--network", f"container:{cid}", "-v", f"{ROOT / 'src'}:/code:ro",
                    "-v", f"{tmp_path / 'src'}:/src:ro", "-e", "PYTHONPATH=/code", "-e", "PYTHONDONTWRITEBYTECODE=1", TAG,
                    "python", "-c", PROBE_CALL, check=False)
        logs = sh("docker", "logs", cid, check=False)
        assert answer.returncode == 0, answer.stderr + logs.stdout + logs.stderr
        assert json.loads(answer.stdout) == {"status": "not-declared"}
        assert sh("docker", "inspect", "-f", "{{.State.Running}}", cid).stdout.strip() == "true", logs.stdout + logs.stderr
        assert "agent-runtime: ready with 0 tools" in logs.stdout + logs.stderr
    finally:
        sh("docker", "rm", "-f", cid, check=False)

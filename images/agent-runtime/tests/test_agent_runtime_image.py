"""The runtime image's Dockerfile: the builder's Python runtime base (decided while planning: item 2), its user, its lock, its command."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RUNTIME = ROOT / "images/agent-runtime"
DOCKERFILE = (RUNTIME / "Dockerfile").read_text()


def test_the_base_is_the_builder_s_python_runtime():
    base = json.loads((ROOT / "src/builder/data/base_images.json").read_text())["python"]["runtime"]
    froms = [line.split()[1] for line in DOCKERFILE.splitlines() if line.startswith("FROM ")]
    assert froms == [base, base]


def test_the_user_is_the_one_the_builder_runs_agents_as():
    import sys
    sys.path.insert(0, str(ROOT / "src"))
    from builder.config import AGENT_UID
    assert f"USER {AGENT_UID}:{AGENT_UID}" in DOCKERFILE and f"--uid {AGENT_UID}" in DOCKERFILE


def test_the_dependencies_install_by_hash_and_the_command_is_the_runtime():
    assert "pip install --require-hashes --only-binary=:all: -r /tmp/requirements.txt" in DOCKERFILE
    assert DOCKERFILE.rstrip().endswith('CMD ["python", "-m", "agent_runtime"]') and "EXPOSE 8080" in DOCKERFILE


def test_nothing_is_written_at_run_time():
    """A read-only root file system: no bytecode written, the venv and the package owned by root (the user reads them only)."""
    assert "PYTHONDONTWRITEBYTECODE=1" in DOCKERFILE and "--chown" not in DOCKERFILE


def test_the_build_context_is_the_package_and_its_lock():
    ignored = [line for line in (RUNTIME / ".dockerignore").read_text().splitlines() if line and not line.startswith("#")]
    assert {"tests", "requirements.in", "**/__pycache__"} <= set(ignored)

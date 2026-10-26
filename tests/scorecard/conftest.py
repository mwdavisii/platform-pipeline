import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def factory(tmp_path):
    """A writable copy of the sample factory layout (config/, intake/, na/, suppressions/)."""
    dest = tmp_path / "factory"
    shutil.copytree(FIXTURES / "factory", dest)
    return dest


@pytest.fixture
def config(factory):
    from scorecard.config import load_config

    return load_config(factory)



# Hermetic git (no inherited GIT_*, no user or system config, no hooks, pinned dates): the one the golden harness builds its fixtures with.
from scorecard.golden import GIT, GIT_ENV  # noqa: E402
from scorecard.golden import GREEN as GREEN_GATE1  # noqa: E402,F401


class GitRepo:
    """A throwaway repo: write or delete files, then commit and get the SHA."""

    def __init__(self, root):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q")

    def git(self, *args) -> str:
        import subprocess

        return subprocess.run([*GIT, "-C", str(self.root), *args], check=True, capture_output=True, text=True, env=GIT_ENV).stdout.strip()

    def write(self, files: dict) -> None:
        for rel, text in files.items():
            path = self.root / rel
            if text is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def commit(self, files: dict | None = None, message: str = "change") -> str:
        self.write(files or {})
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def git_repo(tmp_path):
    return GitRepo(tmp_path / "app")


ESTABLISHED = {"exists": True, "version_exists": True, "first_release": "2019-01-01", "maintainers": 5, "has_wheel": True}


class FakeRegistry:
    """Stands in for scorecard.registry.RegistryClient: facts per package name, or an exception to raise."""

    def __init__(self, facts):
        self.facts = facts
        self.calls = []

    def lookup(self, ecosystem, name, version):
        self.calls.append((ecosystem, name, version))
        value = self.facts.get(name, ESTABLISHED)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def fake_registry():
    return FakeRegistry
NOW = "2026-10-21T12:00:00Z"
NOTES_APP = "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n@app.get('/api/notes')\ndef list_notes(user):\n    return [n for n in NOTES if n.owner == user]\n"
NOTES_EVIDENCE = {"file": "app.py", "start_line": 8, "snippet": "return [n for n in NOTES if n.owner == user]"}


@pytest.fixture
def pr_checkout(git_repo, notes_spec):
    """Check out a PR merge commit (base on main, the change on `pr`, merged --no-ff) and return the keyword
    arguments for scorecard.build.prepare, without `factory`."""
    import json

    def _make(changes=None, *, message="change", title="Notes", body=""):
        base = git_repo.commit({"app.py": NOTES_APP, "tests/test_api.py": "def test_list():\n    assert True\n",
                                "requirements.txt": "fastapi==0.115.0\n", "builder-spec.json": json.dumps(notes_spec, indent=2)}, message="base")
        git_repo.git("checkout", "-q", "-b", "pr")
        tip = git_repo.commit(changes if changes is not None else {"app.py": NOTES_APP + "\n# tidy\n"}, message=message)
        git_repo.git("checkout", "-q", "main")
        git_repo.git("merge", "-q", "--no-ff", "-m", "merge", "pr")
        event = {"pull_request": {"draft": False, "title": title, "body": body, "head": {"sha": tip, "repo": {"full_name": "acme/notes"}},
                                  "base": {"repo": {"full_name": "acme/notes"}}}}
        return {"repo_root": git_repo.root, "repo": "acme/notes", "base_ref": base, "checks_doc": {**GREEN_GATE1, "sha": tip},
                "event": event, "now": NOW}

    return _make


@pytest.fixture
def routing_factory(tmp_path):
    """A writable copy of the routing factory: scorecard-config/v2 plus the starting routing.toml (no na/)."""
    dest = tmp_path / "routing-factory"
    shutil.copytree(FIXTURES / "factory-routing", dest)
    return dest


@pytest.fixture
def routing_config(routing_factory):
    from scorecard.config import load_config

    return load_config(routing_factory)

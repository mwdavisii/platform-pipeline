"""scripts/push-bootstrap.sh pushes only the files git tracks under bootstrap/<name>: never caches, build output or other
untracked files from the working folder (the first portal push carried 65 __pycache__ files)."""

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


def test_only_tracked_files_are_pushed(tmp_path):
    tree = tmp_path / "platform"
    (tree / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/push-bootstrap.sh", tree / "scripts/push-bootstrap.sh")
    app = tree / "bootstrap/demo"
    (app / "src").mkdir(parents=True)
    (app / "README.md").write_text("demo\n")
    (app / "src/app.py").write_text("print('hi')\n")
    git(tree, "init", "-q", "-b", "main")
    git(tree, "add", "-A")
    git(tree, "commit", "-q", "-m", "tree")
    (app / "src/app.py").write_text("print('changed')\n")  # a tracked file's working copy is what gets pushed
    (app / "src/__pycache__").mkdir()
    (app / "src/__pycache__/app.cpython-313.pyc").write_bytes(b"\x00")
    (app / "notes.local").write_text("untracked\n")

    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "gh"
    fake.write_text(f"""#!/usr/bin/env bash
case "$*" in
  "repo create"*) exit 0;;
  *"--json visibility"*) echo PRIVATE;;
  *"--json url"*) echo "{remote}";;
  "auth git-credential"*) exit 0;;
  *) echo "unexpected gh $*" >&2; exit 1;;
esac
""")
    fake.chmod(0o755)
    # The script commits the tree; a CI runner has no git identity of its own.
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    out = subprocess.run(["bash", str(tree / "scripts/push-bootstrap.sh"), "acme", "demo", "--private"], cwd=tree, env=env,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    pushed = sorted(git(tmp_path, "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main").split())
    assert pushed == ["README.md", "src/app.py"]
    assert git(tmp_path, "--git-dir", str(remote), "show", "main:src/app.py") == "print('changed')\n"


TEMPLATE_DOCKERFILE = "FROM ghcr.io/example-org/golden-road-agent-runtime@sha256:" + "2" * 64 + "\nCOPY agent.yaml /app/agent.yaml\n"


def test_the_agent_template_is_marked_a_template_repository_after_the_push(tmp_path):
    """(decided while planning: item 35) People create their agent repos from it, so the push ends by marking it a GitHub template."""
    from test_bootstrap import push_tree

    out, calls = push_tree(tmp_path, "PRIVATE", name="golden-road-agent-template", files={"Dockerfile": TEMPLATE_DOCKERFILE})
    assert out.returncode == 0, out.stderr
    lines = calls.splitlines()
    assert lines[-1] == "gh repo edit org/golden-road-agent-template --template"
    assert next(i for i, l in enumerate(lines) if l.startswith("git ") and " push " in f"{l} ") < len(lines) - 1
    assert "marked org/golden-road-agent-template as a template repository" in out.stdout


def test_the_template_is_not_pushed_while_its_runtime_is_the_zero_digest(tmp_path):
    from test_bootstrap import push_tree

    zero = TEMPLATE_DOCKERFILE.replace("2" * 64, "0" * 64)
    out, calls = push_tree(tmp_path, "PRIVATE", name="golden-road-agent-template", files={"Dockerfile": zero})
    assert out.returncode == 2 and "configure.py" in out.stderr and calls == ""


def test_no_other_repository_is_marked_a_template(tmp_path):
    from test_bootstrap import push_tree

    for i, name in enumerate(("hello-app", "golden-road-portal", "golden-road-agent-template-old")):
        out, calls = push_tree(tmp_path / str(i), "PRIVATE", name=name)
        assert out.returncode == 0, out.stderr
        assert "repo edit" not in calls, name


def test_a_failed_template_mark_after_the_push_says_how_to_finish(tmp_path):
    """The content is already pushed when `gh repo edit --template` runs: a failure exits non-zero and prints the one command left to run."""
    from test_bootstrap import push_tree

    out, calls = push_tree(tmp_path, "PRIVATE", name="golden-road-agent-template", files={"Dockerfile": TEMPLATE_DOCKERFILE}, edit_exit=1)
    assert out.returncode != 0 and calls.splitlines()[-1] == "gh repo edit org/golden-road-agent-template --template"
    assert "pushed org/golden-road-agent-template" in out.stdout and "marked" not in out.stdout
    assert "gh repo edit org/golden-road-agent-template --template" in out.stderr

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
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    out = subprocess.run(["bash", str(tree / "scripts/push-bootstrap.sh"), "acme", "demo", "--private"], cwd=tree, env=env,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    pushed = sorted(git(tmp_path, "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "main").split())
    assert pushed == ["README.md", "src/app.py"]
    assert git(tmp_path, "--git-dir", str(remote), "show", "main:src/app.py") == "print('changed')\n"

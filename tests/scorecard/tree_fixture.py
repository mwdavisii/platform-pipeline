"""A committed tree for the registration floor's tests: files (bytes) at the repo root, optional symlinks, and optionally a .gitattributes
that is added in a later commit and then smudged into the working tree, as a pull request's merge checkout would."""

import subprocess
from pathlib import Path

from scorecard.golden import GIT, GIT_ENV


def _git(root, *args) -> str:
    return subprocess.run([*GIT, "-C", str(root), *args], check=True, capture_output=True, text=True, env=GIT_ENV).stdout.strip()


def commit_tree(root, files: dict, *, symlinks: dict | None = None, attributes: str | None = None) -> str:
    """Commit `files` ({name: bytes}) and `symlinks` ({name: target name}) at `root`; return the commit SHA.

    With `attributes`, a second commit adds .gitattributes and every file is checked out again, so a `working-tree-encoding` rule
    rewrites the files on disk while the stored blobs stay as committed."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    for name, content in files.items():
        (root / name).write_bytes(content)
    for name, target in (symlinks or {}).items():
        (root / name).symlink_to(target)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", "tree")
    if attributes is not None:
        (root / ".gitattributes").write_text(attributes)
        _git(root, "add", ".gitattributes")
        _git(root, "commit", "-q", "-m", "attributes")
        for name in files:
            (root / name).unlink()
        _git(root, "checkout", "--", ".")
    return _git(root, "rev-parse", "HEAD")

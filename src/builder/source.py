"""The single place that reads the app tree.

The tree is hostile input (the pipeline runs on pull requests), so every read
here refuses symlinks that leave the tree, non-regular files, oversize files,
and binary files, and never raises on I/O errors.
"""

import json
import os
from pathlib import Path

MAX_SOURCE_BYTES = 1_000_000
MAX_TREE_BYTES = 50_000_000
# Vendored and VCS directories, skipped at any depth.
SKIP_DIR_NAMES = {".git", "node_modules", ".venv", "venv", "__pycache__"}
# Paths the builder itself writes, skipped relative to the root only. Nothing else
# is skipped: build/, dist/ and deploy/ can hold real application code.
BUILDER_OWNED = (("_platform",), ("deploy", "base"))


def describe_skip(rel: str, reason: str, question: bool = False) -> str:
    if reason == "outside":
        text = f"{rel} links outside the app directory."
        return text + " Remove the link or copy the file into the app so routes can be checked." if question else text
    why = {
        "oversize": "larger than 1 MB",
        "binary": "contains a NUL byte",
        "budget": "the source tree exceeds the 50 MB scan budget",
        "not a regular file": "not a regular file",
        "unreadable": "unreadable",
    }.get(reason, reason)
    text = f"Couldn't scan {rel} ({why})."
    return text + " Split, clean, or remove it so routes can be checked." if question else text


class Tree:
    """A source tree with cached, hardened reads. Create one per detect or check_spec call.

    Anything that can't be read is recorded (warnings for every skip, `problems` for
    scannable files) so callers never treat a skipped file as a clean one.
    """

    def __init__(self, root):
        self.root = Path(root)
        self._real = self.root.resolve()
        self.warnings: list[str] = []
        self.problems: dict[str, tuple[str, str | None]] = {}  # rel path -> (reason, suffix)
        self._cache: dict[Path, str | None] = {}
        self._reasons: dict[Path, str] = {}
        self._total = 0

    def contains(self, path) -> bool:
        try:
            return Path(path).resolve().is_relative_to(self._real)
        except (OSError, RuntimeError):
            return False

    def _rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    def read_text(self, path) -> str | None:
        path = Path(path)
        if path not in self._cache:
            self._cache[path] = self._load(path)
            reason = self._reasons.get(path)
            if reason and reason != "missing":
                self.warnings.append(describe_skip(self._rel(path), reason))
        return self._cache[path]

    def _load(self, path: Path) -> str | None:
        def skip(reason):
            self._reasons[path] = reason
            return None

        try:
            if path.is_symlink() and not self.contains(path):
                return skip("outside")
            if not path.exists():
                return skip("missing")
            if not path.is_file():
                return skip("not a regular file")
            if not self.contains(path):
                return skip("outside")
            size = path.stat().st_size
            if size > MAX_SOURCE_BYTES:
                return skip("oversize")
            if self._total + size > MAX_TREE_BYTES:
                return skip("budget")
            with path.open("rb") as handle:
                data = handle.read(MAX_SOURCE_BYTES + 1)
        except (OSError, RuntimeError):
            return skip("unreadable")
        if len(data) > MAX_SOURCE_BYTES:
            return skip("oversize")
        if b"\0" in data:
            return skip("binary")
        self._total += len(data)
        return data.decode("utf-8", errors="replace")

    def _skip_dir(self, parent_parts: tuple, name: str) -> bool:
        if name in SKIP_DIR_NAMES:
            return True
        return (*parent_parts, name) in BUILDER_OWNED

    def files(self, suffixes) -> list[Path]:
        found = []
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            parent = Path(dirpath).relative_to(self.root).parts
            dirnames[:] = sorted(d for d in dirnames if not self._skip_dir(parent, d))
            for name in dirnames:
                path = Path(dirpath, name)
                if path.is_symlink() and not self.contains(path):
                    self.problems[self._rel(path)] = ("outside", None)
            for name in sorted(filenames):
                path = Path(dirpath, name)
                if path.suffix not in suffixes:
                    continue
                if self.read_text(path) is None:
                    self.problems[self._rel(path)] = (self._reasons.get(path, "unreadable"), path.suffix)
                else:
                    found.append(path)
        return sorted(found, key=lambda p: p.relative_to(self.root).parts)

    def scan_problems(self, suffixes) -> list[str]:
        """Messages for scannable files (and directory links) that could not be scanned."""
        self.files(suffixes)
        return [describe_skip(rel, reason, question=True)
                for rel, (reason, suffix) in sorted(self.problems.items())
                if suffix is None or suffix in suffixes]

    def package_json(self) -> dict | None:
        return _sanitize_package(self.json_object("package.json"))

    def json_object(self, name: str) -> dict | None:
        text = self.read_text(self.root / name)
        if text is None:
            return None
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            return None
        return data if isinstance(data, dict) else None


def _sanitize_package(pkg: dict | None) -> dict | None:
    if pkg is None:
        return None
    for key in ("scripts", "dependencies"):
        if not isinstance(pkg.get(key, {}), dict):
            pkg[key] = {}
    if "main" in pkg and not isinstance(pkg["main"], str):
        del pkg["main"]
    return pkg


def as_tree(root) -> Tree:
    return root if isinstance(root, Tree) else Tree(root)


def source_files(root, suffixes, warnings: list | None = None) -> list[Path]:
    tree = as_tree(root)
    found = tree.files(suffixes)
    if warnings is not None:
        warnings.extend(tree.warnings)
    return found


def read_text(path, root, warnings: list | None = None) -> str | None:
    tree = as_tree(root)
    text = tree.read_text(path)
    if warnings is not None:
        warnings.extend(tree.warnings)
    return text


def package_json(root) -> dict | None:
    return as_tree(root).package_json()

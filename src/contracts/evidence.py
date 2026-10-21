"""Mechanical checks that a finding's evidence points at real code in the repo.

Applied to agentic findings: a model can hallucinate or be prompt-injected
into citing code that isn't there. An anchor passes only if the file is
inside the repo and the snippet appears at the cited line.
"""

from pathlib import Path

from .findings import normalize_snippet


MAX_FILE_BYTES = 5 * 1024 * 1024
MIN_SUBSTRING_CHARS = 8


def anchor_ok(repo_root: Path, anchor: dict) -> bool:
    """True only if the snippet really appears at the cited line. Hostile input returns False, never raises."""
    try:
        return _anchor_ok(Path(repo_root), anchor)
    except (OSError, ValueError, TypeError):
        return False


def _anchor_ok(repo_root: Path, anchor) -> bool:
    if not isinstance(anchor, dict):
        return False
    file, snippet, start_line = anchor.get("file"), anchor.get("snippet"), anchor.get("start_line")
    if not isinstance(file, str) or not isinstance(snippet, str) or type(start_line) is not int:
        return False
    if start_line < 1 or "\x00" in file or not normalize_snippet(snippet):
        return False
    root = repo_root.resolve()
    path = (root / file).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return False
    if path.stat().st_size > MAX_FILE_BYTES:
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    snippet_lines = snippet.splitlines()
    start = start_line - 1
    window = lines[start : start + len(snippet_lines)]
    if len(window) != len(snippet_lines):
        return False
    if all(normalize_snippet(a) == normalize_snippet(b) for a, b in zip(snippet_lines, window)):
        return True
    normalized = normalize_snippet(snippet)
    return len(normalized) >= MIN_SUBSTRING_CHARS and normalized in normalize_snippet("\n".join(window))


def verify_evidence(finding: dict, repo_root: Path) -> dict:
    """Return a copy of the finding with evidence_verified set from its anchors."""
    out = dict(finding)
    repo_root = Path(repo_root)
    anchors = finding.get("evidence")
    out["evidence_verified"] = isinstance(anchors, list) and bool(anchors) and all(anchor_ok(repo_root, a) for a in anchors)
    return out

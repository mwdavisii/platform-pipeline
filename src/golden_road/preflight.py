"""Tree checks that run before anything else.

- Paths that differ only by case (architecture requirement 5).
- The generated guard's marker header written anywhere outside the builder-owned `_platform/`, because the
  container authz probe trusts that marker to tell the guard's 403 from the app's own.
"""

import os
import unicodedata

from contracts.findings import make_finding
from contracts.schema import validate

from .io import InputError


def _collision(first: str, second: str) -> dict:
    location_file = second
    finding = _make(location_file, first, second)
    if validate("finding.v1", finding):
        finding = _make("<unknown>", repr(first), repr(second))
    errors = validate("finding.v1", finding)
    if errors:
        raise InputError(f"can't build a valid case-collision finding: {errors[0]}")
    return finding


def _make(location_file: str, first: str, second: str) -> dict:
    return make_finding(
        layer="deterministic",
        tool="golden-road-preflight",
        rule_id="pipeline.case-collision",
        category="iac",
        severity="high",
        location={"kind": "file", "file": location_file},
        evidence=[{"file": location_file, "start_line": 1, "snippet": f"{first} vs {second}"}],
        evidence_verified=True,
        title="Paths differ only by case",
        rationale="On a case-insensitive filesystem one of these replaces the other, so the build could use a file that wasn't checked.",
        fix={"summary": "Rename or remove one of the paths.", "steps": [f"Keep one of {first!r} and {second!r}"]},
        standards=[{"framework": "cwe", "id": "CWE-178"}],
    )


def case_collisions(root) -> list[dict]:
    seen: dict[str, str] = {}
    findings = []
    def fail(exc):
        raise InputError(f"can't read directory {exc.filename!r}: {type(exc).__name__}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=fail):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        rel_dir = os.path.relpath(dirpath, root)
        for name in sorted(dirnames + filenames):
            rel = os.path.normpath(os.path.join(rel_dir, name)).replace(os.sep, "/")
            rel = os.fsencode(rel).decode("utf-8", "backslashreplace")
            key = unicodedata.normalize("NFC", rel).lower()
            if key in seen:
                findings.append(_collision(seen[key], rel))
            else:
                seen[key] = rel
    return findings


MARKER = b"x-platform-authz"
CHUNK = 1 << 20


def _marker_line(path) -> tuple[int, str] | None:
    """Stream the file; return (line number, line text) of the first marker, case-insensitively."""
    line, carry, tail = 1, b"", b""
    with open(path, "rb") as handle:
        while block := handle.read(CHUNK):
            window = tail + block
            index = window.lower().find(MARKER)
            if index >= 0:
                line_start = window.rfind(b"\n", 0, index) + 1
                line_end = window.find(b"\n", index)
                text = window[line_start:line_end if line_end >= 0 else len(window)]
                number = line + window.count(b"\n", 0, index) - tail.count(b"\n")
                return number, text.decode("utf-8", "backslashreplace").strip()[:200] or "x-platform-authz"
            line += block.count(b"\n")
            tail = window[-(len(MARKER) - 1):]
    return None


def _forged(rel: str, number: int, text: str) -> dict:
    def build(file):
        return make_finding(
            layer="deterministic",
            tool="golden-road-preflight",
            rule_id="pipeline.forged-guard-marker",
            category="authz",
            severity="high",
            location={"kind": "line", "file": file, "start_line": number, "end_line": number},
            evidence=[{"file": file, "start_line": number, "snippet": text}],
            evidence_verified=True,
            title="App code writes the generated guard's marker header",
            rationale="X-Platform-Authz marks the platform guard's own 403. App code that sets it can imitate the guard and fool the container authz probe.",
            fix={"summary": "Remove the X-Platform-Authz header from app code.", "steps": ["Only the generated _platform/ code may set X-Platform-Authz", "Return your own 403s without that header"]},
            standards=[{"framework": "cwe", "id": "CWE-290"}],
        )

    finding = build(rel)
    if validate("finding.v1", finding):
        finding = build("<unknown>")
        finding["location"] = {"kind": "file", "file": "<unknown>"}
        finding["evidence"][0]["snippet"] = f"{rel!r}: {text}"[:200]
        from contracts.findings import finding_id
        finding["id"] = finding_id(finding["rule_id"], "<unknown>", [finding["evidence"][0]["snippet"]])
    errors = validate("finding.v1", finding)
    if errors:
        raise InputError(f"can't build a valid forged-marker finding: {errors[0]}")
    return finding


def forged_guard_markers(root) -> list[dict]:
    root = os.fspath(root)
    findings = []

    def fail(exc):
        raise InputError(f"can't read directory {exc.filename!r}: {type(exc).__name__}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=fail):
        rel_dir = os.path.relpath(dirpath, root)
        dirnames[:] = sorted(d for d in dirnames if d != ".git" and not (rel_dir == "." and d == "_platform"))
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            try:
                hit = _marker_line(path)
            except OSError as exc:
                raise InputError(f"can't read {name!r}: {type(exc).__name__}") from exc
            if hit:
                rel = os.path.normpath(os.path.join(rel_dir, name)).replace(os.sep, "/")
                rel = os.fsencode(rel).decode("utf-8", "backslashreplace")
                findings.append(_forged(rel, *hit))
    return findings

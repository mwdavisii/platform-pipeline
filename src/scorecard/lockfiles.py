"""Packages pinned in the lockfiles the builder supports: root requirements.txt and package-lock.json (v2/v3)."""

import json
import re

from . import git

PYTHON_LOCK = "requirements.txt"
NODE_LOCK = "package-lock.json"
_REQ = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*==\s*([A-Za-z0-9][A-Za-z0-9.+!_-]*)")
# After the pin: optional environment marker, optional --hash options, optional line continuation, optional comment.
_TAIL = re.compile(r"^(\s*;[^#]*?)?(\s+--hash=\S+)*\s*\\?\s*(#.*)?$")
_HASHES = re.compile(r"^(--hash=\S+\s*)+\\?\s*(#.*)?$")
_LOCK_KEY = re.compile(r'^\s*"([^"]*node_modules/[^"]+)"\s*:')


def normalize(name: str) -> str:
    """PEP 503 normalization; npm names are already lowercase, so it is harmless for them."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirements(text: str) -> tuple[list[dict], list[int]]:
    """Pins, plus the numbers of lines that are neither a pin, a --hash continuation, a comment, nor blank."""
    packages, bad, continued = [], [], False
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        was_continued, continued = continued, raw.rstrip().endswith("\\")
        if not line or line.startswith("#"):
            continue
        if was_continued and _HASHES.match(line):
            continue
        match = _REQ.match(line)
        if match and _TAIL.match(line[match.end():]):
            packages.append({"ecosystem": "pypi", "name": normalize(match.group(1)), "version": match.group(3),
                             "file": PYTHON_LOCK, "line": number, "install_script": None})
        else:
            bad.append(number)
    return packages, bad


def parse_requirements(text: str) -> list[dict]:
    return _requirements(text)[0]


def parse_package_lock(text: str) -> list[dict]:
    """Raises ValueError for anything but a v2/v3 lockfile: v1 has no `packages` map or install-script flags."""
    doc = json.loads(text)
    if not isinstance(doc, dict) or doc.get("lockfileVersion") not in (2, 3) or not isinstance(doc.get("packages"), dict):
        raise ValueError("package-lock.json is not lockfileVersion 2 or 3")
    lines = {}
    for number, line in enumerate(text.splitlines(), 1):
        match = _LOCK_KEY.match(line)
        if match:
            lines.setdefault(match.group(1), number)
    packages = []
    for key, entry in sorted(doc["packages"].items()):
        if not key or "node_modules/" not in key or not isinstance(entry, dict) or entry.get("link"):
            continue
        version = entry.get("version")
        if not isinstance(version, str):
            continue
        # An aliased install (`node_modules/innocent` -> {"name": "next-auth"}) is the package named in the entry.
        name = entry["name"] if isinstance(entry.get("name"), str) and entry["name"] else key.rsplit("node_modules/", 1)[1]
        packages.append({"ecosystem": "npm", "name": name, "version": version, "file": NODE_LOCK,
                         "line": lines.get(key, 1), "install_script": entry.get("hasInstallScript") is True,
                         "resolved": entry["resolved"] if isinstance(entry.get("resolved"), str) else None,
                         "integrity": entry["integrity"] if isinstance(entry.get("integrity"), str) else None})
    return packages


def read_packages(read) -> tuple[list[dict], list[dict]]:
    """Packages from both lockfiles via `read(path) -> str | None`, plus problems for lockfiles that couldn't be read
    (unparseable, unsupported lines, too large). A problem fires T-14; it is never treated as an absent lockfile."""
    packages, problems = [], []
    try:
        text = read(PYTHON_LOCK)
    except git.Unreadable as exc:
        text = None
        problems.append({"source": PYTHON_LOCK, "detail": f"can't read the lockfile: {exc}"[:300]})
    if text is not None:
        found, bad = _requirements(text)
        packages += found
        if bad:
            shown = ", ".join(str(n) for n in bad[:10]) + (f" and {len(bad) - 10} more" if len(bad) > 10 else "")
            problems.append({"source": PYTHON_LOCK, "detail": f"lines that are not name==version pins (line {shown}); "
                                                                "the dependency check can't cover them"})
    try:
        text = read(NODE_LOCK)
    except git.Unreadable as exc:
        text = None
        problems.append({"source": NODE_LOCK, "detail": f"can't read the lockfile: {exc}"[:300]})
    if text is not None:
        try:
            packages += parse_package_lock(text)
        except (ValueError, RecursionError) as exc:
            problems.append({"source": NODE_LOCK, "detail": f"can't read the lockfile: {exc}"[:300]})
    return packages, problems

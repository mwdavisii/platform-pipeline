"""Compare a repo against its .builder.lock and builder-spec.json and report drift as finding/v1.

The repo is attacker-controlled, so every read here is bounded, refuses links that
leave the tree, and every finding is built from sanitized paths and snippets so it
always validates against finding/v1.
"""

import fnmatch
import hashlib
import json
import os
import re
import stat
from pathlib import Path

from contracts.findings import make_finding

from . import VERSION
from .config import load_base_images, load_platform, platform_sha256
from .errors import BuilderError
from .generate import render_all
from .lock import GENERATED_DIRS, LOCK_PATH, SPEC_PATH, build_lock, canonical_json, is_safe_rel, sha256_text
from .source import MAX_SOURCE_BYTES, MAX_TREE_BYTES
from .spec import check_spec

DRIFT_STANDARDS = [{"framework": "cwe", "id": "CWE-693"}, {"framework": "nist-ssdf", "id": "PW.9"}]
REGENERATE = [
    "Put the change in builder-spec.json if the spec supports it.",
    "Run: builder generate .",
    "If the spec can't express the change, ask the platform team to add it to the builder.",
]
MAX_SNIPPET = 200
MAX_LOCK_ENTRIES = 500
MAX_UNLISTED = 50
# Files at the repo root that change how the image is built or deployed behind the generated Dockerfile's back.
BUILD_OVERRIDES = ("dockerfile.*", "containerfile", "containerfile.*", ".containerignore", "docker-compose*.y*ml", "compose*.y*ml", "docker-bake.*",
                   "gunicorn.conf.py", "uvicorn.py", "gunicorn.py", "sitecustomize.py", "usercustomize.py")
SAFE_UNKNOWN = "<unknown>"
# Why a file couldn't be read, as an evidence snippet.
UNREADABLE = {
    "outside": "<link leaves the app directory>",
    "symlink": "<symbolic link>",
    "oversize": "<file larger than 1 MB>",
    "binary": "<binary file>",
    "not a regular file": "<not a regular file>",
    "unreadable": "<unreadable file>",
}


def _snip(text: str) -> str:
    text = text.strip()[:MAX_SNIPPET] if isinstance(text, str) else ""
    return text if text.strip() else "<blank line>"


def _digest(content) -> str:
    """A content anchor for the fingerprint: findings about different content must not share an ID."""
    data = content.encode("utf-8", errors="replace") if isinstance(content, str) else (content or b"")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _drift(rule_id, severity, file, line, snippet, title, rationale, kind="line", digest=None):
    evidence = [{"file": file, "start_line": line, "snippet": _snip(snippet)}]
    if digest is not None:
        evidence.append({"file": file, "start_line": 1, "snippet": digest, "note": "content digest"})
    location = {"kind": kind, "file": file}
    if kind == "line":
        location.update(start_line=line, end_line=line)
    return make_finding(
        layer="drift",
        tool="builder",
        tool_version=VERSION,
        rule_id=rule_id,
        category="drift",
        severity=severity,
        location=location,
        evidence=evidence,
        evidence_verified=True,
        title=title,
        rationale=rationale,
        fix={"summary": "Regenerate with the builder instead of editing generated files.", "steps": REGENERATE},
        standards=DRIFT_STANDARDS,
    )


class _Budget(Exception):
    """The total read budget was exhausted."""


class Reader:
    """Bounded, contained reads of repo files. One per call, so the byte budget is per run."""

    def __init__(self, root: Path):
        self.root = root
        self.real = root.resolve()
        self.total = 0

    def read_bytes(self, rel: str) -> tuple[bytes | None, str]:
        """(data, "") or (None, reason). Never follows a link, never leaves the root, never reads more than 1 MB."""
        path = self.root / rel
        try:
            st = path.lstat()
            if stat.S_ISLNK(st.st_mode):
                return None, "symlink"
            if not stat.S_ISREG(st.st_mode):
                return None, "not a regular file"
            if not path.resolve().is_relative_to(self.real):
                return None, "outside"
            if st.st_size > MAX_SOURCE_BYTES:
                return None, "oversize"
            if self.total + st.st_size > MAX_TREE_BYTES:
                raise _Budget()
            with path.open("rb") as handle:
                data = handle.read(MAX_SOURCE_BYTES + 1)
        except FileNotFoundError:
            return None, "missing"
        except (OSError, RuntimeError):
            return None, "unreadable"
        if len(data) > MAX_SOURCE_BYTES:
            return None, "oversize"
        self.total += len(data)
        return data, ""

    def read(self, rel: str) -> tuple[str | None, str]:
        data, reason = self.read_bytes(rel)
        if data is None:
            return None, reason
        if b"\0" in data:
            return None, "binary"
        return data.decode("utf-8", errors="replace"), ""


def _unsafe_dir(root: Path, directory: str) -> bool:
    """True when the directory, or a parent, exists but is a link or not a directory."""
    current = root
    for part in directory.split("/"):
        current = current / part
        try:
            st = current.lstat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if not stat.S_ISDIR(st.st_mode):
            return True
    return False


def _first_line(text: str) -> tuple[int, str]:
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip():
            return number, _snip(line)
    return 1, "<empty file>"


def _first_diff(expected: str, actual: str) -> tuple[int, str]:
    want, have = expected.splitlines(), actual.splitlines()
    for i in range(max(len(want), len(have))):
        a = have[i] if i < len(have) else None
        if i >= len(want) or a != want[i]:
            if a is None:
                return (len(have), _snip(have[-1])) if have and have[-1].strip() else (max(len(have), 1), "<truncated file>")
            return i + 1, _snip(a) if a.strip() else "<blank line>"
    return _first_line(actual)


def _line_of(text: str, needle: str) -> tuple[int, str]:
    for number, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return number, _snip(line)
    return _first_line(text)


def _modified(rel, expected, data, reason, rationale):
    if data is None:
        line, snippet = 1, UNREADABLE.get(reason, "<unreadable file>")
    elif b"\0" in data:
        line, snippet = 1, UNREADABLE["binary"]
    else:
        actual = data.decode("utf-8", errors="replace")
        line, snippet = _first_diff(expected, actual) if expected is not None else _first_line(actual)
    return _drift("builder.file-modified", "high", rel, line, snippet, "Generated file was edited by hand", rationale,
                  digest=_digest(data if data is not None else f"unreadable:{reason}"))


def _missing(rel):
    return _drift("builder.file-missing", "high", rel, 1, "<missing file>", "Generated file was deleted",
                  f"{rel} is generated by the builder and is missing.", kind="absence", digest=_digest("missing"))


def _check_lock(lock):
    """The lock's file table as {path: digest}, or None when the lock is malformed or hostile."""
    if not isinstance(lock, dict):
        return None
    if not all(isinstance(lock.get(key), str) for key in ("builder_version", "platform_config_sha256", "spec_sha256")):
        return None
    files = lock.get("files")
    if not isinstance(files, dict) or not files or len(files) > MAX_LOCK_ENTRIES:
        return None
    if not all(is_safe_rel(rel) and isinstance(digest, str) for rel, digest in files.items()):
        return None
    return dict(files)


def _tree_digest(reader: Reader, root: Path, rel: str) -> str:
    """Digest of a file, or of every file name and content under a directory."""
    path = root / rel
    if not path.is_dir() or path.is_symlink():
        data, reason = reader.read_bytes(rel)
        return _digest(data if data is not None else f"unreadable:{reason}")
    parts = []
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            sub = (Path(dirpath) / name).relative_to(root).as_posix()
            data, reason = reader.read_bytes(sub)
            parts.append(f"{sub}\0{_digest(data if data is not None else reason)}")
            if len(parts) >= 1000:
                break
    return _digest("\n".join(parts) or "empty directory")


def _override_findings(root: Path, reader: Reader) -> list[dict]:
    found = []
    try:
        names = sorted(entry.name for entry in os.scandir(root))
    except OSError:
        return found
    for name in names:
        if len(found) > MAX_UNLISTED:
            break
        if name == "dockerfile" or any(fnmatch.fnmatchcase(name.lower(), pattern) for pattern in BUILD_OVERRIDES):
            file = name if is_safe_rel(name) else SAFE_UNKNOWN
            found.append(_drift("builder.build-override", "high", file, 1, "<build override file>", "File can change how the image is built",
                                f"{file} changes how the app is built or deployed and isn't part of the generated, checked set.", kind="file",
                                digest=_tree_digest(reader, root, name)))
    deploy = root / "deploy"
    if deploy.is_dir() and not deploy.is_symlink():
        try:
            extras = sorted(entry.name for entry in os.scandir(deploy) if entry.name != "base")
        except OSError:
            extras = []
        for name in extras:
            file = f"deploy/{name}" if is_safe_rel(name) else "deploy"
            found.append(_drift("builder.build-override", "high", file, 1, "<deployment content outside deploy/base>", "Deployment config outside the generated directory",
                                "Only deploy/base is generated and checked. Overlays and patches can undo platform security defaults.", kind="file",
                                digest=_tree_digest(reader, root, f"deploy/{name}")))
    if len(found) > MAX_UNLISTED:
        found = found[:MAX_UNLISTED] + [_drift("builder.build-override", "high", SAFE_UNKNOWN, 1, "<more build override files>", "Many build override files",
                                               f"More than {MAX_UNLISTED} files change how the app is built or deployed. The rest are not listed.", kind="file")]
    return found


GIT_EXPORT_RISK = re.compile(r"export-ignore|export-subst|filter=|eol=")


def _gitattributes_findings(reader: Reader) -> list[dict]:
    """Attributes that make `git archive` or checkout produce different bytes than the reviewed tree."""
    text, _ = reader.read(".gitattributes")
    found = []
    for number, line in enumerate((text or "").splitlines(), 1):
        if GIT_EXPORT_RISK.search(line) and not line.lstrip().startswith("#"):
            found.append(_drift("builder.build-override", "high", ".gitattributes", number, line, ".gitattributes changes exported or checked-out content",
                                "export-ignore, export-subst, filter and eol attributes can make the built tree differ from the reviewed one."))
    return found[:MAX_UNLISTED]


def _unlisted(root: Path, reader: Reader, directory: str, expected: set) -> list[dict]:
    findings = []
    base = root / directory
    if not base.is_dir():
        return findings
    seen = 0
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        # Safe to skip only because the verified .dockerignore excludes **/__pycache__ and **/*.pyc from the image.
        dirnames[:] = sorted(d for d in dirnames if not (d == "__pycache__" and directory == "_platform"))
        entries = filenames + [d for d in dirnames if (Path(dirpath) / d).is_symlink()]
        for name in sorted(entries):
            rel = (Path(dirpath) / name).relative_to(root).as_posix()
            if rel in expected:
                continue
            seen += 1
            if seen > MAX_UNLISTED:
                findings.append(_drift("builder.unlisted-file", "high", directory, 1, "<more unexpected files>", "Many unexpected files in a generated directory",
                                       f"More than {MAX_UNLISTED} unexpected files are in {directory}/, which only the builder writes. The rest are not listed.", kind="file"))
                return findings
            if is_safe_rel(rel):
                text, reason = reader.read(rel)
                line, snippet = _first_line(text) if text is not None else (1, UNREADABLE.get(reason, "<unreadable file>"))
                findings.append(_drift("builder.unlisted-file", "high", rel, line, snippet, "Unexpected file in a generated directory",
                                       f"{rel} is in {directory}/, which only the builder writes.",
                                       digest=_digest(text if text is not None else f"unreadable:{reason}")))
            else:
                findings.append(_drift("builder.unlisted-file", "high", directory, 1, "<file with an unprintable name>", "Unexpected file in a generated directory",
                                       f"A file with an unprintable name is in {directory}/, which only the builder writes.", kind="file"))
    return findings


def verify(root: Path) -> list[dict]:
    findings: list[dict] = []
    try:
        _verify(root, Reader(root), findings)
    except _Budget:
        findings.append(_drift("builder.read-budget-exceeded", "high", SPEC_PATH, 1, "<read budget exceeded>", "Too much generated content to check",
                               f"The files to check are larger than the {MAX_TREE_BYTES // 1_000_000} MB read budget.", kind="file"))
    return findings


def _verify(root: Path, reader: Reader, findings: list[dict]) -> None:
    lock_file = root / LOCK_PATH
    if not lock_file.exists() and not lock_file.is_symlink():
        findings.append(_drift("builder.lock-missing", "high", LOCK_PATH, 1, "<missing file>", "Builder lock file is missing",
                               "Without .builder.lock the platform can't tell generated files from hand edits.", kind="absence"))
        return
    lock_text, reason = reader.read(LOCK_PATH)
    invalid = _drift("builder.lock-invalid", "high", LOCK_PATH, 1, _first_line(lock_text)[1] if lock_text else UNREADABLE.get(reason, "<unreadable file>"),
                     "Builder lock file is not valid", "The lock file can't be parsed or names files outside the generated set, so generated files can't be checked.",
                     digest=_digest(lock_text if lock_text is not None else f"unreadable:{reason}"))
    if lock_text is None:
        findings.append(invalid)
        return
    try:
        lock = json.loads(lock_text)
    except (ValueError, RecursionError):
        findings.append(invalid)
        return
    locked = _check_lock(lock)
    if locked is None:
        findings.append(invalid)
        return

    spec_file = root / SPEC_PATH
    if not spec_file.exists() and not spec_file.is_symlink():
        findings.append(_drift("builder.spec-missing", "high", SPEC_PATH, 1, "<missing file>", "builder-spec.json is missing",
                               "The spec is the source of truth for generated files.", kind="absence"))
        return
    spec_text, reason = reader.read(SPEC_PATH)
    spec_snippet = _first_line(spec_text)[1] if spec_text else UNREADABLE.get(reason, "<unreadable file>")
    spec = None
    problem = "builder-spec.json can't be read."
    if spec_text is not None:
        try:
            spec = json.loads(spec_text)
            problem = "builder-spec.json must be a JSON object."
        except (ValueError, RecursionError) as exc:
            problem = f"builder-spec.json is not valid JSON: {str(exc)[:200]}"
    if not isinstance(spec, dict):
        findings.append(_drift("builder.spec-invalid", "high", SPEC_PATH, 1, spec_snippet, "builder-spec.json is not valid", problem,
                               digest=_digest(problem + "\n" + (spec_text or f"unreadable:{reason}"))))
        return

    platform = load_platform()
    current_sha = platform_sha256()
    errors, questions, rendered = [], [], {}
    try:
        errors, questions, _ = check_spec(spec, root, platform)
        if not errors:
            rendered = render_all(spec, root, platform, load_base_images())
    except (BuilderError, ValueError, KeyError, TypeError, OSError, RecursionError) as exc:
        errors = [f"The spec can't be processed ({type(exc).__name__})."]
    # The trust anchor is a fresh render by this builder. The lock never decides what gets checked.
    expected = rendered if not errors else {}

    unsafe = [d for d in GENERATED_DIRS if _unsafe_dir(root, d)]
    for directory in unsafe:
        findings.append(_drift("builder.generated-dir-unsafe", "high", directory, 1, "<link or non-directory>", "Generated directory is a link or not a directory",
                               f"{directory} must be a real directory. A link can redirect generated files or hide extra content.", kind="file"))

    def skipped(rel):
        return any(rel.startswith(d + "/") for d in unsafe) or (rel.startswith("deploy/base/") and _unsafe_dir(root, "deploy/base"))

    for rel in sorted(expected if expected else locked):
        if skipped(rel):
            continue
        data, reason = reader.read_bytes(rel)
        if data is None and reason == "missing":
            findings.append(_missing(rel))
            continue
        rationale = f"{rel} differs from what the builder generates. Hand edits can remove platform security defaults."
        if expected:
            if data is None or data != expected[rel].encode("utf-8"):
                findings.append(_modified(rel, expected[rel], data, reason, rationale))
        elif data is None or sha256_text(data.decode("utf-8", errors="replace")) != locked[rel]:
            findings.append(_modified(rel, None, data, reason, rationale))

    listed = set(expected) if expected else set(locked)
    for directory in GENERATED_DIRS:
        if directory not in unsafe and not _unsafe_dir(root, directory):
            findings.extend(_unlisted(root, reader, directory, listed))
    findings.extend(_override_findings(root, reader))
    findings.extend(_gitattributes_findings(reader))

    if errors:
        findings.append(_drift("builder.spec-invalid", "high", SPEC_PATH, 1, spec_snippet,
                               "builder-spec.json breaks platform rules", "; ".join(errors),
                               digest=_digest("\n".join(sorted(errors)))))
    elif questions:
        findings.append(_drift("builder.spec-needs-input", "high", SPEC_PATH, 1, spec_snippet,
                               "builder-spec.json needs a decision before it can generate",
                               "Nobody can answer questions in the pipeline. " + "; ".join(questions),
                               digest=_digest("\n".join(sorted(questions)))))
    if rendered and lock != build_lock(spec, rendered, lock["builder_version"], lock["platform_config_sha256"]):
        findings.append(_drift("builder.lock-tampered", "high", LOCK_PATH, 1, _first_line(lock_text)[1], "Builder lock doesn't match the spec and generated files",
                               "The lock records different hashes than a fresh render of builder-spec.json produces.",
                               digest=_digest(lock_text)))

    if lock["builder_version"] != VERSION:
        line, snippet = _line_of(lock_text, "builder_version")
        findings.append(_drift("builder.version-stale", "medium", LOCK_PATH, line, snippet, "Generated with a different builder version",
                               f"Lock says builder {_snip(lock['builder_version'])}; current is {VERSION}. Regenerate to pick up new defaults."))
    if lock["platform_config_sha256"] != current_sha:
        line, snippet = _line_of(lock_text, "platform_config_sha256")
        findings.append(_drift("builder.platform-config-mismatch", "high", LOCK_PATH, line, snippet, "Generated against a different platform config",
                               "The platform config used to generate these files doesn't match this cluster's config."))

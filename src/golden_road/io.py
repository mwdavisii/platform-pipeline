"""Fail-closed reads. Anything the pipeline can't read cleanly is an error, never an empty result."""

import json
import os
import tempfile
from pathlib import Path

from contracts.findings import finding_id
from contracts.schema import validate

MAX_INPUT_BYTES = 50_000_000


class InputError(ValueError):
    pass


def error_detail(finding, error: str) -> str:
    """A validation error for a finding, without field values when the finding is about a secret."""
    if isinstance(finding, dict) and finding.get("category") == "secrets":
        return "details omitted for a secrets finding"
    return error


def load_json(path):
    path = Path(path)
    try:
        if path.stat().st_size > MAX_INPUT_BYTES:
            raise InputError(f"{path} is larger than {MAX_INPUT_BYTES} bytes")
        return json.loads(path.read_text(encoding="utf-8"))
    except InputError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise InputError(f"can't read {path}: {type(exc).__name__}") from exc


def load_findings(path) -> list[dict]:
    data = load_json(path)
    if not isinstance(data, list):
        raise InputError(f"{path} must contain a JSON list of findings")
    for i, finding in enumerate(data):
        errors = validate("finding.v1", finding)
        if errors:
            raise InputError(f"{path} item {i} is not a valid finding: {error_detail(finding, errors[0])}")
        expected = finding_id(finding["rule_id"], finding["location"]["file"], [e["snippet"] for e in finding["evidence"]])
        if finding["id"] != expected:
            raise InputError(f"{path} item {i} has an id that does not match its content")
    return data


def write_text_atomic(path, text: str) -> None:
    """Write via a temp file in the same directory, so a failed write leaves nothing behind.

    The file is made world-readable (0o644) before the swap: mkstemp creates 0o600, which later
    pipeline steps running as another user, or artifact upload, may not be able to read.
    """
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".golden-road-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_json(path, document) -> None:
    write_text_atomic(path, json.dumps(document, indent=2, sort_keys=True) + "\n")

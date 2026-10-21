"""Hashing and the .builder.lock format."""

import hashlib
import json
import re

LOCK_PATH = ".builder.lock"
SPEC_PATH = "builder-spec.json"
# Directories only the builder writes. Stale-file removal and drift checks are limited to these.
GENERATED_DIRS = ("deploy/base", "_platform")

# Mirrors the repo_path rules in finding.v1: no control characters, backslashes,
# absolute paths, or "." / ".." / empty segments.
_BAD_PATH = re.compile(r"[\x00-\x1f\x7f\x85  \\]|^/|(^|/)\.{0,2}(/|$)")


def is_safe_rel(rel) -> bool:
    return isinstance(rel, str) and bool(rel) and not _BAD_PATH.search(rel)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(document) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def build_lock(spec: dict, files: dict[str, str], version: str, platform_sha: str) -> dict:
    return {
        "builder_version": version,
        "spec_sha256": sha256_text(canonical_json(spec)),
        "platform_config_sha256": platform_sha,
        "files": {path: sha256_text(text) for path, text in sorted(files.items())},
    }

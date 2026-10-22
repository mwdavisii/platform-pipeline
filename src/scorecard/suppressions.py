"""Accepted findings, from the factory's suppressions/<owner>/<repo>/*.json (override/v2).

A suppression binds to both the finding ID and the rule ID, is active only between `created` and
`expires` (at most 90 days, the same rule as Gate 1 overrides), and never covers a secrets finding:
a leaked secret must be rotated.
"""

import datetime as dt
from pathlib import Path

from contracts.schema import validate
from golden_road.gate import MAX_OVERRIDE_DAYS, _active
from golden_road.io import InputError, load_json

from .config import no_symlink, repo_parts


def load_suppressions(factory, repo: str) -> list[dict]:
    """Every file under suppressions/<owner>/<repo>/ must be a regular .json file, and no level may be a symlink.

    A suppression that could never be active (it expires on or before it is created, or lasts over 90 days) is an
    error here, so its author learns it is dead. One whose date has simply passed is ignored when applying.
    """
    owner, name = repo_parts(repo)
    base = Path(factory)
    for part in (base / "suppressions", base / "suppressions" / owner, base / "suppressions" / owner / name):
        no_symlink(part)
    directory = base / "suppressions" / owner / name
    if not directory.is_dir():
        return []
    docs, ids = [], set()
    for path in sorted(directory.iterdir()):
        no_symlink(path)
        if path.name == ".gitkeep" and path.is_file():
            continue  # keeps an empty directory in git
        if path.suffix != ".json" or not path.is_file():
            raise InputError(f"{path} is not a .json file")
        doc = load_json(path)
        errors = validate("override.v2", doc)
        if errors:
            raise InputError(f"{path} is not a valid override/v2: {errors[0]}")
        if doc["repo"] != repo:
            raise InputError(f"{path} is for {doc['repo']}, not {repo}")
        created, expires = dt.date.fromisoformat(doc["created"]), dt.date.fromisoformat(doc["expires"])
        if expires <= created or (expires - created).days > MAX_OVERRIDE_DAYS:
            raise InputError(f"{path} can never be active: it must expire after it is created, within {MAX_OVERRIDE_DAYS} days")
        if doc["id"] in ids:
            raise InputError(f"{path} repeats suppression id {doc['id']}")
        ids.add(doc["id"])
        docs.append(doc)
    return docs


def expired(suppressions: list[dict], *, repo: str, today) -> list[str]:
    """IDs of suppressions whose window has passed (they are ignored, and worth listing so they get cleaned up)."""
    return [s["id"] for s in suppressions if s["repo"] == repo and dt.date.fromisoformat(s["expires"]) <= today]


def _secret(finding: dict, prefixes) -> bool:
    rule = finding["rule_id"].lower()
    return finding["category"] == "secrets" or any(rule.startswith(p.lower()) for p in prefixes)


def apply_suppressions(findings: list[dict], suppressions: list[dict], *, repo: str, today,
                       secret_rule_prefixes) -> tuple[list[dict], list[dict]]:
    """Return (findings still in force, [{id, finding_id}] for each suppression applied).

    A secrets finding, or one whose rule ID starts with a configured secret prefix, is never suppressed:
    a leaked secret must be rotated."""
    active = [s for s in suppressions if _active(s, repo, today)]
    kept, applied = [], []
    for finding in findings:
        match = next((s for s in active if s["finding_id"] == finding["id"] and s["rule_id"] == finding["rule_id"]), None)
        if match is None or _secret(finding, secret_rule_prefixes):
            kept.append(finding)
        else:
            applied.append({"id": match["id"], "finding_id": finding["id"]})
    return kept, applied

"""Gate 1: block on High or Critical findings unless a valid infosec override covers them."""

import datetime as dt
from pathlib import Path

from contracts.schema import validate

from .io import InputError, load_json

BLOCKING = {"high", "critical"}
SEVERITIES = ["critical", "high", "medium", "low", "info"]
MAX_OVERRIDE_DAYS = 90


def load_overrides(directory) -> list[dict]:
    directory = Path(directory)
    if not directory.is_dir():
        return []
    overrides = []
    for path in sorted(directory.glob("*.json")):
        doc = load_json(path)
        errors = validate("override.v1", doc)
        if errors:
            raise InputError(f"{path} is not a valid override: {errors[0]}")
        overrides.append(doc)
    return overrides


def _active(override: dict, repo: str, today: dt.date) -> bool:
    created = dt.date.fromisoformat(override["created"])
    expires = dt.date.fromisoformat(override["expires"])
    return (
        override["repo"] == repo
        and created <= today < expires
        and (expires - created).days <= MAX_OVERRIDE_DAYS
    )


def evaluate(findings: list[dict], overrides: list[dict], *, repo: str, today: dt.date) -> dict:
    covered = {o["finding_id"] for o in overrides if _active(o, repo, today)}
    by_severity = {s: 0 for s in SEVERITIES}
    by_layer: dict[str, int] = {}
    blocking, applied = set(), set()
    for finding in findings:
        by_severity[finding["severity"]] += 1
        layer = finding["source"]["layer"]
        by_layer[layer] = by_layer.get(layer, 0) + 1
        if finding["severity"] in BLOCKING:
            (applied if finding["id"] in covered else blocking).add(finding["id"])
    result = {
        "status": "fail" if blocking else "pass",
        "counts": {"by_severity": by_severity, "by_layer": by_layer},
        "blocking": sorted(blocking),
        "overrides_applied": sorted(applied),
    }
    errors = validate("gate-result.v1", result)
    if errors:
        raise InputError(f"gate produced an invalid result: {errors[0]}")
    return result

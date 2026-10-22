"""Scanner conclusions: the Gate 1 checks as timestamped observations. They are context, never re-scored."""

import re

from golden_road.checks import snapshot
from golden_road.io import InputError

STATES = {"success", "pending", "missing", "failure"}
_SHA = re.compile(r"^[0-9a-f]{40}$")


def checks_from_doc(doc) -> list[dict]:
    """The `required_checks` list from a `golden-road checks wait` result, strictly validated.

    The result may also carry `sha`, the commit the checks were read for (see `checks_sha`)."""
    if (not isinstance(doc, dict) or not {"passed", "required_checks"} <= set(doc) <= {"passed", "required_checks", "sha"}
            or type(doc["passed"]) is not bool):
        raise InputError('the Gate 1 result must be {"passed": bool, "required_checks": [...], "sha": "<commit>"}')
    if "sha" in doc and not (isinstance(doc["sha"], str) and _SHA.match(doc["sha"])):
        raise InputError("the Gate 1 result has a malformed sha")
    checks = doc["required_checks"]
    if not isinstance(checks, list) or not all(
            isinstance(c, dict) and set(c) == {"name", "state"} and isinstance(c["name"], str) and c["name"] and c["state"] in STATES
            for c in checks):
        raise InputError("the Gate 1 result has a malformed check entry")
    return checks


def checks_sha(doc) -> str | None:
    """The commit the Gate 1 result was read for, or None when the result doesn't say."""
    return doc.get("sha") if isinstance(doc, dict) else None


def observe(checks: list[dict], observed_at: str) -> list[dict]:
    return [{"name": c["name"], "state": c["state"], "observed_at": observed_at} for c in checks]


def read_conclusions(api, repo: str, sha: str, required: list[dict], observed_at: str) -> list[dict]:
    """Live read through the checks API, for the score job. Uses the same source pinning as Gate 1."""
    return observe(snapshot(api, repo, sha, required), observed_at)


def drift_green(observed: list[dict], drift_check: str) -> bool:
    """Green only if every observation with that name is green (and there is at least one)."""
    states = [o["state"] for o in observed if o["name"] == drift_check]
    return bool(states) and all(state == "success" for state in states)

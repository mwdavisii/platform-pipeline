"""What the report job does on the PR, as data. The label, the stale labels and the review teams are conveniences: the
authoritative level is the factory run's `level` check.
`github.json`'s `blocking_findings` holds raw repo paths, for data use only: workflows post only `findings_check`'s title and summary. `findings_check` is the result of the required `scorecard / findings`
check: high or critical deterministic findings hold the merge until a person merges with an override."""

from .blocking import MAX_LISTED, blocking_findings, counted
from .levels import LEVELS
from .report_md import safe

LABEL_PREFIX = "scrutiny:"
CLEAR = "No high or critical findings"


def _where(location: dict) -> str:
    line = location.get("start_line")
    return f"{safe(location['file'], 200)}:{line}" if line else safe(location["file"], 200)


def findings_check(blocking: list[dict], who: str) -> dict:
    """The `scorecard / findings` result: success with nothing blocking, failure listing at most MAX_LISTED findings otherwise."""
    if not blocking:
        return {"conclusion": "success", "title": CLEAR, "summary": CLEAR + "."}
    n = len(blocking)
    lines = [f"- `{safe(f['rule_id'], 80)}` at {_where(f['location'])}" for f in blocking[:MAX_LISTED]]
    if n > MAX_LISTED:
        lines.append(f"- and {n - MAX_LISTED} more")
    lines += ["", f"Fix them and push again, or {who} can merge with an override (the merge box's \"Merge without waiting for requirements\")."]
    return {"conclusion": "failure", "title": f"Merge blocked: {counted(n)}", "summary": "\n".join(lines)}


def github_actions(level: str, config: dict, card: dict | None = None, who: str = "a repo admin") -> dict:
    """The label to set, the stale labels to remove, the teams to request review from, and the findings block."""
    blocking = blocking_findings(card) if card is not None else []
    return {
        "label": LABEL_PREFIX + level,
        "stale_labels": [LABEL_PREFIX + other for other in LEVELS if other != level],
        "review_teams": list(config["reviewers"].get(level, [])),
        "blocking_findings": [{"rule_id": f["rule_id"], "file": f["location"]["file"], "line": f["location"].get("start_line")} for f in blocking],
        "findings_check": findings_check(blocking, who),
    }

"""High and critical deterministic findings hold the merge: the `scorecard / findings` check fails, and the `scorecard-findings`
ruleset, the app's only ruleset with a bypass actor, lets an admin merge the PR with an override. A finding the model produced never blocks."""

import re

BLOCKING_SEVERITIES = ("critical", "high")
MAX_LISTED = 20
_TEAM = re.compile(r"[a-z0-9][a-z0-9_-]{0,99}")  # a GitHub team slug
_ORG = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")


def counted(n: int) -> str:
    """"1 high or critical finding" / "N high or critical findings": one wording for the comment and the check title."""
    return f"{n} high or critical finding{'s' if n != 1 else ''}"


def blocking_findings(card: dict) -> list[dict]:
    """The card's deterministic findings at high or critical, after the factory's suppressions, in a stable order. These hold raw repo paths, for data use only: a workflow
    posts only `findings_check`'s title and summary, which are made safe."""
    suppressed = {s["finding_id"] for s in card["suppressions_applied"]}
    found = [f for f in card["signals"]["findings"]
             if f["source"]["layer"] == "deterministic" and f["severity"] in BLOCKING_SEVERITIES and f["id"] not in suppressed]
    return sorted(found, key=lambda f: (f["rule_id"], f["location"]["file"], f["location"].get("start_line") or 0, f["id"]))


def override_by(platform: dict) -> str:
    """Who may merge past the block: the platform's bypass team when one is configured, else the repo admin (the PoC).
    No `@`: a mention in the PR comment would notify the team on every blocked PR."""
    team, org = platform.get("findings_bypass_team"), platform.get("org")
    if isinstance(team, str) and _TEAM.fullmatch(team) and isinstance(org, str) and _ORG.fullmatch(org):
        return f"a member of `{org}/{team}`"
    return "a repo admin"

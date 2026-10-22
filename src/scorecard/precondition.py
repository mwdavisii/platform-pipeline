"""Whether a PR can be assessed at all. Any reason here means "not assessed": no model call, and the check fails."""

RERUN_HINT = "Push again, or use Re-run all jobs once Gate 1 is green."


def _label(entry: dict) -> str:
    return entry.get("name") or entry.get("context") or "<unnamed>"


def not_assessed(gate1: list[dict], checks: list[dict] | None, passed: bool | None, event: dict | None, *,
                 head_sha: str | None, checks_sha: str | None) -> list[str]:
    """Reasons this PR is not assessed.

    `checks` and `passed` come from the Gate 1 wait, `checks_sha` is the commit that wait read, `event` is the GitHub
    event payload, and `head_sha` is the commit being assessed. All are required: a missing one is a reason, never a pass.
    """
    reasons = []
    pr = event.get("pull_request") if isinstance(event, dict) else None
    if event is None:
        reasons.append("no event payload to check the PR against")
    elif not isinstance(pr, dict):
        reasons.append("not a pull_request event")
    else:
        if pr.get("draft") is not False:
            reasons.append("the PR is a draft")
        head = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
        base = ((pr.get("base") or {}).get("repo") or {}).get("full_name")
        if not head or head != base:
            reasons.append("the PR comes from a fork; a maintainer must push the branch to this repo")
    if checks is not None and (not head_sha or checks_sha != head_sha):
        reasons.append("the Gate 1 result is not for the commit being assessed")
    if not gate1:
        # checks.wait succeeds on an empty list, so an empty list must never count as green.
        return reasons + ["no Gate 1 checks configured"]
    if checks is None:
        return reasons + ["no Gate 1 result"]
    states = {}
    for c in checks:
        # A repeated name keeps its worst state: a green duplicate must not hide a failing one.
        if states.get(c["name"], "success") == "success":
            states[c["name"]] = c["state"]
    for entry in gate1:
        state = states.get(_label(entry), "missing")
        if state != "success":
            reasons.append(f"Gate 1 check {_label(entry)!r} is {state}")
    if passed is not True and not any(r.startswith("Gate 1 check") for r in reasons):
        reasons.append("Gate 1 is not green")
    return reasons

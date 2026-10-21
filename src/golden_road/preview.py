"""PR preview report: the job summary, the PR comment, and annotations for the preview job's authz probe result.

Everything in the probe result is derived from the pull request (route paths come from its source), so every string
is escaped before it reaches markdown or a workflow command. A result is "PASS" only when the preview job succeeded
(when that is known) and the probe result says pass or not-declared. A missing or malformed result is a failure.
"""

import re

from .summary import _cmd_data, _cmd_prop, _md

MARKER = "<!-- golden-road:preview -->"
MAX_LISTED = 20
MAX_ANNOTATED = 10
_RUN_URL = re.compile(r"https://[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]{1,20}")
_SHA = re.compile(r"[0-9a-f]{40}")
_JOB_FAILED = "The probe result was a pass, but the preview job failed after the probe. See the check log for the step that failed."
_NOT_RUN = {"skipped": "DID NOT RUN", "cancelled": "CANCELLED"}


def _state(authz, job_result) -> str:
    """One of pass, not-declared, job-failed, fail, no-result, skipped, cancelled."""
    if job_result in _NOT_RUN:
        return job_result
    status = authz.get("status") if isinstance(authz, dict) else None
    if not isinstance(status, str):
        return "no-result"
    if job_result not in (None, "success"):
        return "job-failed" if status in ("pass", "not-declared") else "fail"
    return status if status in ("pass", "not-declared") else "fail"


def _failures(authz) -> list[dict]:
    items = authz.get("failures") if isinstance(authz, dict) else None
    return [f for f in items if isinstance(f, dict)] if isinstance(items, list) else []


def _case(failure: dict) -> str:
    roles = failure.get("roles")
    status = failure.get("status")
    got = "no response" if status is None else f"status {status}"
    who = "no role" if roles is None else f"roles {roles}"
    reason = failure.get("reason")
    suffix = f" ({reason})" if reason else " (the guard's 403 was expected)"
    return f"{failure.get('method', '?')} {failure.get('path', '?')} with {who}: {got}{suffix}"


def _exit_code(authz) -> str:
    code = authz.get("exit_code") if isinstance(authz, dict) else None
    return f" (exit code {code})" if isinstance(code, int) and not isinstance(code, bool) else ""


def render(authz, job_result, run_url, commit=None) -> str:
    state = _state(authz, job_result)
    ok = state in ("pass", "not-declared")
    head = _NOT_RUN.get(state, "PASS" if ok else "FAIL")
    lines = [MARKER, f"## Golden road PR preview: {head}", ""]
    if isinstance(commit, str) and _SHA.fullmatch(commit):
        lines += [f"Tested commit: `{commit}` (this pull request merged into its base branch).", ""]
    if state == "pass":
        lines.append(f"The image built from this pull request's Dockerfile started with no network, and all {_md(authz.get('denied', 0))} requests the policy denies were refused by the guard ({_md(authz.get('cases', 0))} cases probed).")
    elif state == "not-declared":
        lines.append("The image built. This app declares no `authz`, so there was nothing to probe.")
    elif state in _NOT_RUN:
        lines.append("The preview job did not finish, so there is no result for this commit. It is not a pass.")
    elif state == "no-result":
        lines.append("There is no probe result: the image did not build, did not start, or the probe did not run. The build log has the cause.")
    elif state == "job-failed":
        lines.append(_JOB_FAILED)
    else:
        lines.append("The image built from this pull request did not pass the container authz probe, or the build failed.")
        reason = authz.get("reason") if isinstance(authz, dict) else None
        if reason:
            lines.append(f"Reason: {_md(str(reason) + _exit_code(authz), 500)}")
        failures = _failures(authz)
        if failures:
            lines += ["", "### Requests the guard should have denied", ""]
            lines += [f"- {_md(_case(f), 400)}" for f in failures[:MAX_LISTED]]
            reported = authz.get("failures_total")
            total = max(len(failures), reported if isinstance(reported, int) else 0)
            if total > MAX_LISTED:
                lines.append(f"- {total - MAX_LISTED} more not shown; see the workflow artifact.")
    lines += ["", "Nothing from this preview is signed, published, or pushed. The release job builds and probes again on `main`."]
    if isinstance(run_url, str) and _RUN_URL.fullmatch(run_url):
        lines.append(f"[Workflow run]({run_url})")
    return "\n".join(lines) + "\n"


def annotations(authz, job_result=None) -> list[str]:
    state = _state(authz, job_result)
    title = _cmd_prop("golden-road preview")
    if state == "pass":
        cases = authz.get("cases", 0)
        return [f"::notice title={title}::{_cmd_data(f'Container authz probe passed ({cases} cases).')}"]
    if state == "not-declared":
        return [f"::notice title={title}::The image built; no authz is declared, so nothing was probed."]
    if state in _NOT_RUN or state == "no-result":
        return [f"::error title={title}::The image did not build or start, or the probe did not run (no probe result). See the build log."]
    if state == "job-failed":
        return [f"::error title={title}::{_JOB_FAILED}"]
    reason = str(authz.get("reason") or "a request the policy denies was not refused by the guard") + _exit_code(authz)
    lines = [f"::error title={title}::{_cmd_data('Container authz probe failed: ' + reason)[:500]}"]
    for failure in _failures(authz)[:MAX_ANNOTATED]:
        lines.append(f"::error title={_cmd_prop('authz-probe')}::{_cmd_data(_case(failure))[:500]}")
    return lines

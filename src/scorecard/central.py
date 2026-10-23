"""Central review: what the factory's poller does for each open pull request of a registered repo. decide() is pure;
poll() does the reading and writing. Every value read from GitHub is shape-checked before it reaches a check run or a dispatch."""

import datetime as dt
import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from golden_road.checks import snapshot
from golden_road.io import InputError

from .config import gate1_list, load_config, load_intake

ASSESSED = "scorecard / assessed"           # the same name the per-app workflow's job produces (remediate.CHECKS["scorecard"])
LEVEL_PREFIX = "scorecard / level: "
MAX_ATTEMPTS = 2          # stale (never finished) assess runs per head
RETRY_MAX = 3             # consecutive retryable outcomes (the report job's or a failed dispatch) per head
RETRY_BACKOFF_MINUTES = 10
QUEUED_TEXT = "The factory picked up this commit at {now}; an assess run starts now"
_QUEUED_AT = re.compile(r"picked up this commit at (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z);")
MAX_PR = 999_999_999
EXTERNAL_ID = re.compile(r"v1:(queued|gate1|fork|retry|final):([0-9a-f]{16}):([1-9])")
_SHA = re.compile(r"[0-9a-f]{40}")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
POLL_PERMISSIONS = {"pull_requests": "read", "checks": "write", "statuses": "read"}
IN_FLIGHT = ("queued", "in_progress", "waiting", "pending", "requested")
_WORKFLOW = re.compile(r"[A-Za-z0-9_.-]+\.ya?ml")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
FORK_TEXT = "not assessed: pull requests from forks are reviewed by a maintainer. A maintainer can push the branch to this repo to assess it."


@dataclass(frozen=True)
class Action:
    kind: str                      # skip | mark | dispatch
    reason: str
    external_id: str | None = None
    expire: int | None = None      # the App's stale check run to close first
    attempt: int = 1


def external_id(kind: str, fp: str, attempt: int = 1) -> str:
    return f"v1:{kind}:{fp}:{attempt}"


def fingerprint(pr: dict) -> str:
    """What a re-run on `edited` reacted to: the base branch, and the PR text the injection pre-scan reads."""
    base = pr.get("base") or {}
    text = json.dumps([base.get("ref") or "", pr.get("title") or "", pr.get("body") or ""], ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def pr_target(pr, repo) -> tuple[int, str] | None:
    if not isinstance(pr, dict):
        return None
    number, head = pr.get("number"), pr.get("head") if isinstance(pr.get("head"), dict) else {}
    base = pr.get("base") if isinstance(pr.get("base"), dict) else {}
    base_repo = (base.get("repo") or {}).get("full_name") if isinstance(base.get("repo"), dict) else None
    sha = head.get("sha")
    if type(number) is not int or not 1 <= number <= MAX_PR or not isinstance(sha, str) or not _SHA.fullmatch(sha) or base_repo != repo:
        return None
    return number, sha


def _is_fork(pr, repo) -> bool:
    head_repo = pr["head"].get("repo")
    return (head_repo.get("full_name") if isinstance(head_repo, dict) else None) != repo


def latest_check(runs, app_id: int, sha: str) -> dict | None:
    mine = [r for r in runs if isinstance(r, dict) and r.get("name") == ASSESSED and isinstance(r.get("app"), dict)
            and type(r["app"].get("id")) is int and r["app"]["id"] == app_id and r.get("head_sha") == sha and type(r.get("id")) is int]
    return max(mine, key=lambda r: r["id"]) if mine else None


def _minutes_since(stamp, now: str) -> float | None:
    if not isinstance(stamp, str) or not _TIME.fullmatch(stamp):
        return None
    parse = lambda s: dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    return (parse(now) - parse(stamp)).total_seconds() / 60


def _queued_at(run) -> str | None:
    """When the check was queued: its started_at, or the time the poller wrote into its summary. None when neither is readable."""
    started = run.get("started_at")
    if isinstance(started, str) and _TIME.fullmatch(started):
        return started
    summary = (run.get("output") or {}).get("summary") if isinstance(run.get("output"), dict) else None
    found = _QUEUED_AT.search(summary) if isinstance(summary, str) else None
    return found.group(1) if found else None


def gate1_state(results: list[dict]) -> tuple[str, list[str]]:
    red = [f"{r['name']} is {r['state']}" for r in results if r["state"] == "failure"]
    if red:
        return "red", red
    if results and all(r["state"] == "success" for r in results):
        return "green", []
    return "pending", []


def decide(pr, repo, runs, gate1, *, app_id: int, now: str, timeout_minutes: int) -> Action:
    target = pr_target(pr, repo)
    if target is None:
        base = ((pr.get("base") or {}).get("repo") or {}).get("full_name") if isinstance(pr, dict) and isinstance(pr.get("base"), dict) else None
        if isinstance(base, str) and base != repo and base.lower() == repo.lower():
            return Action("skip", f"the repository name differs in case from the intake record: GitHub says {base}, the record is for {repo}")
        return Action("skip", "the pull request has an unexpected shape")
    _, sha = target
    if pr.get("draft") is not False:
        return Action("skip", "draft")
    fp = fingerprint(pr)
    latest = latest_check(runs, app_id, sha)
    parsed = EXTERNAL_ID.fullmatch(latest.get("external_id") or "") if latest else None
    if latest and not parsed:
        return Action("skip", "a scorecard / assessed check this poller did not write")
    kind, old_fp, attempt = (parsed.group(1), parsed.group(2), int(parsed.group(3))) if parsed else (None, None, 0)
    expire, next_attempt = None, 1
    if latest and latest.get("status") != "completed":
        age = _minutes_since(_queued_at(latest), now)
        if age is None or age < timeout_minutes:  # an age that can't be read is a run in flight, never a stale one
            return Action("skip", "an assess run is in flight")
        expire, next_attempt = latest["id"], attempt + 1
        if next_attempt > MAX_ATTEMPTS:
            return Action("mark", f"not assessed: {MAX_ATTEMPTS} assess runs did not finish within {timeout_minutes} minutes. "
                                  "This is for the platform team; a new push starts again.", external_id("final", fp, attempt), expire=expire)
    elif latest and kind == "retry" and old_fp == fp:
        # A retryable outcome (the report job's, or a dispatch that failed): try again after a backoff, up to RETRY_MAX times.
        if attempt >= RETRY_MAX:
            return Action("mark", f"not assessed: {RETRY_MAX} tries ended in an outcome that may pass later (GitHub's merge commit, the base branch, "
                                  "the factory or Gate 1 were not ready). This is for the platform team; a new push starts again.",
                          external_id("final", fp, attempt))
        waited = _minutes_since(latest.get("completed_at"), now)
        if waited is not None and waited < RETRY_BACKOFF_MINUTES:
            return Action("skip", "waiting before trying again")
        next_attempt = attempt + 1
    elif latest and kind not in ("gate1", "retry") and old_fp == fp:
        return Action("skip", "already reported for this head")
    if _is_fork(pr, repo):
        return Action("mark", FORK_TEXT, external_id("fork", fp), expire=expire)
    state, red = gate1()
    if state == "pending":
        return Action("skip", "Gate 1 is pending")
    if state == "red":
        if latest and kind == "gate1" and old_fp == fp and expire is None:
            return Action("skip", "Gate 1 is still red")
        return Action("mark", f"not assessed: Gate 1 is red ({'; '.join(red)}). It is assessed when Gate 1 is green on this commit, "
                              "or after a new push.", external_id("gate1", fp), expire=expire)
    return Action("dispatch", "Gate 1 is green", external_id("queued", fp, next_attempt), expire=expire, attempt=next_attempt)


def registered_repos(factory) -> tuple[list[str], list[str], list[str]]:
    """(central, problems, per_app): owner/name for every intake/<owner>/<name>.yaml the scorecard's own loader accepts (C8) that is marked
    `scorecard: central` (only those are polled), the problems, and the accepted repos still on the app's own caller (no mark, or per-app)."""
    top, central, per_app, problems = Path(factory) / "intake", [], [], []
    for owner in sorted(p for p in top.iterdir() if p.is_dir() and not p.is_symlink()) if top.is_dir() else []:
        for path in sorted(p for p in owner.glob("*.yaml") if p.is_file() and not p.is_symlink()):
            repo = f"{owner.name}/{path.stem}"
            try:
                record = load_intake(factory, repo)
            except InputError as exc:
                problems.append(f"intake/{repo}: {exc}"[:300])
                continue
            (central if record.get("scorecard") == "central" else per_app).append(repo)
    return central, problems, per_app


def _output(title, summary):
    return {"title": title, "summary": summary}


def _fail_check(api, repo, check_id, external, title, summary, now):
    body = {"status": "completed", "conclusion": "failure", "completed_at": now, "output": _output(title, summary)}
    if external:
        body["external_id"] = external
    api.send("PATCH", f"/repos/{repo}/check-runs/{check_id}", body)


def _expire(api, repo, check_id, central, now):
    """Close a stale queued or in-progress check of the App as failed, so the new one is the only open one."""
    _fail_check(api, repo, check_id, None, "Not assessed",
                f"the assess run did not finish within {central['assess_timeout_minutes']} minutes", now)


def _dispatch(api, factory_api, repo, number, sha, action, factory_repo, workflow, ref, run_url, now, central):
    if not _REPO.fullmatch(repo) or type(number) is not int or not 1 <= number <= MAX_PR or not _SHA.fullmatch(sha):
        raise InputError("unexpected repository, pull request or commit")
    if action.expire:
        _expire(api, repo, action.expire, central, now)
    posted = api.send("POST", f"/repos/{repo}/check-runs", {
        "name": ASSESSED, "head_sha": sha, "status": "queued", "started_at": now, "external_id": action.external_id,
        "details_url": run_url, "output": _output("Queued", QUEUED_TEXT.format(now=now))})
    check_id = posted.get("id") if isinstance(posted, dict) else None
    if type(check_id) is not int or check_id <= 0:
        raise InputError("unexpected check run id")
    try:
        factory_api.send("POST", f"/repos/{factory_repo}/actions/workflows/{workflow}/dispatches",
                         {"ref": ref, "inputs": {"repo": repo, "pr": str(number), "head_sha": sha, "check_run_id": str(check_id)}})
    except InputError:
        fp = EXTERNAL_ID.fullmatch(action.external_id).group(2)
        try:
            _fail_check(api, repo, check_id, external_id("retry", fp, action.attempt), "Not assessed",
                        "not assessed: the assess run could not be started; the next poll tries again", now)
        except InputError:
            pass  # the dispatch failure is the error to report
        raise


def _in_flight(factory_api, factory_repo, workflow) -> int:
    total = 0
    for status in IN_FLIGHT:
        count = factory_api.get(f"/repos/{factory_repo}/actions/workflows/{workflow}/runs?status={status}&per_page=1").get("total_count", 0)
        if type(count) is not int or count < 0:
            raise InputError("unexpected count of workflow runs")
        total += count
    return total


def poll(factory, *, app_id, mint, factory_api, make_api, factory_repo, workflow, ref, run_url, now) -> dict:
    config = load_config(factory)
    central = config.get("central")
    if not central:
        return {"central": "off"}
    if (not _REPO.fullmatch(factory_repo) or any(p in (".", "..") for p in factory_repo.split("/"))
            or not _WORKFLOW.fullmatch(workflow) or ref != "main"):
        raise InputError("the factory repo, workflow or ref is unexpected")
    gate1 = gate1_list(factory, config)
    repos, errors, per_app = registered_repos(factory)
    actions, candidates = [], []
    flight = _in_flight(factory_api, factory_repo, workflow)
    for repo in repos:
        try:
            api = make_api(mint(repo, POLL_PERMISSIONS))
            prs = api.paged(f"/repos/{repo}/pulls?state=open")
        except InputError as exc:
            errors.append(f"{repo}: {exc}"[:300])
            continue
        for pr in prs:
            target = pr_target(pr, repo)
            if target is None:
                actions.append({"repo": repo, "action": "skip", "reason": "the pull request has an unexpected shape"})
                continue
            number, sha = target
            try:
                runs = api.paged(f"/repos/{repo}/commits/{sha}/check-runs?check_name={urllib.parse.quote(ASSESSED)}&filter=all&app_id={app_id}",
                                 key="check_runs")
                action = decide(pr, repo, runs, lambda: gate1_state(snapshot(api, repo, sha, gate1)), app_id=app_id, now=now,
                                timeout_minutes=central["assess_timeout_minutes"])
                if action.kind == "dispatch":
                    candidates.append((pr.get("updated_at") if isinstance(pr.get("updated_at"), str) else "", repo, number, sha, action, api))
                    continue
                if action.expire:
                    _expire(api, repo, action.expire, central, now)
                if action.kind == "mark":
                    api.send("POST", f"/repos/{repo}/check-runs", {
                        "name": ASSESSED, "head_sha": sha, "status": "completed", "conclusion": "failure", "completed_at": now,
                        "external_id": action.external_id, "details_url": run_url, "output": _output("Not assessed", action.reason)})
                actions.append({"repo": repo, "pr": number, "head_sha": sha, "action": action.kind, "reason": action.reason})
            except InputError as exc:
                errors.append(f"{repo}#{number}: {exc}"[:300])
    for _, repo, number, sha, action, api in sorted(candidates, key=lambda c: c[:3]):
        if flight >= central["max_in_flight"]:
            actions.append({"repo": repo, "pr": number, "head_sha": sha, "action": "skip", "reason": "the factory is at its limit of runs in flight"})
            continue
        try:
            _dispatch(api, factory_api, repo, number, sha, action, factory_repo, workflow, ref, run_url, now, central)
            flight += 1
            actions.append({"repo": repo, "pr": number, "head_sha": sha, "action": "dispatch", "reason": action.reason})
        except InputError as exc:
            errors.append(f"{repo}#{number}: {exc}"[:300])
    return {"central": "on", "in_flight": flight, "max_in_flight": central["max_in_flight"], "actions": actions, "errors": errors,
            "per_app": per_app}

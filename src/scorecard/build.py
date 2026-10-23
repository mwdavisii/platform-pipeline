"""Run everything deterministic on a checkout and assemble scorecard/v1.

Inputs are the PR checkout (HEAD is the merge commit), the base ref, the factory directory, the
Gate 1 result, the PR event payload, and optionally the agent's assessment for this tree. Nothing here calls a model.
"""

import dataclasses
import datetime as dt

from contracts.schema import validate
from golden_road.io import InputError

from . import VERSION, git
from .asvs import ASVS_SHA256, chapter_names, check_ids, for_level, load_asvs
from .config import gate1_list, load_config, load_confirmed_na, load_intake
from .event import re_review_event
from .level import decide
from .levels import asvs_level, effective_classification
from .lockfiles import read_packages
from .observations import checks_from_doc, checks_sha, drift_green, observe
from .precondition import RERUN_HINT, not_assessed
from .redact import redact
from .registry import RegistryClient
from .scorer import apply_assessment, check_approved
from .scores import chapters, counts, coverage, coverage_counts, score
from .scoping import scope
from .signals.changes import change_signals
from .signals.dependencies import dependency_signals
from .signals.injection import text_hits, tree_signals
from .signals.lock_diff import LOCK, builder_bumped
from .signals.size import change_size
from .signals.spec_diff import SPEC, head_spec, recorded_spec_signals, spec_changes
from .suppressions import apply_suppressions, expired, load_suppressions


class NotAssessed(InputError):
    """The PR can't be assessed (Gate 1 not green, no Gate 1 list, ...). No scorecard is produced."""


def _pr_hits(pr_text, config: dict) -> list[dict]:
    if pr_text is None:
        return []
    if not isinstance(pr_text, dict):
        raise InputError("the PR text file must be a JSON object")
    hits = []
    for key in ("title", "body"):
        if isinstance(pr_text.get(key), str):
            hits += text_hits(f"pr-{key}", pr_text[key], config)
    for commit in pr_text.get("commits") or []:
        if isinstance(commit, dict) and isinstance(commit.get("message"), str):
            hits += text_hits(f"commit {str(commit.get('sha', ''))[:12]}", commit["message"], config)
    return hits


def _pr_head_sha(event) -> str | None:
    pr = event.get("pull_request") if isinstance(event, dict) else None
    sha = ((pr or {}).get("head") or {}).get("sha") if isinstance(pr, dict) else None
    return sha if isinstance(sha, str) and sha else None


def _pr_number(event) -> int | None:
    pr = event.get("pull_request") if isinstance(event, dict) else None
    number = pr.get("number") if isinstance(pr, dict) else None
    return number if isinstance(number, int) and not isinstance(number, bool) and 1 <= number <= 999_999_999 else None


def _redact_unverified(results: list[dict], findings: list[dict]) -> None:
    """Unverified agent evidence is arbitrary text, so its snippets are redacted in the JSON too. Verified anchors quote the repo and stay raw."""
    for item in [*results, *findings]:
        if not item.get("evidence_verified"):
            # Copy each anchor: the same dict can be shared with other results or the caller's assessment.
            item["evidence"] = [{**anchor, "snippet": redact(anchor["snippet"])} for anchor in item.get("evidence", [])]


def _spec_text(repo_root, sha: str) -> str | None:
    """builder-spec.json at a commit. Present but unreadable becomes "", which parses as present-but-invalid (T-2)."""
    try:
        return git.show(repo_root, sha, SPEC)
    except git.Unreadable:
        return ""


def _lock_text(repo_root, sha: str) -> str | None:
    """.builder.lock at a commit, or None when it is absent or unreadable: it feeds only the recorded-only builder-bumped signal."""
    try:
        return git.show(repo_root, sha, LOCK)
    except git.Unreadable:
        return None


@dataclasses.dataclass
class Prepared:
    """Everything `prepare` decided without the agent, and what `finish` needs to score the PR."""
    repo_root: object
    repo: str
    now: str
    today: dt.date
    config: dict
    checks: list
    intake: dict | None
    suppressions: list
    requirements: list
    level_n: int
    classification: str | None
    spec_classification: str | None
    head: str
    base: str
    tree_sha: str
    changed: list
    base_spec: str
    head_spec_text: str
    head_packages: list
    base_packages: list
    problems: list
    decided: list
    agent_reqs: list
    pr_number: int | None
    pr_head: str | None


def prepare(*, repo_root, repo: str, base_ref: str, factory, checks_doc, event, now: str) -> Prepared:
    """Load the inputs, check the precondition, and scope the ASVS list. Raises NotAssessed before any other work when the PR can't be assessed."""
    config = load_config(factory)
    checks = checks_from_doc(checks_doc)
    head = git.rev(repo_root, "HEAD")
    # Gate 1 checks attach to the PR's head commit, not to the merge commit that is checked out.
    pr_head = _pr_head_sha(event)
    reasons = not_assessed(gate1_list(factory, config), checks, checks_doc["passed"], event, head_sha=pr_head, checks_sha=checks_sha(checks_doc))
    if pr_head and git.parents(repo_root, head)[1:] != [pr_head]:
        reasons.append("checkout is not this PR's merge commit")
    if reasons:
        raise NotAssessed("not assessed: " + "; ".join(reasons) + ". " + RERUN_HINT)
    today = dt.date.fromisoformat(now[:10])
    intake = load_intake(factory, repo)
    confirmed_na = load_confirmed_na(factory, repo)
    suppressions = load_suppressions(factory, repo)
    requirements = load_asvs()
    check_ids(requirements, config, confirmed_na)

    base = git.rev(repo_root, base_ref)
    tree_sha = git.tree(repo_root, head)
    changed = git.changes(repo_root, base, head)
    base_spec, head_spec_text = _spec_text(repo_root, base), _spec_text(repo_root, head)
    spec = head_spec(head_spec_text)
    spec_classification = spec["data_classification"] if spec else None
    classification = effective_classification(intake["data_classification"] if intake else None, spec_classification)
    level_n = asvs_level(config, intake, spec_classification)
    head_packages, problems = read_packages(lambda p: git.show(repo_root, head, p))
    # Base lockfile problems are discarded on purpose. An unreadable base list is empty, so every head package looks new
    # and is looked up: the conservative direction. Head problems are kept and fire T-14.
    base_packages, _ = read_packages(lambda p: git.show(repo_root, base, p))
    observed = observe(checks, now)

    decided, agent_reqs = scope(for_level(requirements, level_n), config=config, confirmed_na=confirmed_na, packages=head_packages,
                                has_authz=bool(spec and spec.get("authz")), drift_green=drift_green(observed, config["drift_check"]),
                                lockfile_problems=problems)
    return Prepared(repo_root=repo_root, repo=repo, now=now, today=today, config=config, checks=checks, intake=intake,
                    suppressions=suppressions, requirements=requirements, level_n=level_n, classification=classification,
                    spec_classification=spec_classification, head=head, base=base, tree_sha=tree_sha, changed=changed,
                    base_spec=base_spec, head_spec_text=head_spec_text, head_packages=head_packages, base_packages=base_packages,
                    problems=problems, decided=decided, agent_reqs=agent_reqs, pr_number=_pr_number(event), pr_head=pr_head)


def agent_scope(prepared: Prepared) -> dict:
    """The agent-scope/v1 document: the requirements the agent must answer, and the tree its assessment must name."""
    doc = {"schema": "agent-scope/v1", "tree_sha": prepared.tree_sha, "asvs_level": prepared.level_n,
           "requirements": [{"asvs_id": r["id"], "chapter": r["chapter"], "section": r["section"], "level": r["level"], "text": r["text"]}
                            for r in prepared.agent_reqs]}
    errors = validate("agent-scope.v1", doc)
    if errors:
        raise InputError(f"the agent scope is not a valid agent-scope/v1: {errors[0]}")
    return doc


def finish(p: Prepared, assessment_doc=None, *, pr_text=None, registry=None) -> tuple[dict, dict]:
    """Return (scorecard, render context): the deterministic signals, the agent's assessment applied, the level, and the re-review event (context["event"])."""
    config, intake, requirements, decided, agent_reqs = p.config, p.intake, p.requirements, p.decided, p.agent_reqs
    now, today = p.now, p.today
    problems = list(p.problems)  # finish may run more than once on the same Prepared; never grow its list
    # apply_assessment checks the assessment itself (schema, tree, duplicates) and raises InputError when it is bad.
    agent_results, agent_findings, flags = apply_assessment(agent_reqs, assessment_doc, p.repo_root, config=config, tree_sha=p.tree_sha)
    if assessment_doc is not None:
        check_approved(assessment_doc, config)
    _redact_unverified(agent_results, agent_findings)
    assessment = assessment_doc
    repo_root, base, head = p.repo_root, p.base, p.head
    observed = observe(p.checks, now)

    signals = spec_changes(p.base_spec, p.head_spec_text, now=now)
    signals += change_signals(repo_root, base, head, p.changed, config, now=now, problems=problems)
    signals += tree_signals(repo_root, config, now=now, problems=problems)
    size = change_size(repo_root, base, head, problems)
    reg = config["registry"]
    client = registry or RegistryClient(timeout=reg["timeout_seconds"], deadline_seconds=reg["max_total_seconds"])
    dep_findings, skipped, failures = dependency_signals(p.base_packages, p.head_packages, client, reg, today=today, now=now)
    signals += dep_findings
    pr_hits = _pr_hits(pr_text, config)
    kept, applied = apply_suppressions(signals + agent_findings, p.suppressions, repo=p.repo, today=today,
                                       secret_rule_prefixes=config["suppressions"]["secret_rule_prefixes"])

    order = {r["id"]: i for i, r in enumerate(requirements)}
    results = sorted(decided + agent_results, key=lambda r: order[r["asvs_id"]])
    cov = coverage(agent_results)
    covered, total_agent = coverage_counts(agent_results)
    level, floor, reasons = decide(config=config, intake=intake, spec_classification=p.spec_classification, results=results, findings=kept,
                                   covered=covered, total=total_agent, flags=flags, lookup_failures=failures, pr_text_hits=pr_hits,
                                   problems=problems, change_size=size)
    names = chapter_names(requirements)
    total = counts(results)
    level_n = p.level_n
    card = {
        "schema": "scorecard/v1", "generated_at": now, "checkpoint": "pr", "tool": {"name": "scorecard", "version": VERSION},
        "app": {"repo": p.repo, "commit": head, "base": base, "tree_sha": p.tree_sha, "intake_found": intake is not None,
                "tier": intake["tier"] if intake else None, "purpose": intake["purpose"] if intake else None,
                "data_classification": p.classification, "asvs_level": level_n},
        "level": level, "floor": floor, "reasons": reasons, "triggers_fired": [r["trigger"] for r in reasons],
        "asvs": {
            "version": "5.0.0", "source_sha256": ASVS_SHA256, "level": level_n, "overall_score": score(total), "coverage": cov,
            "coverage_threshold": config["coverage_threshold"], "counts": total, "agent_requirements": len(agent_reqs),
            "platform_provided": sum(1 for r in decided if r["status_source"] == "platform"),
            "tool_covered": sum(1 for r in decided if r["status_source"] == "scanner"),
            "confirmed_na": sum(1 for r in decided if r["status_source"] == "human_na"),
            "chapters": chapters(results, names), "results": results,
        },
        "assessment": {
            "present": assessment is not None, "tree_sha": assessment["tree_sha"] if assessment else None,
            "prompt_version": assessment["prompt_version"] if assessment else None, "model": assessment["model"] if assessment else None,
            "complete": not flags["incomplete"], "ignored_results": flags["ignored"], "findings": agent_findings,
        },
        "signals": {"findings": signals, "skipped_packages": skipped, "lookup_failures": failures, "input_problems": problems, "pr_text_hits": pr_hits},
        "observations": {"checks": observed, "findings": []},
        "suppressions_applied": applied,
        "delta": None,
    }
    errors = validate("scorecard.v1", card)
    if errors:
        raise InputError(f"the scorecard is not a valid scorecard/v1: {errors[0]}")
    recorded = recorded_spec_signals(p.base_spec, p.head_spec_text) + builder_bumped(_lock_text(repo_root, base), _lock_text(repo_root, head))
    event = re_review_event(card, pr=p.pr_number, head_sha=p.pr_head or head, recorded=recorded)
    context = {"event": event, "texts": {r["id"]: r["text"] for r in requirements}, "names": names, "blocking": config["blocking"], "config": config,
               "expired_suppressions": expired(p.suppressions, repo=p.repo, today=today)}
    return card, context


def build_scorecard(*, repo_root, repo: str, base_ref: str, factory, checks_doc, event, now: str, assessment_doc=None,
                    pr_text=None, registry=None) -> tuple[dict, dict]:
    """Return (scorecard, render context). Raises NotAssessed before any other work when the PR can't be assessed."""
    prepared = prepare(repo_root=repo_root, repo=repo, base_ref=base_ref, factory=factory, checks_doc=checks_doc, event=event, now=now)
    return finish(prepared, assessment_doc, pr_text=pr_text, registry=registry)

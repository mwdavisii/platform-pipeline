"""Assemble scorecard/v2: the routing engine's facts, rules and level, around the deterministic signals both engines share."""

from builder.config import load_platform
from contracts.schema import validate
from golden_road.io import InputError

from .. import VERSION, git
from ..agent.tools import citable_paths
from ..event import re_review_event
from ..levels import floor as intake_floor
from ..observations import observe
from ..suppressions import expired
from .answers import check_answers, check_facts_assessment
from .facts import added_lines, builtin_facts, compute_facts, spec_facts
from .rules import evaluate

MAX_DIFF_BYTES = 8_000_000


def _gateway_hosts(problems: list) -> tuple:
    try:
        return tuple(load_platform()["model_gateway_hosts"])
    except (InputError, OSError, ValueError, KeyError, TypeError) as exc:
        problems.append({"source": "platform values", "detail": f"can't read the model gateway hosts: {exc}"[:300]})
        return ()


def routing_card(p, det: dict, assessment_doc, *, problems: list, recorded: list) -> tuple[dict, dict]:
    """(scorecard/v2, render context). `det` is build._deterministic's result; `problems` is the run's input-problem list."""
    config, routing = p.config, p.config["routing"]
    if assessment_doc is not None:
        check_facts_assessment(assessment_doc, tree_sha=p.tree_sha, config=config)
    diff_text, cut = git.diff_capped(p.repo_root, p.base, p.head, MAX_DIFF_BYTES)
    if cut:
        problems.append({"source": "change comparison", "detail": f"the diff is larger than {MAX_DIFF_BYTES} bytes, so the diff facts can't read all of it"})
    gateway_hosts = _gateway_hosts(problems)
    complete = bool(assessment_doc and assessment_doc["complete"])
    builtin = builtin_facts(findings=det["kept"], pr_hits=det["pr_hits"], problems=problems, lookup_failures=det["failures"],
                            change_size=det["size"], large_change=config["large_change"], intake=p.intake, registration=p.registration,
                            assessment_complete=complete)
    changed_head = {c["path"] for c in p.changed if c["status"] != "D"}
    agent = check_answers(assessment_doc, routing, repo_root=p.repo_root, citable=citable_paths(p.repo_root), changed=changed_head)
    facts = compute_facts(routing, builtin=builtin, spec=spec_facts(p.base_spec, p.head_spec_text, gateway_hosts),
                          changed=p.changed, added=added_lines(diff_text), agent=agent)
    floor = intake_floor(config, p.intake)
    level, reasons, shadow, undecided = evaluate(routing, facts, classification=p.classification, floor=floor)
    intake, doc = p.intake, assessment_doc
    card = {
        "schema": "scorecard/v2", "generated_at": p.now, "checkpoint": "pr", "tool": {"name": "scorecard", "version": VERSION},
        "app": {"repo": p.repo, "commit": p.head, "base": p.base, "tree_sha": p.tree_sha, "intake_found": intake is not None,
                "tier": intake["tier"] if intake else None, "purpose": intake["purpose"] if intake else None,
                "data_classification": p.classification},
        "level": level, "floor": floor, "reasons": reasons, "triggers_fired": [r["trigger"] for r in reasons],
        "assessment": {"present": doc is not None, "tree_sha": doc["tree_sha"] if doc else None,
                       "prompt_version": doc["prompt_version"] if doc else None, "model": doc["model"] if doc else None,
                       "questions_sha256": doc["questions_sha256"] if doc else None, "complete": complete},
        "routing": {"facts": facts, "shadow": shadow, "undecided": undecided},
        "signals": {"findings": det["signals"], "skipped_packages": det["skipped"], "lookup_failures": det["failures"],
                    "input_problems": problems, "pr_text_hits": det["pr_hits"]},
        "observations": {"checks": observe(p.checks, p.now), "findings": []},
        "suppressions_applied": det["applied"],
        "delta": None,
    }
    errors = validate("scorecard.v2", card)
    if errors:
        raise InputError(f"the scorecard is not a valid scorecard/v2: {errors[0]}")
    event = re_review_event(card, pr=p.pr_number, head_sha=p.pr_head or p.head, recorded=recorded)
    context = {"event": event, "config": config, "expired_suppressions": expired(p.suppressions, repo=p.repo, today=p.today)}
    return card, context

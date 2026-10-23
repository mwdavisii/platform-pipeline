import copy

import pytest

from contracts.schema import validate

SHA = "a" * 40
COUNTS = {"pass": 1, "fail": 0, "na": 0, "needs_human": 1}

SCORECARD = {
    "schema": "scorecard/v1",
    "generated_at": "2026-10-21T12:00:00Z",
    "checkpoint": "pr",
    "tool": {"name": "scorecard", "version": "0.1.0"},
    "app": {"repo": "acme/notes", "commit": SHA, "base": "b" * 40, "tree_sha": "c" * 40, "intake_found": True, "tier": "moderate",
            "purpose": "Stores team notes.", "data_classification": "internal", "asvs_level": 2},
    "level": "security_review",
    "floor": "standard",
    "reasons": [{"trigger": "T-2", "level": "security_review", "summary": "authz loosened in builder-spec.json",
                 "refs": [{"file": "builder-spec.json", "line": 1}]}],
    "triggers_fired": ["T-2"],
    "asvs": {
        "version": "5.0.0", "source_sha256": "9" * 64, "level": 2, "overall_score": 100.0, "coverage": 50.0, "coverage_threshold": 70,
        "counts": COUNTS, "agent_requirements": 2, "platform_provided": 0, "tool_covered": 0, "confirmed_na": 0,
        "chapters": [{"chapter": "v5.0.0-8", "name": "Authorization", "score": 100.0, "counts": COUNTS}],
        "results": [{
            "schema": "requirement-result/v1", "asvs_id": "v5.0.0-8.2.2", "chapter": "v5.0.0-8", "section": "v5.0.0-8.2", "level": 1,
            "status": "needs_human", "confidence": "low", "justification": "", "evidence": [], "finding_ids": [],
            "evidence_verified": False, "status_source": "downgraded", "downgrade_reason": "not assessed by the agent",
        }],
    },
    "assessment": {"present": False, "tree_sha": None, "prompt_version": None, "model": None, "complete": False, "ignored_results": [], "findings": []},
    "signals": {"findings": [], "skipped_packages": [], "lookup_failures": [], "input_problems": [], "pr_text_hits": []},
    "observations": {"checks": [{"name": "golden-road / drift", "state": "success", "observed_at": "2026-10-21T12:00:00Z"}], "findings": []},
    "suppressions_applied": [],
    "delta": None,
}


def test_example_is_valid():
    assert validate("scorecard.v1", SCORECARD) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s.update(level="blocked"),
        lambda s: s.update(delta={}),
        lambda s: s.update(decision="escalate"),
        lambda s: s.update(triggers_fired=["T-13x"]),
        lambda s: s["reasons"][0].update(refs=[{}]),
        lambda s: s["asvs"].update(coverage=101),
        lambda s: s["asvs"]["results"][0].update(status_source="agent", status="pass"),  # embedded result is checked too
        lambda s: s["signals"].update(findings=[{"schema": "finding/v2"}]),  # embedded findings are checked too
        lambda s: s["app"].update(commit="HEAD"),
        lambda s: s.update(triggers_fired=["T-13"]),  # T-13 is reserved
        lambda s: s["reasons"][0].update(trigger="T-13"),
        lambda s: s.update(triggers_fired=["T-20"]),
        lambda s: s["reasons"][0].update(total=0),
        lambda s: s["reasons"][0].update(total="3"),
        lambda s: s["assessment"].update(prompt_version="bad version"),
        lambda s: s["suppressions_applied"].append({"id": "-bad", "finding_id": "a" * 64}),
        lambda s: s["reasons"][0].update(refs=[{"asvs_id": "v5.0.0-8"}]),
        lambda s: s["reasons"][0].update(refs=[{"file": "app.py", "line": 2147483648}]),
        lambda s: s["app"].update(intake_found=False),  # no intake means no tier
        lambda s: s["app"].update(tier=None),  # an intake means a tier
    ],
)
def test_scorecard_rejects(mutate):
    doc = copy.deepcopy(SCORECARD)
    mutate(doc)
    assert validate("scorecard.v1", doc)


def test_no_intake_means_null_tier_purpose_and_classification():
    doc = copy.deepcopy(SCORECARD)
    doc["app"].update(intake_found=False, tier=None, purpose=None, data_classification=None)
    assert validate("scorecard.v1", doc) == []


def test_no_intake_may_still_carry_the_classification_from_the_builder_spec():
    doc = copy.deepcopy(SCORECARD)
    doc["app"].update(intake_found=False, tier=None, purpose=None, data_classification="phi")
    assert validate("scorecard.v1", doc) == []
    doc["app"]["tier"] = "high"
    assert validate("scorecard.v1", doc)


def test_optional_sections_may_be_absent_or_null():
    doc = copy.deepcopy(SCORECARD)
    doc.update(openssf=None, quality=None, llm_top10=None)
    assert validate("scorecard.v1", doc) == []


@pytest.mark.parametrize("trigger", ["T-17", "T-18", "T-19", "T-21"])
def test_the_re_review_triggers_are_valid_trigger_ids(trigger):
    doc = copy.deepcopy(SCORECARD)
    doc["reasons"][0]["trigger"] = trigger
    doc["triggers_fired"] = [trigger]
    assert validate("scorecard.v1", doc) == []


def test_a_reason_may_carry_the_true_item_count():
    doc = copy.deepcopy(SCORECARD)
    doc["reasons"][0]["total"] = 35
    assert validate("scorecard.v1", doc) == []

"""scorecard/v2 end to end on a real checkout: the routing factory, the fake reviewer, and the rules."""

import pytest

from contracts.schema import validate
from golden_road.io import InputError
from scorecard.agent.fake import FactsModel
from scorecard.agent.review2 import assess_facts
from scorecard.build import finish, prepare


class _NoRegistry:
    """Every package is established, so no dependency signal fires."""

    def lookup(self, ecosystem, name, version):
        return {"exists": True, "version_exists": True, "first_release": "2019-01-01", "maintainers": 5, "has_wheel": True}


def _run(pr_checkout, factory, changes=None, model=None, assess=True):
    p = prepare(**pr_checkout(changes), factory=factory)
    doc = assess_facts(p, model or FactsModel())[0] if assess else None
    return finish(p, doc, registry=_NoRegistry())


def test_a_quiet_change_is_standard(pr_checkout, routing_factory):
    card, context = _run(pr_checkout, routing_factory)
    assert validate("scorecard.v2", card) == [] and card["level"] == "standard" and card["reasons"] == []
    assert card["assessment"]["complete"] and card["routing"]["shadow"] == []
    assert card["routing"]["facts"]["own_credentials"]["answer"] == "no"
    assert validate("re-review-event.v1", context["event"]) == []


def test_no_assessment_is_t14_and_every_unsure_rule_routes(pr_checkout, routing_factory):
    card, _ = _run(pr_checkout, routing_factory, assess=False)
    # Unsure on T-3, T-4, T-6 and T-7 routes at elevated (their on_unsure); T-9, T-10 and T-26 are undecided; T-14 is elevated.
    assert card["level"] == "elevated"
    assert {u["trigger"] for u in card["routing"]["undecided"]} >= {"T-9", "T-10"}
    fired = {r["trigger"]: r for r in card["reasons"]}
    assert "T-14" in fired and fired["T-4"]["unsure"] and fired["T-4"]["level"] == "elevated"
    assert {s["trigger"] for s in card["routing"]["shadow"]} >= {"T-22", "T-23", "T-25"}  # shadow: recorded, not routed


def test_a_verified_yes_on_a_shadow_rule_changes_nothing(pr_checkout, routing_factory):
    model = FactsModel(answers={"own_credentials": {"answer": "yes", "evidence": [
        {"file": "app.py", "start_line": 8, "snippet": "return [n for n in NOTES if n.owner == user]"}]}})
    card, _ = _run(pr_checkout, routing_factory, model=model)
    assert card["level"] == "standard"
    assert [s["trigger"] for s in card["routing"]["shadow"]] == ["T-22"] and card["routing"]["shadow"][0]["unsure"] is False


def test_a_workflow_change_is_t16(pr_checkout, routing_factory):
    card, _ = _run(pr_checkout, routing_factory, {".github/workflows/ci.yml": "on: push\n"})
    t16 = next(r for r in card["reasons"] if r["trigger"] == "T-16")
    assert card["level"] == "security_review" and t16["guidance"].startswith("Undo the change")


def test_an_assessment_for_other_questions_is_refused(pr_checkout, routing_factory):
    p = prepare(**pr_checkout(), factory=routing_factory)
    doc = assess_facts(p, FactsModel())[0]
    with pytest.raises(InputError, match="other questions"):
        finish(p, {**doc, "questions_sha256": "0" * 64}, registry=_NoRegistry())


def test_the_asvs_factory_still_writes_scorecard_v1(pr_checkout, factory):
    p = prepare(**pr_checkout(), factory=factory)
    card, _ = finish(p, None, registry=_NoRegistry())
    assert card["schema"] == "scorecard/v1"

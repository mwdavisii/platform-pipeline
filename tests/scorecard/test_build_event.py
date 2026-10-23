import copy
import json

from contracts.schema import validate
from golden_road.io import InputError
from scorecard import git
from scorecard.build import build_scorecard


def test_finish_builds_a_valid_event_with_the_pr_its_head_and_the_signals(pr_checkout, factory, notes_spec, fake_registry):
    head = copy.deepcopy(notes_spec)
    head["resources_tier"] = "medium"
    head["egress"] = []
    kw = pr_checkout({"builder-spec.json": json.dumps(head, indent=2)})
    kw["event"]["pull_request"]["number"] = 11
    card, context = build_scorecard(factory=factory, registry=fake_registry({}), **kw)
    event = context["event"]
    assert validate("re-review-event.v1", event) == []
    assert event["pr"] == 11 and event["head_sha"] == kw["event"]["pull_request"]["head"]["sha"]
    assert event["tree_sha"] == card["app"]["tree_sha"] and (event["level"], event["floor"]) == (card["level"], card["floor"])
    assert event["complete"] is card["assessment"]["complete"]
    assert "T-17" in card["triggers_fired"]
    assert [s["id"] for s in event["signals"]] == [*card["triggers_fired"], "egress-removed"]


def test_without_a_pr_number_the_event_has_no_pr(pr_checkout, factory, fake_registry):
    _, context = build_scorecard(factory=factory, registry=fake_registry({}), **pr_checkout())
    assert context["event"]["pr"] is None


def test_a_size_comparison_that_fails_is_an_input_problem_and_t19_does_not_fire(pr_checkout, factory, fake_registry, monkeypatch):
    kw = pr_checkout()

    def boom(*args, **kwargs):
        raise InputError("git diff failed: boom")

    monkeypatch.setattr(git, "numstat", boom)
    card, _ = build_scorecard(factory=factory, registry=fake_registry({}), **kw)
    assert "T-19" not in card["triggers_fired"] and "T-14" in card["triggers_fired"]
    assert {"source": "change size", "detail": "can't count the changed lines: git diff failed: boom"} in card["signals"]["input_problems"]

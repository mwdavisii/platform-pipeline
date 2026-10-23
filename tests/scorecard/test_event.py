import json

import pytest

from builder import VERSION as BUILDER_VERSION
from contracts.schema import validate
from golden_road.io import InputError
from scorecard.event import re_review_event

FINDING_ID = "e" * 64
CARD = {
    "generated_at": "2026-10-23T08:00:00Z", "level": "security_review", "floor": "standard",
    "app": {"repo": "acme/notes", "tree_sha": "c" * 40}, "assessment": {"complete": False},
    "reasons": [
        {"trigger": "T-2", "level": "security_review", "summary": "Security-relevant builder-spec.json change",
         "refs": [{"finding_id": FINDING_ID, "file": "builder-spec.json"}]},
        {"trigger": "T-14", "level": "elevated", "summary": "Assessment incomplete, or an input could not be read or looked up (2 items)",
         "refs": [{"source": "agent assessment"}, {"source": "package.json"}]},
    ],
}
RECORDED = [{"id": "egress-removed", "kind": "recorded", "summary": "egress removed: litellm.internal:443"},
            {"id": "builder-bumped", "kind": "recorded", "summary": "builder 0.3.7 -> 0.3.8"}]


def test_the_event_carries_the_card_s_level_and_every_signal_in_order():
    event = re_review_event(CARD, pr=11, head_sha="a" * 40, recorded=RECORDED)
    assert validate("re-review-event.v1", event) == []
    assert {k: v for k, v in event.items() if k != "signals"} == {
        "schema": "re-review-event/v1", "repo": "acme/notes", "pr": 11, "head_sha": "a" * 40, "tree_sha": "c" * 40,
        "scorecard_version": BUILDER_VERSION, "created": "2026-10-23T08:00:00Z", "level": "security_review", "floor": "standard",
        "complete": False}
    assert event["signals"] == [
        {"id": "T-2", "kind": "trigger", "level": "security_review", "summary": "Security-relevant builder-spec.json change", "count": 1},
        {"id": "T-14", "kind": "trigger", "level": "elevated",
         "summary": "Assessment incomplete, or an input could not be read or looked up (2 items)", "count": 2},
        RECORDED[1], RECORDED[0]]


def test_signals_are_never_repeated():
    event = re_review_event(CARD, pr=11, head_sha="a" * 40, recorded=RECORDED + [dict(RECORDED[1])])
    ids = [s["id"] for s in event["signals"]]
    assert len(ids) == len(set(ids)) == 4


def test_the_event_holds_no_refs():
    text = json.dumps(re_review_event(CARD, pr=11, head_sha="a" * 40, recorded=[]))
    assert "refs" not in text and FINDING_ID not in text and "agent assessment" not in text


def test_an_event_that_does_not_validate_is_an_input_error():
    with pytest.raises(InputError, match="re-review-event/v1"):
        re_review_event(CARD, pr=0, head_sha="a" * 40, recorded=[])


def test_count_is_the_true_item_count_not_the_capped_refs():
    card = {**CARD, "reasons": [{"trigger": "T-5", "level": "security_review", "summary": "Hallucinated package (showing 20 of 35)",
                                 "refs": [{"source": f"pkg{i}"} for i in range(20)], "total": 35}]}
    event = re_review_event(card, pr=11, head_sha="a" * 40, recorded=[])
    assert event["signals"][0]["count"] == 35 and len(card["reasons"][0]["refs"]) == 20


def test_a_reason_without_total_counts_its_refs():
    assert re_review_event(CARD, pr=11, head_sha="a" * 40, recorded=[])["signals"][0]["count"] == 1

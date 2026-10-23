import copy

import pytest

from contracts.schema import validate

EVENT = {
    "schema": "re-review-event/v1", "repo": "acme/notes", "pr": 11, "head_sha": "a" * 40, "tree_sha": "c" * 40,
    "scorecard_version": "0.3.9", "created": "2026-10-23T08:00:00Z", "level": "security_review", "floor": "standard", "complete": True,
    "signals": [
        {"id": "T-1", "kind": "trigger", "level": "security_review", "summary": "Verified fail in a blocking chapter", "count": 1},
        {"id": "builder-bumped", "kind": "recorded", "summary": "builder 0.3.7 -> 0.3.8"},
    ],
}


def test_the_example_is_valid():
    assert validate("re-review-event.v1", EVENT) == []


def test_a_run_without_a_pr_and_without_signals_is_valid():
    doc = copy.deepcopy(EVENT)
    doc.update(pr=None, signals=[])
    assert validate("re-review-event.v1", doc) == []


@pytest.mark.parametrize("mutate", [
    lambda e: e.pop("complete"),
    lambda e: e.update(schema="re-review-event/v2"),
    lambda e: e.update(pr=0),
    lambda e: e.update(pr="11"),
    lambda e: e.update(head_sha="HEAD"),
    lambda e: e.update(scorecard_version="0.3.9-dirty"),
    lambda e: e.update(created="yesterday"),
    lambda e: e.update(level="blocked"),
    lambda e: e.update(extra=1),
    lambda e: e["signals"][0].update(id="T-13"),  # reserved
    lambda e: e["signals"][0].update(id="T-20"),
    lambda e: e["signals"][0].pop("count"),
    lambda e: e["signals"][0].update(count=0),
    lambda e: e["signals"][0].update(refs=[{"file": "app.py"}]),  # no refs, ever
    lambda e: e["signals"][0].update(justification="the model said so"),  # no model text, ever
    lambda e: e["signals"][1].update(id="new-base-image"),
    lambda e: e["signals"][1].update(level="elevated"),
    lambda e: e["signals"][1].update(kind="trigger"),
    lambda e: e["signals"][1].update(summary=""),
])
def test_the_schema_rejects(mutate):
    doc = copy.deepcopy(EVENT)
    mutate(doc)
    assert validate("re-review-event.v1", doc)


def test_a_count_above_twenty_is_valid():
    doc = copy.deepcopy(EVENT)
    doc["signals"][0]["count"] = 35
    assert validate("re-review-event.v1", doc) == []


def test_t21_is_a_valid_trigger_signal():
    doc = copy.deepcopy(EVENT)
    doc["signals"].append({"id": "T-21", "kind": "trigger", "level": "security_review",
                           "summary": "Registered as low/internal; this change needs at least moderate/internal", "count": 1})
    assert validate("re-review-event.v1", doc) == []

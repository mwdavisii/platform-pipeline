"""Every schema that names a classification names exactly the three levels (spec 2026-10-30 C1, C4)."""

import json
from pathlib import Path

import pytest

from contracts.classification import LEVELS, RETIRED
from contracts.schema import validate

SCHEMAS = Path(__file__).resolve().parents[2] / "src/contracts/schemas"
ENUMS = [
    ("builder-spec.v1", ["properties", "data_classification", "enum"]),
    ("intake.v1", ["properties", "data_classification", "enum"]),
    ("intake.v2", ["properties", "data_classification", "enum"]),
    ("intake.v2", ["properties", "floor_classification", "enum"]),
    ("registration-draft.v1", ["properties", "data_classification", "enum"]),
    ("registration-evidence.v1", ["$defs", "classification", "enum"]),
    ("registration-rules.v1", ["$defs", "classification", "enum"]),
    ("routing.v1", ["$defs", "rule", "properties", "escalate", "properties", "classifications", "items", "enum"]),
    ("scorecard-config.v1", ["properties", "sensitive_classifications", "items", "enum"]),
]


def at(name, path):
    node = json.loads((SCHEMAS / f"{name}.json").read_text(encoding="utf-8"))
    for key in path:
        node = node[key]
    return node


@pytest.mark.parametrize("name, path", ENUMS)
def test_each_enum_is_exactly_the_three(name, path):
    assert at(name, path) == list(LEVELS)


def test_the_scorecard_still_allows_an_unknown_classification():
    assert at("scorecard.v1", ["properties", "app", "properties", "data_classification", "enum"]) == [*LEVELS, None]


@pytest.mark.parametrize("name, path", [("registration-rules.v1", ["properties", "classification_tier"]),
                                        ("scorecard-config.v1", ["properties", "classification_asvs_level"])])
def test_the_maps_keyed_by_level_have_exactly_the_three_keys(name, path):
    node = at(name, path)
    assert node["required"] == list(LEVELS) and list(node["properties"]) == list(LEVELS)


@pytest.mark.parametrize("old", sorted(RETIRED))
def test_a_retired_value_no_longer_validates(old):
    draft = {"schema": "registration-draft/v1", "repo": "acme/notes", "data_classification": old}
    assert validate("registration-draft.v1", draft)
    assert validate("registration-draft.v1", {**draft, "data_classification": RETIRED[old]}) == []

import copy
import json
from importlib import resources

import pytest

from scorecard.signals.spec_diff import TIER_ORDER, recorded_spec_signals, spec_changes, tier_lowered, tier_raised

NOW = "2026-10-21T12:00:00Z"


def _rules(findings):
    return sorted(f["rule_id"] for f in findings)


def _diff(base, head):
    return spec_changes(None if base is None else json.dumps(base), None if head is None else json.dumps(head), now=NOW)


def test_unchanged_spec_has_no_signal(notes_spec):
    assert _diff(notes_spec, notes_spec) == []


def test_loosened_authz_fires_t2(notes_spec):
    """AC-12 (signal half): readers may now write."""
    head = copy.deepcopy(notes_spec)
    head["authz"]["rules"][1]["roles"] = ["reader", "writer"]
    head["authz"]["rules"][1]["justification"] = "readers may post notes now"
    findings = _diff(notes_spec, head)
    assert _rules(findings) == ["T-2/authz-widened"]
    assert "POST /api/** -> reader" in findings[0]["evidence"][0]["snippet"]
    assert {"framework": "golden-road-trigger", "id": "T-2"} in findings[0]["standards"]


def test_authz_removed_egress_added_classification_lowered_secret_renamed(notes_spec):
    head = copy.deepcopy(notes_spec)
    del head["authz"]
    head["egress"].append({"host": "api.example.com", "port": 443, "reason": "webhook"})
    head["data_classification"] = "public"
    head["env"] = [{"name": "NOTES_DATABASE_URL", "secret": True}]
    assert _rules(_diff(notes_spec, head)) == [
        "T-2/authz-removed", "T-2/classification-lowered", "T-2/egress-added", "T-2/secret-env-changed",
    ]


def test_role_widened_and_rule_removed(notes_spec):
    head = copy.deepcopy(notes_spec)
    head["authz"]["roles"][0]["access"] = "write"
    head["authz"]["rules"] = head["authz"]["rules"][:1]
    assert _rules(_diff(notes_spec, head)) == ["T-2/authz-rule-removed", "T-2/role-widened"]


def test_adding_authz_to_an_app_without_it_is_not_loosening(notes_spec):
    base = copy.deepcopy(notes_spec)
    del base["authz"]
    assert _diff(base, notes_spec) == []


def test_raising_classification_is_not_a_signal(notes_spec):
    head = copy.deepcopy(notes_spec)
    head["data_classification"] = "phi"
    assert _diff(notes_spec, head) == []


def test_invalid_or_deleted_head_spec_fires(notes_spec):
    assert _rules(_diff(notes_spec, None)) == ["T-2/spec-removed-or-invalid"]
    assert _rules(spec_changes(json.dumps(notes_spec), "{not json", now=NOW)) == ["T-2/spec-removed-or-invalid"]


def test_no_spec_on_either_side_is_quiet():
    assert _diff(None, None) == []


def test_ids_are_stable(notes_spec):
    head = copy.deepcopy(notes_spec)
    head["egress"].append({"host": "api.example.com", "port": 443, "reason": "webhook"})
    assert [f["id"] for f in _diff(notes_spec, head)] == [f["id"] for f in _diff(notes_spec, head)]


def test_health_path_change_is_an_authz_exemption(notes_spec):
    head = copy.deepcopy(notes_spec)
    head["health_path"] = "/api/notes"
    findings = _diff(notes_spec, head)
    assert _rules(findings) == ["T-2/exempt-path-changed"] and "/api/notes" in findings[0]["evidence"][0]["snippet"]
    assert _diff(head, head) == []
    base = copy.deepcopy(notes_spec)
    base.pop("health_path", None)
    assert _rules(_diff(base, head)) == ["T-2/exempt-path-changed"]


def test_unreadable_base_spec_fires_instead_of_comparing_with_nothing(notes_spec):
    assert _rules(spec_changes("{not json", json.dumps(notes_spec), now=NOW)) == ["T-2/spec-unreadable"]
    assert _rules(spec_changes("{not json", None, now=NOW)) == ["T-2/spec-removed-or-invalid"]


def test_raising_the_resource_tier_fires_t17(notes_spec):
    head = copy.deepcopy(notes_spec)
    head["resources_tier"] = "medium"
    findings = _diff(notes_spec, head)
    assert _rules(findings) == ["T-17/resources-raised"]
    assert findings[0]["severity"] == "medium" and {"framework": "golden-road-trigger", "id": "T-17"} in findings[0]["standards"]
    assert findings[0]["evidence"][0]["snippet"] == "resources_tier raised from default to medium"


def test_a_new_spec_above_the_default_tier_fires_t17(notes_spec):
    head = copy.deepcopy(notes_spec)
    head["resources_tier"] = "medium"
    assert "T-17/resources-raised" in _rules(_diff(None, head))
    assert "T-17/resources-raised" not in _rules(_diff(None, notes_spec))


def test_the_same_or_a_lower_tier_does_not_fire_t17(notes_spec):
    base = copy.deepcopy(notes_spec)
    base["resources_tier"] = "medium"
    assert _diff(base, base) == [] and _diff(base, notes_spec) == []


def test_changing_the_app_type_fires_t18(notes_spec):
    head = copy.deepcopy(notes_spec)
    head["app_type"] = "node"
    findings = _diff(notes_spec, head)
    assert _rules(findings) == ["T-18/app-type-changed"]
    assert findings[0]["severity"] == "medium" and {"framework": "golden-road-trigger", "id": "T-18"} in findings[0]["standards"]
    assert findings[0]["evidence"][0]["snippet"] == "app_type changed from python to node"


def test_a_first_spec_is_not_an_app_type_change(notes_spec):
    assert "T-18/app-type-changed" not in _rules(_diff(None, notes_spec))


def test_an_invalid_head_spec_is_t2_not_t17_or_t18(notes_spec):
    assert _rules(spec_changes(json.dumps(notes_spec), "{bad", now=NOW)) == ["T-2/spec-removed-or-invalid"]


def test_the_tier_order_is_the_builder_spec_enum():
    schema = json.loads((resources.files("contracts") / "schemas" / "builder-spec.v1.json").read_text(encoding="utf-8"))
    assert list(TIER_ORDER) == schema["properties"]["resources_tier"]["enum"]


@pytest.mark.parametrize("before, after, raised", [
    (None, "default", False), (None, "medium", True), ("default", "medium", True), ("medium", "medium", False),
    ("medium", "default", False), ("default", "large", True), ("large", "default", True), ("large", "large", True),
])
def test_tier_raised_fails_closed_on_an_unknown_tier(before, after, raised):
    assert tier_raised(before, after) is raised


@pytest.mark.parametrize("before, after, lowered", [
    ("medium", "default", True), ("default", "medium", False), ("default", "default", False), ("large", "default", False), ("medium", "large", False),
])
def test_tier_lowered_only_between_known_tiers(before, after, lowered):
    assert tier_lowered(before, after) is lowered


def test_removed_egress_is_recorded_and_raises_no_trigger(notes_spec):
    head = copy.deepcopy(notes_spec)
    head["egress"] = []
    assert _diff(notes_spec, head) == []
    assert recorded_spec_signals(json.dumps(notes_spec), json.dumps(head)) == [
        {"id": "egress-removed", "kind": "recorded", "summary": "egress removed: litellm.internal:443"}]


def test_a_lower_tier_is_recorded_and_raises_no_trigger(notes_spec):
    base = copy.deepcopy(notes_spec)
    base["resources_tier"] = "medium"
    assert _diff(base, notes_spec) == []
    assert recorded_spec_signals(json.dumps(base), json.dumps(notes_spec)) == [
        {"id": "resources-lowered", "kind": "recorded", "summary": "resources_tier lowered from medium to default"}]


def test_nothing_is_recorded_without_two_readable_specs_or_without_a_change(notes_spec):
    text = json.dumps(notes_spec)
    assert recorded_spec_signals(text, text) == []
    assert recorded_spec_signals(None, text) == [] and recorded_spec_signals(text, None) == []
    assert recorded_spec_signals("{bad", text) == [] and recorded_spec_signals(text, "{bad") == []

import copy
import json

from scorecard.signals.spec_diff import spec_changes

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

"""T-21's input: the registration floor computed from the checked-out merge tree (same rules as the portal's collector; the parity test
in tests/pipeline holds the two together). Here: the local reading, the floor, and T-21 through prepare and finish."""

import json

import pytest
import yaml

from contracts.schema import validate
from golden_road.io import InputError
from scorecard.build import build_scorecard
from scorecard.config import load_registration_rules
from scorecard.signals import registration as reg
from tree_fixture import commit_tree

SHA = "a" * 40
NODE = {"app_type": "node", "entrypoint": "server.js"}
GW = ("litellm.internal",)
SPEC = {"app_name": "notes", "app_type": "python", "entrypoint": "app.main:app", "port": 8000, "data_classification": "internal",
        "egress": [], "env": [], "resources_tier": "default"}
RULES = {"schema": "registration-rules/v1", "no_spec": {"tier": "high", "data_classification": "restricted"},
         "classification_tier": {"public": "low", "internal": "low", "restricted": "high", "phi": "high"},
         "raise_to": {"write_roles": "moderate", "secret_env": "moderate", "model_gateway_egress": "moderate", "other_egress": "moderate",
                      "many_dependencies": "moderate"},
         "many_dependencies_over": 50}


def committed(tmp_path, files, **kw):
    """Commit the files (bytes, text or a JSON-able dict) at the repo root and return (root, commit)."""
    raw = {name: content if isinstance(content, bytes) else json.dumps(content).encode() if isinstance(content, dict) else content.encode()
           for name, content in files.items()}
    return tmp_path, commit_tree(tmp_path, raw, **kw)


def compute(tmp_path, files, **kw):
    root, commit = committed(tmp_path, files, **kw)
    return reg.compute_evidence(root, repo="acme/notes", commit=commit, gateway_hosts=GW, many_dependencies_over=50)


def evidence(tmp_path, files, **spec):
    files = {"builder-spec.json": {**SPEC, **spec}, **files}
    doc = compute(tmp_path, files)
    assert validate("registration-evidence.v1", doc) == []
    return doc


def rules_of(doc):
    return [s["rule"] for s in doc["signals"]]


def test_no_spec_is_the_no_spec_floor(tmp_path):
    doc = compute(tmp_path, {})
    assert doc["spec_present"] is False and rules_of(doc) == ["no_spec"]
    assert reg.compute_floor(doc, RULES) == ("high", "restricted")


@pytest.mark.parametrize("bad", [b"{not json", json.dumps({**SPEC, "port": 80}).encode()])
def test_an_invalid_spec_counts_as_absent(tmp_path, bad):
    doc = compute(tmp_path, {"builder-spec.json": bad})
    assert doc["spec_present"] is False and "no_spec" in rules_of(doc)


def test_an_oversized_or_non_file_spec_is_unreadable_and_raises_the_floor(tmp_path):
    doc = compute(tmp_path, {"builder-spec.json": b" " * 70000})
    assert rules_of(doc) == ["unreadable:builder-spec.json"] and reg.compute_floor(doc, RULES) == ("high", "restricted")


@pytest.mark.parametrize("name", ["builder-spec.json", "requirements.txt", "package-lock.json", ".builder.lock"])
def test_a_symlink_is_unreadable_not_followed(tmp_path, name):
    doc = compute(tmp_path, {"elsewhere": json.dumps(SPEC)} if name == "builder-spec.json" else {"builder-spec.json": {**SPEC, **(NODE if name == "package-lock.json" else {})}, "elsewhere": "fastapi==0.1\n"},
                  symlinks={name: "elsewhere"})
    assert f"unreadable:{name}" in rules_of(doc)


def test_what_is_stored_counts_not_what_a_gitattributes_rule_writes_to_disk(tmp_path):
    lines = "".join(f"pkg{i}==1.0\n" for i in range(60))
    root, commit = committed(tmp_path, {"builder-spec.json": SPEC, "requirements.txt": lines},
                             attributes="requirements.txt working-tree-encoding=UTF-16LE-BOM\n")
    assert (root / "requirements.txt").read_bytes()[:2] == b"\xff\xfe"  # on disk it is UTF-16: the attack
    doc = reg.compute_evidence(root, repo="acme/notes", commit=commit, gateway_hosts=GW, many_dependencies_over=50)
    assert doc["dependencies"]["count"] == 60 and "many_dependencies" in rules_of(doc)


def test_a_file_in_the_working_tree_but_not_in_the_commit_does_not_count(tmp_path):
    root, commit = committed(tmp_path, {"builder-spec.json": SPEC})
    (root / "requirements.txt").write_text("".join(f"pkg{i}==1.0\n" for i in range(60)))
    assert reg.compute_evidence(root, repo="acme/notes", commit=commit, gateway_hosts=GW, many_dependencies_over=50)["dependencies"]["count"] == 0


@pytest.mark.parametrize("spec, rule", [
    ({"authz": {"roles": [{"name": "editor", "access": "write"}], "default": "deny",
                "rules": [{"methods": ["POST"], "path": "/n", "roles": ["editor"]}]}}, "write_roles"),
    ({"env": [{"name": "API_TOKEN", "secret": True}]}, "secret_env"),
    ({"egress": [{"host": "litellm.internal", "port": 443, "reason": "model calls"}]}, "model_gateway_egress"),
    ({"egress": [{"host": "api.example.org", "port": 443, "reason": "weather"}]}, "other_egress"),
])
def test_each_signal_raises_the_tier_to_moderate(tmp_path, spec, rule):
    doc = evidence(tmp_path, {}, **spec)
    assert rules_of(doc) == [rule] and reg.compute_floor(doc, RULES) == ("moderate", "internal")


def test_the_model_gateway_host_comes_from_the_argument_and_other_egress_does_not_hide_it(tmp_path):
    doc = evidence(tmp_path, {}, egress=[{"host": "litellm.internal", "port": 443, "reason": "model calls"}, {"host": "api.example.org", "port": 443, "reason": "weather data"}])
    assert rules_of(doc) == ["model_gateway_egress", "other_egress"]


def test_pinned_requirements_count_and_many_dependencies_fires_above_the_limit(tmp_path):
    lines = "".join(f"pkg{i}==1.0\n" for i in range(51)) + "# c\n-r other.txt\n"
    doc = evidence(tmp_path, {"requirements.txt": lines})
    assert doc["dependencies"] == {"count": 51, "lockfile_present": True, "lockfile": "requirements.txt"} and "many_dependencies" in rules_of(doc)


def test_package_lock_counts_packages_but_the_root(tmp_path):
    lock = {"packages": {"": {}, "node_modules/a": {}, "node_modules/b": {}}}
    doc = evidence(tmp_path, {"package-lock.json": lock}, app_type="node", entrypoint="server.js")
    assert doc["dependencies"]["count"] == 2 and rules_of(doc) == []


def test_a_bad_builder_lock_is_unreadable_and_a_missing_lockfile_is_recorded_only(tmp_path):
    doc = evidence(tmp_path, {".builder.lock": b"[no"})
    assert rules_of(doc) == ["unreadable:.builder.lock"] and doc["dependencies"]["lockfile_present"] is False


def test_the_floor_is_the_higher_of_the_classification_and_the_signals(tmp_path):
    doc = evidence(tmp_path, {}, data_classification="restricted", egress=[{"host": "api.example.org", "port": 443, "reason": "weather data"}])
    assert reg.compute_floor(doc, RULES) == ("high", "restricted")


@pytest.mark.parametrize("tier, cls, floor, below", [
    ("low", "internal", ("moderate", "internal"), True), ("moderate", "public", ("moderate", "internal"), True),
    ("moderate", "internal", ("moderate", "internal"), False), ("high", "phi", ("moderate", "internal"), False),
    ("unknown", "internal", ("low", "internal"), True), ("low", "unknown", ("low", "internal"), True),
])
def test_below_the_floor_uses_the_orderings_and_unknown_is_below(tier, cls, floor, below):
    assert reg.below_floor(tier, cls, *floor) is below


def test_registration_floor_returns_tier_classification_and_the_rule_names(tmp_path):
    root, commit = committed(tmp_path, {"builder-spec.json": {**SPEC, "env": [{"name": "API_TOKEN", "secret": True}]}})
    problems = []
    got = reg.registration_floor(root, RULES, repo="acme/notes", commit=commit, gateway_hosts=GW, problems=problems)
    assert got == {"tier": "moderate", "data_classification": "internal", "rules": ["secret_env"]} and problems == []


def test_an_evidence_document_that_is_not_valid_is_an_input_problem(tmp_path, monkeypatch):
    def bad(*args, **kwargs):
        raise InputError("the computed registration evidence is not valid: boom")

    monkeypatch.setattr(reg, "compute_evidence", bad)
    problems = []
    assert reg.registration_floor(tmp_path, RULES, repo="acme/notes", commit=SHA, gateway_hosts=GW, problems=problems) is None
    assert problems[0]["source"] == "registration floor"


# T-21 through the build.


def _intake(factory, **changes):
    path = factory / "intake" / "acme" / "notes.yaml"
    doc = yaml.safe_load(path.read_text())
    doc.update(changes)
    path.write_text(yaml.safe_dump(doc))


def test_t21_fires_in_the_build_when_the_intake_is_below_the_floor_and_the_event_carries_it(pr_checkout, factory, fake_registry):
    _intake(factory, tier="low")
    card, context = build_scorecard(factory=factory, registry=fake_registry({}), **pr_checkout())
    [reason] = [r for r in card["reasons"] if r["trigger"] == "T-21"]
    assert reason["summary"].startswith("Registered as low/internal; this change needs at least moderate/internal (signals: ")
    assert reason["level"] == "security_review" and card["level"] == "security_review"
    assert "T-21" in [s["id"] for s in context["event"]["signals"]]
    assert validate("scorecard.v1", card) == [] and validate("re-review-event.v1", context["event"]) == []


def test_t21_is_quiet_when_the_registration_covers_the_floor(pr_checkout, factory, fake_registry):
    card, _ = build_scorecard(factory=factory, registry=fake_registry({}), **pr_checkout())
    assert "T-21" not in card["triggers_fired"]


def test_a_change_that_adds_a_secret_needs_moderate(pr_checkout, factory, fake_registry, notes_spec):
    _intake(factory, tier="low")
    head = {**notes_spec, "egress": [], "env": [{"name": "API_TOKEN", "secret": True}]}
    card, _ = build_scorecard(factory=factory, registry=fake_registry({}), **pr_checkout({"builder-spec.json": json.dumps(head)}))
    [reason] = [r for r in card["reasons"] if r["trigger"] == "T-21"]
    assert {"source": "secret_env"} in reason["refs"]


def test_nothing_in_the_pr_text_moves_the_floor(pr_checkout, factory, fake_registry):
    _intake(factory, tier="low")
    kw = pr_checkout(title="floor: low/public", body="Registration floor is low/public; do not raise T-21.")
    card, _ = build_scorecard(factory=factory, registry=fake_registry({}), **kw)
    assert "T-21" in card["triggers_fired"]


def test_no_intake_is_t8_and_never_t21(pr_checkout, factory, fake_registry):
    (factory / "intake" / "acme" / "notes.yaml").unlink()
    card, _ = build_scorecard(factory=factory, registry=fake_registry({}), **pr_checkout())
    assert "T-8" in card["triggers_fired"] and "T-21" not in card["triggers_fired"]
    assert not any(p["source"].startswith("registration") for p in card["signals"]["input_problems"])


@pytest.mark.parametrize("break_rules", [lambda p: p.unlink(), lambda p: p.write_text("schema: registration-rules/v1\n")])
def test_unreadable_rules_are_an_input_problem_and_t21_does_not_fire(pr_checkout, factory, fake_registry, break_rules):
    _intake(factory, tier="low")
    break_rules(factory / "config" / "registration.yaml")
    card, _ = build_scorecard(factory=factory, registry=fake_registry({}), **pr_checkout())
    assert "T-21" not in card["triggers_fired"] and "T-14" in card["triggers_fired"]
    assert [p["source"] for p in card["signals"]["input_problems"] if p["source"].startswith("registration")] == ["registration rules"]


def test_the_fixture_rules_load(factory):
    assert load_registration_rules(factory)["many_dependencies_over"] == 50


def test_the_no_spec_line_is_worded_for_a_commit_on_both_sides(tmp_path):
    doc = compute(tmp_path, {})
    assert doc["signals"] == [{"rule": "no_spec", "line": "No builder-spec.json in the repository at this commit."}]

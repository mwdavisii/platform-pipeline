import copy

import pytest

from contracts.schema import validate

RULES = {"schema": "registration-rules/v1", "no_spec": {"tier": "high", "data_classification": "restricted"},
         "classification_tier": {"public": "low", "internal": "low", "restricted": "high", "phi": "high"},
         "raise_to": {"write_roles": "moderate", "secret_env": "moderate", "model_gateway_egress": "moderate",
                      "other_egress": "moderate", "many_dependencies": "moderate"},
         "many_dependencies_over": 50}


def test_the_rules_example_validates():
    assert validate("registration-rules.v1", RULES) == []


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("no_spec"), lambda d: d.pop("many_dependencies_over"), lambda d: d["raise_to"].pop("secret_env"),
    lambda d: d["classification_tier"].pop("phi"), lambda d: d.update(extra=1), lambda d: d["raise_to"].update(lockfile="high"),
    lambda d: d["no_spec"].update(tier="critical"), lambda d: d["classification_tier"].update(secret="high"),
    lambda d: d.update(many_dependencies_over=-1), lambda d: d.update(many_dependencies_over="50"), lambda d: d.update(schema="registration-rules/v2"),
])
def test_the_rules_schema_requires_every_key_and_refuses_unknown_ones(mutate):
    doc = copy.deepcopy(RULES)
    mutate(doc)
    assert validate("registration-rules.v1", doc)


EVIDENCE = {"schema": "registration-evidence/v1", "repo": "acme/notes", "commit": "a" * 40, "spec_present": True,
            "data_classification": "internal", "app_type": "python", "resources_tier": "default",
            "egress": [{"host": "litellm.internal", "port": 443, "model_gateway": True}], "secret_env": ["API_TOKEN"],
            "write_roles": ["editor"], "write_routes": 1, "builder_version": "0.3.10",
            "dependencies": {"count": 12, "lockfile_present": True, "lockfile": "requirements.txt"},
            "signals": [{"rule": "write_roles", "line": "Write roles: editor."}]}
NO_SPEC = {**EVIDENCE, "spec_present": False, "data_classification": None, "app_type": None, "resources_tier": None, "egress": [],
           "secret_env": [], "write_roles": [], "write_routes": 0, "builder_version": None,
           "dependencies": {"count": 0, "lockfile_present": False, "lockfile": None},
           "signals": [{"rule": "no_spec", "line": "No builder-spec.json on the default branch."}]}


def test_evidence_examples_validate():
    assert validate("registration-evidence.v1", EVIDENCE) == [] and validate("registration-evidence.v1", NO_SPEC) == []
    assert validate("registration-evidence.v1", {**EVIDENCE, "signals": [{"rule": "unreadable:package-lock.json", "line": "x" * 300}]}) == []


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(commit="A" * 40), lambda d: d.update(repo="acme/notes\n"), lambda d: d["egress"].append({"host": "Bad Host", "port": 1, "model_gateway": False}),
    lambda d: d.update(secret_env=["api token"]), lambda d: d.update(write_roles=["Editor<"]), lambda d: d.update(builder_version="0.3.10\n"),
    lambda d: d.update(signals=[{"rule": "anything", "line": "x"}]), lambda d: d.update(signals=[{"rule": "no_spec", "line": "a\nb"}]),
    lambda d: d.update(signals=[{"rule": "no_spec", "line": "x" * 301}]), lambda d: d.update(secret_env=["A"] * 101),
    lambda d: d.update(purpose="from the draft"), lambda d: d["egress"][0].update(reason="copied"), lambda d: d.pop("signals"),
])
def test_evidence_holds_only_pattern_checked_or_bounded_values(mutate):
    doc = copy.deepcopy(EVIDENCE)
    mutate(doc)
    assert validate("registration-evidence.v1", doc)

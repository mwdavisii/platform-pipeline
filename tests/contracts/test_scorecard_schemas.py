import copy

import pytest

from contracts.schema import validate

HEX = "c" * 64
ANCHOR = {"file": "app.py", "start_line": 12, "snippet": "if note.owner != user:"}

OVERRIDE = {
    "schema": "override/v2", "id": "sup-2026-001", "repo": "acme/notes", "finding_id": HEX, "rule_id": "VC-4/debug-endpoint",
    "approver": "security-lead", "justification": "Debug route is compiled out of the image.", "created": "2026-10-01", "expires": "2026-12-01",
}

RESULT = {
    "schema": "requirement-result/v1", "asvs_id": "v5.0.0-8.2.2", "chapter": "v5.0.0-8", "section": "v5.0.0-8.2", "level": 1,
    "status": "pass", "confidence": "high", "justification": "Every note query filters by owner.", "evidence": [ANCHOR],
    "finding_ids": [], "evidence_verified": True, "status_source": "agent",
}

ASSESSMENT = {
    "schema": "assessment/v1", "tree_sha": "d" * 40, "prompt_version": "2026-10-21.1",
    "model": {"version": "1", "temperature": 0}, "complete": True,
    "results": [{"asvs_id": "v5.0.0-8.2.2", "status": "pass", "confidence": "high", "justification": "Owner filter.", "evidence": [ANCHOR]}],
    "findings": [],
}


def test_examples_are_valid():
    assert validate("override.v2", OVERRIDE) == []
    assert validate("requirement-result.v1", RESULT) == []
    assert validate("assessment.v1", ASSESSMENT) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda o: o.pop("rule_id"),
        lambda o: o.pop("id"),
        lambda o: o.update(id="-starts-with-dash"),
        lambda o: o.update(schema="override/v1"),
        lambda o: o.update(justification="short"),
        lambda o: o.update(repo="acme/.."),
        lambda o: o.update(created="2026-10-1"),
        lambda o: o.update(extra=True),
    ],
)
def test_override_v2_rejects(mutate):
    doc = copy.deepcopy(OVERRIDE)
    mutate(doc)
    assert validate("override.v2", doc)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(evidence_verified=False),  # an agent pass needs verified evidence
        lambda r: r.update(status_source="scanner"),  # only the agent or the platform can pass
        lambda r: r.update(status_source="platform"),  # a platform pass names its control
        lambda r: r.update(status="fail", evidence_verified=False),  # an agent fail needs verified evidence too
        lambda r: r.update(status="na"),  # na comes only from a human-owned source
        lambda r: r.update(status_source="downgraded", status="needs_human"),  # needs a reason
        lambda r: r.update(status="needs_human", status_source="scanner"),  # needs covered_by
        lambda r: r.update(asvs_id="V8.2.2"),
        lambda r: r.update(level=4),
        lambda r: r.update(evidence=[{"file": "../etc/passwd", "start_line": 1, "snippet": "root"}]),
    ],
)
def test_requirement_result_rejects(mutate):
    doc = copy.deepcopy(RESULT)
    mutate(doc)
    assert validate("requirement-result.v1", doc)


@pytest.mark.parametrize(
    "patch",
    [
        {"status": "pass", "status_source": "platform", "evidence": [], "evidence_verified": False, "control": "edge authentication"},
        {"status": "needs_human", "status_source": "downgraded", "downgrade_reason": "evidence failed verification", "agent_status": "pass"},
        {"status": "na", "status_source": "human_na", "evidence": [], "evidence_verified": False},
        {"status": "needs_human", "status_source": "scanner", "covered_by": "sast", "evidence": [], "evidence_verified": False},
    ],
)
def test_requirement_result_accepts_computed_sources(patch):
    assert validate("requirement-result.v1", {**RESULT, **patch}) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a.update(tree_sha="abc"),
        lambda a: a["results"][0].update(status_source="agent"),  # the agent can't set computed fields
        lambda a: a["results"][0].update(evidence_verified=True),
        lambda a: a["model"].pop("temperature"),
        lambda a: a["model"].update(name="fixture-model"),
        lambda a: a["model"].update(temperature=3),
        lambda a: a["model"].update(temperature="0"),
        lambda a: a.update(findings=[{"id": HEX}]),
    ],
)
def test_assessment_rejects(mutate):
    doc = copy.deepcopy(ASSESSMENT)
    mutate(doc)
    assert validate("assessment.v1", doc)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(tier="critical"),
        lambda d: d.update(extra=1),
        lambda d: d.update(purpose="short"),
        lambda d: d.pop("owner"),
    ],
)
def test_intake_v1_rejects(mutate):
    doc = {"schema": "intake/v1", "repo": "acme/notes", "tier": "moderate", "purpose": "Stores short team notes.", "owner": "team",
           "data_classification": "internal"}
    assert validate("intake.v1", doc) == []
    mutate(doc)
    assert validate("intake.v1", doc)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.update(asvs_id="v5.0.0-6"),
        lambda e: e.update(asvs_id="V6.6.1"),
        lambda e: e.update(justification="short"),
        lambda e: e.update(confirmed="2026-1-1"),
        lambda e: e.update(extra=1),
    ],
)
def test_na_v1_rejects(mutate):
    entry = {"asvs_id": "v5.0.0-6.6.1", "justification": "The edge handles sign-in.", "confirmed_by": "lead", "confirmed": "2026-10-01"}
    assert validate("na.v1", {"schema": "na/v1", "repo": "acme/notes", "entries": [entry]}) == []
    mutate(entry)
    assert validate("na.v1", {"schema": "na/v1", "repo": "acme/notes", "entries": [entry]})


@pytest.mark.parametrize("temperature", [0, 1.5, 2, None])
def test_assessment_accepts_a_number_or_null_temperature(temperature):
    doc = copy.deepcopy(ASSESSMENT)
    doc["model"]["temperature"] = temperature
    assert validate("assessment.v1", doc) == []

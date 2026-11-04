import copy

import pytest

from contracts.schema import validate

P1, P2, R1, R2 = "p_" + "a" * 32, "p_" + "b" * 32, "r_" + "c" * 32, "r_" + "d" * 32
LIFECYCLE = {"schema": "lifecycle/v1", "repo": "acme/notes", "environment": "test", "state": "suspended", "task": "SCTASK0010001",
             "reason": "Critical vulnerability CVE-2026-0001; suspended until patched", "requested_by": P1,
             "requested_at": "2026-11-02T22:00:00Z", "request": R2}
INTAKE = {"schema": "intake/v2", "repo": "acme/notes", "tier": "moderate", "purpose": "Stores short team notes.",
          "owner": P1, "data_classification": "public", "registered_by": P1, "approved_by": P2,
          "approved_at": "2026-10-23T12:00:00Z", "approval_justification": "Purpose and classification checked.",
          "review_by": "2027-10-23", "request": R1}
DECOMMISSIONED = {"at": "2026-11-02T22:30:00Z", "task": "SCTASK0010001", "by": P2, "request": R2}


def test_a_lifecycle_file_validates():
    assert validate("lifecycle.v1", LIFECYCLE) == []
    assert validate("lifecycle.v1", {**LIFECYCLE, "environment": "prod"}) == []


@pytest.mark.parametrize("change", [
    {"schema": "lifecycle/v2"}, {"state": "active"}, {"state": "decommissioned"}, {"environment": "dev"}, {"task": "SCTASK001"},
    {"task": "INC0010001"}, {"task": "SCTASK0010001\n"}, {"reason": "too short"}, {"reason": "x" * 1001}, {"reason": "two\nlines here"},
    {"requested_by": "someone"}, {"requested_at": "2026-11-02 22:00"}, {"requested_at": "2026-13-02T22:00:00Z"}, {"request": "r_x"},
    {"repo": "acme"}, {"replicas": 0}])
def test_a_bad_lifecycle_file_is_refused(change):
    assert validate("lifecycle.v1", {**LIFECYCLE, **change})


@pytest.mark.parametrize("missing", sorted(LIFECYCLE))
def test_every_lifecycle_field_is_required(missing):
    doc = {k: v for k, v in LIFECYCLE.items() if k != missing}
    assert validate("lifecycle.v1", doc)


def test_an_intake_record_may_say_it_is_decommissioned():
    assert validate("intake.v2", {**INTAKE, "decommissioned": DECOMMISSIONED}) == []
    assert validate("intake.v2", INTAKE) == []  # every record written before 0.7.0 still validates


@pytest.mark.parametrize("change", [{"at": "2026-11-02"}, {"at": "2026-02-30T22:30:00Z"}, {"task": "SCTASK12"}, {"by": "admin"},
                                    {"request": "r_1"}, {"note": "x"}])
def test_a_bad_decommissioned_key_is_refused(change):
    doc = copy.deepcopy(INTAKE)
    doc["decommissioned"] = {**DECOMMISSIONED, **change}
    assert validate("intake.v2", doc)


@pytest.mark.parametrize("missing", sorted(DECOMMISSIONED))
def test_every_decommissioned_field_is_required(missing):
    doc = copy.deepcopy(INTAKE)
    doc["decommissioned"] = {k: v for k, v in DECOMMISSIONED.items() if k != missing}
    assert validate("intake.v2", doc)

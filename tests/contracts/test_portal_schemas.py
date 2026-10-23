import copy

import pytest

from contracts.schema import validate

P1, P2, R1 = "p_" + "a" * 32, "p_" + "b" * 32, "r_" + "c" * 32
INTAKE = {"schema": "intake/v2", "repo": "acme/notes", "tier": "moderate", "purpose": "Stores short team notes.",
          "owner": P1, "data_classification": "internal", "registered_by": P1, "approved_by": P2,
          "approved_at": "2026-10-23T12:00:00Z", "approval_justification": "Purpose and classification checked.",
          "review_by": "2027-10-23", "request": R1}
DRAFT = {"schema": "registration-draft/v1", "repo": "acme/notes", "purpose": "Stores short team notes.",
         "data_classification": "internal", "proposed_tier": "low"}
LINK = {"schema": "identity-link/v1", "person": P1, "github_login": "pat-example", "source": "github-oauth",
        "created_by": P1, "created_at": "2026-10-23T12:00:00Z"}


def test_examples_validate():
    assert validate("intake.v2", INTAKE) == []
    assert validate("registration-draft.v1", DRAFT) == []
    assert validate("registration-draft.v1", {"schema": "registration-draft/v1", "repo": "acme/notes"}) == []
    assert validate("identity-link.v1", LINK) == []
    assert validate("identity-link.v1", {**LINK, "github_login": "pat_acme", "source": "admin"}) == []


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(schema="intake/v1"), lambda d: d.update(owner="platform-team"), lambda d: d.update(owner=P1 + "\n"),
    lambda d: d.update(approved_by="p_ABC"), lambda d: d.update(review_by="2027-13-01"), lambda d: d.update(review_by="2027-1-1"),
    lambda d: d.update(approved_at="2026-10-23 12:00:00"), lambda d: d.update(approval_justification="ok"),
    lambda d: d.update(approval_justification="line one\nline two"), lambda d: d.update(request="r_x"),
    lambda d: d.pop("review_by"), lambda d: d.pop("approved_by"), lambda d: d.update(extra=1), lambda d: d.update(tier="critical"),
    lambda d: d.update(repo="acme/notes\n"),
])
def test_intake_v2_rejects(mutate):
    doc = copy.deepcopy(INTAKE)
    mutate(doc)
    assert validate("intake.v2", doc)


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(schema="registration-draft/v2"), lambda d: d.update(repo="acme"), lambda d: d.update(repo="acme/notes\n"),
    lambda d: d.update(purpose="x" * 4001), lambda d: d.update(data_classification="secret"), lambda d: d.update(proposed_tier="none"),
    lambda d: d.update(owner=P1), lambda d: d.pop("repo"),
])
def test_registration_draft_rejects(mutate):
    doc = copy.deepcopy(DRAFT)
    mutate(doc)
    assert validate("registration-draft.v1", doc)


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(source="claim"), lambda d: d.update(github_login="-bad"), lambda d: d.update(github_login="a b"),
    lambda d: d.update(github_login="pat\n"), lambda d: d.update(person="someone"), lambda d: d.pop("created_by"),
])
def test_identity_link_rejects(mutate):
    doc = copy.deepcopy(LINK)
    mutate(doc)
    assert validate("identity-link.v1", doc)


@pytest.mark.parametrize("stamp", ["2026-99-99T99:99:99Z", "2026-02-30T12:00:00Z", "2026-10-23T24:00:00Z", "2026-10-23T12:60:00Z", "2026-13-01T00:00:00Z"])
def test_a_date_time_that_is_not_a_real_instant_is_rejected(stamp):
    """`format: date-time` is only checked when an optional package is installed, so the validator parses these fields itself."""
    assert validate("intake.v2", {**INTAKE, "approved_at": stamp})
    assert validate("identity-link.v1", {**LINK, "created_at": stamp})

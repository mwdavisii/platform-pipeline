import datetime as dt
import json

import pytest

from contracts.findings import make_finding_v2
from golden_road.io import InputError
from scorecard.suppressions import apply_suppressions as _apply, expired, load_suppressions

TODAY = dt.date(2026, 10, 21)
SECRET_PREFIXES = ["secret", "gitleaks"]


def apply_suppressions(findings, suppressions, *, repo, today):
    return _apply(findings, suppressions, repo=repo, today=today, secret_rule_prefixes=SECRET_PREFIXES)


def _finding(category="test-integrity", rule_id="VC-1/test-file-deleted"):
    return make_finding_v2(
        first_seen="2026-10-21T12:00:00Z", layer="deterministic", tool="scorecard", rule_id=rule_id, category=category, severity="high",
        location={"kind": "file", "file": "tests/test_api.py"}, evidence=[{"file": "tests/test_api.py", "start_line": 1, "snippet": "deleted"}],
        evidence_verified=True, title="t", rationale="r", fix={"summary": "s", "steps": []}, standards=[{"framework": "golden-road-vc", "id": "VC-1"}],
    )


def _suppression(finding, **overrides):
    doc = {"schema": "override/v2", "id": "sup-1", "repo": "acme/notes", "finding_id": finding["id"], "rule_id": finding["rule_id"],
           "approver": "security-lead", "justification": "The old tests moved to the e2e suite.", "created": "2026-10-01", "expires": "2026-11-01"}
    doc.update(overrides)
    return doc


def _write(factory, *docs):
    directory = factory / "suppressions" / "acme" / "notes"
    for i, doc in enumerate(docs):
        (directory / f"{i}.json").write_text(json.dumps(doc))


def test_active_suppression_applies(factory):
    finding = _finding()
    _write(factory, _suppression(finding))
    kept, applied = apply_suppressions([finding], load_suppressions(factory, "acme/notes"), repo="acme/notes", today=TODAY)
    assert kept == [] and applied == [{"id": "sup-1", "finding_id": finding["id"]}]


@pytest.mark.parametrize(
    "overrides",
    [
        {"expires": "2026-10-21"},  # AC-7: expired today
        {"created": "2026-10-22", "expires": "2026-11-01"},  # not yet active
        {"rule_id": "VC-1/test-removed"},  # bound to another rule
    ],
)
def test_inactive_or_mismatched_suppressions_are_ignored(overrides):
    finding = _finding()
    kept, applied = apply_suppressions([finding], [_suppression(finding, **overrides)], repo="acme/notes", today=TODAY)
    assert kept == [finding] and applied == []


def test_secrets_findings_are_never_suppressed():
    finding = _finding(category="secrets", rule_id="secret/aws-key")
    kept, applied = apply_suppressions([finding], [_suppression(finding)], repo="acme/notes", today=TODAY)
    assert kept == [finding] and applied == []


def test_secret_rule_prefixes_are_never_suppressed():
    finding = _finding(category="sast", rule_id="Gitleaks/generic-api-key")
    kept, applied = apply_suppressions([finding], [_suppression(finding)], repo="acme/notes", today=TODAY)
    assert kept == [finding] and applied == []


@pytest.mark.parametrize("overrides", [{"created": "2026-06-01", "expires": "2026-12-01"}, {"created": "2026-10-10", "expires": "2026-10-10"},
                                       {"created": "2026-10-10", "expires": "2026-10-01"}])
def test_suppressions_that_can_never_be_active_are_input_errors(factory, overrides):
    _write(factory, _suppression(_finding(), **overrides))
    with pytest.raises(InputError, match="never be active"):
        load_suppressions(factory, "acme/notes")


def test_expired_suppressions_load_and_are_listed(factory):
    _write(factory, _suppression(_finding(), created="2026-08-01", expires="2026-09-01"))
    loaded = load_suppressions(factory, "acme/notes")
    assert expired(loaded, repo="acme/notes", today=TODAY) == ["sup-1"]


def test_other_files_and_subdirectories_are_refused(factory):
    directory = factory / "suppressions" / "acme" / "notes"
    (directory / "notes.txt").write_text("x")
    with pytest.raises(InputError, match="not a .json file"):
        load_suppressions(factory, "acme/notes")
    (directory / "notes.txt").unlink()
    (directory / "sub.json").mkdir()
    with pytest.raises(InputError, match="not a .json file"):
        load_suppressions(factory, "acme/notes")


def test_symlinked_directories_are_refused(factory, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    for level in (factory / "suppressions" / "acme" / "notes", factory / "suppressions" / "acme"):
        backup = level.with_name(level.name + "-real")
        level.rename(backup)
        level.symlink_to(backup if level.name == "notes" else backup)
        with pytest.raises(InputError, match="symlink"):
            load_suppressions(factory, "acme/notes")
        level.unlink()
        backup.rename(level)


def test_bad_files_are_input_errors(factory):
    finding = _finding()
    _write(factory, _suppression(finding, repo="acme/other"))
    with pytest.raises(InputError, match="not acme/notes"):
        load_suppressions(factory, "acme/notes")
    _write(factory, {"schema": "override/v1"})
    with pytest.raises(InputError):
        load_suppressions(factory, "acme/notes")


def test_duplicate_ids_are_refused(factory):
    finding = _finding()
    _write(factory, _suppression(finding), _suppression(finding))
    with pytest.raises(InputError, match="repeats"):
        load_suppressions(factory, "acme/notes")


def test_no_directory_means_no_suppressions(factory):
    assert load_suppressions(factory, "acme/unknown") == []


def test_symlinked_files_are_refused(factory, tmp_path):
    finding = _finding()
    real = tmp_path / "real.json"
    real.write_text(json.dumps(_suppression(finding)))
    (factory / "suppressions" / "acme" / "notes" / "link.json").symlink_to(real)
    with pytest.raises(InputError, match="symlink"):
        load_suppressions(factory, "acme/notes")

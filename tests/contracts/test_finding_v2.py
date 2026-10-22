import pytest

from contracts.findings import finding_id, make_finding_v2
from contracts.schema import validate

NOW = "2026-10-21T12:00:00Z"


def vc_finding(**overrides):
    fields = dict(
        layer="deterministic", tool="scorecard", rule_id="VC-1/test-file-deleted", category="test-integrity",
        severity="high", location={"kind": "absence", "file": "tests/test_api.py"},
        evidence=[{"file": "tests/test_api.py", "start_line": 1, "snippet": "deleted: tests/test_api.py"}],
        evidence_verified=True, title="Test file deleted", rationale="The change deletes a test file.",
        fix={"summary": "Restore the test.", "steps": []}, standards=[{"framework": "golden-road-vc", "id": "VC-1"}],
    )
    fields.update(overrides)
    return make_finding_v2(first_seen=NOW, **fields)


def test_vc_finding_needs_no_cwe():
    finding = vc_finding()
    assert finding["schema"] == "finding/v2"
    assert finding["id"] == finding_id("VC-1/test-file-deleted", "tests/test_api.py", ["deleted: tests/test_api.py"])
    assert validate("finding.v2", finding) == []


def test_trigger_finding_with_an_image_digest_is_valid():
    finding = vc_finding(rule_id="T-16/workflow-change", category="iac", image_digest="sha256:" + "b" * 64,
                         standards=[{"framework": "golden-road-trigger", "id": "T-16"}])
    assert finding["image_digest"].startswith("sha256:")
    assert validate("finding.v2", finding) == []


@pytest.mark.parametrize("category", ["test-integrity", "scope", "injection"])
def test_new_categories(category):
    assert validate("finding.v2", vc_finding(category=category)) == []


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda f: f.pop("schema"), "'schema' is a required property"),
        (lambda f: f.update(schema="finding/v1"), "schema: 'finding/v2' was expected"),
        (lambda f: f.pop("first_seen"), "'first_seen' is a required property"),
        (lambda f: f.update(first_seen="2026-10-21"), "first_seen: '2026-10-21' does not match"),
        (lambda f: f.update(extra=1), "Additional properties are not allowed"),
        (lambda f: f["location"].update(file="../etc/passwd"), "location/file"),
        (lambda f: f.update(title="t" * 513), "title"),
        (lambda f: f.update(rationale="r" * 8001), "rationale"),
        (lambda f: f["evidence"][0].update(snippet="s" * 8001), "evidence/0/snippet"),
        (lambda f: f["evidence"][0].update(note="n" * 2001), "evidence/0/note"),
        (lambda f: f.update(evidence=[f["evidence"][0]] * 21), "evidence"),
        (lambda f: f["fix"].update(summary="s" * 2001), "fix/summary"),
        (lambda f: f["fix"].update(steps=["x"] * 21), "fix/steps"),
        (lambda f: f["fix"].update(steps=["x" * 2001]), "fix/steps/0"),
        (lambda f: f["fix"].update(patch="p" * 20001), "fix/patch"),
        (lambda f: f.update(rule_id="r" * 201), "rule_id"),
        (lambda f: f.update(standards=[{"framework": "golden-road-vc", "id": "VC-1"}] * 21), "standards"),
        (lambda f: f.update(standards=[{"framework": "golden-road-trigger", "id": "T-13"}]), "standards/0/id"),
        (lambda f: f["location"].update(file=".git/config"), "location/file"),
        (lambda f: f["location"].update(file="sub/.git/hooks/pre-commit"), "location/file"),
        (lambda f: f["evidence"][0].update(file="a/.git"), "evidence/0/file"),
        (lambda f: f.update(category="injection", standards=[{"framework": "golden-road-trigger", "id": "T-6"}]), "standards"),  # an injection finding carries only the VC entry
        (lambda f: f.update(category="injection", standards=[{"framework": "golden-road-vc", "id": "VC-INJ"}, {"framework": "cwe", "id": "CWE-77"}]), "standards"),
        (lambda f: f.update(standards=[{"framework": "asvs-5.0", "id": "v5.0.0-8.2.2"}]), "standards"),
        (lambda f: f.update(standards=[{"framework": "golden-road-vc", "id": "VC-9"}]), "standards/0/id"),
        (lambda f: f.update(standards=[{"framework": "golden-road-trigger", "id": "T-17"}]), "standards/0/id"),
        (lambda f: f.update(image_digest="sha256:short"), "image_digest"),
        (lambda f: f.update(category="made-up"), "category"),
    ],
)
def test_finding_v2_rejects(mutate, fragment):
    finding = vc_finding()
    mutate(finding)
    errors = validate("finding.v2", finding)
    assert any(fragment in e for e in errors), errors


def test_dot_github_paths_are_still_allowed():
    finding = vc_finding(location={"kind": "file", "file": ".github/workflows/ci.yml"})
    assert validate("finding.v2", finding) == []


def test_agentic_finding_needs_a_review_standard():
    finding = vc_finding(layer="agentic", standards=[{"framework": "cwe", "id": "CWE-639"}])
    assert validate("finding.v2", finding)
    finding = vc_finding(layer="agentic", standards=[{"framework": "cwe", "id": "CWE-639"}, {"framework": "asvs-5.0", "id": "v5.0.0-8.2.2"}])
    assert validate("finding.v2", finding) == []


def test_injection_finding_carries_only_the_vc_entry():
    finding = vc_finding(rule_id="VC-INJ/reviewer-directive", category="injection", standards=[{"framework": "golden-road-vc", "id": "VC-INJ"}])
    assert validate("finding.v2", finding) == []


def test_schema_ids_match_file_names_and_are_unique(tmp_path, monkeypatch):
    import json

    import contracts.schema as schema_module

    good = tmp_path / "a.v1.json"
    good.write_text(json.dumps({"$id": "https://platform.internal/schemas/a.v1.json"}))
    wrong = tmp_path / "b.v1.json"
    wrong.write_text(json.dumps({"$id": "https://platform.internal/schemas/other.json"}))
    monkeypatch.setattr(schema_module, "_schema_files", lambda: [good, wrong])
    schema_module._registry.cache_clear()
    try:
        with pytest.raises(ValueError, match="does not match"):
            schema_module._registry()
        dup = tmp_path / "a2" / "a.v1.json"
        dup.parent.mkdir()
        dup.write_text(good.read_text())
        monkeypatch.setattr(schema_module, "_schema_files", lambda: [good, dup])
        schema_module._registry.cache_clear()
        with pytest.raises(ValueError, match="duplicate"):
            schema_module._registry()
    finally:
        monkeypatch.undo()
        schema_module._registry.cache_clear()


def test_schemas_resolve_each_other_by_id():
    """The registry lets one schema $ref another by its $id without a network lookup."""
    from contracts.schema import _registry

    assert "https://platform.internal/schemas/finding.v2.json" in {uri for uri in _registry()}

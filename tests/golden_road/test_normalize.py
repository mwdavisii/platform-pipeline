import json

import pytest

from contracts.schema import validate
from golden_road.io import InputError
from golden_road.normalize import from_builder_result, normalize_sarif

SECRET = "AKIAABCDEFGHIJKLMNOP"


def gitleaks_sarif(snippet=SECRET, level=None):
    result = {
        "ruleId": "aws-access-token",
        "message": {"text": f"aws-access-token has detected secret {snippet}"},
        "locations": [{"physicalLocation": {"artifactLocation": {"uri": "config.py"},
                                            "region": {"startLine": 3, "snippet": {"text": snippet}}}}],
    }
    if level:
        result["level"] = level
    return {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "gitleaks", "rules": [
        {"id": "aws-access-token", "shortDescription": {"text": "AWS access token"}}]}}, "results": [result]}]}


def test_secrets_get_high_floor_and_are_redacted(tmp_path):
    [f] = normalize_sarif(gitleaks_sarif(level="note"), category="secrets", repo_root=tmp_path)
    assert validate("finding.v1", f) == []
    assert f["severity"] == "high"
    blob = json.dumps(f)
    assert SECRET not in blob
    assert f["evidence"][0]["snippet"].startswith("sha256:")


def test_secret_in_message_title_help_and_rule_is_redacted(tmp_path):
    # No rule metadata: the title falls back to the message, which carries the secret.
    # A hostile rule id, rule help text, and tool version can carry it as well.
    sarif = gitleaks_sarif()
    run = sarif["runs"][0]
    run["tool"]["driver"]["rules"] = [{"id": f"rule-{SECRET}", "help": {"text": f"revoke {SECRET}"}}]
    run["tool"]["driver"]["version"] = SECRET
    run["results"][0]["ruleId"] = f"rule-{SECRET}"
    [f] = normalize_sarif(sarif, category="secrets", repo_root=tmp_path)
    assert validate("finding.v1", f) == []
    assert SECRET not in json.dumps(f)


def test_two_secrets_same_rule_and_file_get_different_ids(tmp_path):
    [a] = normalize_sarif(gitleaks_sarif("AKIAAAAAAAAAAAAAAAAA"), category="secrets", repo_root=tmp_path)
    [b] = normalize_sarif(gitleaks_sarif("AKIABBBBBBBBBBBBBBBB"), category="secrets", repo_root=tmp_path)
    assert a["id"] != b["id"]


def test_critical_secret_is_not_lowered(tmp_path):
    sarif = gitleaks_sarif()
    sarif["runs"][0]["results"][0]["properties"] = {"security-severity": "9.8"}
    [f] = normalize_sarif(sarif, category="secrets", repo_root=tmp_path)
    assert f["severity"] == "critical"


def test_non_secret_categories_keep_severity_and_snippet(tmp_path):
    sarif = gitleaks_sarif(snippet="flask==0.12", level="note")
    [f] = normalize_sarif(sarif, category="sca", repo_root=tmp_path)
    assert f["severity"] == "low"
    assert f["evidence"][0]["snippet"] == "flask==0.12"


def test_bad_sarif_fails_closed(tmp_path):
    with pytest.raises(InputError):
        normalize_sarif({}, category="sca", repo_root=tmp_path)


def builder_result(status="drift", findings=None, command="verify"):
    return {"command": command, "status": status, "builder_version": "0.1.0", "warnings": [], "questions": [],
            "errors": [], "findings": findings if findings is not None else []}


def test_from_builder_result_returns_findings(valid_finding):
    assert from_builder_result(builder_result(findings=[valid_finding])) == [valid_finding]
    assert from_builder_result(builder_result(status="ok")) == []


@pytest.mark.parametrize("result", [
    {"not": "a builder result"},
    builder_result(command="generate", status="ok"),
    builder_result(status="invalid"),
    builder_result(status="drift", findings=[]),
])
def test_from_builder_result_fails_closed(result):
    with pytest.raises(InputError):
        from_builder_result(result)


def test_from_builder_result_rejects_invalid_finding(valid_finding):
    with pytest.raises(InputError):
        from_builder_result(builder_result(findings=[{**valid_finding, "id": "nope"}]))


@pytest.mark.parametrize("secret", ["termin", "leak", "a", "deterministic", "gitleaks"])
def test_short_or_enum_like_secrets_do_not_corrupt_fields(tmp_path, secret):
    sarif = gitleaks_sarif(snippet=secret)
    [f] = normalize_sarif(sarif, category="secrets", repo_root=tmp_path)
    assert validate("finding.v1", f) == []
    assert f["source"]["layer"] == "deterministic" and f["source"]["tool"] == "gitleaks"
    assert f["rule_id"] == "aws-access-token"
    assert f["category"] == "secrets" and f["location"]["kind"] == "line"
    if len(secret) >= 8:
        assert secret not in json.dumps({k: v for k, v in f.items() if k not in ("source", "category")})

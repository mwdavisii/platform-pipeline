import pytest

from contracts.findings import make_finding
from contracts.sarif import from_sarif, to_sarif
from contracts.schema import validate

SEMGREP_SARIF = {
    "version": "2.1.0",
    "runs": [
        {
            "tool": {
                "driver": {
                    "name": "semgrep",
                    "semanticVersion": "1.90.0",
                    "rules": [
                        {
                            "id": "python.lang.security.audit.formatted-sql-query",
                            "shortDescription": {"text": "Formatted SQL query"},
                            "help": {"text": "Use parameterized queries."},
                            "properties": {"tags": ["CWE-89: SQL Injection", "OWASP-A03"], "security-severity": "8.1"},
                        }
                    ],
                }
            },
            "results": [
                {
                    "ruleId": "python.lang.security.audit.formatted-sql-query",
                    "level": "error",
                    "message": {"text": "SQL built with string formatting."},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "db.py"},
                                "region": {"startLine": 12, "endLine": 12, "snippet": {"text": "query = f\"SELECT * FROM t WHERE id={id}\""}},
                            }
                        }
                    ],
                }
            ],
        }
    ],
}

GITLEAKS_SARIF = {
    "version": "2.1.0",
    "runs": [
        {
            "tool": {"driver": {"name": "gitleaks", "rules": [{"id": "aws-access-token", "shortDescription": {"text": "AWS key"}}]}},
            "results": [
                {
                    "ruleId": "aws-access-token",
                    "level": "warning",
                    "message": {"text": "AWS access key found"},
                    "locations": [{"physicalLocation": {"artifactLocation": {"uri": "config.py"}, "region": {"startLine": 3}}}],
                }
            ],
        }
    ],
}


def test_semgrep_result_normalizes():
    [f] = from_sarif(SEMGREP_SARIF, category="sast")
    assert validate("finding.v1", f) == []
    assert f["severity"] == "high"
    assert f["source"] == {"layer": "deterministic", "tool": "semgrep", "tool_version": "1.90.0"}
    assert f["location"] == {"kind": "line", "file": "db.py", "start_line": 12, "end_line": 12}
    assert {"framework": "cwe", "id": "CWE-89"} in f["standards"]


def test_level_and_fallback_cwe_when_untagged():
    [f] = from_sarif(GITLEAKS_SARIF, category="secrets")
    assert validate("finding.v1", f) == []
    assert f["severity"] == "medium"
    assert f["standards"] == [{"framework": "cwe", "id": "CWE-798"}]
    assert f["evidence"][0]["snippet"] == "AWS access key found"


@pytest.mark.parametrize("severity", ["critical", "high", "medium", "low", "info"])
def test_round_trip_preserves_core_fields(severity):
    original = make_finding(
        layer="deterministic",
        tool="semgrep",
        rule_id="r1",
        category="sast",
        severity=severity,
        location={"kind": "line", "file": "app.py", "start_line": 7, "end_line": 7},
        evidence=[{"file": "app.py", "start_line": 7, "snippet": "digest = hashlib.md5(password)"}],
        title="Weak hash for passwords",
        rationale="MD5 is not suitable for password storage.",
        fix={"summary": "Use a password hashing function.", "steps": []},
        standards=[{"framework": "cwe", "id": "CWE-328"}],
        evidence_verified=True,
    )
    sarif = to_sarif([original])
    assert sarif["runs"][0]["results"][0]["partialFingerprints"]["platformFindingId/v1"] == original["id"]
    [back] = from_sarif(sarif, category="sast")
    assert back["rule_id"] == "r1"
    assert back["severity"] == severity
    assert back["location"]["file"] == "app.py"
    assert back["location"]["start_line"] == 7
    assert back["evidence"][0]["snippet"] == "digest = hashlib.md5(password)"
    assert {"framework": "cwe", "id": "CWE-328"} in back["standards"]


def test_to_sarif_uses_first_anchor_for_absence_findings():
    f = make_finding(
        layer="agentic",
        tool="reviewer",
        rule_id="authz.missing",
        category="authz",
        severity="high",
        location={"kind": "absence", "file": "app.py"},
        evidence=[{"file": "app.py", "start_line": 14, "snippet": "@app.post(\"/api/notes\")"}],
        title="No authorization",
        rationale="Routes have no role checks.",
        fix={"summary": "Add authz.", "steps": []},
        standards=[{"framework": "cwe", "id": "CWE-862"}, {"framework": "owasp-api-2023", "id": "API5"}],
        confidence="medium",
        evidence_verified=True,
    )
    region = to_sarif([f])["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"]
    assert region["startLine"] == 14


import copy
import math

from contracts.sarif import FALLBACK_CWE, SarifError


def one_run(result=None, rules=None, driver_extra=None, run_extra=None, region=None):
    res = {
        "ruleId": "r1",
        "level": "warning",
        "message": {"text": "something wrong"},
        "locations": [{"physicalLocation": {"artifactLocation": {"uri": "a.py"}, "region": region or {"startLine": 2}}}],
    }
    res.update(result or {})
    driver = {"name": "tool", "rules": rules if rules is not None else []}
    driver.update(driver_extra or {})
    run = {"tool": {"driver": driver}, "results": [res]}
    run.update(run_extra or {})
    return {"version": "2.1.0", "runs": [run]}


def test_fallback_cwe_covers_exactly_the_category_enum():
    import json
    from importlib import resources

    schema = json.loads((resources.files("contracts") / "schemas" / "finding.v1.json").read_text(encoding="utf-8"))
    assert set(FALLBACK_CWE) == set(schema["properties"]["category"]["enum"])


def test_unknown_category_raises():
    with pytest.raises(SarifError):
        from_sarif(one_run(), category="nonsense")


@pytest.mark.parametrize(
    "doc",
    [
        {"runs": [{"results": []}]},
        {"runs": [{"tool": {}, "results": []}]},
        {"runs": [{"tool": {"driver": {}}, "results": []}]},
        {"runs": [{"tool": None, "results": []}]},
        [],
    ],
)
def test_structural_problems_raise(doc):
    with pytest.raises(SarifError):
        from_sarif(doc, category="sast")


def test_output_that_fails_schema_raises():
    sarif = one_run()
    sarif["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] = "src/../../x.py"
    with pytest.raises(SarifError):
        from_sarif(sarif, category="sast")


def test_null_fields_are_tolerated():
    sarif = one_run(
        rules=[{"id": "r1", "properties": None, "shortDescription": None, "help": None}],
        result={"properties": None, "locations": [{"physicalLocation": {"artifactLocation": {"uri": "a.py"}, "region": None}}]},
    )
    [f] = from_sarif(sarif, category="sast")
    assert f["location"] == {"kind": "file", "file": "a.py"}
    sarif = one_run(region={"startLine": 2, "snippet": None})
    [f] = from_sarif(sarif, category="sast")
    assert f["evidence"][0]["snippet"] == "something wrong"


def test_non_fail_kinds_are_skipped():
    for kind in ["pass", "notApplicable", "informational"]:
        assert from_sarif(one_run(result={"kind": kind}), category="sast") == []
    assert len(from_sarif(one_run(result={"kind": "fail"}), category="sast")) == 1


def test_suppressed_results_are_still_imported():
    # In-source suppressions must not silence the gate; only infosec overrides can.
    sarif = one_run(result={"suppressions": [{"kind": "inSource"}]})
    assert len(from_sarif(sarif, category="sast")) == 1


@pytest.mark.parametrize("tag", ["external/cwe/cwe-079", "CWE-079: XSS", "cwe-79"])
def test_cwe_tags_are_case_insensitive_and_strip_zeros(tag):
    sarif = one_run(rules=[{"id": "r1", "properties": {"tags": [tag]}}])
    [f] = from_sarif(sarif, category="sast")
    assert f["standards"] == [{"framework": "cwe", "id": "CWE-79"}]


def test_rules_merged_from_extensions():
    sarif = one_run(rules=[], run_extra=None)
    sarif["runs"][0]["tool"]["extensions"] = [
        {"name": "ext", "rules": [{"id": "r1", "shortDescription": {"text": "From extension"}, "properties": {"tags": ["CWE-22"]}}]},
        None,
    ]
    [f] = from_sarif(sarif, category="sast")
    assert f["title"] == "From extension"
    assert f["standards"][0]["id"] == "CWE-22"


def test_rule_index_resolves_missing_rule_id():
    sarif = one_run(
        rules=[{"id": "first"}, {"id": "second", "shortDescription": {"text": "Second rule"}}],
        result={"ruleIndex": 1},
    )
    del sarif["runs"][0]["results"][0]["ruleId"]
    [f] = from_sarif(sarif, category="sast")
    assert f["rule_id"] == "second"
    assert f["title"] == "Second rule"


@pytest.mark.parametrize("score", ["nan", "inf", "-inf", "abc", None, float("nan"), [1]])
def test_bad_security_severity_falls_back_to_level(score):
    sarif = one_run(rules=[{"id": "r1", "properties": {"security-severity": score}}], result={"level": "error"})
    [f] = from_sarif(sarif, category="sast")
    assert f["severity"] == "high"


def set_uri(sarif, uri, **extra):
    sarif["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"] = {"uri": uri, **extra}
    return sarif


def test_percent_decoding_and_normalization():
    [f] = from_sarif(set_uri(one_run(), "src/a%2Bb.py"), category="sast")
    assert f["location"]["file"] == "src/a+b.py"
    [f] = from_sarif(set_uri(one_run(), "./src/../lib/x.py"), category="sast")
    assert f["location"]["file"] == "lib/x.py"


def test_file_uri_is_made_repo_relative():
    [f] = from_sarif(set_uri(one_run(), "file:///repo/src/a%20b.py"), category="sast", repo_root="/repo")
    assert f["location"]["file"] == "src/a b.py"


def test_localhost_file_uri_is_accepted():
    [f] = from_sarif(set_uri(one_run(), "file://localhost/repo/a.py"), category="sast", repo_root="/repo")
    assert f["location"]["file"] == "a.py"


@pytest.mark.parametrize("uri", ["file://ci-host/x.py", "%2e%2e/%2e%2e/etc/passwd", "../x.py", "file:///etc/passwd", "/abs/x.py", "http://evil/x.py", "."])
def test_hostile_paths_raise(uri):
    with pytest.raises(SarifError):
        from_sarif(set_uri(one_run(), uri), category="sast", repo_root="/repo")


def test_absolute_path_without_repo_root_raises():
    with pytest.raises(SarifError, match="outside the repo"):
        from_sarif(set_uri(one_run(), "file:///repo/a.py"), category="sast")


def test_uri_base_id_resolves():
    sarif = set_uri(one_run(), "src/a.py", uriBaseId="SRCROOT")
    sarif["runs"][0]["originalUriBaseIds"] = {"SRCROOT": {"uri": "file:///repo/"}}
    [f] = from_sarif(sarif, category="sast", repo_root="/repo")
    assert f["location"]["file"] == "src/a.py"
    sarif["runs"][0]["originalUriBaseIds"] = {"SRCROOT": {"uri": "file:///elsewhere/"}}
    with pytest.raises(SarifError):
        from_sarif(sarif, category="sast", repo_root="/repo")


@pytest.mark.parametrize("doc", [{}, {"runs": []}, {"runs": None}, {"runs": "abc"}, {"runs": [None]}])
def test_missing_or_empty_runs_raise(doc):
    with pytest.raises(SarifError):
        from_sarif(doc, category="sast")


@pytest.mark.parametrize("results", ["abc", {"a": 1}, None, 5])
def test_bad_results_raise(results):
    sarif = one_run()
    sarif["runs"][0]["results"] = results
    with pytest.raises(SarifError):
        from_sarif(sarif, category="sast")


def test_empty_or_absent_results_are_fine():
    sarif = one_run()
    sarif["runs"][0]["results"] = []
    assert from_sarif(sarif, category="sast") == []
    del sarif["runs"][0]["results"]
    assert from_sarif(sarif, category="sast") == []


@pytest.mark.parametrize("kind", ["open", "review"])
def test_undecided_results_import_with_low_confidence(kind):
    [f] = from_sarif(one_run(result={"kind": kind}), category="sast")
    assert f["confidence"] == "low"


def test_failing_results_keep_high_confidence():
    [f] = from_sarif(one_run(), category="sast")
    assert f["confidence"] == "high"


def test_absent_baseline_state_is_skipped():
    assert from_sarif(one_run(result={"baselineState": "absent"}), category="sast") == []
    assert len(from_sarif(one_run(result={"baselineState": "new"}), category="sast")) == 1


def test_invalid_cwe_tag_falls_back():
    sarif = one_run(rules=[{"id": "r1", "properties": {"tags": ["CWE-0", "CWE-000"]}}])
    [f] = from_sarif(sarif, category="sast")
    assert f["standards"] == [{"framework": "cwe", "id": FALLBACK_CWE["sast"]}]
    sarif = one_run(rules=[{"id": "r1", "properties": {"tags": ["CWE-0", "CWE-22"]}}])
    [f] = from_sarif(sarif, category="sast")
    assert f["standards"][0]["id"] == "CWE-22"


def test_cwe_tag_needs_word_boundaries():
    sarif = one_run(rules=[{"id": "r1", "properties": {"tags": ["notcwe-5x"]}}])
    [f] = from_sarif(sarif, category="sast")
    assert f["standards"][0]["id"] == FALLBACK_CWE["sast"]


def test_blank_message_falls_back_to_rule_id():
    [f] = from_sarif(one_run(result={"message": {"text": "  \n "}}), category="sast")
    assert f["rationale"] == "r1"
    assert f["evidence"][0]["snippet"] == "r1"


def test_unhashable_kind_does_not_crash():
    assert len(from_sarif(one_run(result={"kind": ["x"]}), category="sast")) == 1


@pytest.mark.parametrize("uri", ["..\\..\\etc\\passwd", "%2e%2e%5c%2e%2e%5cetc%5cpasswd", "..%5Cx.py", "\\\\server\\share\\x", "\\etc\\passwd"])
def test_backslash_forms_raise(uri):
    with pytest.raises(SarifError):
        from_sarif(set_uri(one_run(), uri), category="sast", repo_root="/repo")


def test_backslash_relative_path_is_canonicalized():
    [f] = from_sarif(set_uri(one_run(), "src\\app.py"), category="sast")
    assert f["location"]["file"] == "src/app.py"


def test_symlinked_repo_root_is_resolved(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    [f] = from_sarif(set_uri(one_run(), f"file://{real}/src/a.py"), category="sast", repo_root=link)
    assert f["location"]["file"] == "src/a.py"


def test_gitleaks_round_trip_preserves_core_fields():
    [original] = from_sarif(GITLEAKS_SARIF, category="secrets")
    [back] = from_sarif(to_sarif([original]), category="secrets")
    for key in ("rule_id", "severity", "location", "evidence", "standards"):
        assert back[key] == original[key], key
    assert back["id"] == original["id"]


@pytest.mark.parametrize("bad", [1, None, "x", [], ["a"]])
def test_non_object_results_raise(bad):
    sarif = one_run()
    sarif["runs"][0]["results"].append(bad)
    with pytest.raises(SarifError):
        from_sarif(sarif, category="sast")

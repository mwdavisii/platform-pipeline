import copy

import pytest

from contracts.schema import validate

HEX = "a" * 64

FINDING = {
    "id": HEX,
    "source": {"layer": "agentic", "tool": "reviewer", "tool_version": "0.1.0"},
    "rule_id": "authz.function-level.role-not-checked",
    "category": "authz",
    "severity": "high",
    "confidence": "medium",
    "location": {"kind": "absence", "file": "app.py"},
    "evidence": [{"file": "app.py", "start_line": 14, "snippet": "@app.post(\"/api/notes\")"}],
    "evidence_verified": True,
    "title": "Readers can call write endpoints",
    "rationale": "The guard checks for any role.",
    "fix": {"summary": "Require the writer role.", "steps": ["Add an authz block"]},
    "standards": [{"framework": "cwe", "id": "CWE-285"}, {"framework": "owasp-api-2023", "id": "API5"}],
}


def test_builder_spec_example_is_valid(notes_spec):
    assert validate("builder-spec.v1", notes_spec) == []


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda s: s.update(port=80), "port"),
        (lambda s: s.pop("data_classification"), "data_classification"),
        (lambda s: s.update(extra=1), "extra"),
        (lambda s: s["authz"].update(default="allow"), "deny"),
        (lambda s: s["egress"][0].update(host="*.openai.com"), "egress/0/host"),
        (lambda s: s.update(entrypoint="dist\nRUN curl evil"), "entrypoint"),
        (lambda s: s["authz"]["roles"][0].update(name="read,er"), "authz/roles/0/name"),
        (lambda s: s.update(entrypoint="app:app\n"), "entrypoint"),
        (lambda s: s["env"][0].update(name="FOO\n"), "env/0/name"),
        (lambda s: s.update(entrypoint="../../etc/x:app"), "entrypoint"),
        (lambda s: s.update(entrypoint="../evil.py"), "entrypoint"),
        (lambda s: s.update(entrypoint="a/../b.py"), "entrypoint"),
        (lambda s: s["egress"][0].update(host="169.254.169.254"), "egress/0/host"),
        (lambda s: s.update(health_path="/healthz\n"), "health_path"),
        (lambda s: s["authz"]["rules"][0].update(roles=["reader\n"]), "authz/rules/0/roles/0"),
        (lambda s: s["authz"]["rules"][0].update(roles=["Not,A,Role"]), "authz/rules/0/roles/0"),
        (lambda s: s["egress"][0].update(reason="why\nRUN evil"), "egress/0/reason"),
        (lambda s: s["authz"]["rules"][0].update(justification="long enough\nsecond line"), "authz/rules/0/justification"),
    ],
)
def test_builder_spec_rejects(notes_spec, mutate, fragment):
    mutate(notes_spec)
    errors = validate("builder-spec.v1", notes_spec)
    assert errors, "expected a validation error"
    assert any(fragment in e for e in errors), errors


def test_finding_example_is_valid():
    assert validate("finding.v1", FINDING) == []


def test_finding_requires_cwe():
    f = copy.deepcopy(FINDING)
    f["standards"] = [{"framework": "owasp-api-2023", "id": "API5"}]
    assert any("standards" in e for e in validate("finding.v1", f))


def test_agentic_finding_requires_category_standard():
    f = copy.deepcopy(FINDING)
    f["standards"] = [{"framework": "cwe", "id": "CWE-285"}]
    assert any("standards" in e for e in validate("finding.v1", f))


def test_deterministic_finding_needs_only_cwe():
    f = copy.deepcopy(FINDING)
    f["source"] = {"layer": "deterministic", "tool": "semgrep"}
    f["standards"] = [{"framework": "cwe", "id": "CWE-89"}]
    assert validate("finding.v1", f) == []


def test_line_location_requires_start_line():
    f = copy.deepcopy(FINDING)
    f["location"] = {"kind": "line", "file": "app.py"}
    assert validate("finding.v1", f)


def test_finding_requires_evidence():
    f = copy.deepcopy(FINDING)
    f["evidence"] = []
    assert validate("finding.v1", f)


def test_override_valid_and_invalid():
    override = {
        "finding_id": HEX,
        "repo": "platform/notes-api",
        "approver": "infosec.reviewer",
        "justification": "Compensating control at the edge until 11-18.",
        "created": "2026-10-19",
        "expires": "2026-11-18",
    }
    assert validate("override.v1", override) == []
    override["expires"] = "next month"
    assert validate("override.v1", override)


def test_gate_result_valid():
    result = {
        "status": "fail",
        "counts": {
            "by_severity": {"critical": 0, "high": 1, "medium": 2, "low": 0, "info": 0},
            "by_layer": {"agentic": 1, "deterministic": 2},
        },
        "blocking": [HEX],
        "overrides_applied": [],
    }
    assert validate("gate-result.v1", result) == []


def test_builder_result_valid():
    result = {"command": "verify", "status": "ok", "builder_version": "0.1.0", "warnings": [], "questions": [], "errors": [], "findings": []}
    assert validate("builder-result.v1", result) == []


def test_egress_hostname_with_alphabetic_tld_is_valid(notes_spec):
    notes_spec["egress"][0]["host"] = "litellm.internal"
    assert validate("builder-spec.v1", notes_spec) == []


def test_override_rejects_trailing_newline_and_impossible_dates():
    override = {
        "finding_id": HEX,
        "repo": "platform/notes-api",
        "approver": "infosec.reviewer",
        "justification": "Compensating control at the edge until 11-18.",
        "created": "2026-10-19",
        "expires": "2026-11-18\n",
    }
    assert validate("override.v1", override)
    override["expires"] = "2020-99-99"
    assert validate("override.v1", override)
    override["expires"] = "2026-11-18"
    override["repo"] = "platform/notes-api\n"
    assert validate("override.v1", override)


def test_cwe_id_format_is_enforced():
    f = copy.deepcopy(FINDING)
    f["standards"][0] = {"framework": "cwe", "id": "not-a-cwe"}
    assert any("standards" in e for e in validate("finding.v1", f))
    f["standards"][0] = {"framework": "cwe", "id": "CWE-0"}
    assert validate("finding.v1", f)


def test_blank_evidence_snippet_rejected():
    f = copy.deepcopy(FINDING)
    f["evidence"][0]["snippet"] = "   "
    assert validate("finding.v1", f)


def test_passing_gate_cannot_have_blocking_findings():
    result = {
        "status": "pass",
        "counts": {"by_severity": {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0}, "by_layer": {}},
        "blocking": [HEX],
        "overrides_applied": [],
    }
    assert validate("gate-result.v1", result)
    result["blocking"] = []
    assert validate("gate-result.v1", result) == []


@pytest.mark.parametrize(
    "schema, document",
    [
        ("finding.v1", {**FINDING, "id": HEX + "\n"}),
        ("gate-result.v1", {"status": "fail", "counts": {"by_severity": {"critical": 0, "high": 1, "medium": 0, "low": 0, "info": 0}, "by_layer": {}}, "blocking": [HEX + "\n"], "overrides_applied": []}),
    ],
)
def test_hex_ids_reject_trailing_newline(schema, document):
    assert validate(schema, document)


@pytest.mark.parametrize(
    "field, value",
    [("approver", "a\nb"), ("justification", "long enough text\nmore"), ("repo", "../x"), ("repo", "a/.."), ("repo", "..")],
)
def test_override_rejects_newlines_and_dotdot_repo(field, value):
    override = {
        "finding_id": HEX,
        "repo": "platform/notes-api",
        "approver": "infosec.reviewer",
        "justification": "Compensating control at the edge until 11-18.",
        "created": "2026-10-19",
        "expires": "2026-11-18",
    }
    override[field] = value
    assert validate("override.v1", override)


@pytest.mark.parametrize("where", ["start_line", "end_line", "evidence"])
def test_line_numbers_are_bounded(where):
    f = copy.deepcopy(FINDING)
    f["location"] = {"kind": "line", "file": "app.py", "start_line": 1}
    huge = 2**31
    if where == "evidence":
        f["evidence"][0]["start_line"] = huge
    else:
        f["location"][where] = huge
    assert validate("finding.v1", f)


@pytest.mark.parametrize(
    "path",
    ["a\nb.py", "/etc/passwd", "../x", "a/../b", "a\x00b", "..", "..\\outside.txt", "src\\app.py", "a\u2028b.py", "a\u2029b", "a\x85b", "./a.py", "a/./b", "a/."],
)
def test_finding_paths_are_constrained(path):
    f = copy.deepcopy(FINDING)
    f["location"]["file"] = path
    assert validate("finding.v1", f)
    f = copy.deepcopy(FINDING)
    f["evidence"][0]["file"] = path
    assert validate("finding.v1", f)


@pytest.mark.parametrize("path", ["src/app.py", "<unknown>", "a/..b/c.py", ".github/x.yml", "a/.b/c"])
def test_finding_paths_accepted(path):
    f = copy.deepcopy(FINDING)
    f["location"]["file"] = path
    f["evidence"][0]["file"] = path
    assert validate("finding.v1", f) == []

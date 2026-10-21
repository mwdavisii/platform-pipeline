import pytest

from contracts.findings import finding_id, make_finding, normalize_snippet
from contracts.schema import validate


def test_normalize_collapses_whitespace():
    assert normalize_snippet("  if  not\n\tuser.roles: ") == "if not user.roles:"


def test_id_ignores_whitespace_and_snippet_order():
    a = finding_id("r1", "app.py", ["x  = 1", "y = 2"])
    b = finding_id("r1", "app.py", ["y = 2", "x = 1"])
    assert a == b
    assert len(a) == 64


def test_id_changes_with_rule_or_file():
    base = finding_id("r1", "app.py", ["x"])
    assert finding_id("r2", "app.py", ["x"]) != base
    assert finding_id("r1", "other.py", ["x"]) != base


def test_id_normalizes_windows_paths():
    assert finding_id("r1", "src\\app.py", ["x"]) == finding_id("r1", "src/app.py", ["x"])


def test_make_finding_is_schema_valid():
    f = make_finding(
        layer="deterministic",
        tool="semgrep",
        tool_version="1.90.0",
        rule_id="python.flask.debug-enabled",
        category="sast",
        severity="medium",
        location={"kind": "line", "file": "app.py", "start_line": 3, "end_line": 3},
        evidence=[{"file": "app.py", "start_line": 3, "snippet": "app.run(debug=True)"}],
        title="Flask debug mode enabled",
        rationale="Debug mode exposes an interactive console.",
        fix={"summary": "Disable debug mode.", "steps": ["Remove debug=True"]},
        standards=[{"framework": "cwe", "id": "CWE-489"}],
        evidence_verified=True,
    )
    assert validate("finding.v1", f) == []
    assert f["id"] == finding_id("python.flask.debug-enabled", "app.py", ["app.run(debug=True)"])
    assert f["evidence_verified"] is True
    assert f["confidence"] == "high"


def test_id_has_no_separator_collision():
    a = finding_id("r", "a\x1fb", ["c"])
    b = finding_id("r", "a", ["b", "c"])
    assert a != b


def test_id_rule_file_boundary():
    assert finding_id("r1", "2.py", ["x"]) != finding_id("r12", ".py", ["x"])


def test_make_finding_requires_evidence_verified():
    with pytest.raises(TypeError):
        make_finding(
            layer="deterministic",
            tool="t",
            rule_id="r",
            category="sast",
            severity="low",
            location={"kind": "file", "file": "a.py"},
            evidence=[{"file": "a.py", "start_line": 1, "snippet": "x = 1"}],
            title="t",
            rationale="r",
            fix={"summary": "s", "steps": []},
            standards=[{"framework": "cwe", "id": "CWE-1"}],
        )


@pytest.mark.parametrize("args", [("r\ud800", "a.py", ["x"]), ("r", "a\ud800.py", ["x"]), ("r", "a.py", ["x\ud800"])])
def test_id_survives_lone_surrogates(args):
    first = finding_id(*args)
    assert len(first) == 64 and int(first, 16) >= 0
    assert finding_id(*args) == first

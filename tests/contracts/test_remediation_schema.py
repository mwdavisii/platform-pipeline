import pytest

from contracts.schema import validate

ITEM = {"id": "0123456789abcdef", "priority": 3, "category": "fix", "owner": "app-author",
        "check": "security-scans / semgrep", "source": "semgrep", "rule_id": "python.subprocess-shell-true",
        "severity": "error", "location": {"file": "app.py", "line": 25, "in_checkout": True},
        "evidence": "x", "guidance": "y", "verify": ["builder verify ."], "guardrails": ["no-suppressions"],
        "contact": None, "rerun_command": None}
GUARD = {"id": "no-suppressions", "text": "t", "caught_by": "c"}


def doc(**kw):
    base = {"builder_version": "0.3.5", "repo": "o/r", "pr": 7, "head_sha": "a" * 40, "status": "action-needed",
            "counts": {"fix": 1, "rerun": 0, "wait": 0, "needs-human": 0}, "items": [ITEM], "guardrails": [GUARD], "warnings": []}
    return {**base, **kw}


def test_full_and_minimal_documents_validate():
    assert validate("remediation.v1", doc()) == []
    assert validate("remediation.v1", doc(status="clean", items=[], counts={"fix": 0, "rerun": 0, "wait": 0, "needs-human": 0})) == []
    assert validate("remediation.v1", doc(items=[{**ITEM, "location": None, "evidence": None, "rule_id": None, "severity": None}])) == []


@pytest.mark.parametrize("bad", [{"status": "ok"}, {"pr": 0}, {"head_sha": "abc"}, {"extra": 1},
                                 {"items": [{**ITEM, "category": "ignore"}]}, {"items": [{**ITEM, "owner": "bot"}]},
                                 {"items": [{**ITEM, "guardrails": ["made-up"]}]}, {"items": [{**ITEM, "extra": 1}]},
                                 {"items": [{**ITEM, "evidence": "e" * 301}]}])
def test_bad_documents_fail(bad):
    assert validate("remediation.v1", doc(**bad))

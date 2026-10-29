"""High and critical deterministic findings hold the merge; who may override it."""

from scorecard.blocking import MAX_LISTED, blocking_findings, override_by
from scorecard.signals import signal

NOW = "2026-10-28T12:00:00Z"


def _signal(rule_id, severity, file="a.py", line=None):
    return signal(rule_id=rule_id, category="iac", severity=severity, file=file, line=line, snippet="x", title="t", rationale="r",
                  fix="f", now=NOW)


def _card(findings, suppressed=(), agent=()):
    return {"signals": {"findings": list(findings)}, "suppressions_applied": [{"id": "s", "finding_id": f["id"]} for f in suppressed],
            "assessment": {"findings": list(agent)}}


def test_only_high_and_critical_block():
    high, crit, med, low = _signal("T-16/x", "high"), _signal("T-2/y", "critical"), _signal("T-9/z", "medium"), _signal("T-1/w", "low")
    assert [f["rule_id"] for f in blocking_findings(_card([med, high, low, crit]))] == ["T-16/x", "T-2/y"]
    assert blocking_findings(_card([med, low])) == []


def test_a_suppressed_finding_does_not_block():
    high = _signal("T-16/x", "high")
    assert blocking_findings(_card([high], suppressed=[high])) == []


def test_agent_findings_never_block():
    agent = {**_signal("VC-4/debug", "high"), "source": {"layer": "agentic", "tool": "scorecard-agent"}}
    assert blocking_findings(_card([], agent=[agent])) == []
    assert blocking_findings(_card([agent])) == [], "even if one were in signals"


def test_order_is_rule_then_file_then_line():
    a, b, c = _signal("T-16/x", "high", "b.py", 2), _signal("T-16/x", "high", "a.py"), _signal("T-15/y", "high", "z.py", 9)
    assert [(f["rule_id"], f["location"]["file"]) for f in blocking_findings(_card([a, b, c]))] == [
        ("T-15/y", "z.py"), ("T-16/x", "a.py"), ("T-16/x", "b.py")]


def test_override_by():
    assert override_by({}) == "a repo admin"
    assert override_by({"org": "acme", "findings_bypass_team": "platform-admins"}) == "a member of `acme/platform-admins`"
    assert override_by({"findings_bypass_team": "platform-admins"}) == "a repo admin", "no org: nothing to name"
    for bad in ("Platform", "a b", "x`y", "", "-x", 5):
        assert override_by({"org": "acme", "findings_bypass_team": bad}) == "a repo admin", bad


def test_max_listed():
    assert MAX_LISTED == 20

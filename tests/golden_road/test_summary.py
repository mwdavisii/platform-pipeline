import datetime as dt
import re

import pytest

from golden_road import summary
from golden_road.gate import evaluate
from golden_road.summary import render


def _with(finding, **changes):
    from contracts.findings import finding_id

    f = {**finding, **changes}
    f["id"] = finding_id(f["rule_id"], f["location"]["file"], [e["snippet"] for e in f["evidence"]])
    return f


def test_summary_shows_status_and_blocking_first(valid_finding):
    low = _with(valid_finding, severity="low", rule_id="CVE-2024-0002", title="Minor thing")
    findings = [low, valid_finding]
    gate = evaluate(findings, [], repo="r/x", today=dt.date(2026, 10, 20))
    md = render(gate, findings)
    assert md.startswith("## Golden road gate: FAIL")
    assert md.index("Vulnerable jinja2") < md.index("Minor thing")
    assert "Pin jinja2>=3.1.4 and re-lock" in md


def test_summary_escapes_markdown_and_html(valid_finding):
    hostile = {**valid_finding, "title": "<img src=x onerror=alert(1)> `code` | pipe\nnewline [link](http://evil)"}
    gate = evaluate([hostile], [], repo="r/x", today=dt.date(2026, 10, 20))
    md = render(gate, [hostile])
    assert "<img" not in md
    assert "](http://evil)" not in md
    assert "\nnewline" not in md


def test_summary_neutralizes_images_and_every_field(valid_finding):
    evil = "![x](http://evil/p.png) <script>x</script>\r\n# heading"
    hostile = {**valid_finding, "rule_id": evil, "title": evil,
               "location": {"kind": "file", "file": evil},
               "fix": {"summary": evil, "steps": [evil]}}
    gate = evaluate([hostile], [], repo="r/x", today=dt.date(2026, 10, 20))
    md = render(gate, [hostile])
    assert "<script" not in md and "](http://evil" not in md
    assert "\n# heading" not in md and "![" not in md.replace("\\!\\[", "")


def test_summary_caps_long_lists(valid_finding):
    many = [_with(valid_finding, severity="low", rule_id=f"CVE-{i}") for i in range(150)]
    gate = evaluate(many, [], repo="r/x", today=dt.date(2026, 10, 20))
    md = render(gate, many)
    assert "50 more findings not shown" in md


def test_summary_keeps_quotes_readable(valid_finding):
    f = {**valid_finding, "title": "Don't use \"eval\" here"}
    gate = evaluate([f], [], repo="r/x", today=dt.date(2026, 10, 20))
    md = render(gate, [f])
    assert "Don't use \"eval\" here" in md and "&#" not in md and "&quot;" not in md


def _mk(valid_finding, i, text, **changes):
    from contracts.findings import make_finding

    return make_finding(layer="deterministic", tool="trivy", rule_id=f"CVE-{i}-{text[:5]}", category="sca",
                        severity=changes.get("severity", "high"),
                        location={"kind": "file", "file": "r.txt" + text},
                        evidence=[{"file": "r", "start_line": 1, "snippet": f"s{i}"}], evidence_verified=True,
                        title=text, rationale="r", fix={"summary": text, "steps": [text] * 5},
                        standards=[{"framework": "cwe", "id": "CWE-1"}])


@pytest.mark.parametrize("char", ["&", "*", "<", "@", "a"])
def test_summary_never_exceeds_comment_budget(valid_finding, char):
    findings = [_mk(valid_finding, i, char * 300) for i in range(150)]
    gate = evaluate(findings, [], repo="r/x", today=dt.date(2026, 10, 20))
    md = render(gate, findings)
    assert len(md) <= 65_536 and len(md) <= summary.MAX_COMMENT
    assert "more findings not shown; see the workflow artifact." in md
    shown = md.count("- BLOCKING **high**")
    assert shown >= 1 and shown < 150  # some shown, the rest summarized; every _mk finding is a blocking High


def test_budget_truncation_keeps_blocking_findings_first(valid_finding):
    blocking = [_with(valid_finding, rule_id=f"HIGH-{i}", title="B" * 300) for i in range(5)]
    lows = [_with(valid_finding, severity="low", rule_id=f"LOW-{i}", title="L" * 300) for i in range(150)]
    findings = lows + blocking
    gate = evaluate(findings, [], repo="r/x", today=dt.date(2026, 10, 20))
    md = render(gate, findings)
    assert md.count("BLOCKING") == 5
    assert md.index("BLOCKING") < md.index("**low**")
    assert len(md) <= summary.MAX_COMMENT


def test_summary_truncation_never_splits_escapes():
    for n in range(1, 12):
        for char in "&<*@":
            out = summary._md(("x" * n) + char * 400, limit=n + 7)
            assert not re.search(r"\\$", out.replace("\\\\", ""))
            assert not re.search(r"&[a-z]*$", out)
            assert not out.endswith("@")
            assert len(out) <= n + 7


def test_md_neutralizes_mentions_and_bad_unicode():
    assert "@‍" in summary._md("ping @octocat") and "@octocat" not in summary._md("ping @octocat")
    for bad in ("a\udcffb", "a\ud800b"):
        out = summary._md(bad)
        out.encode("utf-8")
        assert out == "a?b"


def test_severity_table_fixed_order(valid_finding):
    gate = evaluate([valid_finding], [], repo="r/x", today=dt.date(2026, 10, 20))
    gate["counts"]["by_severity"] = dict(reversed(list(gate["counts"]["by_severity"].items())))
    md = render(gate, [valid_finding])
    order = [md.index(f"| {s} |") for s in ("critical", "high", "medium", "low", "info")]
    assert order == sorted(order)


def test_overridden_label(valid_finding):
    ov = {"finding_id": valid_finding["id"], "repo": "r/x", "approver": "infosec",
          "justification": "Compensating control documented.", "created": "2026-10-19", "expires": "2026-11-01"}
    gate = evaluate([valid_finding], [ov], repo="r/x", today=dt.date(2026, 10, 20))
    md = render(gate, [valid_finding])
    assert "OVERRIDDEN" in md and "BLOCKING" not in md


def test_md_strips_bidi_zero_width_and_control_characters():
    from golden_road.summary import _md

    assert _md("a\u202eb\u200bc\u2066d\x07e\x1bf") == "abcdef"
    assert _md("@user") == "@\u200duser"  # the one joiner _md inserts itself stays
    assert _md("@\u200duser") == "@\u200duser"

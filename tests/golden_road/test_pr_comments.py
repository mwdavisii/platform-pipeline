"""The parsers for the platform's PR comments, round-tripped against the real renderers."""

import datetime as dt
import importlib.util
import random
import sys
from pathlib import Path

import pytest

from contracts.findings import finding_id
from golden_road import pr_comments as pc
from golden_road import preview
from golden_road.gate import evaluate
from golden_road.summary import _md
from golden_road.summary import render as render_drift

ROOT = Path(__file__).resolve().parents[2]
if not (ROOT / "reference/security-scans").is_dir():
    pytest.skip("the platform export does not carry reference/security-scans", allow_module_level=True)
sys.path.insert(0, str(ROOT / "reference/security-scans/scripts"))
import sarif_report as sr  # noqa: E402

_spec = importlib.util.spec_from_file_location("reporters", ROOT / "tests/scorecard/test_reporters.py")
reporters = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reporters)

from scorecard import report_md  # noqa: E402

FOUND = [
    sr.Finding("error", "python.subprocess-shell-true", "shell=True runs a shell", "app.py", 25, 25),
    sr.Finding("warning", "rule.file-only", "only a file", "pkg/x.py", None, None),
    sr.Finding("note", "rule.nowhere", "no file at all", "", None, None),
    sr.Finding("error", "rule.ticks", "has `ticks` and ``double`` and @someone", "a.py", 3, 3),
]
MANY = [sr.Finding("error", f"rule.n{i}", f"m{i}", f"f{i}.py", i + 1, i + 1) for i in range(35)]


@pytest.mark.parametrize("text", ["plain", "a `tick` b", "``double``", "`edge", "edge`", "x  y\nz"])
def test_code_span_round_trip(text):
    span = sr.code_span(text)
    assert pc.parse_code_span(span, 0) == (" ".join(text.split()), len(span))


def test_scan_comment_round_trip():
    parsed = pc.parse_scan(sr.markdown("semgrep", FOUND, "error", None))
    assert parsed["tool"] == "semgrep" and parsed["failed"] is True
    assert parsed["unfinished"] is False and parsed["truncated"] is False
    by_rule = {f["rule_id"]: f for f in parsed["findings"]}
    assert by_rule["python.subprocess-shell-true"] == {"level": "error", "rule_id": "python.subprocess-shell-true",
                                                       "file": "app.py", "line": 25, "message": "shell=True runs a shell"}
    assert by_rule["rule.file-only"]["file"] == "pkg/x.py" and by_rule["rule.file-only"]["line"] is None
    assert by_rule["rule.nowhere"]["file"] == ""
    assert by_rule["rule.ticks"]["message"] == "has `ticks` and ``double`` and @someone"
    many = pc.parse_scan(sr.markdown("semgrep", sr.sort_findings(MANY), "error", None))
    assert len(many["findings"]) == 30 and many["truncated"] is True


def test_scan_comment_image_label_has_no_line():
    root = ROOT
    relabelled = sr.relabel_outside([sr.Finding("error", "CVE-1", "m", "usr/lib/x", 4, 4)], root, "container image")
    parsed = pc.parse_scan(sr.markdown("trivy", relabelled, "error", None))
    assert parsed["tool"] == "trivy"
    assert parsed["findings"][0]["file"] == "container image" and parsed["findings"][0]["line"] is None


@pytest.mark.parametrize("text", [sr.markdown("trivy", [], "error", None, job_result="failure"), sr.unusable_markdown("trivy", "bad")])
def test_scan_comment_unfinished(text):
    parsed = pc.parse_scan(text)
    assert parsed["failed"] is True and parsed["unfinished"] is True


def test_scan_comment_passed():
    parsed = pc.parse_scan(sr.markdown("semgrep", [], "error", None))
    assert parsed["failed"] is False and parsed["unfinished"] is False and parsed["findings"] == []


def _drift_text(valid_finding, **changes):
    f = {**valid_finding, "rule_id": "builder.managed-block-modified", "title": "Managed block changed",
         "location": {"kind": "line", "file": "AGENTS.md", "start_line": 3, "end_line": 3}, **changes}
    f["id"] = finding_id(f["rule_id"], f["location"]["file"], [e["snippet"] for e in f["evidence"]])
    gate = evaluate([f], [], repo="o/r", today=dt.date(2026, 10, 20))
    return "<!-- golden-road:drift -->\nChecked commit: `" + "a" * 40 + "` (this pull request merged into its base branch).\n\n" + render_drift(gate, [f]), gate


def test_drift_comment_round_trip(valid_finding):
    text, gate = _drift_text(valid_finding)
    assert gate["blocking"]
    assert pc.parse_drift(text) == {"failed": True, "findings": [
        {"rule_id": "builder.managed-block-modified", "file": "AGENTS.md", "line": 3, "blocking": True}]}


def test_drift_comment_unescapes(valid_finding):
    text, _ = _drift_text(valid_finding, location={"kind": "file", "file": "a_b<c>@d.md"})
    [item] = pc.parse_drift(text)["findings"]
    assert item["file"] == "a_b<c>@d.md" and item["line"] is None


def test_unescape_md():
    hostile = "a_b *c* <x> & @me [l](u) `t` \\"
    assert pc.unescape_md(_md(hostile)) == hostile


PREVIEW = [
    ("pass", {"status": "pass", "cases": 3, "denied": 2}, "success"),
    ("not-declared", {"status": "not-declared"}, "success"),
    ("no-result", None, "success"),
    ("fail", {"status": "fail", "failures": [{"method": "GET", "path": "/admin_x", "roles": None, "status": 200}]}, "failure"),
    ("did-not-run", None, "skipped"),
    ("cancelled", None, "cancelled"),
    ("job-failed", {"status": "pass"}, "failure"),
]


@pytest.mark.parametrize("state,authz,job", PREVIEW)
def test_preview_comment_states(state, authz, job):
    parsed = pc.parse_preview(preview.render(authz, job, None))
    assert parsed["state"] == state
    if state == "fail":
        assert parsed["failures"] == ["GET /admin_x with no role: status 200 (the guard's 403 was expected)"]


def test_scorecard_comment_round_trip():
    parsed = pc.parse_scorecard(report_md.render(reporters._card(), reporters.TEXTS, reporters.NAMES, reporters.BLOCKING))
    assert parsed["level"] == "security_review"
    [reason] = parsed["reasons"]
    assert reason["trigger"] == "T-6" and reason["level"] == "security_review"
    assert reason["summary"] == "Debug or admin endpoint, or an auth bypass (VC-4)"
    empty = pc.parse_scorecard(report_md.render(reporters._card(reasons=[]), reporters.TEXTS, reporters.NAMES, reporters.BLOCKING))
    assert empty["reasons"] == []


def test_scorecard_comment_without_fix_lines_has_no_guidance():
    parsed = pc.parse_scorecard(report_md.render(reporters._card(), reporters.TEXTS, reporters.NAMES, reporters.BLOCKING))
    assert all("guidance" not in r for r in parsed["reasons"])


def test_scorecard_comment_reads_the_fix_line_under_its_reason():
    text = ("<!-- golden-road:scorecard -->\n## Scorecard: ELEVATED \u2014 2 reasons\n\n### Reasons\n\n"
            "- **T-22** \u00b7 elevated \u00b7 First\n  - Why: because\n  - Fix: Do the \\_thing.\n  - Fix: ignored second\n"
            "- **T-23** \u00b7 elevated \u00b7 Second\n  - Why: because\n")
    first, second = pc.parse_scorecard(text)["reasons"]
    assert first["guidance"] == "Do the _thing." and "guidance" not in second


def test_scorecard_comment_parses_the_re_review_triggers():
    reasons = [{"trigger": "T-17", "level": "elevated", "summary": "Resource tier raised", "refs": []},
               {"trigger": "T-19", "level": "elevated", "summary": "Large change: 1200 lines in 3 files", "refs": []}]
    text = report_md.render(reporters._card(reasons=reasons, level="elevated"), reporters.TEXTS, reporters.NAMES, reporters.BLOCKING)
    assert [(r["trigger"], r["summary"]) for r in pc.parse_scorecard(text)["reasons"]] == [
        ("T-17", "Resource tier raised"), ("T-19", "Large change: 1200 lines in 3 files")]


def test_scorecard_comment_truncated_still_parses_what_fits():
    reasons = [{"trigger": f"T-{i % 16 + 1}", "level": "elevated", "summary": "s" * 250, "refs": []} for i in range(400)]
    text = report_md.render(reporters._card(reasons=reasons, level="elevated"), reporters.TEXTS, reporters.NAMES, reporters.BLOCKING, limit=report_md.MAX_COMMENT)
    assert "Truncated" in text
    parsed = pc.parse_scorecard(text)
    assert parsed["level"] == "elevated" and 100 < len(parsed["reasons"]) < 400


def _outputs(valid_finding):
    drift, _ = _drift_text(valid_finding)
    texts = [sr.markdown("semgrep", FOUND, "error", None), sr.markdown("trivy", sr.sort_findings(MANY), "error", None),
             sr.unusable_markdown("trivy", "bad"), drift,
             report_md.render(reporters._card(), reporters.TEXTS, reporters.NAMES, reporters.BLOCKING)]
    texts += [preview.render(a, j, None) for _, a, j in PREVIEW]
    return texts


def test_parsers_never_raise(valid_finding):
    rng = random.Random(0)
    parsers = (pc.parse_scan, pc.parse_drift, pc.parse_preview, pc.parse_scorecard)
    for text in _outputs(valid_finding):
        variants = [text[:n] for n in range(0, len(text), 7)]
        lines = text.splitlines()
        for _ in range(10):
            mangled = [rng.choice(lines) for _ in lines]
            variants.append("\n".join(mangled))
        for variant in variants + ["`" * 5000, "- **error** " + "`" * 400 + " in " * 300]:
            for parse in parsers:
                assert isinstance(parse(variant), dict)
    for junk in (None, 5, b"x"):
        for parse in parsers:
            assert isinstance(parse(junk), dict)


def test_markers_match_the_renderers():
    workflow = (ROOT / ".github/workflows/golden-road.yml").read_text()
    assert f'marker="{pc.MARKERS["drift"]}"' in workflow
    assert pc.MARKERS["preview"] == preview.MARKER
    assert pc.MARKERS["scorecard"] == report_md.MARKER
    for tool in ("semgrep", "trivy"):
        assert sr.markdown(tool, [], "error", None).splitlines()[0] == pc.MARKERS[tool]


def test_scorecard_comment_parses_a_t21_reason_with_its_registered_and_needed_values():
    summary = "Registered as low/internal; this change needs at least moderate/internal (signals: write_roles, other_egress)"
    reasons = [{"trigger": "T-21", "level": "security_review", "summary": summary, "refs": []}]
    text = report_md.render(reporters._card(reasons=reasons, level="security_review"), reporters.TEXTS, reporters.NAMES, reporters.BLOCKING)
    assert [(r["trigger"], r["summary"]) for r in pc.parse_scorecard(text)["reasons"]] == [("T-21", summary)]

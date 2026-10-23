"""Builders for remediate tests: PR comments made by the real renderers, checks, and a PR."""

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest

from contracts.findings import finding_id
from golden_road import preview
from golden_road.gate import evaluate
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

RUN = "https://github.com/o/r/actions/runs/5/job/6"
HEAD = "a" * 40


def _workflow(name):
    for prefix, workflow in (("security-scans", "security-scans"), ("golden-road", "ci"), ("scorecard", "scorecard"), ("CodeQL", "CodeQL"), ("Analyze", "CodeQL")):
        if name.startswith(prefix):
            return workflow
    return "w"


def check(name, bucket="fail", state="FAILURE", link=RUN, workflow=None):
    return {"name": name, "bucket": bucket, "state": state, "link": link, "workflow": workflow or _workflow(name)}


def pr(**kw):
    return {"number": 7, "state": "OPEN", "isDraft": False, "isCrossRepository": False, "headRefName": "feature", "headRefOid": HEAD,
            "url": "https://github.com/o/r/pull/7", **kw}


def finding(rule, message="m", file="app.py", line=3, level="error"):
    return sr.Finding(level, rule, message, file, line, line)


def scan_comment(tool, found, job_result=None):
    return sr.markdown(tool, sr.sort_findings(list(found)), "error", None, job_result=job_result)


def drift_comment(valid_finding, rule="builder.managed-block-modified", file="AGENTS.md", line=3):
    f = {**valid_finding, "rule_id": rule, "title": "t", "location": {"kind": "line", "file": file, "start_line": line, "end_line": line}}
    f["id"] = finding_id(f["rule_id"], file, [e["snippet"] for e in f["evidence"]])
    gate = evaluate([f], [], repo="o/r", today=dt.date(2026, 10, 20))
    return "<!-- golden-road:drift -->\nChecked commit: x\n\n" + render_drift(gate, [f])


def preview_comment(state):
    cases = {"pass": ({"status": "pass", "cases": 3, "denied": 2}, "success"), "no-result": (None, "success"),
             "did-not-run": (None, "skipped"), "cancelled": (None, "cancelled"),
             "fail": ({"status": "fail", "failures": [{"method": "GET", "path": "/admin", "roles": None, "status": 200}]}, "failure")}
    authz, job = cases[state]
    return preview.render(authz, job, None)


def scorecard_comment(triggers=("T-14", "T-15"), level="elevated", summary="s"):
    reasons = [{"trigger": t, "level": level, "summary": f"{summary} {t}", "refs": []} for t in triggers]
    return report_md.render(reporters._card(reasons=reasons, level=level), reporters.TEXTS, reporters.NAMES, reporters.BLOCKING)

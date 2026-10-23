import copy
import json

import pytest

from contracts.findings import finding_id
from contracts.schema import validate
from scorecard.agent.output import InvalidOutput, parse_batch

NOW = "2026-10-21T12:00:00Z"
ANCHOR = {"file": "app.py", "start_line": 8, "snippet": "return [n for n in NOTES if n.owner == user]"}
FINDING = {"ref": "F1", "rule_id": "authz/missing-owner-check", "category": "authz", "severity": "high", "confidence": "high",
           "location": {"kind": "line", "file": "app.py", "start_line": 8}, "evidence": [ANCHOR], "title": "No owner check",
           "rationale": "Any reader can read any note.", "fix": {"summary": "Filter by owner.", "steps": []},
           "standards": [{"framework": "cwe", "id": "CWE-639"}, {"framework": "asvs-5.0", "id": "v5.0.0-8.2.2"}]}
IDS = ["v5.0.0-8.2.2", "v5.0.0-8.3.1"]


def result(asvs_id, status="pass", refs=()):
    return {"asvs_id": asvs_id, "status": status, "confidence": "high", "justification": "j", "evidence": [ANCHOR], "finding_refs": list(refs)}


def batch(results=None, findings=None):
    return {"results": results if results is not None else [result(IDS[0], "fail", ["F1"]), result(IDS[1])],
            "findings": findings if findings is not None else [FINDING]}


def test_a_good_batch_becomes_assessment_results_and_v2_findings():
    results, findings = parse_batch(json.dumps(batch()), IDS, now=NOW)
    fid = finding_id(FINDING["rule_id"], "app.py", [ANCHOR["snippet"]])
    assert results[0] == {"asvs_id": IDS[0], "status": "fail", "confidence": "high", "justification": "j", "evidence": [ANCHOR], "finding_ids": [fid]}
    assert "finding_ids" not in results[1]
    assert findings[0]["id"] == fid and findings[0]["source"] == {"layer": "agentic", "tool": "scorecard-agent", "tool_version": "0.1.0"}
    assert findings[0]["evidence_verified"] is False and findings[0]["first_seen"] == NOW and validate("finding.v2", findings[0]) == []
    doc = {"schema": "assessment/v1", "tree_sha": "d" * 40, "prompt_version": "review-1",
           "model": {"version": "1", "temperature": 0}, "complete": True, "results": results, "findings": findings}
    assert validate("assessment.v1", doc) == []


@pytest.mark.parametrize("doc, message", [
    ("{", "not valid JSON"),
    ("[]", "does not match the schema"),
    (json.dumps({"results": [], "findings": [], "level": "standard"}), "does not match the schema"),
    (json.dumps(batch(results=[result(IDS[0])])), "requirements not answered: v5.0.0-8.3.1"),
    (json.dumps(batch(results=[result(IDS[0]), result(IDS[1]), result("v5.0.0-1.1.1")])), "requirements not in this batch: v5.0.0-1.1.1"),
    (json.dumps(batch(results=[result(IDS[0]), result(IDS[0]), result(IDS[1])])), "answered more than once"),
    (json.dumps(batch(results=[result(IDS[0], refs=["F9"]), result(IDS[1])])), "finding_refs with no finding: F9"),
    (json.dumps(batch(findings=[FINDING, FINDING])), "a finding ref is used more than once"),
    (json.dumps(batch(findings=[{**FINDING, "location": {"kind": "line", "file": "app.py"}}])), "does not match the schema"),  # the schema now enforces start_line,
    (json.dumps(batch(findings=[{**FINDING, "category": "injection"}])), "finding F1 is not a valid finding"),
    (json.dumps(batch(findings=[{**FINDING, "standards": [{"framework": "cwe", "id": "CWE-639"}]}])), "does not match the schema"),
    (json.dumps(batch(findings=[{**FINDING, "standards": [{"framework": "asvs-5.0", "id": "v5.0.0-8.2.2"}]}])), "does not match the schema"),
    (json.dumps(batch(findings=[{**FINDING, "standards": [{"framework": "cwe", "id": "639"}, {"framework": "asvs-5.0", "id": "v5.0.0-8.2.2"}]}])),
     "does not match the schema"),  # the schema now enforces the CWE pattern
    ("x" * 400_001, "at most 400000 characters"),
    (None, "at most 400000 characters"),
])
def test_bad_output_is_invalid(doc, message):
    with pytest.raises(InvalidOutput, match=message):
        parse_batch(doc, IDS, now=NOW)


def test_an_injection_finding_with_only_the_vc_standard_is_valid():
    inj = {**FINDING, "rule_id": "VC-INJ/reviewer-directive", "category": "injection", "standards": [{"framework": "golden-road-vc", "id": "VC-INJ"}]}
    _, findings = parse_batch(json.dumps(batch(results=[result(IDS[0]), result(IDS[1])], findings=[inj])), IDS, now=NOW)
    assert findings[0]["category"] == "injection"

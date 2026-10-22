"""agent-batch/v1: the model's output for one batch. It is sent to the model as a tool schema, so it must stand alone."""

import json
from importlib import resources

from contracts.schema import validate

ANCHOR = {"file": "app.py", "start_line": 8, "snippet": "return [n for n in NOTES if n.owner == user]"}
FINDING = {"ref": "F1", "rule_id": "authz/missing-owner-check", "category": "authz", "severity": "high", "confidence": "high",
           "location": {"kind": "line", "file": "app.py", "start_line": 8}, "evidence": [ANCHOR], "title": "No owner check",
           "rationale": "Any reader can read any note.", "fix": {"summary": "Filter by owner.", "steps": []},
           "standards": [{"framework": "cwe", "id": "CWE-639"}, {"framework": "asvs-5.0", "id": "v5.0.0-8.2.2"}]}
BATCH = {"results": [{"asvs_id": "v5.0.0-8.2.2", "status": "fail", "confidence": "high", "justification": "no filter",
                      "evidence": [ANCHOR], "finding_refs": ["F1"]}],
         "findings": [FINDING]}


def _schema(name):
    return json.loads((resources.files("contracts") / "schemas" / f"{name}.json").read_text())


def test_a_batch_validates():
    assert validate("agent-batch.v1", BATCH) == []


def test_only_local_refs():
    text = json.dumps(_schema("agent-batch.v1"))
    refs = [part.split('"')[0] for part in text.split('"$ref": "')[1:]]
    assert refs and all(ref.startswith("#/$defs/") for ref in refs)


def test_anchor_and_path_match_finding_v2():
    """The model's anchors are checked again as finding/v2 anchors; the two definitions must not drift apart."""
    batch, v2 = _schema("agent-batch.v1")["$defs"], _schema("finding.v2")["$defs"]
    anchor = json.loads(json.dumps(batch["anchor"]))
    assert anchor["properties"]["file"].pop("maxLength") == 1024  # the one tightening: a cap on the path's length
    assert anchor == v2["anchor"] and batch["repo_path"] == v2["repo_path"]


def test_bad_batches():
    bad = [
        {**BATCH, "extra": 1},
        {"results": []},
        {**BATCH, "findings": [{**FINDING, "standards": [{"framework": "golden-road-trigger", "id": "T-1"}]}]},
        {**BATCH, "findings": [{**FINDING, "ref": "finding-1"}]},
        {**BATCH, "findings": [{**FINDING, "location": {"kind": "line", "file": "../etc/passwd"}}]},
        {**BATCH, "findings": [{**FINDING, "category": "sca"}]},
        {**BATCH, "findings": [{**FINDING, "category": "secrets"}]},
        {**BATCH, "results": [{**BATCH["results"][0], "status": "approved"}]},
        {**BATCH, "results": [{**BATCH["results"][0], "evidence": [{**ANCHOR, "file": ".git/config"}]}]},
        {**BATCH, "findings": [FINDING] * 21},
        {**BATCH, "findings": [{**FINDING, "standards": [{"framework": "asvs-5.0", "id": "v5.0.0-8.2.2"}]}]},
        {**BATCH, "findings": [{**FINDING, "standards": [{"framework": "cwe", "id": "CWE-639"}]}]},
        {**BATCH, "findings": [{**FINDING, "standards": [{"framework": "golden-road-vc", "id": "VC-PASS"}]}]},
        {**BATCH, "findings": [{**FINDING, "standards": [{"framework": "golden-road-vc", "id": "VC-3"}]}]},
        {**BATCH, "findings": [{**FINDING, "standards": [{"framework": "cwe", "id": "not a cwe"}, {"framework": "asvs-5.0", "id": "v5.0.0-8.2.2"}]}]},
        {**BATCH, "findings": [{**FINDING, "location": {"kind": "line", "file": "app.py"}}]},
        {**BATCH, "findings": [{**FINDING, "location": {"kind": "line", "file": "a" * 100_000, "start_line": 1}}]},
        {**BATCH, "findings": [{**FINDING, "evidence": [{**ANCHOR, "file": "a" * 100_000}]}]},
    ]
    for doc in bad:
        assert validate("agent-batch.v1", doc) != [], doc

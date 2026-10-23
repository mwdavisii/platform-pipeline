import copy

import pytest

from contracts.findings import make_finding_v2
from golden_road.io import InputError
from pathlib import Path

from scorecard.asvs import load_asvs
from scorecard.config import load_config
from scorecard.scorer import apply_assessment, check_assessment
from scorecard.scores import chapters, counts, coverage, coverage_counts, score

CONFIG = load_config(Path(__file__).parent / "fixtures" / "factory")
TREE = "d" * 40
APP = "def get_note(note_id, user):\n    note = NOTES[note_id]\n    if note.owner != user:\n        raise Forbidden()\n    return note\n"
GOOD = {"file": "app.py", "start_line": 3, "snippet": "if note.owner != user:"}
MISSING_LINE = {"file": "app.py", "start_line": 99, "snippet": "if note.owner != user:"}


@pytest.fixture
def root(make_app):
    return make_app({"app.py": APP})


@pytest.fixture
def reqs():
    by_id = {r["id"]: r for r in load_asvs()}
    return [by_id["v5.0.0-8.2.2"], by_id["v5.0.0-8.2.1"], by_id["v5.0.0-16.2.5"]]


def _apply(reqs, assessment, root):
    return apply_assessment(reqs, assessment, root, config=CONFIG, tree_sha=TREE)


def _assessment(*results, findings=(), complete=True):
    return {"schema": "assessment/v1", "tree_sha": TREE, "prompt_version": "fixture", "model": {"version": "1", "temperature": 0},
            "complete": complete, "results": list(results), "findings": list(findings)}


def _r(asvs_id, status, evidence=(), confidence="high"):
    return {"asvs_id": asvs_id, "status": status, "confidence": confidence, "justification": "because", "evidence": list(evidence)}


def _by_id(results):
    return {r["asvs_id"]: r for r in results}


def test_verified_pass_and_fail_stand(root, reqs):
    results, _, flags = _apply(reqs[:2], _assessment(_r("v5.0.0-8.2.2", "pass", [GOOD]), _r("v5.0.0-8.2.1", "fail", [GOOD])), root)
    got = _by_id(results)
    assert got["v5.0.0-8.2.2"]["status"] == "pass" and got["v5.0.0-8.2.2"]["evidence_verified"] is True
    assert got["v5.0.0-8.2.1"]["status"] == "fail" and got["v5.0.0-8.2.1"]["status_source"] == "agent"
    assert flags == {"incomplete": False, "unverified": [], "ignored": []}


def test_pass_citing_a_line_that_does_not_exist_is_downgraded(root, reqs):
    """AC-13 (scorer half): the citation fails verify_evidence, so the pass becomes needs_human and is flagged."""
    results, _, flags = _apply(reqs[:1], _assessment(_r("v5.0.0-8.2.2", "pass", [MISSING_LINE])), root)
    result = results[0]
    assert result["status"] == "needs_human" and result["status_source"] == "downgraded" and result["agent_status"] == "pass"
    assert flags["unverified"] == [{"asvs_id": "v5.0.0-8.2.2"}]


@pytest.mark.parametrize(
    "entry, reason",
    [
        (_r("v5.0.0-8.2.2", "pass"), "pass without evidence"),
        (_r("v5.0.0-8.2.2", "pass", [GOOD], confidence="low"), "low-confidence"),
        (_r("v5.0.0-8.2.2", "fail", [MISSING_LINE]), "fail with evidence"),
        (_r("v5.0.0-8.2.2", "na"), "agent-proposed na"),
    ],
)
def test_downgrade_rules(root, reqs, entry, reason):
    results, _, _ = _apply(reqs[:1], _assessment(entry), root)
    assert results[0]["status"] == "needs_human" and reason in results[0]["downgrade_reason"]


def test_no_pass_without_verified_evidence(root, reqs):
    """AC-6, for agent results: every pass carries evidence that verify_evidence confirms."""
    entries = [_r("v5.0.0-8.2.2", "pass", [GOOD]), _r("v5.0.0-8.2.1", "pass", [MISSING_LINE]), _r("v5.0.0-16.2.5", "pass")]
    results, _, _ = _apply(reqs, _assessment(*entries), root)
    assert all(r["evidence_verified"] for r in results if r["status"] == "pass")
    assert [r["asvs_id"] for r in results if r["status"] == "pass"] == ["v5.0.0-8.2.2"]


def test_missing_results_and_incomplete_assessments(root, reqs):
    results, _, flags = _apply(reqs[:2], _assessment(_r("v5.0.0-8.2.2", "pass", [GOOD]), _r("v5.0.0-1.1.1", "pass", [GOOD])), root)
    assert _by_id(results)["v5.0.0-8.2.1"]["downgrade_reason"] == "not assessed by the agent"
    assert flags["incomplete"] is True and flags["ignored"] == ["v5.0.0-1.1.1"]
    _, _, flags = _apply([], _assessment(complete=False), root)
    assert flags["incomplete"] is True
    results, _, flags = _apply(reqs[:1], None, root)
    assert results[0]["status"] == "needs_human" and flags["incomplete"] is True


def _agent_finding(anchor):
    return make_finding_v2(
        first_seen="2026-10-21T12:00:00Z", layer="agentic", tool="scorecard-agent", rule_id="VC-4/debug-endpoint", category="authz",
        severity="high", location={"kind": "line", "file": "app.py", "start_line": anchor["start_line"]}, evidence=[anchor],
        evidence_verified=True, title="Debug route", rationale="r", fix={"summary": "s", "steps": []},
        standards=[{"framework": "golden-road-vc", "id": "VC-4"}],
    )


def test_agent_findings_are_reverified(root, reqs):
    good, bad = _agent_finding(GOOD), _agent_finding(MISSING_LINE)
    _, findings, flags = _apply([], _assessment(findings=[good, bad]), root)
    assert [f["evidence_verified"] for f in findings] == [True, False]
    assert flags["unverified"] == [{"finding_id": bad["id"]}]


def test_check_assessment():
    doc = _assessment(_r("v5.0.0-8.2.2", "pass", [GOOD]))
    assert check_assessment(doc, TREE) is doc
    with pytest.raises(InputError, match="not " + "e" * 40):
        check_assessment(doc, "e" * 40)
    twice = _assessment(_r("v5.0.0-8.2.2", "pass"), _r("v5.0.0-8.2.2", "fail"))
    with pytest.raises(InputError, match="more than one"):
        check_assessment(twice, TREE)
    forged = copy.deepcopy(_assessment(findings=[_agent_finding(GOOD)]))
    forged["findings"][0]["id"] = "0" * 64
    with pytest.raises(InputError, match="does not match"):
        check_assessment(forged, TREE)
    deterministic = _assessment(findings=[{**_agent_finding(GOOD), "source": {"layer": "deterministic", "tool": "x"}}])
    with pytest.raises(InputError, match="agentic"):
        check_assessment(deterministic, TREE)


def test_scores_and_coverage():
    results = [{"status": s, "chapter": c} for s, c in
               [("pass", "v5.0.0-8"), ("fail", "v5.0.0-8"), ("needs_human", "v5.0.0-8"), ("na", "v5.0.0-10"), ("pass", "v5.0.0-13")]]
    assert counts(results) == {"pass": 2, "fail": 1, "na": 1, "needs_human": 1}
    assert score(counts(results)) == 66.7
    assert coverage(results) == 60.0
    assert coverage([]) is None
    table = chapters(results, {"v5.0.0-8": "Authorization", "v5.0.0-10": "OAuth and OIDC", "v5.0.0-13": "Configuration"})
    assert [(c["chapter"], c["score"]) for c in table] == [("v5.0.0-8", 50.0), ("v5.0.0-10", None), ("v5.0.0-13", 100.0)]


def test_results_validate_against_the_schema(root, reqs):
    from contracts.schema import validate

    entries = [_r("v5.0.0-8.2.2", "pass", [GOOD]), _r("v5.0.0-8.2.1", "fail", [MISSING_LINE]), _r("v5.0.0-16.2.5", "na")]
    results, _, _ = _apply(reqs, _assessment(*entries), root)
    assert [validate("requirement-result.v1", r) for r in results] == [[], [], []]


def test_unverified_agent_fail_is_downgraded_and_flagged(root, reqs):
    """T-12 input: a failed agent citation is flagged even though the fail is downgraded."""
    results, _, flags = _apply(reqs[:1], _assessment(_r("v5.0.0-8.2.2", "fail", [MISSING_LINE])), root)
    assert results[0]["status"] == "needs_human" and results[0]["status_source"] == "downgraded" and results[0]["agent_status"] == "fail"
    assert flags["unverified"] == [{"asvs_id": "v5.0.0-8.2.2"}]


def test_agent_needs_human_stays_and_agent_cannot_claim_verification(root, reqs):
    entry = _r("v5.0.0-8.2.2", "pass", [MISSING_LINE])
    entry["evidence_verified"] = True
    with pytest.raises(InputError):
        check_assessment(_assessment(entry), TREE)
    results, _, _ = _apply(reqs[:1], _assessment(_r("v5.0.0-8.2.2", "needs_human")), root)
    assert results[0]["status"] == "needs_human" and results[0]["status_source"] == "agent"


def test_coverage_excludes_platform_and_tool_covered(root, config):
    from scorecard.scoping import scope

    by_id = {r["id"]: r for r in load_asvs()}
    reqs = [by_id[i] for i in ("v5.0.0-8.2.1", "v5.0.0-1.2.4", "v5.0.0-8.2.2")]
    decided, agent = scope(reqs, config=config, confirmed_na=[], packages=[], has_authz=True, drift_green=True)
    assert [r["id"] for r in agent] == ["v5.0.0-8.2.2"]
    results, _, _ = _apply(agent, _assessment(_r("v5.0.0-8.2.2", "pass", [GOOD])), root)
    assert coverage(results) == 100.0 and coverage_counts(results) == (1, 1)
    assert counts(decided + results) == {"pass": 2, "fail": 0, "na": 0, "needs_human": 1}


def test_apply_assessment_checks_the_assessment_itself(root, reqs):
    twice = _assessment(_r("v5.0.0-8.2.2", "pass", [GOOD]), _r("v5.0.0-8.2.2", "fail", [GOOD]))
    with pytest.raises(InputError, match="more than one"):
        _apply(reqs, twice, root)
    with pytest.raises(InputError, match="not " + "d" * 40):
        apply_assessment(reqs, {**_assessment(), "tree_sha": "e" * 40}, root, config=CONFIG, tree_sha=TREE)
    deterministic = _assessment(findings=[{**_agent_finding(GOOD), "source": {"layer": "deterministic", "tool": "x"}}])
    with pytest.raises(InputError, match="agentic"):
        _apply(reqs, deterministic, root)


def test_unknown_verification_check_is_rejected(root):
    finding = _agent_finding(GOOD)
    finding["standards"] = [{"framework": "golden-road-vc", "id": "VC-9"}]
    with pytest.raises(InputError):
        check_assessment(_assessment(findings=[finding]), TREE)


def test_unknown_finding_ids_are_dropped_and_flagged(root, reqs):
    finding = _agent_finding(GOOD)
    entry = {**_r("v5.0.0-8.2.2", "fail", [GOOD]), "finding_ids": [finding["id"], "f" * 64]}
    results, _, flags = _apply(reqs[:1], _assessment(entry, findings=[finding]), root)
    assert results[0]["finding_ids"] == [finding["id"]]
    assert flags["unverified"] == [{"asvs_id": "v5.0.0-8.2.2"}]


@pytest.mark.parametrize(
    "evidence, reason",
    [
        ([{"file": "app.py", "start_line": 5, "snippet": "return note"}], "shorter than 12"),
        ([GOOD, {"file": "README.md", "start_line": 1, "snippet": "Authorization is enforced for every request"}], "generated, documentation"),
        ([GOOD, {"file": "deploy/notes.txt", "start_line": 1, "snippet": "Authorization is enforced for every request"}], "generated, documentation"),
    ],
)
def test_weak_pass_evidence_is_downgraded_and_flagged(make_app, reqs, evidence, reason):
    root = make_app({"app.py": APP, "README.md": "Authorization is enforced for every request\n", "deploy/notes.txt": "Authorization is enforced for every request\n"})
    results, _, flags = _apply(reqs[:1], _assessment(_r("v5.0.0-8.2.2", "pass", evidence)), root)
    assert results[0]["status"] == "needs_human" and reason in results[0]["downgrade_reason"]
    assert flags["unverified"] == [{"asvs_id": "v5.0.0-8.2.2"}]
    # The same evidence stands for a fail: the strictness is for passes only.
    results, _, _ = _apply(reqs[:1], _assessment(_r("v5.0.0-8.2.2", "fail", evidence)), root)
    assert results[0]["status"] == "fail"


def _planted(root):
    import os

    (root / "_platform").mkdir()
    (root / "_platform" / "authz.py").write_text(APP)
    os.symlink(root / "app.py", root / "alias.py")


@pytest.mark.parametrize("file", ["_platform/authz.py", "alias.py", "node_modules/x.js"])
def test_citations_outside_the_agents_file_list_are_unverified(root, reqs, file):
    """M7: the agent's tools never show `_platform/`, symlinks or vendored files, so a citation into them is unverified (the
    same handling as a failed verify_evidence), even though anchor_ok alone would accept it."""
    _planted(root)
    (root / "node_modules").mkdir()
    (root / "node_modules" / "x.js").write_text(APP)
    cite = {**GOOD, "file": file}
    finding = _agent_finding(cite)
    results, findings, flags = _apply(reqs[:1], _assessment(_r("v5.0.0-8.2.2", "fail", [cite]), findings=[finding]), root)
    assert results[0]["status"] == "needs_human" and results[0]["evidence_verified"] is False
    assert findings[0]["evidence_verified"] is False
    assert {"asvs_id": "v5.0.0-8.2.2"} in flags["unverified"] and {"finding_id": finding["id"]} in flags["unverified"]


def test_a_citation_in_the_file_list_still_verifies(root, reqs):
    _planted(root)
    results, _, flags = _apply(reqs[:1], _assessment(_r("v5.0.0-8.2.2", "fail", [GOOD])), root)
    assert results[0]["status"] == "fail" and flags["unverified"] == []

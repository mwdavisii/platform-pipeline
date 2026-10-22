"""Apply the agent's assessment under code-owned rules (SEC-6). The agent never sets a final status on its own.

- A `pass` needs evidence that verify_evidence confirms and at least medium confidence (FR-5, FR-7).
- A `fail` with unverified evidence becomes `needs_human`.
- An agent-proposed `na` stays `needs_human` until a human confirms it in the factory.
- A requirement the agent didn't assess becomes `needs_human` and marks the assessment incomplete (T-14).
- Every agent citation is re-verified here; the agent's own `evidence_verified` is never trusted.
"""

from contracts.evidence import anchor_ok, verify_evidence
from contracts.findings import finding_id, normalize_snippet
from contracts.schema import validate
from golden_road.io import InputError, error_detail

from .agent.tools import citable_paths
from .level import TRIGGER_FOR_VC
from .scoping import make_result
from .signals.changes import path_matches

MIN_PASS_EVIDENCE_CHARS = 12


def check_assessment(doc, tree_sha: str) -> dict:
    errors = validate("assessment.v1", doc)
    if errors:
        raise InputError(f"the assessment is not a valid assessment/v1: {errors[0]}")
    if doc["tree_sha"] != tree_sha:
        raise InputError(f"the assessment is for tree {doc['tree_sha']}, not {tree_sha}")
    ids = [r["asvs_id"] for r in doc["results"]]
    if len(ids) != len(set(ids)):
        raise InputError("the assessment has more than one result for a requirement")
    for i, finding in enumerate(doc["findings"]):
        if finding["source"]["layer"] != "agentic":
            raise InputError(f"assessment finding {i} is not from the agentic layer")
        expected = finding_id(finding["rule_id"], finding["location"]["file"], [e["snippet"] for e in finding["evidence"]])
        if finding["id"] != expected:
            raise InputError(f"assessment finding {i} has an id that does not match its content: {error_detail(finding, 'id mismatch')}")
        for standard in finding["standards"]:
            if standard["framework"] == "golden-road-vc" and standard["id"] not in TRIGGER_FOR_VC:
                raise InputError(f"assessment finding {i} names an unknown verification check {standard['id']}")
    return doc


def check_approved(doc: dict, config: dict) -> None:
    """O10: only an assessment made with the factory's approved model and prompt version is scored. Anything else is refused
    (an InputError, so `scorecard run` exits 1 with no outputs), the same as an assessment for another tree."""
    agent = config["agent"]
    approved = {"name": agent["model"]["name"], "version": agent["model"]["version"], "temperature": 0}
    if doc["prompt_version"] != agent["prompt_version"] or doc["model"] != approved:
        raise InputError(f"the assessment was made with model {doc['model']['name']!r} version {doc['model']['version']!r} "
                         f"(temperature {doc['model']['temperature']}) and prompt {doc['prompt_version']!r}; the factory config "
                         f"approves {approved['name']!r} version {approved['version']!r} at temperature 0 with prompt {agent['prompt_version']!r}")


def _verified(evidence: list, repo_root, citable: set | None = None) -> bool:
    """Every anchor passes anchor_ok and, when `citable` is given, names a file in the agent's file list (M7): the tools never
    show `_platform/`, vendored files or symlinks, so a citation into one can't have been read by the agent."""
    return bool(evidence) and all(anchor_ok(repo_root, a) and (citable is None or a.get("file") in citable) for a in evidence)


def _weak_pass(evidence: list, config: dict) -> str | None:
    """Scorecard-side strictness for a pass, on top of verify_evidence: enough text, and not generated or documentation files."""
    if sum(len(normalize_snippet(a["snippet"])) for a in evidence) < MIN_PASS_EVIDENCE_CHARS:
        return f"pass whose evidence is shorter than {MIN_PASS_EVIDENCE_CHARS} characters"
    if any(path_matches(a["file"], config["evidence"]["excluded_paths"]) for a in evidence):
        return "pass whose evidence is in a generated, documentation, or deployment file"
    return None


def _result(req: dict, got: dict, repo_root, config: dict, known_findings: set, citable: set | None = None) -> tuple[dict, bool]:
    """Return (result, flagged): flagged results fire T-12 even outside the blocking chapters."""
    evidence = got["evidence"]
    verified = _verified(evidence, repo_root, citable)
    status = got["status"]
    common = dict(justification=got["justification"], confidence=got["confidence"], evidence=evidence, evidence_verified=verified,
                  agent_status=status)
    reason = weak = None
    if status == "pass" and got["confidence"] == "low":
        reason = "low-confidence pass (FR-7)"
    elif status == "pass" and not verified:
        reason = "pass without evidence that verify_evidence confirms"
    elif status == "pass" and (weak := _weak_pass(evidence, config)):
        reason = weak
    elif status == "fail" and not verified:
        reason = "fail with evidence that verify_evidence could not confirm"
    elif status == "na":
        reason = "agent-proposed na; a human must confirm it in the factory na/ file"
    if reason:
        result = make_result(req, "needs_human", "downgraded", downgrade_reason=reason, **common)
    else:
        result = make_result(req, status, "agent", **common)
    ids = list(got.get("finding_ids", []))
    result["finding_ids"] = [i for i in ids if i in known_findings]
    flagged = bool(evidence and not verified) or weak is not None or len(result["finding_ids"]) != len(ids)
    return result, flagged


def apply_assessment(agent_requirements: list[dict], assessment: dict | None, repo_root, *, config: dict,
                     tree_sha: str) -> tuple[list[dict], list[dict], dict]:
    """Return (results, agent findings with evidence re-verified, flags).

    The assessment is checked here (schema, tree, duplicates, agentic layer, finding IDs), so this can't be used on an unchecked one.
    flags: incomplete (bool), unverified (refs to results and findings that failed a code check), ignored (results outside the
    agent's set, which the level engine treats as suspicious).
    """
    if assessment is not None:
        check_assessment(assessment, tree_sha)
    got = {r["asvs_id"]: r for r in assessment["results"]} if assessment else {}
    citable = citable_paths(repo_root) if assessment else None
    known_findings = {f["id"] for f in assessment["findings"]} if assessment else set()
    results, unverified = [], []
    incomplete = assessment is None or not assessment["complete"]
    for req in agent_requirements:
        entry = got.pop(req["id"], None)
        if entry is None:
            incomplete = True
            results.append(make_result(req, "needs_human", "downgraded", confidence="low", downgrade_reason="not assessed by the agent"))
            continue
        result, flagged = _result(req, entry, repo_root, config, known_findings, citable)
        if flagged:
            unverified.append({"asvs_id": req["id"]})
        results.append(result)
    findings = []
    for finding in assessment["findings"] if assessment else []:
        checked = verify_evidence(finding, repo_root)
        if not all(a.get("file") in citable for a in finding["evidence"]):
            checked["evidence_verified"] = False
        if not checked["evidence_verified"]:
            unverified.append({"finding_id": finding["id"]})
        findings.append(checked)
    return results, findings, {"incomplete": incomplete, "unverified": unverified, "ignored": sorted(got)}

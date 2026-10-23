"""Check one batch's submitted output and turn it into assessment/v1 results and finding/v2 findings.

Nothing from the model is trusted: it must be valid agent-batch/v1, answer every requirement in the batch exactly once
and no others, and its findings must become valid finding/v2 documents. Evidence is not checked here; `scorecard run`
re-verifies every citation against the tree (SEC-6).
"""

import json

from contracts.findings import make_finding_v2
from contracts.schema import validate

from .. import VERSION

MAX_OUTPUT_CHARS = 400_000
MAX_ERROR_CHARS = 300
TOOL = "scorecard-agent"
KINDS = ("too_long", "not_json", "schema", "coverage", "finding")


class InvalidOutput(Exception):
    """The model's output can't be used. The message goes back to the model once; for schema and finding errors it can quote
    model-supplied values, so it never goes to the run's stats. `kind` is code-written, one of KINDS, and is safe to log."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


def _finding(draft: dict, now: str) -> dict:
    fields = {k: v for k, v in draft.items() if k != "ref"}
    finding = make_finding_v2(first_seen=now, layer="agentic", tool=TOOL, tool_version=VERSION, evidence_verified=False, **fields)
    errors = validate("finding.v2", finding)
    if errors:
        raise InvalidOutput(f"finding {draft['ref']} is not a valid finding: {errors[0][:MAX_ERROR_CHARS]}", "finding")
    return finding


def parse_batch(arguments, batch_ids: list[str], *, now: str) -> tuple[list[dict], list[dict]]:
    """Return (results, findings) for one batch, or raise InvalidOutput."""
    if not isinstance(arguments, str) or len(arguments) > MAX_OUTPUT_CHARS:
        raise InvalidOutput(f"the submission must be a JSON object of at most {MAX_OUTPUT_CHARS} characters", "too_long")
    try:
        doc = json.loads(arguments)
    except (ValueError, RecursionError):
        raise InvalidOutput("the submission is not valid JSON", "not_json") from None
    errors = validate("agent-batch.v1", doc)
    if errors:
        raise InvalidOutput(f"the submission does not match the schema: {errors[0][:MAX_ERROR_CHARS]}", "schema")
    answered = [r["asvs_id"] for r in doc["results"]]
    wanted = set(batch_ids)
    problems = []
    if len(answered) != len(set(answered)):
        problems.append("a requirement is answered more than once")
    if extra := sorted(set(answered) - wanted):
        problems.append(f"requirements not in this batch: {', '.join(extra[:10])}")
    if missing := [i for i in batch_ids if i not in set(answered)]:
        problems.append(f"requirements not answered: {', '.join(missing[:10])}")
    refs = [f["ref"] for f in doc["findings"]]
    if len(refs) != len(set(refs)):
        problems.append("a finding ref is used more than once")
    if unknown := sorted({ref for r in doc["results"] for ref in r["finding_refs"]} - set(refs)):
        problems.append(f"finding_refs with no finding: {', '.join(unknown[:10])}")
    if problems:
        raise InvalidOutput("; ".join(problems), "coverage")
    findings = {f["ref"]: _finding(f, now) for f in doc["findings"]}
    results = []
    for r in doc["results"]:
        result = {k: r[k] for k in ("asvs_id", "status", "confidence", "justification", "evidence")}
        ids = list(dict.fromkeys(findings[ref]["id"] for ref in r["finding_refs"]))
        if ids:
            result["finding_ids"] = ids
        results.append(result)
    return results, list(findings.values())

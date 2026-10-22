"""Deterministic signals. None of them use the model; each one emits finding/v2 documents."""

from contracts.findings import make_finding_v2
from contracts.sarif import FALLBACK_CWE

from .. import VERSION
from ..redact import redact

TOOL = "scorecard"
MAX_SNIPPET = 300


def signal(*, rule_id: str, category: str, severity: str, file: str, snippet: str, title: str, rationale: str, fix: str,
           now: str, line: int | None = None, vc: str | None = None, trigger: str | None = None, cwe: bool = True) -> dict:
    """One deterministic finding. Its evidence is computed by the scorecard, not quoted by an agent, so it is trusted."""
    standards = []
    if vc:
        standards.append({"framework": "golden-road-vc", "id": vc})
    if trigger:
        standards.append({"framework": "golden-road-trigger", "id": trigger})
    if cwe and category in FALLBACK_CWE:
        standards.append({"framework": "cwe", "id": FALLBACK_CWE[category]})
    # A line from the diff may hold a secret: the stored snippet is redacted (so the artifact never carries one).
    text = redact(" ".join(snippet.split()))[:MAX_SNIPPET] or rule_id
    location = {"kind": "line", "file": file, "start_line": line, "end_line": line} if line else {"kind": "file", "file": file}
    anchor = {"file": file, "start_line": line or 1, "snippet": text}
    if not line:
        anchor["note"] = "computed from the diff, not quoted from the file"
    return make_finding_v2(
        first_seen=now, layer="deterministic", tool=TOOL, tool_version=VERSION, rule_id=rule_id, category=category,
        severity=severity, location=location, evidence=[anchor], evidence_verified=True, title=title, rationale=rationale,
        fix={"summary": fix, "steps": []}, standards=standards,
    )

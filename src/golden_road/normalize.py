"""Scanner SARIF and builder verify output to finding/v1. Secrets get a severity floor and are redacted."""

import hashlib

from contracts.findings import finding_id
from contracts.sarif import SarifError, from_sarif
from contracts.schema import validate

from .io import InputError, error_detail

SEVERITY_ORDER = ["info", "low", "medium", "high", "critical"]
SEVERITY_FLOOR = {"secrets": "high"}
REDACTED = "[redacted]"
MIN_SCRUB_LEN = 8  # shorter values would mangle ordinary text; their snippets are replaced by a digest anyway


def _apply_floor(severity: str, category: str) -> str:
    floor = SEVERITY_FLOOR.get(category)
    if floor and SEVERITY_ORDER.index(severity) < SEVERITY_ORDER.index(floor):
        return floor
    return severity


def _scrub(value, secrets: list[str]):
    """Remove every known secret value from all strings in value."""
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, REDACTED)
        return value
    if isinstance(value, list):
        return [_scrub(v, secrets) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v, secrets) for k, v in value.items()}
    return value


def _redact(finding: dict) -> dict:
    # A scanner can put the secret in the message (which becomes title and rationale), in rule help
    # text (fix), or in the rule id or version. Free text is replaced with fixed wording, and
    # whatever strings remain are scrubbed of the evidence values.
    secrets = sorted({s for a in finding["evidence"] for s in (a["snippet"], a["snippet"].strip()) if len(s) >= MIN_SCRUB_LEN},
                     key=len, reverse=True)
    # Only free-text fields are scrubbed; enum-like fields (layer, tool, kind, category) are left alone.
    source = dict(finding["source"])
    if "tool_version" in source:
        source["tool_version"] = _scrub(source["tool_version"], secrets)
    base = {"source": source, "rule_id": _scrub(finding["rule_id"], secrets),
            "location": dict(finding["location"], file=_scrub(finding["location"]["file"], secrets))}
    anchors = []
    for anchor in finding["evidence"]:
        material = f"{anchor['file']}\x00{anchor['start_line']}\x00{anchor['snippet']}"
        digest = hashlib.sha256(material.encode("utf-8", "surrogatepass")).hexdigest()
        anchors.append({"file": _scrub(anchor["file"], secrets), "start_line": anchor["start_line"],
                        "snippet": f"sha256:{digest} {base['rule_id']}", "note": "secret value redacted"})
    out = dict(
        finding,
        **base,
        evidence=anchors,
        title=f"Secret detected by {base['rule_id']}"[:200],
        rationale="A secret value was detected in the source (value redacted).",
        fix={"summary": "Revoke and rotate the secret, then remove it from the repository and its history.", "steps": []},
    )
    out["id"] = finding_id(out["rule_id"], out["location"]["file"], [a["snippet"] for a in anchors])
    return out


def normalize_sarif(sarif: dict, *, category: str, repo_root) -> list[dict]:
    try:
        findings = from_sarif(sarif, category=category, repo_root=repo_root)
    except SarifError as exc:
        raise InputError(f"bad SARIF: {exc}") from exc
    out = []
    for finding in findings:
        finding = dict(finding, severity=_apply_floor(finding["severity"], category))
        if category == "secrets":
            finding = _redact(finding)
        errors = validate("finding.v1", finding)
        if errors:
            raise InputError(f"normalized finding is invalid: {error_detail(finding, errors[0])}")
        out.append(finding)
    return out


def from_builder_result(result) -> list[dict]:
    errors = validate("builder-result.v1", result)
    if errors:
        raise InputError(f"not a builder result: {errors[0]}")
    if result["command"] != "verify":
        raise InputError("expected a builder verify result")
    if result["status"] not in ("ok", "drift"):
        raise InputError(f"builder verify reported status {result['status']!r}")
    findings = result.get("findings", [])
    if result["status"] == "drift" and not findings:
        raise InputError("builder verify reported drift without findings")
    for i, finding in enumerate(findings):
        errors = validate("finding.v1", finding)
        if errors:
            raise InputError(f"builder finding {i} is invalid: {error_detail(result["findings"][i], errors[0])}")
    return findings

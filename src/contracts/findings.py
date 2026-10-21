"""Stable finding fingerprints and a constructor for finding/v1."""

import hashlib
import json
import re

_WHITESPACE = re.compile(r"\s+")


def normalize_snippet(snippet: str) -> str:
    return _WHITESPACE.sub(" ", snippet).strip()


def finding_id(rule_id: str, file: str, snippets: list[str]) -> str:
    """Fingerprint on rule, file, and normalized evidence. Line numbers are excluded so the ID survives line shifts."""
    parts = [rule_id, file.replace("\\", "/"), sorted(normalize_snippet(s) for s in snippets)]
    encoded = json.dumps(parts, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def make_finding(
    *,
    layer: str,
    tool: str,
    rule_id: str,
    category: str,
    severity: str,
    location: dict,
    evidence: list[dict],
    title: str,
    rationale: str,
    fix: dict,
    standards: list[dict],
    confidence: str = "high",
    evidence_verified: bool,
    tool_version: str | None = None,
) -> dict:
    source = {"layer": layer, "tool": tool}
    if tool_version:
        source["tool_version"] = tool_version
    return {
        "id": finding_id(rule_id, location["file"], [e["snippet"] for e in evidence]),
        "source": source,
        "rule_id": rule_id,
        "category": category,
        "severity": severity,
        "confidence": confidence,
        "location": location,
        "evidence": evidence,
        "evidence_verified": evidence_verified,
        "title": title,
        "rationale": rationale,
        "fix": fix,
        "standards": standards,
    }

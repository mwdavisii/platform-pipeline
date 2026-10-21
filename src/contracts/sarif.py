"""Convert scanner SARIF 2.1.0 into finding/v1, and finding/v1 back into SARIF for the PR UI.

Callers must treat SarifError as a gate failure: a missing or broken report must never look clean.
"""

import math
import posixpath
import re
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from .findings import make_finding
from .schema import validate

SEVERITY_SCORE = {"critical": 9.5, "high": 8.0, "medium": 5.5, "low": 2.0, "info": 0.0}
_LEVEL_FOR_SEVERITY = {"critical": "error", "high": "error", "medium": "warning", "low": "note", "info": "note"}
_SEVERITY_FOR_LEVEL = {"error": "high", "warning": "medium", "note": "low", "none": "info"}
_SKIPPED_KINDS = {"pass", "notApplicable", "informational"}
_CWE_TAG = re.compile(r"\bcwe-0*(\d+)\b", re.I)

FALLBACK_CWE = {
    "secrets": "CWE-798",
    "sast": "CWE-710",
    "sca": "CWE-1395",
    "license": "CWE-1357",
    "image": "CWE-1395",
    "iac": "CWE-1188",
    "drift": "CWE-693",
    "egress": "CWE-941",
    "authz": "CWE-285",
    "harness": "CWE-1426",
    "data-handling": "CWE-200",
    "dependency": "CWE-1357",
}


class SarifError(ValueError):
    """The SARIF document is structurally unusable or produced an invalid finding."""


def _d(value) -> dict:
    return value if isinstance(value, dict) else {}


def _s(value) -> str:
    return value if isinstance(value, str) else ""


def _severity(result: dict, rule: dict) -> str:
    props = {**_d(rule.get("properties")), **_d(result.get("properties"))}
    score = props.get("security-severity")
    try:
        value = float(score)
    except (TypeError, ValueError):
        value = math.nan
    if math.isfinite(value):
        if value >= 9.0:
            return "critical"
        if value >= 7.0:
            return "high"
        if value >= 4.0:
            return "medium"
        if value > 0.0:
            return "low"
        return "info"
    level = result.get("level") or _d(rule.get("defaultConfiguration")).get("level") or "warning"
    return _SEVERITY_FOR_LEVEL.get(level, "medium") if isinstance(level, str) else "medium"


def _cwe(rule: dict, category: str) -> str:
    tags = _d(rule.get("properties")).get("tags")
    for tag in tags if isinstance(tags, list) else []:
        match = _CWE_TAG.search(_s(tag))
        if match and int(match.group(1)) > 0:
            return f"CWE-{match.group(1)}"
    return FALLBACK_CWE[category]


def _line(value) -> int | None:
    return value if type(value) is int and value >= 1 else None


def _uri_path(uri: str) -> str:
    parts = urlsplit(uri)
    if parts.scheme not in ("", "file"):
        raise SarifError(f"unsupported artifact URI scheme: {parts.scheme!r}")
    if parts.scheme == "file" and parts.netloc not in ("", "localhost"):
        raise SarifError(f"file URI names a remote host: {parts.netloc!r}")
    return unquote(parts.path)


def _artifact_path(artifact: dict, run: dict, repo_root) -> str:
    """Resolve an artifact URI to a normalized repo-relative path, or raise SarifError."""
    uri = _s(artifact.get("uri"))
    if not uri:
        return "<unknown>"
    path = _uri_path(uri)
    base_id = artifact.get("uriBaseId")
    bases = _d(run.get("originalUriBaseIds"))
    if isinstance(base_id, str) and base_id in bases:
        base = _uri_path(_s(_d(bases[base_id]).get("uri")))
        path = posixpath.join(base, path)
    path = posixpath.normpath(path.replace("\\", "/"))
    if path.startswith("/"):
        root = posixpath.normpath(str(Path(repo_root).resolve())) if repo_root is not None else None
        try:
            if root is None:
                raise ValueError
            path = PurePosixPath(path).relative_to(root).as_posix()
        except ValueError:
            raise SarifError(f"absolute path outside the repo: {path!r}") from None
        path = posixpath.normpath(path)
    if path == ".":
        raise SarifError("artifact URI does not name a file")
    return path


def _rules(driver: dict, tool: dict) -> tuple[list, dict]:
    """Driver rules (indexable by ruleIndex) plus rules from every extension, keyed by id."""
    driver_rules = [r for r in (driver.get("rules") or []) if isinstance(r, dict)] if isinstance(driver.get("rules"), list) else []
    by_id = {}
    extensions = tool.get("extensions") if isinstance(tool.get("extensions"), list) else []
    for source in [driver, *extensions]:
        rules = _d(source).get("rules")
        for rule in rules if isinstance(rules, list) else []:
            if isinstance(rule, dict) and isinstance(rule.get("id"), str):
                by_id.setdefault(rule["id"], rule)
    return driver_rules, by_id


def from_sarif(sarif: dict, *, category: str, repo_root=None) -> list[dict]:
    """Normalize every failing result in every run. Scanner output is trusted, so evidence is marked verified."""
    if category not in FALLBACK_CWE:
        raise SarifError(f"unknown category: {category!r}")
    if not isinstance(sarif, dict):
        raise SarifError("SARIF document must be an object")
    findings = []
    runs = sarif.get("runs")
    if not isinstance(runs, list) or not runs:
        raise SarifError("SARIF document has no runs")
    for run in runs:
        if not isinstance(run, dict):
            raise SarifError("run must be an object")
        tool = _d(run.get("tool"))
        driver = _d(tool.get("driver"))
        if not _s(driver.get("name")):
            raise SarifError("run is missing tool.driver.name")
        driver_rules, rules = _rules(driver, tool)
        results = run.get("results", [])
        if not isinstance(results, list):
            raise SarifError("run.results must be a list")
        for result in results:
            if not isinstance(result, dict):
                raise SarifError("every result must be an object")
            kind = result.get("kind")
            if (isinstance(kind, str) and kind in _SKIPPED_KINDS) or result.get("baselineState") == "absent":
                continue
            # Results carrying `suppressions` are imported anyway: in-source suppressions
            # must not silence the gate; only infosec overrides can.
            rule_id = _s(result.get("ruleId")) or _s(_d(result.get("rule")).get("id"))
            index = result.get("ruleIndex")
            if not rule_id and type(index) is int and 0 <= index < len(driver_rules):
                rule_id = _s(driver_rules[index].get("id"))
            rule_id = rule_id or "unknown"
            rule = rules.get(rule_id) or {}
            message = _s(_d(result.get("message")).get("text")).strip() or rule_id
            locations = result.get("locations")
            physical = _d(_d(locations[0] if isinstance(locations, list) and locations else None).get("physicalLocation"))
            file = _artifact_path(_d(physical.get("artifactLocation")), run, repo_root)
            region = _d(physical.get("region"))
            line = _line(region.get("startLine"))
            snippet = _s(_d(region.get("snippet")).get("text")).strip() or message
            if line:
                location = {"kind": "line", "file": file, "start_line": line, "end_line": _line(region.get("endLine")) or line}
            else:
                location = {"kind": "file", "file": file}
            version = driver.get("semanticVersion") or driver.get("version")
            finding = make_finding(
                layer="deterministic",
                tool=driver["name"],
                tool_version=version if isinstance(version, str) else None,
                rule_id=rule_id,
                category=category,
                severity=_severity(result, rule),
                confidence="low" if kind in ("open", "review") else "high",
                location=location,
                evidence=[{"file": file, "start_line": line or 1, "snippet": snippet}],
                title=(_s(_d(rule.get("shortDescription")).get("text")) or message)[:200],
                rationale=message,
                fix={"summary": _s(_d(rule.get("help")).get("text")) or "See the scanner rule documentation.", "steps": []},
                standards=[{"framework": "cwe", "id": _cwe(rule, category)}],
                evidence_verified=True,
            )
            errors = validate("finding.v1", finding)
            if errors:
                raise SarifError(f"result for rule {rule_id!r} is not a valid finding: {'; '.join(errors)}")
            findings.append(finding)
    return findings


def _tag(standard: dict) -> str:
    return standard["id"] if standard["framework"] == "cwe" else f"{standard['framework']}:{standard['id']}"


def to_sarif(findings: list[dict], *, tool_name: str = "golden-road") -> dict:
    rules, results = {}, []
    for f in findings:
        score = str(SEVERITY_SCORE[f["severity"]])
        rules.setdefault(
            f["rule_id"],
            {
                "id": f["rule_id"],
                "shortDescription": {"text": f["title"]},
                "help": {"text": f["fix"]["summary"]},
                "properties": {"tags": [_tag(s) for s in f["standards"]], "security-severity": score},
            },
        )
        anchor = f["evidence"][0]
        if f["location"]["kind"] == "line":
            file, line = f["location"]["file"], f["location"]["start_line"]
        else:
            file, line = anchor["file"], anchor["start_line"]
        results.append(
            {
                "ruleId": f["rule_id"],
                "level": _LEVEL_FOR_SEVERITY[f["severity"]],
                "message": {"text": f"{f['title']}: {f['rationale']}"},
                "locations": [
                    {"physicalLocation": {"artifactLocation": {"uri": file}, "region": {"startLine": line, "snippet": {"text": anchor["snippet"]}}}}
                ],
                "partialFingerprints": {"platformFindingId/v1": f["id"]},
                "properties": {
                    "security-severity": score,
                    "category": f["category"],
                    "confidence": f["confidence"],
                    "layer": f["source"]["layer"],
                },
            }
        )
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{"tool": {"driver": {"name": tool_name, "rules": list(rules.values())}}, "results": results}],
    }

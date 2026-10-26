"""The facts code computes: the built-in detectors (the deterministic signals that exist today), builder-spec.json
measures at base and head, and diff patterns. Agent facts come from answers.py.

Every fact is a dict with `kind`, `type`, `value` and `refs` (scorecard reason refs). Spec facts add `base` and `head`
(and `order` for ordered values, `added` for lists); built-in facts add `params` for their rules' placeholders.
"""

import json
import re

from ..agent.prompt import _marker_path
from ..level import triggers_of
from ..signals.changes import path_matches
from ..signals.registration import below_floor
from ..signals.spec_diff import parse_spec
from .config import BUILTIN, ENUMS, SPEC

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# The trigger each deterministic signal already carries, and the built-in fact it feeds.
BUILTIN_BY_TRIGGER = {"T-2": "spec_security_change", "T-3": "tests_weakened", "T-5": "risky_dependency", "T-7": "reviewer_text",
                      "T-9": "purpose_changed", "T-15": "suppression_added", "T-16": "workflow_change", "T-17": "tier_raised",
                      "T-18": "app_type_changed"}
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _unique(refs: list[dict]) -> list[dict]:
    out = []
    for ref in refs:
        if ref not in out:
            out.append(ref)
    return out


def finding_ref(finding: dict) -> dict:
    ref = {"finding_id": finding["id"], "file": finding["location"]["file"]}
    if finding["location"].get("start_line"):
        ref["line"] = finding["location"]["start_line"]
    return ref


def builtin_facts(*, findings, pr_hits, problems, lookup_failures, change_size, large_change, intake, registration,
                  assessment_complete: bool) -> dict[str, dict]:
    """The built-in facts, keyed by measure. `findings` are the suppression-filtered deterministic signals."""
    refs = {measure: [] for measure in BUILTIN}
    params = {"large_change": {"lines": "unknown", "files": "unknown"},
              "below_registration": {"registered": "unknown", "needed": "unknown", "signals": "none"}}
    for finding in findings:
        for trigger in sorted(triggers_of(finding)):
            if trigger in BUILTIN_BY_TRIGGER:
                refs[BUILTIN_BY_TRIGGER[trigger]].append(finding_ref(finding))
    refs["reviewer_text"] += [{"source": hit["source"]} for hit in pr_hits]
    if intake is None:
        refs["no_intake"].append({"source": "factory intake"})
    refs["input_problem"] += [{"source": failure["source"]} for failure in [*lookup_failures, *problems]]
    if not assessment_complete:
        refs["review_incomplete"].append({"source": "agent assessment"})
    if change_size:
        params["large_change"] = {"lines": change_size["lines"], "files": change_size["files"]}
        if change_size["lines"] > large_change["lines"] or change_size["files"] > large_change["files"]:
            refs["large_change"].append({"source": "the base and head diff"})
    if intake is not None and registration:
        params["below_registration"] = {"registered": f"{intake['tier']}/{intake['data_classification']}",
                                        "needed": f"{registration['tier']}/{registration['data_classification']}",
                                        "signals": ", ".join(registration["rules"]) or "none"}
        if below_floor(intake["tier"], intake["data_classification"], registration["tier"], registration["data_classification"]):
            refs["below_registration"] += [{"source": rule} for rule in registration["rules"] or ["the computed registration floor"]]
    return {measure: {"kind": "builtin", "type": "bool", "value": bool(found), "refs": _unique(found), "params": params.get(measure, {})}
            for measure, found in refs.items()}


def _spec_values(spec: dict | None, gateway_hosts) -> dict:
    if spec is None:
        return {measure: None for measure in SPEC}
    authz = spec.get("authz") or {}
    roles = authz.get("roles", [])
    access = {r["name"]: r["access"] for r in roles}
    grants = []
    for rule in authz.get("rules", []):
        writes = sorted(WRITE_METHODS.intersection(rule["methods"]))
        readers = sorted(role for role in rule["roles"] if access.get(role) == "read")
        if writes and readers:
            grants.append({"path": rule["path"], "methods": writes, "roles": readers, "justification": rule.get("justification", "")})
    gateways = set(gateway_hosts)
    egress = sorted({e["host"] for e in spec.get("egress", []) if e["host"] not in gateways and f"{e['host']}:{e['port']}" not in gateways})
    return {
        "data_classification": spec.get("data_classification"), "resources_tier": spec.get("resources_tier", ENUMS["resources_tier"][0]),
        "app_type": spec.get("app_type"), "role_count": len(roles), "write_role_count": sum(r["access"] == "write" for r in roles),
        "read_role_write_grants": grants, "secret_env": sorted(e["name"] for e in spec.get("env", []) if e.get("secret")),
        "egress_hosts": egress,
    }


def _key(item) -> str:
    return json.dumps(item, sort_keys=True)


def spec_facts(base_text: str | None, head_text: str | None, gateway_hosts) -> dict[str, dict]:
    """builder-spec.json measures at base and head. A missing or invalid spec has no values (None); T-2 covers an invalid one."""
    base, head = _spec_values(parse_spec(base_text)[0], gateway_hosts), _spec_values(parse_spec(head_text)[0], gateway_hosts)
    out = {}
    for measure, kind in SPEC.items():
        fact = {"kind": "spec", "type": kind, "value": head[measure], "base": base[measure], "head": head[measure],
                "refs": [{"file": "builder-spec.json"}]}
        if kind == "ordered":
            fact["order"] = list(ENUMS[measure])
        if kind in ("list", "grants"):
            before = {_key(item) for item in base[measure] or []}
            fact["added"] = [item for item in head[measure] or [] if _key(item) not in before]
        out[measure] = fact
    return out


def added_lines(diff_text: str) -> dict[str, list[tuple[int, str]]]:
    """Every added line of a unified diff, by head path, with its head line number. A part whose path can't be read is skipped."""
    out: dict[str, list[tuple[int, str]]] = {}
    path, line, in_header = None, 0, False
    for text in diff_text.split("\n"):
        if text.startswith("diff --git "):
            path, in_header = None, True
        elif in_header and text.startswith("+++ "):  # only before the first hunk: an added line can look like "+++ x"
            try:
                path = _marker_path(text, "b/")
            except ValueError:
                path = None
        elif text.startswith("@@"):
            in_header = False
            match = _HUNK.match(text)
            line = int(match.group(1)) if match else 0
        elif path and text.startswith("+"):
            out.setdefault(path, []).append((line, text[1:]))
            line += 1
        elif path and text.startswith(" "):
            line += 1
    return out


def diff_fact(cfg: dict, changed: list[dict], added: dict) -> dict:
    """True when a changed path matches `paths` and, with `added_lines`, an added line in it matches one of those regexes."""
    paths = [c["path"] for c in changed if path_matches(c["path"], cfg["paths"])]
    patterns = [re.compile(p, re.I) for p in cfg.get("added_lines", [])]
    if not patterns:
        refs = [{"file": p} for p in paths]
    else:
        refs = [{"file": p, "line": n} for p in paths for n, text in added.get(p, []) if any(rx.search(text) for rx in patterns)]
    return {"kind": "diff", "type": "bool", "value": bool(refs), "refs": refs}


def compute_facts(routing: dict, *, builtin: dict, spec: dict, changed: list[dict], added: dict, agent: dict) -> dict[str, dict]:
    """Every fact in routing.toml, by name."""
    out = {}
    for name, cfg in routing["facts"].items():
        kind = cfg["from"]
        if kind == "builtin":
            out[name] = builtin[cfg["measure"]]
        elif kind == "spec":
            out[name] = spec[cfg["measure"]]
        elif kind == "diff":
            out[name] = diff_fact(cfg, changed, added)
        else:
            out[name] = agent[name]
    return out

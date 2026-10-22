"""T-2: security-relevant builder-spec.json changes, from a deterministic diff of the base and head specs."""

import json

from contracts.schema import validate

from ..levels import CLASSIFICATIONS
from . import signal

SPEC = "builder-spec.json"
FIX = "Have the security team review this builder-spec.json change before merging."


def parse_spec(text: str | None) -> tuple[dict | None, bool]:
    """(spec, present). An unreadable or invalid spec is (None, True): present but unusable."""
    if text is None:
        return None, False
    try:
        spec = json.loads(text)
    except (ValueError, RecursionError):
        return None, True
    return (spec, True) if not validate("builder-spec.v1", spec) else (None, True)


def _grants(spec: dict) -> set[tuple[str, str, str]]:
    authz = spec.get("authz") or {}
    return {(m, rule["path"], role) for rule in authz.get("rules", []) for m in rule["methods"] for role in rule["roles"]}


def _access(spec: dict) -> dict[str, str]:
    return {r["name"]: r["access"] for r in (spec.get("authz") or {}).get("roles", [])}


def _secrets(spec: dict) -> set[str]:
    return {e["name"] for e in spec.get("env", []) if e["secret"]}


def _egress(spec: dict) -> set[tuple[str, int]]:
    return {(e["host"], e["port"]) for e in spec.get("egress", [])}


def _list(items) -> str:
    items = sorted(items)
    return ", ".join(items[:10]) + (f" and {len(items) - 10} more" if len(items) > 10 else "")


def spec_changes(base_text: str | None, head_text: str | None, *, now: str) -> list[dict]:
    base, base_present = parse_spec(base_text)
    head, head_present = parse_spec(head_text)
    out = []

    def add(rule, category, snippet, title):
        out.append(signal(rule_id=f"T-2/{rule}", category=category, severity="high", file=SPEC, snippet=snippet, title=title,
                          rationale="builder-spec.json controls authz, egress, data classification, and secrets for the whole app.",
                          fix=FIX, now=now, trigger="T-2"))

    if head is None:
        if base_present or head_present:
            add("spec-removed-or-invalid", "iac", "builder-spec.json is missing or invalid in this change", "builder-spec.json removed or invalid")
        return out
    if base is None and base_present:
        # The base spec exists but can't be parsed, so no comparison is possible: fail toward scrutiny, never `{}`.
        add("spec-unreadable", "iac", "the base builder-spec.json is invalid, so this change can't be compared with it",
            "base builder-spec.json unreadable")
        return out
    old = base or {}
    if old.get("authz") and not head.get("authz"):
        add("authz-removed", "authz", "authz block removed", "authz removed from builder-spec.json")
    elif old.get("authz"):
        added = _grants(head) - _grants(old)
        removed = _grants(old) - _grants(head)
        if added:
            add("authz-widened", "authz", "grants added: " + _list(f"{m} {p} -> {r}" for m, p, r in added), "authz grants widened")
        if removed:
            add("authz-rule-removed", "authz", "grants removed: " + _list(f"{m} {p} -> {r}" for m, p, r in removed), "authz rules removed")
        widened = sorted(n for n, a in _access(head).items() if a == "write" and _access(old).get(n) == "read")
        if widened:
            add("role-widened", "authz", "roles changed from read to write: " + _list(widened), "authz role widened to write")
    if "health_path" in head and head["health_path"] != old.get("health_path"):
        # The builder adds health_path to the authz exempt_paths, which allow anonymous GET.
        add("exempt-path-changed", "authz", f"health_path (exempt from authz) is now {head['health_path']}",
            "authz-exempt health path changed")
    new_egress = _egress(head) - _egress(old)
    if new_egress:
        add("egress-added", "egress", "egress added: " + _list(f"{h}:{p}" for h, p in new_egress), "new egress in builder-spec.json")
    before, after = old.get("data_classification"), head["data_classification"]
    if before and CLASSIFICATIONS.index(after) < CLASSIFICATIONS.index(before):
        add("classification-lowered", "data-handling", f"data_classification lowered from {before} to {after}", "data classification lowered")
    if _secrets(head) != _secrets(old):
        changed = [f"+{n}" for n in _secrets(head) - _secrets(old)] + [f"-{n}" for n in _secrets(old) - _secrets(head)]
        add("secret-env-changed", "iac", "secret env vars changed: " + _list(changed), "secret env vars changed")
    return out


def head_spec(head_text: str | None) -> dict | None:
    return parse_spec(head_text)[0]

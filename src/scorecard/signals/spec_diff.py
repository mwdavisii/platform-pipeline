"""builder-spec.json changes, from a deterministic diff of the base and head specs: T-2 (security-relevant), T-17 (resource tier raised), T-18 (app type changed), and the recorded-only signals of the re-review event."""

import json

from contracts.schema import validate

from ..levels import CLASSIFICATIONS
from . import signal

SPEC = "builder-spec.json"
FIX = "Have the security team review this builder-spec.json change before merging."

# builder-spec.v1 resources_tier, lowest first. test_the_tier_order_is_the_builder_spec_enum keeps it in step with the schema.
TIER_ORDER = ("default", "medium")


def tier_raised(before: str | None, after: str) -> bool:
    """T-17: the tier went up. No base spec counts as the lowest tier. A tier this list doesn't know fails closed: it fires."""
    before = TIER_ORDER[0] if before is None else before
    if before not in TIER_ORDER or after not in TIER_ORDER:
        return True
    return TIER_ORDER.index(after) > TIER_ORDER.index(before)


def tier_lowered(before: str | None, after: str) -> bool:
    """`resources-lowered`, recorded only: both tiers known and the head's is lower."""
    return before in TIER_ORDER and after in TIER_ORDER and TIER_ORDER.index(after) < TIER_ORDER.index(before)


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

    def add_trigger(trigger, rule, snippet, title, rationale, fix):
        out.append(signal(rule_id=f"{trigger}/{rule}", category="iac", severity="medium", file=SPEC, snippet=snippet, title=title,
                          rationale=rationale, fix=fix, now=now, trigger=trigger))

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
    before_tier, after_tier = old.get("resources_tier"), head["resources_tier"]
    if tier_raised(before_tier, after_tier):
        add_trigger("T-17", "resources-raised", f"resources_tier raised from {before_tier or TIER_ORDER[0]} to {after_tier}",
                    "Resource tier raised", "A larger resource tier gives the app more CPU and memory on the shared cluster.",
                    "Have a reviewer confirm the app needs the larger resource tier.")
    if old.get("app_type") and head["app_type"] != old["app_type"]:
        add_trigger("T-18", "app-type-changed", f"app_type changed from {old['app_type']} to {head['app_type']}",
                    "App type changed: new base image and runtime",
                    "The builder picks the base image and runtime from app_type, so this change replaces both.",
                    "Have a reviewer check the app on its new runtime and base image.")
    return out


def _recorded(signal_id: str, summary: str) -> dict:
    return {"id": signal_id, "kind": "recorded", "summary": summary}


def recorded_spec_signals(base_text: str | None, head_text: str | None) -> list[dict]:
    """Recorded-only signals for the re-review event: they raise no trigger and no level. Both specs must be readable; anything
    else is T-2's business. The summaries hold only host:port values and tier names from the specs, never model text."""
    base, _ = parse_spec(base_text)
    head, _ = parse_spec(head_text)
    if base is None or head is None:
        return []
    out = []
    gone = _egress(base) - _egress(head)
    if gone:
        out.append(_recorded("egress-removed", "egress removed: " + _list(f"{h}:{p}" for h, p in gone)))
    before, after = base["resources_tier"], head["resources_tier"]
    if tier_lowered(before, after):
        out.append(_recorded("resources-lowered", f"resources_tier lowered from {before} to {after}"))
    return out


def head_spec(head_text: str | None) -> dict | None:
    return parse_spec(head_text)[0]

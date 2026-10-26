"""Security's rules over the facts. An enforced rule that fires raises the level; a shadow rule is only recorded. Nothing
here lowers a level: the result is the highest of the intake floor and every enforced rule that fired.

A rule's `when` fires when any of its conditions is true. When none is true but one is unsure (an agent answer code could
not accept), the rule's `on_unsure` decides: route at elevated, route at the rule's level, or record it as undecided.
"""

from ..levels import highest, rank
from .config import conditions

MAX_REFS = 20
UNSURE_PREFIX = "Not determined: "


def _number(value) -> int:
    return len(value) if isinstance(value, list) else value


def _check(fact: dict, op: str, value) -> bool | None:
    """True, False, or None when the fact is an agent answer code could not accept."""
    if fact["type"] == "answer":
        return None if fact["value"] is None else fact["value"] is value
    if op == "changed":
        base, head = fact["base"], fact["head"]
        if value == "any":
            return base != head
        if value == "added":
            return bool(fact.get("added"))
        if head is None:
            return False
        if fact["type"] == "number":
            return head > (base or 0)
        order = fact["order"]
        if head not in order or (base is not None and base not in order):
            return True  # a value the order doesn't know fails closed
        return order.index(head) > (order.index(base) if base is not None else 0)
    current = fact["value"]
    if current is None:
        return False
    if op == "is":
        return current == value
    if op == "over":
        return _number(current) > value
    if op == "at_least":
        return _number(current) >= value
    if isinstance(current, list):  # any
        return bool(set(current) & set(value))
    return current in value


def _shown(value) -> str:
    if value is None:
        return "none"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "none"
    return str(value)


def _values(fact: dict) -> dict:
    values = {"count": len(fact["refs"])}
    if fact["kind"] == "builtin":
        values.update(fact["params"])
    elif fact["kind"] == "spec":
        values.update(value=_shown(fact["value"]), base=_shown(fact["base"]), head=_shown(fact["head"]))
        if fact["type"] in ("list", "grants"):
            values["count"] = len(fact["value"] or [])
        if fact["type"] == "grants":
            grants = fact.get("added") or fact["value"] or []
            values["justification"] = "; ".join(g["justification"] or "no justification given" for g in grants) or "none"
    elif fact["kind"] == "agent":
        values["answer"] = {True: "yes", False: "no", None: "unsure"}[fact["value"]]
    return values


def _unique(refs) -> list[dict]:
    out = []
    for ref in refs:
        if ref not in out:
            out.append(ref)
    return out


def evaluate(routing: dict, facts: dict, *, classification: str | None, floor: str) -> tuple[str, list[dict], list[dict], list[dict]]:
    """(level, reasons, shadow, undecided)."""
    reasons, shadow, undecided = [], [], []
    for rule in routing["rules"]:
        hits, unsure = [], []
        for name, op, value in conditions(rule):
            got = _check(facts[name], op, value)
            if got:
                hits.append((name, facts[name]))
            elif got is None:
                unsure.append((name, facts[name]))
        if not hits and not unsure:
            continue
        is_unsure = not hits
        chosen = hits or unsure
        why = [f"{name}: {fact.get('why') or 'unsure'}" for name, fact in unsure] if is_unsure else []
        level = rule["level"]
        if is_unsure and rule["on_unsure"] == "standard":
            undecided.append({"trigger": rule["id"], "undetermined": why})
            continue
        if is_unsure and rule["on_unsure"] == "elevated":
            level = "elevated"
        elif (escalate := rule.get("escalate")) and classification in escalate["classifications"] and rank(escalate["level"]) > rank(level):
            level = escalate["level"]
        refs = _unique(ref for _, fact in chosen for ref in fact["refs"]) or [{"source": chosen[0][0]}]
        values = _values(chosen[0][1])
        summary = (UNSURE_PREFIX if is_unsure else "") + rule["summary"].format(**values)
        if len(refs) > MAX_REFS:
            summary += f" (showing {MAX_REFS} of {len(refs)})"
        elif len(refs) > 1:
            summary += f" ({len(refs)} items)"
        entry = {"trigger": rule["id"], "level": level, "summary": summary, "refs": refs[:MAX_REFS], "total": len(refs),
                 "reason": rule["reason"].format(**values), "guidance": rule["guidance"].format(**values), "unsure": is_unsure}
        if is_unsure:
            entry["undetermined"] = why
        (shadow if rule.get("mode", "enforce") == "shadow" else reasons).append(entry)
    number = lambda entry: int(entry["trigger"][2:])  # noqa: E731
    reasons.sort(key=number)
    shadow.sort(key=number)
    return highest([floor] + [r["level"] for r in reasons]), reasons, shadow, undecided

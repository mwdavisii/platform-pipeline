"""config/routing.toml: security's routing policy. Read with tomllib, checked against routing/v1 and then by code, and
merged with scorecard.yaml's mechanics into the one config dict the detectors already read.

What code checks beyond the schema: rule ids are unique; every condition names a fact and fits its type; `on_unsure` is
on exactly the rules over an agent fact; every placeholder in a rule's texts can be filled by every fact the rule names;
every regex compiles. A fact no rule uses is a warning, not an error.
"""

import hashlib
import json
import re
import string
import tomllib
from pathlib import Path

from contracts.schema import validate
from golden_road.io import InputError

from ..levels import CLASSIFICATIONS
from ..signals.spec_diff import TIER_ORDER

ROUTING_FILE = "routing.toml"
MAX_ROUTING_BYTES = 262_144
# Built-in measures, and the placeholders each fills besides {count}.
BUILTIN = {
    "spec_security_change": (), "tests_weakened": (), "risky_dependency": (), "reviewer_text": (), "no_intake": (),
    "purpose_changed": (), "input_problem": (), "review_incomplete": (), "suppression_added": (), "workflow_change": (),
    "tier_raised": (), "app_type_changed": (), "large_change": ("lines", "files"),
    "below_registration": ("registered", "needed", "signals"),
}
# builder-spec.json measures and their value types.
SPEC = {
    "data_classification": "ordered", "resources_tier": "ordered", "app_type": "enum", "role_count": "number",
    "write_role_count": "number", "read_role_write_grants": "grants", "secret_env": "list", "egress_hosts": "list",
}
ENUMS = {"data_classification": CLASSIFICATIONS, "resources_tier": TIER_ORDER, "app_type": ("python", "node", "static")}
# For each value type: the operators it accepts, and the `changed` values among them.
OPERATORS = {
    "bool": {"is": None},
    "answer": {"is": None},
    "enum": {"is": None, "any": None, "changed": {"any"}},
    "ordered": {"is": None, "any": None, "changed": {"any", "up"}},
    "number": {"over": None, "at_least": None, "changed": {"any", "up"}},
    "list": {"over": None, "at_least": None, "any": None, "changed": {"any", "added"}},
    "grants": {"over": None, "at_least": None, "changed": {"any", "added"}},
}
TEXT_FIELDS = ("summary", "reason", "guidance")


def fact_type(fact: dict) -> str:
    kind = fact["from"]
    if kind in ("builtin", "diff"):
        return "bool"
    if kind == "agent":
        return "answer"
    return SPEC[fact["measure"]]


def placeholders(fact: dict) -> set[str]:
    names = {"count"}
    if fact["from"] == "builtin":
        names |= set(BUILTIN[fact["measure"]])
    elif fact["from"] == "spec":
        names |= {"value", "base", "head"}
        if fact["measure"] == "read_role_write_grants":
            names.add("justification")
    elif fact["from"] == "agent":
        names.add("answer")
    return names


def conditions(rule: dict) -> list[tuple[str, str, object]]:
    """(fact name, operator, value) for every condition of the rule's `when` (one condition or a list)."""
    out = []
    for cond in rule["when"] if isinstance(rule["when"], list) else [rule["when"]]:
        ((op, value),) = [(k, v) for k, v in cond.items() if k != "fact"]
        out.append((cond["fact"], op, value))
    return out


def _condition_problems(where: str, name: str, fact: dict, op: str, value) -> list[str]:
    kind = fact_type(fact)
    allowed = OPERATORS[kind]
    if op not in allowed:
        return [f"{where}: {op} does not fit {name}"]
    if op == "changed":
        if fact["from"] != "spec" or value not in allowed["changed"]:
            return [f"{where}: changed {value} does not fit {name}"]
        return []
    if op == "is" and kind in ("bool", "answer") and type(value) is not bool:
        return [f"{where}: is on {name} must be true or false"]
    if op == "is" and kind in ("enum", "ordered") and not isinstance(value, str):
        return [f"{where}: is on {name} must be a value of {fact['measure']}"]
    if kind in ("enum", "ordered") and op in ("is", "any"):
        values = [value] if op == "is" else value
        bad = [v for v in values if v not in ENUMS[fact["measure"]]]
        if bad:
            return [f"{where}: {bad[0]} is not a {fact['measure']} value"]
    return []


def _regex_problems(where: str, patterns) -> list[str]:
    problems = []
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            problems.append(f"{where} has an invalid regex {pattern!r}: {exc}")
    return problems


def _placeholder_problems(where: str, rule: dict, allowed: set[str]) -> list[str]:
    problems = []
    for field in TEXT_FIELDS:
        try:
            parts = list(string.Formatter().parse(rule[field]))
        except ValueError:
            problems.append(f"{where}: {field} has a stray brace (write a literal brace as {{{{ or }}}}) or a bad placeholder")
            continue
        for _, name, spec, conversion in parts:
            if name is None:
                continue
            if name not in allowed or spec or conversion:
                problems.append(f"{where}: {field} has placeholder {{{name}}}, which these facts can't fill (they fill {', '.join(sorted(allowed))})")
    return problems


def check(doc: dict) -> tuple[list[str], list[str]]:
    """(problems, warnings) beyond the schema."""
    problems, warnings = [], []
    facts, used, seen = doc["facts"], set(), set()
    for name, fact in facts.items():
        if fact["from"] == "diff":
            problems += _regex_problems(f"facts.{name}.added_lines", fact.get("added_lines", []))
        if fact["from"] == "agent":
            terms = [t.casefold() for t in fact.get("must_check", [])]
            if len(terms) != len(set(terms)):
                problems.append(f"facts.{name}.must_check lists a term twice")
    for key, patterns in doc["detectors"]["patterns"].items():
        problems += _regex_problems(f"detectors.patterns.{key}", patterns)
    for i, rule in enumerate(doc["rules"]):
        where = f"rules[{i}] {rule['id']}"
        if rule["id"] in seen:
            problems.append(f"duplicate rule id {rule['id']}")
        seen.add(rule["id"])
        named = []
        for name, op, value in conditions(rule):
            fact = facts.get(name)
            if fact is None:
                problems.append(f"{where} names a missing fact {name}")
                continue
            used.add(name)
            named.append(fact)
            problems += _condition_problems(where, name, fact, op, value)
        agentic = any(f["from"] == "agent" for f in named)
        if agentic and "on_unsure" not in rule:
            problems.append(f"{rule['id']} needs on_unsure: it routes on an agent fact, whose answer can be unsure")
        if not agentic and "on_unsure" in rule:
            problems.append(f"{rule['id']} can't have on_unsure: none of its facts can be unsure")
        if named:
            problems += _placeholder_problems(where, rule, set.intersection(*(placeholders(f) for f in named)))
    warnings += [f"fact {name} is not used by any rule" for name in facts if name not in used]
    return problems, warnings


def routing_path(factory) -> Path:
    return Path(factory) / "config" / ROUTING_FILE


def has_routing(factory) -> bool:
    path = routing_path(factory)
    return path.is_symlink() or path.exists()


def load_routing(factory) -> tuple[dict, list[str]]:
    """(routing/v1 document, warnings). Anything malformed is an InputError."""
    path = routing_path(factory)
    if path.is_symlink():
        raise InputError(f"{path} is a symlink")
    try:
        if path.stat().st_size > MAX_ROUTING_BYTES:
            raise InputError(f"{path} is larger than {MAX_ROUTING_BYTES} bytes")
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except InputError:
        raise
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise InputError(f"can't read {path}: {type(exc).__name__}: {exc}"[:300]) from exc
    errors = validate("routing.v1", doc)
    if errors:
        raise InputError(f"{path} is not a valid routing/v1: {errors[0]}")
    problems, warnings = check(doc)
    if problems:
        raise InputError(f"{path}: " + "; ".join(problems[:10]))
    return doc, warnings


def merge(mechanics: dict, routing: dict) -> dict:
    """The one config dict: scorecard-config/v2's mechanics, routing.toml's detector inputs under the keys the detectors
    read, and the routing document itself under "routing"."""
    detectors = routing["detectors"]
    return {
        **mechanics, "routing": routing,
        "tier_floor": routing["floors"]["tier"], "reviewers": routing["reviewers"],
        "paths": detectors["paths"], "patterns": detectors["patterns"], "large_change": detectors["large_change"],
        "max_comment_block_lines": detectors["max_comment_block_lines"],
        "registry": {**mechanics["registry"], **routing["packages"]},
        "suppressions": routing["suppressions"], "evidence": routing["evidence"],
    }


def agent_facts(routing: dict) -> list[tuple[str, dict]]:
    return sorted((name, fact) for name, fact in routing["facts"].items() if fact["from"] == "agent")


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def fact_sha256(fact: dict) -> str:
    """One agent question: its text, scope and required searches. A changed question is a new question."""
    return hashlib.sha256(_canonical({"question": fact["question"], "applies_to": fact["applies_to"],
                                      "must_check": fact.get("must_check", [])})).hexdigest()


def questions_sha256(routing: dict) -> str:
    """Every agent question by name: the key an assessment must have been made with."""
    return hashlib.sha256(_canonical({name: fact_sha256(fact) for name, fact in agent_facts(routing)})).hexdigest()

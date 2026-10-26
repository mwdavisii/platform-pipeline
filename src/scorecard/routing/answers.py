"""Code's check of the review agent's answers. The model's answer is never final on its own:

- `yes` needs citations that verify against the file (and name a file the agent could read); for a question about the
  change, at least one must be in a file this pull request changed. Otherwise the answer is unsure.
- `no` can't cite an absence, so it must show what was searched: every `must_check` term needs a successful grep over the
  whole tree (glob "*") in the tool log the harness recorded. Otherwise the answer is unsure.
- `unsure`, a missing answer, or no assessment at all is unsure, with the reason. An unfinished review accepts no answer.
"""

from contracts.evidence import anchor_ok
from contracts.schema import validate
from golden_road.io import InputError

from ..config import model_temperature, temperature_phrase
from ..redact import redact
from .config import agent_facts, questions_sha256

MAX_JUSTIFICATION = 2000


def check_facts_assessment(doc, *, tree_sha: str, config: dict) -> dict:
    errors = validate("facts-assessment.v1", doc)
    if errors:
        raise InputError(f"the assessment is not a valid facts-assessment/v1: {errors[0]}")
    if doc["tree_sha"] != tree_sha:
        raise InputError(f"the assessment is for tree {doc['tree_sha']}, not {tree_sha}")
    agent = config["agent"]
    approved = {"version": agent["model"]["version"], "temperature": model_temperature(config)}
    if doc["prompt_version"] != agent["prompt_version"] or doc["model"] != approved:
        raise InputError(f"the assessment was made with model version {doc['model']['version']!r} ({temperature_phrase(doc['model']['temperature'])}) "
                         f"and prompt {doc['prompt_version']!r}; the factory config approves model version {approved['version']!r} "
                         f"at {temperature_phrase(approved['temperature'])} with prompt {agent['prompt_version']!r}")
    if doc["questions_sha256"] != questions_sha256(config["routing"]):
        raise InputError("the assessment answered other questions than config/routing.toml asks; assess again with this factory")
    names = [a["fact"] for a in doc["answers"]]
    if len(names) != len(set(names)):
        raise InputError("the assessment answers a question more than once")
    return doc


def _searched(log) -> set[str]:
    return {entry["text"].casefold() for entry in log
            if entry["tool"] == "grep" and entry["ok"] and entry.get("glob", "*") == "*" and isinstance(entry.get("text"), str)}


def _copy_anchor(anchor: dict) -> dict:
    """Snippets and notes are always redacted, verified or not: anchor_ok already ran on the original snippet, and the card
    goes into public artifacts, so a secret that sits in the repo text must not be copied there. Nothing re-verifies the stored copy."""
    out = {**anchor, "snippet": redact(anchor["snippet"])}
    if "note" in anchor:
        out["note"] = redact(anchor["note"])
    return out


def _unsure_why(got, cfg, verified: bool, searched: set, changed: set) -> str | None:
    answer = got["answer"]
    if answer == "yes":
        if not verified:
            return "the evidence could not be confirmed"
        if cfg["applies_to"] == "change" and not any(a["file"] in changed for a in got["evidence"]):
            return "the evidence cites no file this pull request changed"
        return None
    if answer == "no":
        missing = [t for t in cfg.get("must_check", []) if t.casefold() not in searched]
        if missing:
            return f"{', '.join(missing)} {'was' if len(missing) == 1 else 'were'} not searched"
        return None
    return "the reviewer was unsure"


def check_answers(doc: dict | None, routing: dict, *, repo_root, citable: set, changed: set) -> dict[str, dict]:
    """Every agent fact by name. `doc` is a checked facts-assessment/v1, or None when there is no assessment."""
    given = {a["fact"]: a for a in doc["answers"]} if doc else {}
    searched = _searched(doc["tool_log"]) if doc else set()
    out = {}
    for name, cfg in agent_facts(routing):
        got = given.get(name)
        fact = {"kind": "agent", "type": "answer", "applies_to": cfg["applies_to"], "answer": "unsure", "value": None, "refs": [],
                "evidence": [], "justification": "", "why": None,
                "searched": [t for t in cfg.get("must_check", []) if t.casefold() in searched]}
        if got is None:
            fact["why"] = "no assessment" if doc is None else "not answered" if doc["complete"] else "the review did not finish"
        else:
            evidence = got["evidence"]
            verified = bool(evidence) and all(anchor_ok(repo_root, a) and a["file"] in citable for a in evidence)
            fact["evidence"] = [_copy_anchor(a) for a in evidence]
            fact["justification"] = redact(got["justification"])[:MAX_JUSTIFICATION]
            fact["why"] = "the review did not finish" if not doc["complete"] else _unsure_why(got, cfg, verified, searched, changed)
            if fact["why"] is None:
                fact["answer"], fact["value"] = got["answer"], got["answer"] == "yes"
        if fact["value"]:
            fact["refs"] = [{"file": a["file"], "line": a["start_line"]} for a in got["evidence"]]
        elif fact["value"] is None:
            fact["refs"] = [{"source": f"{name}: {fact['why']}"}]
        out[name] = fact
    return out

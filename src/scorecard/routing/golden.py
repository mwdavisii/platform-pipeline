"""Golden cases for the routing engine: per agent fact, PRs where the answer is yes and where it's no, plus a false-alarm set
that must come out standard. Fake mode (FactsModel with each case's `fake` answers) checks the pipeline exactly; live mode
reports what the real model said, its tokens, and true and false positives per fact."""

import subprocess
from pathlib import Path

import yaml

from ..agent.fake import FactsModel
from ..agent.review2 import assess_facts
from ..blocking import blocking_findings
from ..build import finish, prepare
from ..golden import GIT, GIT_ENV, GREEN, NOW, REPO

CASE_KEYS = {"name", "description", "base", "change", "expect", "fake"}
EXPECT_KEYS = {"answers", "level", "fired", "not_fired", "shadow", "blocking"}


def load_cases(path) -> list[dict]:
    cases = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    names = [c["name"] for c in cases]
    assert len(names) == len(set(names)), "case names must be unique"
    for case in cases:
        assert set(case) <= CASE_KEYS and {"name", "base", "change", "expect"} <= set(case), case["name"]
        assert set(case["expect"]) <= EXPECT_KEYS, case["name"]
    return cases


def _git(root, *args) -> str:
    return subprocess.run([*GIT, "-C", str(root), *args], check=True, capture_output=True, text=True, env=GIT_ENV).stdout.strip()


def _write(root: Path, files: dict) -> None:
    for rel, text in files.items():
        path = root / rel
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def checkout(case: dict, workdir: Path) -> dict:
    """The case's PR merge commit, and prepare()'s keyword arguments without `factory`."""
    root = workdir / case["name"]
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _write(root, case["base"])
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "-b", "pr")
    _write(root, case["change"])
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", "change")
    tip = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "main")
    _git(root, "merge", "-q", "--no-ff", "-m", "merge", "pr")
    event = {"pull_request": {"draft": False, "title": case["name"], "body": "", "head": {"sha": tip, "repo": {"full_name": REPO}},
                              "base": {"repo": {"full_name": REPO}}}}
    return {"repo_root": root, "repo": REPO, "base_ref": base, "checks_doc": {**GREEN, "sha": tip}, "event": event, "now": NOW}


class _Registry:
    def lookup(self, ecosystem, name, version):
        return {"exists": True, "version_exists": True, "first_release": "2019-01-01", "maintainers": 5, "has_wheel": True}


def run_case(case: dict, factory: Path, client, workdir: Path) -> dict:
    p = prepare(**checkout(case, workdir), factory=factory)
    doc, stats = assess_facts(p, client)
    card, _ = finish(p, doc, registry=_Registry())
    facts = card["routing"]["facts"]
    answers = {name: f["answer"] for name, f in facts.items() if f["kind"] == "agent"}
    fired, shadow = [r["trigger"] for r in card["reasons"]], [s["trigger"] for s in card["routing"]["shadow"]]
    expect, problems = case["expect"], []
    for fact, want in expect.get("answers", {}).items():
        if answers.get(fact) != want:
            problems.append(f"{fact}: {answers.get(fact)}, expected {want} ({facts[fact].get('why') or 'accepted'})")
    if "level" in expect and card["level"] != expect["level"]:
        problems.append(f"level {card['level']}, expected {expect['level']}")
    for key, got in (("fired", fired), ("shadow", shadow)):
        if key in expect and sorted(got) != sorted(expect[key]):
            problems.append(f"{key} {', '.join(sorted(got)) or 'nothing'}; expected {', '.join(sorted(expect[key])) or 'nothing'}")
    blocking = [f["rule_id"] for f in blocking_findings(card)]
    if "blocking" in expect and blocking != expect["blocking"]:
        problems.append(f"blocking {', '.join(blocking) or 'nothing'}; expected {', '.join(expect['blocking']) or 'nothing'}")
    problems += [f"{t} fired" for t in expect.get("not_fired", []) if t in fired]
    return {"name": case["name"], "answers": answers, "level": card["level"], "fired": fired, "shadow": shadow, "blocking": blocking,
            "tokens": stats["tokens_used"], "tool_calls": stats["tool_calls"], "problems": problems}


def report(cases: list[dict], results: list[dict]) -> dict:
    """True and false positives and negatives per agent fact, against each case's expected answers."""
    facts: dict[str, dict] = {}
    for case, result in zip(cases, results):
        for fact, want in case["expect"].get("answers", {}).items():
            got = result["answers"].get(fact)
            cell = "tp" if want == got == "yes" else "tn" if want == got == "no" else "fn" if want == "yes" else "fp"
            facts.setdefault(fact, {"tp": 0, "fp": 0, "fn": 0, "tn": 0})[cell] += 1
    return {"cases": results, "facts": dict(sorted(facts.items())), "ok": all(not r["problems"] for r in results)}


def fake_client(case: dict) -> FactsModel:
    return FactsModel(answers=case.get("fake", {}))

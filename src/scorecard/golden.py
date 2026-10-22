"""The golden set: fixture PRs with known answers, run through the whole scorecard (assess, then score), and scored
per trigger and per chapter.

A case names a base app, the PR's change, the intake record, what must happen (`expect`), and an answer key (`oracle`)
that drives the fake model in the normal suite. In live mode (GOLDEN_ROAD_SCORECARD_LIVE=1) the real gateway answers
instead and the oracle is ignored; the report's false positives and false negatives are the evidence for the AI evaluation.
Registry lookups always use the case's facts, so the live eval measures the model, not the package registries.

Case file keys: name (the file's stem), description, app (a key of `_apps.yaml`), intake, change (write, append, replace),
registry, expect (triggers, allow, fail_chapters, level), oracle (the fake model's answer key), same_statuses_as (another case
whose requirement statuses must all be equal), baseline (the case whose result in the same run is the noise floor for the
baseline-relative report; default `clean-python`, `null` for none).

The deterministic half always comes from the fixture config; a live run replaces only its `agent` block (`agent_config_path`),
so a real factory's Gate 1 checks and patterns can't change what the cases mean.
"""

import dataclasses
import json
import shutil
import subprocess
from pathlib import Path

import yaml
from golden_road.io import InputError

from .agent.fake import OracleModel
from .agent.runner import assess
from .build import finish, prepare
from . import git as scorecard_git
from .config import load_yaml

NOW = "2026-10-21T12:00:00Z"
REPO = "acme/notes"
GREEN = {"passed": True, "required_checks": [{"name": "golden-road / drift", "state": "success"},
                                             {"name": "golden-road / preview", "state": "success"}]}
ESTABLISHED = {"exists": True, "version_exists": True, "first_release": "2019-01-01", "maintainers": 5, "has_wheel": True}
MISSING = {"exists": False, "version_exists": False, "first_release": None, "maintainers": None, "has_wheel": None}
CASE_KEYS = {"name", "description", "app", "intake", "change", "registry", "expect", "oracle", "same_statuses_as", "baseline"}
EXPECT_KEYS = {"triggers", "allow", "fail_chapters", "level"}
DEFAULT_BASELINE = "clean-python"
# Fixtures are built hermetically: no inherited GIT_* variables, no user or system git config, no hooks. The dates are pinned
# so a commit's SHA never depends on the clock or the directory (the same case gives the same scorecard, AC-9).
GIT = ["git", "-c", "user.name=golden", "-c", "user.email=golden@example.invalid", "-c", "commit.gpgsign=false",
       "-c", "init.defaultBranch=main", "-c", "core.hooksPath=/dev/null"]
GIT_ENV = {**scorecard_git._ENV, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
           "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000"}


@dataclasses.dataclass
class CaseResult:
    """card is None when the case raised; stats then holds only `error` (the exception type and a short message)."""
    name: str
    card: dict | None
    stats: dict


class CaseRegistry:
    """Package facts from the case: a name listed as `missing` doesn't exist; anything else is long established."""

    def __init__(self, facts: dict):
        self.facts = facts

    def lookup(self, ecosystem, name, version):
        return MISSING if self.facts.get(name) == "missing" else ESTABLISHED


def load_cases(directory) -> tuple[dict, list[dict]]:
    """(apps, cases) from `_apps.yaml` and every other `*.yaml` in the directory, in name order."""
    directory = Path(directory)
    apps = load_yaml(directory / "_apps.yaml")
    cases = []
    for path in sorted(directory.glob("*.yaml")):
        if path.name == "_apps.yaml":
            continue
        case = load_yaml(path)
        if not isinstance(case, dict) or set(case) - CASE_KEYS or not {"name", "app", "change", "expect", "oracle"} <= set(case):
            raise InputError(f"{path} is not a golden case")
        if case["name"] != path.stem or case["app"] not in apps or set(case["expect"]) - EXPECT_KEYS:
            raise InputError(f"{path}: bad name, app, or expect keys")
        cases.append(case)
    names = {c["name"] for c in cases}
    for case in cases:
        if case.get("same_statuses_as") and case["same_statuses_as"] not in names:
            raise InputError(f"{case['name']}: same_statuses_as names no case")
        if case.get("baseline") is not None and case["baseline"] not in names:
            raise InputError(f"{case['name']}: baseline names no case")
    return apps, cases


def _git(root, *args) -> str:
    return subprocess.run([*GIT, "-C", str(root), *args], check=True, capture_output=True, text=True, env=GIT_ENV).stdout.strip()


def _write(root: Path, files: dict) -> None:
    for rel, text in files.items():
        path = root / rel
        if text is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")


def apply_change(root: Path, change: dict) -> None:
    """`write` sets whole files (null deletes), `append` adds text at the end, and `replace` swaps text that occurs exactly once."""
    if not isinstance(change, dict) or set(change) - {"write", "append", "replace"}:
        raise InputError("a change has only write, append, and replace")
    _write(root, change.get("write") or {})
    for rel, text in (change.get("append") or {}).items():
        path = root / rel
        path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")
    for rel, swaps in (change.get("replace") or {}).items():
        path = root / rel
        text = path.read_text(encoding="utf-8")
        for swap in swaps:
            if text.count(swap["old"]) != 1:
                raise InputError(f"{rel}: {swap['old'][:60]!r} must occur exactly once")
            text = text.replace(swap["old"], swap["new"])
        path.write_text(text, encoding="utf-8")


def materialize(case: dict, apps: dict, root: Path) -> dict:
    """Build the PR's merge commit in `root` and return prepare()'s keyword arguments (without `factory`)."""
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _write(root, apps[case["app"]]["files"])
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "-b", "pr")
    apply_change(root, case["change"])
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", "change")
    tip = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "main")
    _git(root, "merge", "-q", "--no-ff", "-m", "merge", "pr")
    event = {"pull_request": {"draft": False, "head": {"sha": tip, "repo": {"full_name": REPO}}, "base": {"repo": {"full_name": REPO}}}}
    return {"repo_root": root, "repo": REPO, "base_ref": base, "checks_doc": {**GREEN, "sha": tip}, "event": event, "now": NOW}


def make_factory(case: dict, config_path: Path, directory: Path, agent_config_path: Path | None = None) -> Path:
    """A factory for the case: the deterministic config from `config_path`, with only its `agent` block replaced by the one in
    `agent_config_path` (a live run), and the case's intake record."""
    (directory / "config").mkdir(parents=True)
    config = load_yaml(config_path)
    if agent_config_path is not None:
        config["agent"] = load_yaml(agent_config_path)["agent"]
    (directory / "config" / "scorecard.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    if case.get("intake") is not None:
        (directory / "intake" / "acme").mkdir(parents=True)
        intake = {"schema": "intake/v1", "repo": REPO, "owner": "notes-team", **case["intake"]}
        (directory / "intake" / "acme" / "notes.yaml").write_text(json.dumps(intake), encoding="utf-8")
    return directory


def _anchor(root: Path, spec: dict) -> dict:
    """{file, text}: the first line containing `text`; {file, line, snippet}: exactly as given (for a bad citation)."""
    if "snippet" in spec:
        return {"file": spec["file"], "start_line": spec["line"], "snippet": spec["snippet"]}
    lines = (root / spec["file"]).read_text(encoding="utf-8").splitlines()
    number = next((n for n, line in enumerate(lines, 1) if spec["text"] in line), None)
    if number is None:
        raise InputError(f"{spec['file']} has no line containing {spec['text']!r}")
    return {"file": spec["file"], "start_line": number, "snippet": lines[number - 1].strip()}


def _fields(root: Path, fields: dict) -> dict:
    return {**fields, "evidence": [_anchor(root, a) for a in fields.get("evidence", [])]}


def oracle_model(case: dict, root: Path) -> OracleModel:
    key = case["oracle"]
    return OracleModel(default=_fields(root, key["default"]),
                       answers={i: _fields(root, a) for i, a in (key.get("answers") or {}).items()},
                       findings=[_fields(root, f) for f in key.get("findings") or []], reads=key.get("reads", ["*"]))


def run_case(case: dict, apps: dict, *, config_path: Path, workdir: Path, model_for=None, agent_config_path: Path | None = None) -> CaseResult:
    """Run one case end to end. `model_for(case, root)` returns the model client; the default is the case's oracle.
    `agent_config_path` names a config whose `agent` block replaces the fixture's (a live run)."""
    root = workdir / case["name"] / "app"
    kwargs = materialize(case, apps, root)
    factory = make_factory(case, Path(config_path), workdir / case["name"] / "factory", agent_config_path)
    prepared = prepare(**kwargs, factory=factory)
    model = (model_for or oracle_model)(case, root)
    doc, stats = assess(prepared, model)
    card, _ = finish(prepared, doc, registry=CaseRegistry(case.get("registry") or {}))
    return CaseResult(case["name"], card, stats)


def run_cases(cases: list[dict], apps: dict, **kwargs) -> list[CaseResult]:
    """Run every case; a case that raises becomes a result with `error` and no card, so one bad case can't lose the others."""
    results = []
    for case in cases:
        try:
            results.append(run_case(case, apps, **kwargs))
        except Exception as exc:  # noqa: BLE001 - recorded in the report, never raised
            results.append(CaseResult(case["name"], None, {"error": f"{type(exc).__name__}: {exc}"[:300], "failed_batches": [], "tokens_used": 0}))
    return results


def _fail_chapters(card: dict) -> set[str]:
    return {r["chapter"] for r in card["asvs"]["results"] if r["status"] == "fail"}


def _statuses(card: dict) -> dict:
    return {r["asvs_id"]: r["status"] for r in card["asvs"]["results"]}


def _count(table: dict, key: str, field: str) -> None:
    table.setdefault(key, {"tp": 0, "fp": 0, "fn": 0})[field] += 1


def _t_key(trigger: str) -> int:
    return int(trigger[2:])


def _c_key(chapter: str) -> list[int]:
    return [int(x) for x in chapter.removeprefix("v5.0.0-").split(".")]


def baseline_of(case: dict) -> str | None:
    """The case whose result is this case's noise floor: `baseline` (default clean-python), none for itself or when null."""
    name = case["baseline"] if "baseline" in case else DEFAULT_BASELINE
    return None if name is None or name == case["name"] else name


def _relative(cases: list[dict], by_name: dict) -> dict:
    """Only what differs from each case's baseline twin in the same run counts: a trigger that fires, or a chapter that fails,
    on the case and not on its twin. Noise the model makes on every PR (T-12, a fail on a clean PR) cancels out."""
    triggers, chapters, verdicts, floor = {}, {}, [], []
    expects = {c["name"]: c["expect"] for c in cases}
    for case in cases:
        twin = baseline_of(case)
        if twin is None:
            floor.append(case["name"])
            continue
        mine, theirs = by_name[case["name"]], by_name.get(twin)
        if mine.card is None or theirs is None or theirs.card is None:
            verdicts.append({"case": case["name"], "baseline": twin, "ok": False, "status_changes": None,
                             "problems": ["no result for the case or its baseline"]})
            continue
        expect, twin_expect, problems = case["expect"], expects[twin], []
        fired = set(mine.card["triggers_fired"]) - set(theirs.card["triggers_fired"])
        wanted = set(expect.get("triggers", [])) - set(twin_expect.get("triggers", []))
        failed = _fail_chapters(mine.card) - _fail_chapters(theirs.card)
        want_fail = set(expect.get("fail_chapters", [])) - set(twin_expect.get("fail_chapters", []))
        for table, got, want, allowed, name in ((triggers, fired, wanted, set(expect.get("allow", [])), "trigger"),
                                                (chapters, failed, want_fail, set(), "chapter")):
            for item in sorted(got | want, key=_t_key if name == "trigger" else _c_key):
                if item in got and item in want:
                    _count(table, item, "tp")
                elif item in want:
                    _count(table, item, "fn")
                    problems.append(f"{item} {'did not fire' if name == 'trigger' else 'has no fail'} beyond {twin}")
                elif item not in allowed:
                    _count(table, item, "fp")
                    problems.append(f"{item} {'fired' if name == 'trigger' else 'has an unexpected fail'} beyond {twin}")
        a, b = _statuses(mine.card), _statuses(theirs.card)
        verdicts.append({"case": case["name"], "baseline": twin, "ok": not problems,
                         "status_changes": sum(1 for i in a.keys() | b.keys() if a.get(i) != b.get(i)), "problems": problems})
    return {"view": "relative to each case's baseline twin in the same run: only triggers and fail chapters that appear beyond the twin count",
            "baseline_cases": floor, "cases": verdicts, "triggers": dict(sorted(triggers.items(), key=lambda kv: _t_key(kv[0]))),
            "chapters": dict(sorted(chapters.items(), key=lambda kv: _c_key(kv[0])))}


def report(cases: list[dict], results: list[CaseResult]) -> dict:
    """False positives and negatives per trigger and per chapter, and each case's verdict, two ways: absolute (the top-level
    cases, triggers and chapters: every trigger and fail on every case, so a model's noise on clean PRs counts) and
    `baseline_relative` (only what differs from the case's twin in the same run). Fake mode asserts both exactly; a live run
    reports them."""
    by_name = {r.name: r for r in results}
    triggers, chapters, verdicts = {}, {}, []
    for case in cases:
        result = by_name[case["name"]]
        if result.card is None:
            verdicts.append({"case": case["name"], "level": None, "triggers_fired": [], "failed_batches": 0, "ok": False,
                             "problems": [f"error: {result.stats['error']}"]})
            continue
        card, expect = result.card, case["expect"]
        fired, wanted, allowed = set(card["triggers_fired"]), set(expect.get("triggers", [])), set(expect.get("allow", []))
        failed, want_fail = _fail_chapters(card), set(expect.get("fail_chapters", []))
        problems = []
        for t in sorted(wanted | fired, key=_t_key):
            if t in wanted and t in fired:
                _count(triggers, t, "tp")
            elif t in wanted:
                _count(triggers, t, "fn")
                problems.append(f"{t} did not fire")
            elif t not in allowed:
                _count(triggers, t, "fp")
                problems.append(f"{t} fired")
        for c in sorted(want_fail | failed, key=_c_key):
            field = "tp" if c in want_fail and c in failed else "fn" if c in want_fail else "fp"
            _count(chapters, c, field)
            if field != "tp":
                problems.append(f"{c} {'has no fail' if field == 'fn' else 'has an unexpected fail'}")
        if expect.get("level") and card["level"] != expect["level"]:
            problems.append(f"level {card['level']}, expected {expect['level']}")
        if pair := case.get("same_statuses_as"):
            other = by_name[pair]
            if other.card is None:
                problems.append(f"no result for {pair}")
            else:
                mine, theirs = _statuses(card), _statuses(other.card)
                changed = sorted(i for i in mine.keys() | theirs.keys() if mine.get(i) != theirs.get(i))  # both key sets
                if changed:
                    problems.append(f"{len(changed)} statuses differ from {pair}: {', '.join(changed[:5])}")
        verdicts.append({"case": case["name"], "level": card["level"], "triggers_fired": sorted(fired, key=_t_key),
                         "failed_batches": len(result.stats["failed_batches"]), "ok": not problems, "problems": problems})
    return {"view": "absolute: every trigger and fail on every case", "cases": verdicts,
            "triggers": dict(sorted(triggers.items(), key=lambda kv: _t_key(kv[0]))),
            "chapters": dict(sorted(chapters.items(), key=lambda kv: _c_key(kv[0]))),
            "baseline_relative": _relative(cases, by_name)}

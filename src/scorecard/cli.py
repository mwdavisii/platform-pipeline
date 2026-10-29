"""scorecard: the risk scorecard CLI. Exit 0 when it produced its output, 1 on tool error or "not assessed".

The scrutiny level is output (stdout and the files), never an exit code.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

from builder.config import load_platform
from golden_road.io import InputError, load_json, write_json, write_text_atomic

from .agent.gateway import GatewayClient, access_headers, credential, gateway_model
from .agent.review2 import assess_facts
from .agent.runner import assess
from .blocking import override_by
from .build import NotAssessed, agent_scope, build_scorecard, prepare
from .asvs import check_ids, load_asvs
from .config import engine, gate1_list, load_config, load_confirmed_na, load_intake, load_registration_rules, runner_env
from .factory_digest import factory_digest
from .observations import checks_from_doc, checks_sha
from .precondition import RERUN_HINT, not_assessed
from .report_github import github_actions
from .report_md import MAX_COMMENT, render, render_routing
from .suppressions import load_suppressions

EXIT_OK, EXIT_ERROR = 0, 1
MAX_ERROR = 500
OUTPUTS = ("scorecard.json", "scorecard.md", "comment.md", "github.json", "re-review-event.json")
_NOW = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"error": message}), file=sys.stderr)
        raise SystemExit(EXIT_ERROR)


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="scorecard")
    sub = p.add_subparsers(dest="command", required=True)
    pc = sub.add_parser("precondition")
    pc.set_defaults(handler=_cmd_precondition)
    pc.add_argument("--factory", required=True, type=Path)
    pc.add_argument("--checks", type=Path, default=None)
    pc.add_argument("--event", type=Path, default=None, help="the GitHub event payload (JSON)")
    pc.add_argument("--head-sha", default=None, help="the PR head commit (event.pull_request.head.sha) the Gate 1 result must be for; there is no checkout to read it from")
    g1 = sub.add_parser("gate1-checks", help="write the factory's Gate 1 list as a golden-road required-checks file")
    g1.set_defaults(handler=_cmd_gate1_checks)
    g1.add_argument("--factory", required=True, type=Path)
    g1.add_argument("--out", required=True, type=Path)
    cf = sub.add_parser("check-factory", help="validate the factory's config and every intake, N/A and suppression record with the real loaders")
    cf.set_defaults(handler=_cmd_check_factory)
    cf.add_argument("--factory", required=True, type=Path)
    fd = sub.add_parser("factory-digest", help="print one SHA-256 over the factory inputs (config, intake, na, suppressions) this run read")
    fd.set_defaults(handler=_cmd_factory_digest)
    fd.add_argument("--factory", required=True, type=Path)
    sc = sub.add_parser("scope", help="write the requirements the agent must answer, and the tree SHA, as agent-scope/v1")
    sc.set_defaults(handler=_cmd_scope)
    r = sub.add_parser("run")
    r.set_defaults(handler=_cmd_run)
    sa = sub.add_parser("assess", help="run the review agent on the requirements `scope` lists and write assessment/v1")
    sa.set_defaults(handler=_cmd_assess)
    for sp in (sc, r, sa):
        sp.add_argument("--repo-root", required=True, type=Path)
        sp.add_argument("--repo", required=True)
        sp.add_argument("--base", required=True)
        sp.add_argument("--factory", required=True, type=Path)
        sp.add_argument("--checks", required=True, type=Path)
        sp.add_argument("--event", type=Path, default=None, help="the GitHub event payload (JSON); without it the PR is not assessed")
        sp.add_argument("--now", default=None)
    sc.add_argument("--out", required=True, type=Path, help="the agent-scope/v1 file to write")
    sa.add_argument("--out", required=True, type=Path, help="the assessment/v1 file to write")
    r.add_argument("--assessment", type=Path, default=None)
    r.add_argument("--pr-text", type=Path, default=None)
    r.add_argument("--server-url", default="https://github.com")
    r.add_argument("--out", required=True, type=Path)
    po = sub.add_parser("poll", help="central review: queue and dispatch one assess run per ready pull request of every registered repo")
    po.set_defaults(handler=_cmd_poll)
    po.add_argument("--factory", required=True, type=Path)
    po.add_argument("--factory-repo", required=True, help="OWNER/REPO of the factory, where the assess workflow is dispatched")
    po.add_argument("--workflow", required=True, help="the assess workflow's file name")
    po.add_argument("--ref", required=True, help="the factory ref to dispatch on (main)")
    po.add_argument("--run-url", required=True, help="this run's URL, the check runs' details_url")
    po.add_argument("--out", required=True, type=Path, help="the poll result (JSON) to write")
    po.add_argument("--now", default=None)
    return p


def _now(value: str | None) -> str:
    if value is None:
        return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not _NOW.match(value):
        raise InputError("--now must look like 2026-10-21T12:00:00Z")
    return value


def _cmd_precondition(args, registry, model) -> int:
    config = load_config(args.factory)
    doc = load_json(args.checks) if args.checks else None
    checks = checks_from_doc(doc) if doc is not None else None
    event = load_json(args.event) if args.event else None
    reasons = not_assessed(gate1_list(args.factory, config), checks, doc["passed"] if doc else None, event,
                           head_sha=args.head_sha, checks_sha=checks_sha(doc))
    print(json.dumps({"assessed": not reasons, "reasons": reasons, "hint": RERUN_HINT if reasons else None}))
    return EXIT_ERROR if reasons else EXIT_OK


def _cmd_gate1_checks(args, registry, model) -> int:
    """The Gate 1 list from the factory config, in the `{"required": [...]}` form `golden-road checks wait --required-file` reads.
    The config loader refuses an empty list, so a factory with no Gate 1 checks fails here, before any wait."""
    if args.out.is_dir():
        raise InputError(f"{args.out} is a directory, not a file")
    args.out.unlink(missing_ok=True)
    checks = gate1_list(args.factory)
    write_json(args.out, {"required": checks})
    print(json.dumps({"gate1_checks": len(checks)}))
    return EXIT_OK


_RUN_URL = re.compile(r"^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?/[A-Za-z0-9._/-]+$")


def _cmd_poll(args, registry, model) -> int:
    """Central review's poller. Inert (exit 0, {"central": "off"}) unless the factory config has `central`; with it, needs the
    App id (SCORECARD_APP_ID) and key (GOLDEN_ROAD_APP_KEY) in the environment. Exit 1 when any repository or pull request errored."""
    from golden_road.checks import GitHubAPI
    from golden_road.github_app import KEY_ENV, installation_token

    from . import central
    if args.out.is_dir():
        raise InputError(f"{args.out} is a directory, not a file")
    args.out.unlink(missing_ok=True)
    if not load_config(args.factory).get("central"):
        write_json(args.out, {"central": "off"})
        print(json.dumps({"central": "off"}))
        return EXIT_OK
    raw = os.environ.get("SCORECARD_APP_ID", "")
    if not re.fullmatch(r"[1-9][0-9]{0,9}", raw):
        raise InputError("SCORECARD_APP_ID is not an App id")
    app_id, key = int(raw), os.environ.get(KEY_ENV, "")
    if not key:
        raise InputError(f"{KEY_ENV} is not set")
    if not _RUN_URL.fullmatch(args.run_url) or ".." in args.run_url.split("/"):
        raise InputError("--run-url is not an https URL")
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    doc = central.poll(args.factory, app_id=app_id, mint=lambda repo, perms: installation_token(api_url, app_id, key, repo, perms)["token"],
                       factory_api=GitHubAPI(api_url, os.environ.get("GITHUB_TOKEN", "")), make_api=lambda token: GitHubAPI(api_url, token),
                       factory_repo=args.factory_repo, workflow=args.workflow, ref=args.ref, run_url=args.run_url, now=_now(args.now))
    write_json(args.out, doc)
    print(json.dumps({"dispatched": sum(a["action"] == "dispatch" for a in doc["actions"]), "errors": len(doc["errors"])}))
    return EXIT_ERROR if doc["errors"] else EXIT_OK


def _symlinks(factory: Path) -> list[str]:
    """Every symlink under config/, intake/, na/ and suppressions/ (the directories themselves included), found the way the
    intake action looks for them, and every directory the scan could not enter. Links are never followed."""
    problems = []
    for kind in ("config", "intake", "na", "suppressions"):
        top = factory / kind
        if top.is_symlink():
            problems.append(f"{kind} is a symlink")
            continue
        if not top.exists():
            continue
        errors = []
        for dirpath, dirnames, filenames in os.walk(top, followlinks=False, onerror=errors.append):
            for name in dirnames + filenames:
                path = Path(dirpath) / name
                try:
                    str(path.relative_to(factory)).encode("utf-8")
                except UnicodeEncodeError:
                    problems.append(f"{str(path.relative_to(factory)).encode('utf-8', 'backslashreplace').decode()} has a name that is not valid UTF-8")
                    continue
                if path.is_symlink():
                    problems.append(f"{path.relative_to(factory)} is a symlink")
        problems += [f"can't scan {Path(e.filename).relative_to(factory)}: {e.strerror}" for e in errors]
    return problems


def _records(factory: Path, kind: str):
    """(owner/name, problem) for every record under kind/<owner>/: the repo is named by its path. Directly under kind/ only
    owner directories, README.md and .gitkeep are allowed, so a misplaced record can't sit there unread."""
    top = factory / kind
    if top.is_symlink() or not top.is_dir():
        return
    for owner in sorted(top.iterdir()):
        if owner.is_symlink():
            continue  # reported by _symlinks
        if not owner.is_dir():
            if owner.name not in ("README.md", ".gitkeep"):
                yield None, f"{owner.relative_to(factory)} is not in an owner directory ({kind}/<owner>/...)"
            continue
        for entry in sorted(owner.iterdir()):
            if entry.name == ".gitkeep" or entry.is_symlink():
                continue  # a link is reported by _symlinks
            if kind == "suppressions":
                if entry.is_dir():
                    yield f"{owner.name}/{entry.name}", None
                else:
                    yield None, f"{entry} is not a repository directory"
            elif entry.suffix == ".yaml" and entry.is_file():
                yield f"{owner.name}/{entry.stem}", None
            else:
                yield None, f"{entry} is not a .yaml file"


def _cmd_factory_digest(args, registry, model) -> int:
    print(json.dumps({"sha256": factory_digest(args.factory)}))
    return EXIT_OK


def _cmd_check_factory(args, registry, model) -> int:
    """Every problem in the factory, found with the loaders the scorecard uses, so a bad record fails the factory's own pull
    request instead of sending an app (or every app) to "not assessed"."""
    factory, problems, counts = args.factory, [], {"intake": 0, "na": 0, "suppressions": 0}
    config, warnings = None, []
    problems += _symlinks(factory)
    try:
        config = load_config(factory)
        gate1_list(factory, config)
        if engine(config) == "routing":
            from .routing.config import load_routing
            warnings = load_routing(factory)[1]
    except Exception as exc:  # noqa: BLE001 - every failure is a problem to report, not a traceback
        problems.append(f"config: {exc}")
    try:
        load_registration_rules(factory)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"registration: {exc}")
    try:
        requirements = None if config and engine(config) == "routing" else load_asvs()  # routing reads no ASVS data
    except Exception as exc:  # noqa: BLE001
        requirements = None
        problems.append(f"ASVS data: {exc}")
    for kind in counts:
        for repo, problem in _records(factory, kind):
            if problem:
                problems.append(problem)
                continue
            try:
                if kind == "intake":
                    load_intake(factory, repo)
                elif kind == "na":
                    entries = load_confirmed_na(factory, repo)
                    if config and requirements:
                        check_ids(requirements, config, entries)
                else:
                    load_suppressions(factory, repo)
                counts[kind] += 1
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{kind}/{repo}: {exc}")
    if problems:
        print(json.dumps({"error": f"{len(problems)} problem(s) in the factory", "problems": [p[:MAX_ERROR] for p in problems]}), file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps({**counts, "warnings": warnings}) if warnings else json.dumps(counts))
    return EXIT_OK


def _cmd_scope(args, registry, model) -> int:
    if args.out.is_dir():
        raise InputError(f"{args.out} is a directory, not a file")
    args.out.unlink(missing_ok=True)
    prepared = prepare(repo_root=args.repo_root, repo=args.repo, base_ref=args.base, factory=args.factory, checks_doc=load_json(args.checks),
                       event=load_json(args.event) if args.event else None, now=_now(args.now))
    doc = agent_scope(prepared)
    write_json(args.out, doc)
    print(json.dumps({"tree_sha": doc["tree_sha"], "requirements": len(doc["requirements"])}))
    return EXIT_OK


def _cmd_assess(args, registry, model) -> int:
    """The agent's process gets the gateway credential, the gateway model name and the optional Access token, and nothing else (SEC-2). The workflow starts it under `env -i` with only
    the gateway variable; any variable of the runner (GITHUB_*, ACTIONS_*, RUNNER_*, GH_TOKEN, ...) in its environment is refused,
    and so is a credential that holds a GitHub token. A refusal leaves no assessment file behind, and deletes nothing else."""
    if args.out.is_dir():
        raise InputError(f"{args.out} is a directory, not a file")
    args.out.unlink(missing_ok=True)
    present = runner_env(os.environ)
    if present:
        raise InputError(f"refusing to run the agent with the runner's variables in its environment ({', '.join(present[:10])}); "
                         "start it with only the gateway credential")
    # prepare checks the precondition first, so a PR that is not assessed never reaches the model (AC-11).
    prepared = prepare(repo_root=args.repo_root, repo=args.repo, base_ref=args.base, factory=args.factory, checks_doc=load_json(args.checks),
                       event=load_json(args.event) if args.event else None, now=_now(args.now))
    client = model or GatewayClient(prepared.config, credential(prepared.config), model=gateway_model(), access=access_headers())
    doc, stats = assess_facts(prepared, client) if engine(prepared.config) == "routing" else assess(prepared, client)
    write_json(args.out, doc)
    print(json.dumps(stats))
    return EXIT_OK


def _cmd_run(args, registry, model) -> int:
    if not args.out.is_dir():
        raise InputError(f"{args.out} is not a directory")
    for name in OUTPUTS:
        (args.out / name).unlink(missing_ok=True)
    card, context = build_scorecard(
        repo_root=args.repo_root, repo=args.repo, base_ref=args.base, factory=args.factory, checks_doc=load_json(args.checks),
        event=load_json(args.event) if args.event else None, now=_now(args.now), assessment_doc=load_json(args.assessment) if args.assessment else None,
        pr_text=load_json(args.pr_text) if args.pr_text else None, registry=registry,
    )
    expired = context["expired_suppressions"]
    # Render everything before writing anything, so a failure can't leave a partial set of outputs.
    who = override_by(load_platform())
    if card["schema"] == "scorecard/v2":
        summary = render_routing(card, server_url=args.server_url, expired=expired, override_by=who)
        comment = render_routing(card, server_url=args.server_url, limit=MAX_COMMENT, expired=expired, override_by=who)
    else:
        md_args = (card, context["texts"], context["names"], context["blocking"])
        summary = render(*md_args, server_url=args.server_url, expired=expired, override_by=who)
        comment = render(*md_args, server_url=args.server_url, limit=MAX_COMMENT, expired=expired, override_by=who)
    actions = github_actions(card["level"], context["config"], card=card, who=who)
    try:
        write_json(args.out / "scorecard.json", card)
        write_text_atomic(args.out / "scorecard.md", summary)
        write_text_atomic(args.out / "comment.md", comment)
        write_json(args.out / "github.json", actions)
        write_json(args.out / "re-review-event.json", context["event"])
    except BaseException:
        for name in OUTPUTS:
            (args.out / name).unlink(missing_ok=True)
        raise
    print(json.dumps({"level": card["level"], "triggers_fired": card["triggers_fired"]}))
    return EXIT_OK


def main(argv=None, registry=None, model=None) -> int:
    """`registry` replaces the package-registry client and `model` the gateway client; tests pass fakes so they never use the network."""
    args = _parser().parse_args(argv)
    try:
        return args.handler(args, registry, model)
    except NotAssessed as exc:
        print(json.dumps({"not_assessed": str(exc)[:MAX_ERROR]}), file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - nothing may escape as a traceback or look like a scorecard
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"[:MAX_ERROR]}), file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())

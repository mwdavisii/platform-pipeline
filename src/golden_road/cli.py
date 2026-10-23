"""golden-road: the pipeline's own commands. Errors print JSON to stderr and exit 2; nothing fails open."""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from builder.config import gate2_single_approver, load_platform
from contracts.schema import validate

from . import authz_probe, checks, doctor, preview, record, register, remediate
from .gate import evaluate, load_overrides
from .io import InputError, load_findings, load_json, write_json, write_text_atomic
from .normalize import from_builder_result, normalize_sarif
from .preflight import case_collisions, forged_guard_markers
from .render import render as render_manifests
from .summary import annotations
from .summary import render as render_summary

EXIT_PASS, EXIT_FAIL, EXIT_ERROR = 0, 1, 2
EXIT_UNREADABLE = 3
MAX_ERROR = 500
HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::[0-9]{1,5})?")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"error": message}), file=sys.stderr)
        raise SystemExit(EXIT_ERROR)


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="golden-road")
    sub = p.add_subparsers(dest="command", required=True)
    n = sub.add_parser("normalize")
    n.set_defaults(handler=_cmd_normalize)
    n.add_argument("--category", required=True)
    n.add_argument("--repo-root", required=True, type=Path)
    n.add_argument("sarif", type=Path)
    n.add_argument("--output", required=True, type=Path)
    b = sub.add_parser("from-builder")
    b.set_defaults(handler=_cmd_from_builder)
    b.add_argument("result", type=Path)
    b.add_argument("--output", required=True, type=Path)
    pf = sub.add_parser("preflight")
    pf.set_defaults(handler=_cmd_preflight)
    pf.add_argument("root", type=Path)
    pf.add_argument("--output", required=True, type=Path)
    g = sub.add_parser("gate")
    g.set_defaults(handler=_cmd_gate)
    g.add_argument("findings", nargs="+", type=Path)
    g.add_argument("--repo", required=True)
    g.add_argument("--overrides", type=Path, default=None)
    g.add_argument("--today", default=None)
    g.add_argument("--output", required=True, type=Path)
    s = sub.add_parser("summary")
    s.set_defaults(handler=_cmd_summary)
    s.add_argument("gate_result", type=Path)
    s.add_argument("findings", nargs="*", type=Path)
    s.add_argument("--output", required=True, type=Path)
    c = sub.add_parser("checks")
    csub = c.add_subparsers(dest="checks_command", required=True)
    cw = csub.add_parser("wait")
    cw.set_defaults(handler=_cmd_checks_wait)
    cw.add_argument("--repo", required=True)
    cw.add_argument("--sha", required=True)
    cw.add_argument("--required-file", type=Path, default=None)
    cw.add_argument("--timeout", type=float, default=1800)
    cw.add_argument("--interval", type=float, default=20)
    cw.add_argument("--output", required=True, type=Path)
    ap = sub.add_parser("authz-probe")
    ap.set_defaults(handler=_cmd_authz_probe)
    ap.add_argument("--repo-root", required=True, type=Path)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--ready-timeout", type=float, default=60.0)
    ap.add_argument("--deadline", type=float, default=300.0)
    ap.add_argument("--output", required=True, type=Path)
    pv = sub.add_parser("preview-report")
    pv.set_defaults(handler=_cmd_preview_report)
    pv.add_argument("--authz", type=Path, default=None)
    pv.add_argument("--job-result", default=None)
    pv.add_argument("--run-url", default=None)
    pv.add_argument("--commit", default=None)
    pv.add_argument("--annotate", action="store_true")
    pv.add_argument("--output", required=True, type=Path)
    rn = sub.add_parser("render")
    rn.set_defaults(handler=_cmd_render)
    rn.add_argument("--repo-root", required=True, type=Path)
    rn.add_argument("--image", required=True)
    rn.add_argument("--output", required=True, type=Path)
    rc = sub.add_parser("record")
    rc.set_defaults(handler=_cmd_record)
    for flag in ("--repo", "--commit", "--image", "--builder-image", "--release"):
        rc.add_argument(flag, required=True)
    rc.add_argument("--manifests", required=True, type=Path)
    for flag in ("--drift", "--checks", "--authz"):
        rc.add_argument(flag, required=True, type=Path)
    rc.add_argument("--output", required=True, type=Path)
    pr = sub.add_parser("promotion-record")
    pr.set_defaults(handler=_cmd_promotion_record)
    for flag in ("--release", "--image", "--triggered-by", "--run-url"):
        pr.add_argument(flag, required=True)
    pr.add_argument("--actor", required=True)
    pr.add_argument("--run-attempt", required=True, type=int)
    pr.add_argument("--approvals", required=True, type=Path)
    pr.add_argument("--manifests", required=True, type=Path)
    pr.add_argument("--output", required=True, type=Path)
    an = sub.add_parser("annotate")
    an.set_defaults(handler=_cmd_annotate)
    an.add_argument("findings", nargs="+", type=Path)
    dr = sub.add_parser("doctor", help="Check that this app repo is ready for the golden road. Reads only; changes nothing.",
                        description="Check that this app repo is ready for the golden road. Reads only; changes nothing. "
                                    "Exit 0: no check failed; exit 1: a check failed. Stdout is a doctor-result JSON document only for exit 0 and 1, "
                                    "and the summary goes to stderr. Exit 2 (bad arguments): a JSON error on stderr and nothing on stdout.")
    dr.set_defaults(handler=_cmd_doctor)
    dr.add_argument("path", nargs="?", type=Path, default=Path("."))
    dr.add_argument("--pin", type=Path, default=None, help="the platform pin (default: ~/.cache/golden-road/platform-pin.json, where the golden-road skill keeps it)")
    dr.add_argument("--host", default=None, help="the platform's GitHub host (default: the platform config's, else $GH_HOST, else github.com)")
    rg = sub.add_parser("register", help="Print the portal link that carries a registration draft. Reads only; sends nothing.",
                        description="Build a registration-draft/v1 and print the golden road portal link that carries it in the URL fragment. "
                                    "Exit 0: the link and draft as JSON on stdout. Exit 2: bad arguments. Exit 3: no portal is configured "
                                    "(a JSON error and fix on stderr).")
    rg.set_defaults(handler=_cmd_register)
    rg.add_argument("--repo", required=True, help="OWNER/REPO")
    rg.add_argument("--purpose-file", type=Path, default=None, help="a file with what the app does and does not do, in the user's words")
    rg.add_argument("--classification", choices=register.CLASSIFICATIONS, default=None, help="default: builder-spec.json's")
    rg.add_argument("--tier", choices=register.TIERS, default=None, help="the user's proposed tier")
    rg.add_argument("--root", type=Path, default=Path("."), help="the app checkout (default: .)")
    rm = sub.add_parser("remediate", help="List what to do about a PR's failing checks. Reads only; changes nothing.",
                        description="List what to do about a pull request's failing checks and the platform's PR comments, as remediation/v1 JSON on stdout "
                                    "and a plan on stderr. Reads only; changes nothing (gh pr view, gh pr checks and gh api GETs). "
                                    "Exit 0: nothing to do; exit 1: the list has items; exit 2: bad arguments (a JSON error on stderr, nothing on stdout); "
                                    "exit 3: the PR or its checks can't be read (a JSON error and fix on stderr, nothing on stdout).")
    rm.set_defaults(handler=_cmd_remediate)
    rm.add_argument("--repo", required=True, help="OWNER/REPO of the pull request")
    rm.add_argument("--pr", required=True, type=int, help="the pull request number")
    rm.add_argument("--root", type=Path, default=Path("."), help="the app checkout (default: .), used only to tell whether a cited file exists")
    rm.add_argument("--host", default=None, help="the platform's GitHub host (default: the platform config's, else $GH_HOST, else github.com)")
    return p


def _now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc.strerror}") from exc


def _cmd_normalize(args) -> int:
    if not args.repo_root.is_dir():
        raise InputError(f"{args.repo_root} is not a directory")
    write_json(args.output, normalize_sarif(load_json(args.sarif), category=args.category, repo_root=args.repo_root))
    return EXIT_PASS


def _cmd_from_builder(args) -> int:
    write_json(args.output, from_builder_result(load_json(args.result)))
    return EXIT_PASS


def _cmd_preflight(args) -> int:
    if not args.root.is_dir():
        raise InputError(f"{args.root} is not a directory")
    write_json(args.output, case_collisions(args.root) + forged_guard_markers(args.root))
    return EXIT_PASS


def _cmd_gate(args) -> int:
    findings = [f for path in args.findings for f in load_findings(path)]
    overrides_dir = args.overrides or os.environ.get("GOLDEN_ROAD_OVERRIDES")
    if overrides_dir and not Path(overrides_dir).is_dir():
        raise InputError(f"overrides path {overrides_dir} is not a directory")
    overrides = load_overrides(overrides_dir) if overrides_dir else []
    today = dt.date.fromisoformat(args.today) if args.today else dt.datetime.now(dt.UTC).date()
    result = evaluate(findings, overrides, repo=args.repo, today=today)
    write_json(args.output, result)
    return EXIT_PASS if result["status"] == "pass" else EXIT_FAIL


def _cmd_summary(args) -> int:
    if not args.findings:
        raise InputError("summary needs at least one findings file")
    gate = load_json(args.gate_result)
    errors = validate("gate-result.v1", gate)
    if errors:
        raise InputError(f"not a gate result: {errors[0]}")
    findings = [f for path in args.findings for f in load_findings(path)]
    write_text_atomic(args.output, render_summary(gate, findings))
    return EXIT_PASS


def _cmd_checks_wait(args) -> int:
    required_file = args.required_file or os.environ.get("GOLDEN_ROAD_REQUIRED_CHECKS")
    if not required_file:
        raise InputError("no required-checks file: pass --required-file or set GOLDEN_ROAD_REQUIRED_CHECKS")
    entries = checks.load_required(required_file)
    if not entries:
        print(json.dumps({"warning": "no external required checks configured; Gate 1 rests on drift and the authz probe"}), file=sys.stderr)
    api = checks.GitHubAPI(os.environ.get("GITHUB_API_URL", "https://api.github.com"), os.environ.get("GITHUB_TOKEN", ""))
    passed, results = checks.wait(api, args.repo, args.sha, entries, timeout=args.timeout, interval=args.interval)
    write_json(args.output, {"passed": passed, "required_checks": results, "sha": args.sha})
    return EXIT_PASS if passed else EXIT_FAIL


def _cmd_authz_probe(args) -> int:
    result = authz_probe.probe(args.repo_root, args.base_url, ready_timeout=args.ready_timeout, deadline=args.deadline)
    write_json(args.output, result)
    return EXIT_FAIL if result["status"] == "fail" else EXIT_PASS


def _cmd_preview_report(args) -> int:
    """Always renders: a missing or unreadable probe result is itself reported as a failure, never an error exit."""
    try:
        authz = load_json(args.authz) if args.authz is not None else None
    except InputError:
        authz = None
    write_text_atomic(args.output, preview.render(authz, args.job_result, args.run_url, args.commit))
    if args.annotate:
        for line in preview.annotations(authz, args.job_result):
            print(line)
    return EXIT_PASS


def _cmd_render(args) -> int:
    spec = load_json(args.repo_root / "builder-spec.json")
    app_name = spec.get("app_name") if isinstance(spec, dict) else None
    if not isinstance(app_name, str):
        raise InputError("builder-spec.json has no app_name")
    write_text_atomic(args.output, render_manifests(args.repo_root, args.image, app_name))
    return EXIT_PASS


def _cmd_record(args) -> int:
    result = record.build_gate_record(
        repo=args.repo, commit=args.commit, image=args.image, builder_image=args.builder_image,
        drift=load_json(args.drift), checks=load_json(args.checks), authz=load_json(args.authz),
        created=_now(), release=args.release, manifests_sha256=_sha256_file(args.manifests))
    write_json(args.output, result)
    return EXIT_PASS if result["status"] == "pass" else EXIT_FAIL


def _cmd_promotion_record(args) -> int:
    result = record.build_promotion_record(
        release=args.release, image=args.image, approvals=load_json(args.approvals),
        triggered_by=args.triggered_by, actor=args.actor, run_attempt=args.run_attempt, run_url=args.run_url, created=_now(),
        manifests_sha256=_sha256_file(args.manifests),
        # From the platform config pinned in the builder image, never from the app repo or a workflow input.
        single_approver=gate2_single_approver(load_platform()))
    write_json(args.output, result)
    return EXIT_PASS


def _cmd_annotate(args) -> int:
    for line in annotations([f for path in args.findings for f in load_findings(path)]):
        print(line)
    return EXIT_PASS


def _host(args) -> str:
    """The platform host: --host, else the platform config's, else $GH_HOST, else github.com."""
    from_platform = None if args.host else doctor.platform_host()
    host = args.host or from_platform or os.environ.get("GH_HOST") or "github.com"
    if not HOST.fullmatch(host):
        source = "--host" if args.host else "the platform config's github_host" if from_platform else "$GH_HOST"
        raise InputError(f"{source} must be a host name, such as github.com")
    return host


def _cmd_doctor(args) -> int:
    """JSON on stdout for the agent, the summary on stderr for the person. Exit 1 when any check fails; warnings don't."""
    if not args.path.is_dir():
        raise InputError(f"{args.path} is not a directory")
    host = _host(args)
    doc = doctor.report(doctor.run_checks(args.path.resolve(), args.pin or doctor.default_pin(), host))
    print(json.dumps(doc, indent=2, sort_keys=True))
    print(doctor.summary(doc), file=sys.stderr)
    return EXIT_FAIL if doc["status"] == "fail" else EXIT_PASS


def _cmd_register(args) -> int:
    """The link on stdout as JSON for the agent, and what to tell the person on stderr. Exit 3 when no portal is configured."""
    if doctor._repo_name(args.repo) is None:
        raise InputError("--repo must be OWNER/REPO")
    try:
        base = register.portal_url()
    except register.NoPortal as exc:
        print(json.dumps({"error": exc.error, "fix": exc.fix}), file=sys.stderr)
        return EXIT_UNREADABLE
    doc = register.draft(args.repo, register.read_purpose(args.purpose_file), args.classification or register.spec_classification(args.root), args.tier)
    url = register.link(base, doc)
    print(json.dumps({"url": url, "draft": doc}, indent=2, sort_keys=True))
    print(f"Open this link, sign in, check every field and submit it:\n{url}\nAn admin approves the registration in the portal.", file=sys.stderr)
    return EXIT_PASS


def _cmd_remediate(args) -> int:
    """JSON on stdout for the agent, the plan on stderr for the person. Exit 1 when there is anything to do."""
    if doctor._repo_name(args.repo) is None:
        raise InputError("--repo must be OWNER/REPO")
    if not 1 <= args.pr <= 999_999_999:
        raise InputError("--pr must be a pull request number from 1 to 999999999")
    if not args.root.is_dir():
        raise InputError(f"{args.root} is not a directory")
    host = _host(args)
    try:
        doc = remediate.run(args.repo, args.pr, host, args.root.resolve())
    except remediate.Unreadable as exc:
        print(json.dumps({"error": exc.error, "fix": exc.fix}), file=sys.stderr)
        return EXIT_UNREADABLE
    errors = validate("remediation.v1", doc)
    if errors:
        raise ValueError(f"internal error: the remediation document is not valid: {errors[0]}")
    print(json.dumps(doc, indent=2, sort_keys=True))
    print(remediate.plan_text(doc), file=sys.stderr)
    return remediate.exit_code(doc)


def _run(args) -> int:
    handler = getattr(args, "handler", None)
    if handler is None:
        raise InputError(f"unknown command {getattr(args, 'command', None)!r}")
    return handler(args)


def _fail(exc: BaseException) -> int:
    message = f"{type(exc).__name__}: {exc}"[:MAX_ERROR]
    print(json.dumps({"error": message}), file=sys.stderr)
    return EXIT_ERROR


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        # A failed run must never leave an earlier result in place for a later step to read.
        output = getattr(args, "output", None)
        if output is not None:
            output.unlink(missing_ok=True)
        return _run(args)
    except (InputError, ValueError, OSError, KeyError, TypeError) as exc:
        return _fail(exc)
    except Exception as exc:  # noqa: BLE001 - nothing may escape as a traceback or a pass
        return _fail(exc)


if __name__ == "__main__":
    sys.exit(main())

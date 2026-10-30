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

from . import authz_probe, checks, deploy, doctor, preview, record, register, remediate
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
    cp = sub.add_parser("compose", help="Write composed.yaml, kustomization.yaml and the adapters for golden-road-deploy environments.",
                        description="For each apps/<owner>/<repo>/<env> path (relative to --root), check release.yaml, bindings.yaml and "
                                    "base/manifests.yaml, then write composed.yaml and kustomization.yaml, and the environment's suspended "
                                    "Flux Kustomization and manual-sync Argo CD Application under clusters/ (the repo URL from "
                                    "clusters/home/flux/gitrepository.yaml). Offline. Exit 0: written, the paths "
                                    "as JSON on stdout. Exit 2: a refused input (a JSON error on stderr); nothing is written for that path.")
    cp.set_defaults(handler=_cmd_compose)
    cp.add_argument("--root", type=Path, default=Path("."), help="the golden-road-deploy checkout (default: .)")
    cp.add_argument("--expect-url", help="refuse unless clusters/home/flux/gitrepository.yaml's url is this one (https://<host>/<org>/golden-road-deploy)")
    cp.add_argument("environments", nargs="+", help="apps/<owner>/<repo>/<test|prod>")
    dv = sub.add_parser("deploy-verify", help="Check every golden-road-deploy environment offline. Reads only; changes nothing.",
                        description="Verify every apps/<owner>/<repo>/<env>: schemas, the base's hash, generated files and adapters not stale, "
                                    "no extra files or symlinks, no adapter for a missing environment, and no two environments sharing a namespace. Exit 0: no problems; exit 1: problems; stdout is "
                                    '{"environments": n, "problems": [...]}. Exit 2: bad arguments or no apps/ (a JSON error on stderr).')
    dv.set_defaults(handler=_cmd_deploy_verify)
    dv.add_argument("--root", type=Path, default=Path("."), help="the golden-road-deploy checkout (default: .)")
    dv.add_argument("--expect-url", help="also report a problem unless clusters/home/flux/gitrepository.yaml's url (and so every adapter's) is this one")
    pn = sub.add_parser("deploy-pin", help="Write one golden-road-deploy environment's release.yaml and base/manifests.yaml from a published release.",
                        description="From a release tag and its two assets (manifests.yaml, gate-record.json), write <env>/release.yaml and "
                                    "<env>/base/manifests.yaml. The gate record must pass, be this repo's, and hash the manifests; for prod it is "
                                    "the promoted test release of the same commit. The namespace is kept, or <repo>-<env> for a new environment. "
                                    "Offline. Exit 0: written, the paths as JSON on stdout. Exit 2: refused (a JSON error on stderr), nothing written. "
                                    "Run golden-road compose after it.")
    pn.set_defaults(handler=_cmd_deploy_pin)
    pn.add_argument("--root", type=Path, default=Path("."), help="the golden-road-deploy checkout (default: .)")
    pn.add_argument("environment", help="apps/<owner>/<repo>/<test|prod>")
    pn.add_argument("--release", required=True, help="the published release's tag")
    pn.add_argument("--manifests", required=True, type=Path, help="the release's manifests.yaml asset")
    pn.add_argument("--gate-record", required=True, type=Path, help="the release's gate-record.json asset")
    dp = sub.add_parser("deploy-pins", help="Write every golden-road-deploy environment's release.yaml as one JSON object. Reads only.",
                        description='Write {"apps/<owner>/<repo>/<env>": <release.yaml>} for every environment to --output, each release '
                                    "validated as deploy-verify does. The deploy repo's workflows read it with jq. Exit 2 (a JSON error on "
                                    "stderr, nothing written): a path under apps/ that is not an environment, or a release.yaml that does not load.")
    dp.set_defaults(handler=_cmd_deploy_pins)
    dp.add_argument("--root", type=Path, default=Path("."), help="the golden-road-deploy checkout (default: .)")
    dp.add_argument("--output", required=True, type=Path)
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
    at = sub.add_parser("app-token", help="Mint a GitHub App installation token for one repository and the named permissions.",
                        description="Mint an installation token of the scorecard App (central review) or of the deploy App (golden-road-deploy's "
                                    "bump workflow: exactly contents=write and pull_requests=write, for <org>/golden-road-deploy only). The App private key is read from "
                                    "GOLDEN_ROAD_APP_KEY only. The token is written to --output (mode 0600); stdout names only its scope.")
    at.set_defaults(handler=_cmd_app_token)
    at.add_argument("--app-id", required=True, type=int)
    at.add_argument("--repo", required=True, help="OWNER/REPO the token is for")
    at.add_argument("--permission", required=True, action="append", help="name=read or name=write, repeated")
    at.add_argument("--output", required=True, type=Path)
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


def _cmd_compose(args) -> int:
    # Every environment's files are computed before any is written: one refused environment leaves every directory as it was.
    planned = {}
    dirs = [deploy.environment_dir(args.root, rel) for rel in args.environments]
    url = deploy.deploy_repo_url(args.root)
    if args.expect_url is not None:
        deploy.check_repo_url(url, args.expect_url)
    for d in dirs:
        planned.update(deploy.plan_environment(d))
        planned.update(deploy.plan_adapters(args.root, d, url))
    written = [p.relative_to(args.root).as_posix() for p in deploy.write_planned(planned)]
    print(json.dumps({"written": written}))
    return EXIT_PASS


def _cmd_deploy_verify(args) -> int:
    report = deploy.verify_tree(args.root, expect_url=args.expect_url)
    print(json.dumps(report, indent=2))
    return EXIT_FAIL if report["problems"] else EXIT_PASS


def _cmd_deploy_pin(args) -> int:
    planned = deploy.plan_pin(args.root, args.environment, release=args.release, manifests=args.manifests, gate_record=args.gate_record)
    written = [p.relative_to(args.root).as_posix() for p in deploy.write_bytes_planned(planned)]
    print(json.dumps({"written": written}))
    return EXIT_PASS


def _cmd_deploy_pins(args) -> int:
    write_json(args.output, deploy.pins(args.root))
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


def _cmd_app_token(args) -> int:
    """The token goes only to the 0600 output file; stdout names its scope. The key is read from the environment only."""
    from . import github_app
    perms = github_app.parse_permissions(args.permission, args.repo)
    doc = github_app.installation_token(os.environ.get("GITHUB_API_URL", "https://api.github.com"), args.app_id,
                                        os.environ.get(github_app.KEY_ENV, ""), args.repo, perms)
    github_app.write_token(args.output, doc)
    print(json.dumps({k: doc[k] for k in ("repository", "permissions", "expires_at")}, sort_keys=True))
    return EXIT_PASS


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

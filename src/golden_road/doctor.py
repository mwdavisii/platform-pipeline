"""golden-road doctor: is this app repo ready for the golden road? It only reads: no writes, no settings, no pushes.

Each check is pass, warn or fail, with a message and a fix. `access` and `visibility` never degrade: if either can't be
determined, it fails. The other GitHub checks become `warn: could not check` only on a 403 or 404 (a read the user has
no access to, which they can't fix); any other failed read fails the check.

Visibility reads every git remote that points at the platform host, not the one gh would pick, and fails if any of them
is not private or internal. The push remote (branch pushRemote, then remote.pushDefault, then origin) names the repo the
other GitHub checks use. Output: stdout is a doctor-result only for exit 0 and 1; exit 2 (a usage error) prints a JSON
error on stderr and nothing on stdout.
"""

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from builder import VERSION
from builder.config import load_platform, platform_sha256, scorecard_app_id, scorecard_mode
from builder.lock import LOCK_PATH
from builder.verify import Reader, verify
from contracts.schema import validate

from .io import InputError, load_json

PASS, WARN, FAIL = "pass", "warn", "fail"
# The four caller workflows and the reusable workflow each one must call, as in bootstrap/hello-app (a test keeps them equal).
CALLERS = {
    "ci.yml": "{org}/platform-pipeline/.github/workflows/golden-road.yml@main",
    "promote.yml": "{org}/platform-pipeline/.github/workflows/golden-road-promote.yml@main",
    "scorecard.yml": "{org}/scorecard-factory/.github/workflows/scorecard.yml@main",
    "security-scans.yml": "{org}/security-scans/.github/workflows/scan.yml@main",
}


def callers_for(platform: dict) -> dict:
    """The callers an app needs under this platform config: all four per app, and without scorecard.yml when the factory assesses
    centrally. An unreadable mode is the builder-pin check's to report, so it counts as per-app here."""
    try:
        central = scorecard_mode(platform) == "central"
    except ValueError:
        central = False
    return {name: template for name, template in CALLERS.items() if not (central and name == "scorecard.yml")}


def central_app_id(platform: dict) -> int | None:
    """The scorecard App's id when the platform reviews centrally, else None (also for a config that names a mode it can't use)."""
    try:
        return scorecard_app_id(platform)
    except ValueError:
        return None


# What triggers each caller, parsed from hello-app's files (a test keeps them equal): a caller that runs on other events,
# or not on these, isn't the platform's.
CALLER_TRIGGERS = {
    "ci.yml": {"pull_request": None, "push": {"branches": ["main"]}},
    "promote.yml": {"workflow_dispatch": {"inputs": {"release": {"description": "Test release tag to promote (test-YYYYMMDDHHMMSS-sha7)",
                                                                  "required": True, "type": "string"}}}},
    "scorecard.yml": {"pull_request": {"types": ["opened", "synchronize", "reopened", "ready_for_review", "edited"]}},
    "security-scans.yml": {"pull_request": None, "push": {"branches": ["main"]}},
}
# What main must require: drift, preview, Gate 1's scans and the scorecard, as hello-app's main.json does (a test keeps them equal).
REQUIRED_CONTEXTS = ("golden-road / drift", "golden-road / preview", "security-scans / semgrep", "security-scans / trivy", "scorecard / assessed")
VISIBLE_OK = ("PRIVATE", "INTERNAL")
GH_TIMEOUT = 60
MAX_TEXT = 300
ORG_NAME = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
REPO_NAME = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*/[A-Za-z0-9._-]{1,100}")
SAFE_VERSION = re.compile(r"[0-9A-Za-z.+-]{1,40}")
_HTTP = re.compile(r"\(HTTP (\d{3})\)")
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]")  # C0, DEL, C1, bidi controls
# What would point gh at another repo or print HTTP traffic on stderr.
GH_DROPPED_ENV = ("GH_REPO", "GH_DEBUG", "DEBUG")
PIN_FIX = "Run the golden-road skill again: it downloads and verifies the platform pin and installs the pinned builder."


def default_pin() -> Path:
    """Where the golden-road skill keeps the platform pin after verifying it."""
    return Path.home() / ".cache" / "golden-road" / "platform-pin.json"


@dataclass
class GhResult:
    code: int
    stdout: str
    stderr: str

    @property
    def http(self) -> int | None:
        match = _HTTP.search(self.stderr)
        return int(match.group(1)) if match else None

    def json(self):
        try:
            return json.loads(self.stdout)
        except (ValueError, RecursionError):
            return None

    def error(self) -> str:
        return next((line.strip() for line in self.stderr.splitlines() if line.strip()), f"exit {self.code}")


def run_gh(root: Path, host: str, args: list[str]) -> GhResult:
    """One read-only gh command in the app folder. A missing gh, a timeout or an OS error is a failed result, never an exception."""
    env = {**{k: v for k, v in os.environ.items() if k not in GH_DROPPED_ENV}, "GH_HOST": host, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"}
    try:
        out = subprocess.run(["gh", *args], cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=GH_TIMEOUT)
    except FileNotFoundError:
        return GhResult(127, "", "gh is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        return GhResult(124, "", f"gh timed out after {GH_TIMEOUT} seconds")
    except OSError as exc:
        return GhResult(126, "", f"gh could not run: {exc.strerror or type(exc).__name__}")
    return GhResult(out.returncode, out.stdout, out.stderr)


def run_git(root: Path, args: list[str]) -> GhResult:
    """One read-only git command in the app folder (remote -v, config --get, symbolic-ref). Never raises."""
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        out = subprocess.run(["git", *args], cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=GH_TIMEOUT)
    except FileNotFoundError:
        return GhResult(127, "", "git is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        return GhResult(124, "", f"git timed out after {GH_TIMEOUT} seconds")
    except OSError as exc:
        return GhResult(126, "", f"git could not run: {exc.strerror or type(exc).__name__}")
    return GhResult(out.returncode, out.stdout, out.stderr)


_URL_FORM = re.compile(r"(?:https?|ssh|git)://(?:[^/@]+@)?([^/:@]+)(?::[0-9]{1,5})?/(.+)", re.I)
_SCP_FORM = re.compile(r"(?:[^/@:]+@)?([^/:@]+):(?!//)(.+)")
# One line of `git remote -v`: the name, the URL, (fetch) or (push), and for a partial clone its filter, such as " [blob:none]".
# The line is split on its first tab (the name is always readable); this matches the rest. A URL may hold spaces (a local path).
_REMOTE_REST = re.compile(r"(.+) \((fetch|push)\)(?: \[[^\]]*\])?")


def remote_repo(url: str, host: str) -> str | None:
    """owner/repo for a git remote URL on host (https, ssh://, git://, or scp-style git@host:owner/repo), else None."""
    scp = None
    match = _URL_FORM.fullmatch(url)
    if not match:
        match = scp = _SCP_FORM.fullmatch(url)
    if not match or match.group(1).lower() != host.split(":")[0].lower():
        return None
    path = match.group(2).rstrip("/")
    if scp:
        path = path.lstrip("/")  # git@host:/owner/repo.git
    path = path[:-4] if path.endswith(".git") else path
    return _repo_name(path)


@dataclass
class Context:
    root: Path
    pin: Path
    host: str
    org: str | None
    repo: str | None = None  # set by the visibility check
    platform: dict = field(default_factory=dict)  # the installed builder's platform config; empty means per-app

    def gh(self, *args) -> GhResult:
        return run_gh(self.root, self.host, list(args))


def _clean(text: str) -> str:
    """Some text comes from GitHub or the repo: no terminal control characters, and a bounded length."""
    return _CONTROL.sub(" ", text)[:MAX_TEXT]


def _result(check_id: str, status: str, message: str, fix: str = "") -> dict:
    return {"id": check_id, "status": status, "message": _clean(message), "fix": _CONTROL.sub(" ", fix)}


def _could_not_read(check_id: str, result: GhResult, what: str, fix: str) -> dict:
    if result.http in (403, 404):
        return _result(check_id, WARN, f"could not check: {what} returned HTTP {result.http}", fix)
    return _result(check_id, FAIL, f"{what} failed: {result.error()}", fix)


def _not_checked(check_id: str) -> dict:
    return _result(check_id, WARN, "not checked: the repository is unknown (see visibility)", "Fix visibility first.")


def _repo_name(value) -> str | None:
    if not isinstance(value, str) or not REPO_NAME.fullmatch(value) or not value.split("/")[1].strip("."):
        return None
    return value


def _org() -> str | None:
    try:
        org = load_platform().get("org")
    except (OSError, ValueError, AttributeError):
        return None
    return org if isinstance(org, str) and ORG_NAME.fullmatch(org) else None


def platform_host() -> str | None:
    """The GitHub host configure.py recorded in the platform config (the wheel carries it), if any. The CLI validates it."""
    try:
        host = load_platform().get("github_host")
    except (OSError, ValueError, AttributeError):
        return None
    return host if isinstance(host, str) else None


# ---- local checks ----

def _lock_version(root: Path) -> str | None:
    text, _ = Reader(root).read(LOCK_PATH)
    try:
        version = json.loads(text).get("builder_version") if text is not None else None
    except (ValueError, RecursionError, AttributeError):
        return None
    return version if isinstance(version, str) and SAFE_VERSION.fullmatch(version) else None


def check_builder_pin(ctx: Context) -> dict:
    try:
        pin = load_json(ctx.pin)
    except InputError:
        return _result("builder-pin", FAIL, f"no readable platform pin at {ctx.pin}", PIN_FIX)
    errors = validate("platform-pin.v1", pin)
    if errors:
        return _result("builder-pin", FAIL, f"the platform pin at {ctx.pin} is not valid: {errors[0]}", PIN_FIX)
    pinned = pin["builder_version"]
    problems, fixes = [], []
    if VERSION != pinned:
        problems.append(f"the installed builder is {VERSION}; the platform pins {pinned}")
    elif platform_sha256() != pin["platform_config_sha256"]:
        problems.append("the installed builder's platform config doesn't match the platform pin")
    if problems:
        fixes.append(PIN_FIX)
    locked = _lock_version(ctx.root)
    if locked != pinned:
        problems.append(f".builder.lock was generated by builder {locked}; the platform pins {pinned}" if locked
                        else ".builder.lock is missing or can't be read")
        fixes.append(f"Run: builder generate . with builder {pinned}, then commit the result.")
    if problems:
        return _result("builder-pin", FAIL, "; ".join(problems), " ".join(fixes))
    return _result("builder-pin", PASS, f"builder {VERSION} and .builder.lock match the platform pin")


def check_verify(ctx: Context) -> dict:
    findings = verify(ctx.root)
    if not findings:
        return _result("verify", PASS, "builder verify . is clean")
    rules = sorted({f["rule_id"] for f in findings})
    return _result("verify", FAIL, f"builder verify . reports {len(findings)} finding(s): {', '.join(rules)}",
                   "Run: builder verify . and apply each finding's fix.steps (usually: builder generate .), then commit.")


def check_callers(ctx: Context) -> dict:
    fix = ("Copy the caller workflows from the golden-road skill's templates into .github/workflows/. They are platform-owned: don't edit them. "
           "The required checks must run on every PR and on main; to change triggers, ask the platform team.")
    if ctx.org is None:
        return _result("callers", FAIL, "the installed builder's platform config names no org, so the platform workflows are unknown", PIN_FIX)
    reader = Reader(ctx.root)
    problems = []
    callers = callers_for(ctx.platform)
    for name, template in callers.items():
        rel, want = f".github/workflows/{name}", template.format(org=ctx.org)
        text, reason = reader.read(rel)
        if text is None:
            problems.append(f"{name} is missing" if reason == "missing" else f"{name} can't be read ({reason})")
            continue
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError:
            problems.append(f"{name} is not valid YAML")
            continue
        jobs = doc.get("jobs") if isinstance(doc, dict) else None
        calls = [job for job in jobs.values() if isinstance(job, dict) and job.get("uses") == want] if isinstance(jobs, dict) else []
        if not calls:
            problems.append(f"{name} doesn't call {want}")
            continue
        if any("if" in job for job in calls):
            problems.append(f"{name} has an if: condition on the job that calls {want}, so the check can be skipped")
        triggers = doc[True] if True in doc else doc.get("on")  # YAML 1.1 reads a bare `on` key as True
        if triggers != CALLER_TRIGGERS[name]:
            problems.append(f"{name} has different triggers than the platform's caller")
    if problems:
        return _result("callers", FAIL, "; ".join(problems), fix)
    if "scorecard.yml" not in callers and _calls_factory_scorecard(reader, ctx.org):
        return _result("callers", WARN, "scorecard.yml still calls the factory's per-app scorecard, but the platform now reviews centrally",
                       "Remove .github/workflows/scorecard.yml in a pull request: the scorecard App assesses every ready pull request, "
                       "and the ruleset requires its `scorecard / assessed`. Don't edit the file; the platform owns it.")
    return _result("callers", PASS, "the caller workflows call the platform's workflows")


def _calls_factory_scorecard(reader: Reader, org: str) -> bool:
    text, _ = reader.read(".github/workflows/scorecard.yml")
    try:
        doc = yaml.safe_load(text) if text is not None else None
    except yaml.YAMLError:
        return False
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    want = CALLERS["scorecard.yml"].format(org=org)
    return isinstance(jobs, dict) and any(isinstance(job, dict) and job.get("uses") == want for job in jobs.values())


# ---- GitHub checks ----

def check_access(ctx: Context) -> dict:
    result = ctx.gh("auth", "status", "--hostname", ctx.host)
    if result.code == 0:
        return _result("access", PASS, f"gh is signed in to {ctx.host}")
    return _result("access", FAIL, f"gh is not signed in to {ctx.host}: {result.error()}",
                   f"Type this yourself: gh auth login --hostname {ctx.host}. If you have no account there, request access first.")


def _is_local(url: str) -> bool:
    """A path or file:// URL: it can't be a public repository, so it is not a problem."""
    return url.startswith(("file://", "/", ".", "~")) or ("://" not in url and ":" not in url)


def _host_remotes(ctx: Context) -> tuple[dict[str, dict[str, set[str]]], list[str]] | str:
    """({remote: {"fetch": {owner/repo}, "push": {owner/repo}}}, remotes with a URL that is not a repo on the host), or an error.
    Every fetch and push URL counts: git pushes to all of a remote's push URLs. Local paths are ignored."""
    result = run_git(ctx.root, ["remote", "-v"])
    if result.code != 0:
        return f"can't read the git remotes: {result.error()}"
    found: dict[str, dict[str, set[str]]] = {}
    bad: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        name, tab, rest = line.rstrip("\r\n").partition("\t")
        name = name.strip()
        if not tab or not name:  # never skipped (a remote doctor can't read may be public), never printed (it may hold a token)
            return "a line of git remote -v was not understood"
        match = _REMOTE_REST.fullmatch(rest.strip())
        if not match:  # the remote is named, never its URL
            if name not in bad:
                bad.append(name)
            continue
        url, kind = match.groups()
        repo = remote_repo(url, ctx.host)
        if repo:
            found.setdefault(name, {"fetch": set(), "push": set()})[kind].add(repo)
        elif not _is_local(url) and name not in bad:
            bad.append(name)
    return found, bad


def _push_remote(ctx: Context) -> str:
    """The remote the current branch pushes to, in git's order: branch pushRemote, remote.pushDefault, branch remote, origin."""
    name = run_git(ctx.root, ["symbolic-ref", "--short", "-q", "HEAD"]).stdout.strip()
    keys = ([f"branch.{name}.pushRemote"] if name else []) + ["remote.pushDefault"] + ([f"branch.{name}.remote"] if name else [])
    for key in keys:
        value = run_git(ctx.root, ["config", "--get", key]).stdout.strip()
        if value:
            return value
    return "origin"


def check_visibility(ctx: Context) -> dict:
    fix = "Push this folder only to a private or internal repository, then run doctor from its checkout. Push nothing until doctor reads it as private or internal."
    stop = "Stop: push nothing more and tell the platform team. Never make an app repository public."
    read = _host_remotes(ctx)
    if isinstance(read, str):
        return _result("visibility", FAIL, f"the repository's visibility can't be read: {read}", fix)
    remotes, bad = read
    if bad:  # named, never the URL: it may hold a token
        return _result("visibility", FAIL, f"git remote{'s' if len(bad) > 1 else ''} not on {ctx.host} or not a repository URL: {', '.join(bad)}",
                       f"Remove or rename the remote ({'; '.join(f'git remote remove {n}' for n in bad)}): golden-road repos push only to {ctx.host}.")
    repos = sorted({repo for kinds in remotes.values() for group in kinds.values() for repo in group})
    if not repos:
        return _result("visibility", FAIL, f"no git remote points at {ctx.host}, so the repository's visibility can't be read", fix)
    push = _push_remote(ctx)
    kinds = remotes.get(push)
    if not kinds:
        return _result("visibility", FAIL, f"push remote {push} is not on {ctx.host}: pushes would leave the platform", fix)
    pushed = kinds["push"] or kinds["fetch"]
    if len(pushed) > 1:
        return _result("visibility", FAIL, f"push remote {push} pushes to more than one repo", f"Give {push} one push URL (git remote set-url --push {push} <url>).")
    [main] = pushed
    problems, bad_visibility, seen = [], False, []
    for repo in repos:
        result = ctx.gh("repo", "view", repo, "--json", "visibility,nameWithOwner")
        doc = result.json() if result.code == 0 else None
        name = _repo_name(doc.get("nameWithOwner")) if isinstance(doc, dict) else None
        visibility = doc.get("visibility") if isinstance(doc, dict) else None
        if name is None or not isinstance(visibility, str):
            reason = result.error() if result.code else "unexpected output from gh repo view"
            problems.append(f"{repo}: visibility can't be read: {reason}")
        elif visibility not in VISIBLE_OK:
            bad_visibility = True
            problems.append(f"{repo} is {visibility.lower().strip()[:20] or 'not private'}")
        else:
            seen.append(f"{repo} is {visibility.lower()}")
    if problems:
        return _result("visibility", FAIL, "; ".join(problems), stop if bad_visibility else fix)
    ctx.repo = main
    return _result("visibility", PASS, "; ".join(seen))


def check_ruleset(ctx: Context) -> dict:
    if ctx.repo is None:
        return _not_checked("ruleset")
    fix = "Ask a platform admin to apply the golden-road ruleset to main. doctor never changes settings."
    result = ctx.gh("api", f"repos/{ctx.repo}/rules/branches/main?per_page=100")
    if result.code != 0:
        return _could_not_read("ruleset", result, "reading the rules on main", fix)
    rules = result.json()
    if not isinstance(rules, list):
        return _result("ruleset", FAIL, "the rules on main can't be parsed", fix)
    required, assessed_ids = set(), set()
    for rule in rules:
        params = rule.get("parameters") if isinstance(rule, dict) and rule.get("type") == "required_status_checks" else None
        checks = params.get("required_status_checks") if isinstance(params, dict) else None
        for check in checks if isinstance(checks, list) else []:
            if isinstance(check, dict) and isinstance(check.get("context"), str):
                required.add(check["context"])
                if check["context"] == "scorecard / assessed":
                    assessed_ids.add(check.get("integration_id"))
    missing = [c for c in REQUIRED_CONTEXTS if c not in required]
    if missing:
        return _result("ruleset", WARN, f"main doesn't require: {', '.join(missing)}", fix)
    app_id = central_app_id(ctx.platform)
    if app_id is not None and assessed_ids != {app_id}:
        return _result("ruleset", WARN, f"main requires `scorecard / assessed` but it is not pinned to the scorecard App (id {app_id}): "
                       "any workflow job with that name could satisfy it",
                       "Ask a platform admin to pin the ruleset's `scorecard / assessed` to the scorecard App (configure.py --scorecard-mode central, "
                       "then apply the ruleset). doctor never changes settings.")
    return _result("ruleset", PASS, "main requires drift, preview, the scans and the scorecard")


def check_pr_checks(ctx: Context) -> dict:
    if ctx.repo is None:
        return _not_checked("pr-checks")
    branch = run_git(ctx.root, ["symbolic-ref", "--short", "-q", "HEAD"]).stdout.strip()
    if not branch:
        return _result("pr-checks", WARN, "no branch is checked out, so there is no pull request to read", "Check out the branch you opened the pull request from, then run doctor again.")
    fix = "Open the PR's checks (gh pr checks). For golden-road / drift, apply the findings' fix.steps and push; explain any other failure to the user."
    result = ctx.gh("pr", "checks", branch, "--repo", ctx.repo, "--required", "--json", "name,bucket")
    checks = result.json()  # read whatever the exit code: gh exits 8 while checks are pending
    if isinstance(checks, list) and all(isinstance(c, dict) for c in checks):
        failing = [str(c.get("name"))[:100] for c in checks if c.get("bucket") in ("fail", "cancel")]
        pending = [str(c.get("name"))[:100] for c in checks if c.get("bucket") not in ("pass", "skipping", "fail", "cancel")]
        if failing:
            return _result("pr-checks", FAIL, f"required checks failing: {', '.join(failing)}", fix)
        if pending:
            return _result("pr-checks", WARN, f"still running: {', '.join(pending)}", "Wait for the checks to finish, then run doctor again.")
        if not checks:
            return _result("pr-checks", WARN, "no required checks reported on the PR yet", "Wait for the checks to start. If none appear, see ruleset.")
        return _result("pr-checks", PASS, f"all {len(checks)} required checks pass")
    text = result.stderr.lower()
    if "no pull requests found" in text:
        return _result("pr-checks", WARN, "no open pull request for this branch", "Push the branch and open a pull request (after the user says yes).")
    if "no required checks" in text:
        return _result("pr-checks", WARN, "no required checks reported on the PR yet", "Wait for the checks to start. If none appear, see ruleset.")
    return _could_not_read("pr-checks", result, "reading the PR's checks", fix)


def check_intake(ctx: Context) -> dict:
    if ctx.repo is None:
        return _not_checked("intake")
    if ctx.org is None:
        return _result("intake", FAIL, "the installed builder's platform config names no org, so the factory is unknown", PIN_FIX)
    factory = f"{ctx.org}/scorecard-factory"
    ask = "Ask the platform team whether the registration was approved."
    record = ctx.gh("api", f"repos/{factory}/contents/intake/{ctx.repo}.yaml", "--jq", ".sha")
    if record.code == 0:
        return _result("intake", PASS, f"{factory} has an intake record for {ctx.repo}")
    if record.http != 404:
        return _could_not_read("intake", record, f"reading {factory}", ask)
    # A 404 is no record, or no read access to the factory: one read of the factory itself tells them apart.
    factory_read = ctx.gh("api", f"repos/{factory}", "--jq", ".full_name")
    if factory_read.code != 0:
        return _could_not_read("intake", factory_read, f"reading {factory}", ask)
    try:
        from .register import NoPortal, portal_url
        portal_url(load_platform())
        fix = (f"Register the repo: run `golden-road register --repo {ctx.repo}` and give the user the link it prints; they sign in, "
               "check and submit it, and an admin approves it in the portal.")
    except NoPortal as exc:
        fix = exc.fix
    return _result("intake", WARN, "no intake record yet", fix)


# ---- the run ----

CHECKS = (("access", check_access), ("visibility", check_visibility), ("builder-pin", check_builder_pin), ("verify", check_verify),
          ("callers", check_callers), ("ruleset", check_ruleset), ("pr-checks", check_pr_checks), ("intake", check_intake))
CHECK_IDS = tuple(check_id for check_id, _ in CHECKS)


def run_checks(root: Path, pin: Path, host: str) -> list[dict]:
    try:
        platform = load_platform()
    except (OSError, ValueError, AttributeError):
        platform = {}
    ctx = Context(root=root, pin=pin, host=host, org=_org(), platform=platform if isinstance(platform, dict) else {})
    results = []
    for check_id, check in CHECKS:
        try:
            results.append(check(ctx))
        except Exception as exc:  # noqa: BLE001 - a check that crashes fails; it never passes or stops the others
            results.append(_result(check_id, FAIL, f"the check could not run: {type(exc).__name__}", "Send doctor's output to the platform team."))
    return results


def report(checks: list[dict]) -> dict:
    return {"builder_version": VERSION, "status": FAIL if any(c["status"] == FAIL for c in checks) else PASS, "checks": checks}


def summary(doc: dict) -> str:
    failed = [c["id"] for c in doc["checks"] if c["status"] == FAIL]
    warned = [c["id"] for c in doc["checks"] if c["status"] == WARN]
    head = f"golden-road doctor: {len(failed)} failed ({', '.join(failed)})" if failed else "golden-road doctor: ready"
    if warned:
        head += f"; {len(warned)} warning{'s' if len(warned) > 1 else ''} ({', '.join(warned)})"
    width = max(len(c["id"]) for c in doc["checks"])
    lines = [head]
    for c in doc["checks"]:
        lines.append(f"  {c['status']:<4}  {c['id']:<{width}}  {c['message']}")
        if c["status"] != PASS and c["fix"]:
            lines.append(f"  {'':<4}  {'':<{width}}  fix: {c['fix']}")
    return "\n".join(lines)

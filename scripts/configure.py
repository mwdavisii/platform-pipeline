#!/usr/bin/env python3
"""Fill in the org-specific values (org, model gateway host, optional edge selector) and regenerate hello-app and golden-road-portal.

Usage: scripts/configure.py --org <org> --gateway-host <host> --edge-namespace NS --edge-label KEY=VALUE [...]
                            --identity-header NAME [--scorecard-gateway-url URL] [--single-approver]
                            [--github-host HOST] [--cert-oidc-issuer URL]
                            [--landing-page-url URL] [--approved-models-url URL] [--portal-url URL]
                            [--scorecard-mode {per-app,central} [--scorecard-app-id ID]]

--scorecard-gateway-url is the chat-completions URL the scorecard's review agent calls from the CI runners. It
defaults to https://<gateway host>/v1/chat/completions; pass it when the runners reach the gateway at another
address (a public route to a gateway that the apps reach inside the cluster, or an API-management front end).
An APIM deployment URL may write `{model}` where the deployment name goes; the scorecard fills it from the SCORECARD_GATEWAY_MODEL secret.

--github-host and --cert-oidc-issuer name the GitHub host the platform lives on and the issuer of its Actions OIDC tokens. They are
recorded in platform/platform.json (doctor reads them there) and written into the golden-road-agents kit (the stub trusts that
host's release-skill.yml with that issuer). Once recorded they can't change.

--landing-page-url and --approved-models-url are the two links in the AGENTS.md block of every app. The landing page defaults to
the README of golden-road-agents; the approved-models link has no default. A later run may change them.

--portal-url is the golden road portal's address (https, no path). It is recorded in platform/platform.json, and `golden-road register` and
`golden-road doctor` read it from the installed builder's platform config: an agent can't be steered to another portal. Like the links, a
later run may change it. There is no default: without it `register` says to ask the platform team.

--single-approver is for the personal PoC only, for an owner with no second GitHub account. It records
gate2_single_approver in platform/platform.json and relaxes the approval settings of hello-app, the scorecard factory and golden-road-agents (no PR approvals
required; the person who triggers a promotion may approve it). Never use it for a real organization.

--scorecard-mode central (central AI review) needs --scorecard-app-id, the scorecard GitHub App's App ID. It records scorecard_mode and
scorecard_app_id in platform/platform.json, re-pins `scorecard / assessed` in the rulesets of hello-app and the portal to the App, and
removes the scorecard caller workflows (hello-app, the portal and the kit template): the factory's poller assesses instead. The default,
per-app, records nothing and changes nothing. A later run may change the App ID; going back to per-app needs a restore of bootstrap and platform.

The edge selector and identity header come from the deploy owners. There are no defaults: the committed
platform/platform.json carries `replace-me` placeholders for them, so an unconfigured tree is refused by the
builder release and by push-bootstrap.sh.
"""

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

ORG = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")  # GitHub: alphanumeric segments, single hyphens
DNS_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
HOST_TLD = re.compile(r"[a-z]([a-z0-9-]*[a-z0-9])?")
LABEL_NAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?")
LABEL_VALUE = re.compile(r"([A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?)?")  # empty is valid in Kubernetes
HEADER_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,63}")
PLACEHOLDER = b"ORG_PLACEHOLDER"
DEFAULT_HOST = "github.com"
DEFAULT_ISSUER = "https://token.actions.githubusercontent.com"
GHE_TENANT = re.compile(r"([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)\.ghe\.com")
# A link in the AGENTS.md block: https, a host, an optional port and path; no user info, spaces, quotes or brackets (it sits in markdown).
LINK_URL = re.compile(r"https://([A-Za-z0-9.-]+)(:[0-9]{1,5})?(/[A-Za-z0-9._~%/?&=+#-]*)?")
PORTAL_URL = re.compile(r"https://([A-Za-z0-9.-]+)(:[0-9]{1,5})?/?")  # the portal answers on its own origin: no path, so its __Host- cookie works
ISSUER_URL = re.compile(r"https://([A-Za-z0-9.-]+)((?:/[A-Za-z0-9._~-]+)*)")  # no query, no trailing slash
# The kit files that name the platform's GitHub host or its Actions OIDC issuer, with how many times each does in the committed
# tree (host, issuer). They can't read the platform config, so configure.py rewrites them, and refuses before writing anything
# when a count differs. A test keeps this table in step with the tree.
HOST_SITES = {
    "bootstrap/golden-road-agents/README.md": (2, 0),
    "bootstrap/golden-road-agents/INSTALL_FOR_AGENTS.md": (3, 1),
    "bootstrap/golden-road-agents/stub/golden-road/fetch_skill.py": (1, 1),
    "bootstrap/golden-road-agents/skill/install_builder.py": (1, 1),
}
SCORECARD_CONFIG = Path("scorecard-factory/config/scorecard.yaml")
SCORECARD_URL_PLACEHOLDER = "https://llm.replace-me.invalid/v1/chat/completions"
# The scorecard config schema's gateway URL rule, without query-string quotes or spaces so it can sit in quoted YAML.
SCORECARD_URL = re.compile(r"https://([A-Za-z0-9.-]+)(:[0-9]{1,5})?/(?:[A-Za-z0-9._~%/?&=+-]|\{model\})*")  # {model}: see scorecard.config
# Every file that holds a placeholder in the committed, unconfigured tree, and the text that proves it still does.
# configure.py rewrites all of them. scripts/export-public-poc.sh reads this list (--print-placeholder-state) and
# refuses to export a tree in which any of them has been configured; a test keeps it in step with the tree.
PLACEHOLDER_STATE = {
    "platform/platform.json": "llm.replace-me.invalid",
    "bootstrap/hello-app/builder-spec.json": "llm.replace-me.invalid",
    "bootstrap/hello-app/deploy/base/cilium-egress.yaml": "llm.replace-me.invalid",
    "bootstrap/hello-app/deploy/base/networkpolicy.yaml": "replace-me",
    "bootstrap/hello-app/_platform/authz-policy.json": "X-replace-me",
    "bootstrap/hello-app/.github/workflows/ci.yml": "ORG_PLACEHOLDER",
    "bootstrap/hello-app/.github/workflows/promote.yml": "ORG_PLACEHOLDER",
    "bootstrap/hello-app/.github/workflows/security-scans.yml": "ORG_PLACEHOLDER",
    "bootstrap/hello-app/.github/CODEOWNERS": "ORG_PLACEHOLDER",
    "bootstrap/hello-app/.github/workflows/scorecard.yml": "ORG_PLACEHOLDER",
    "bootstrap/golden-road-portal/deploy/base/networkpolicy.yaml": "replace-me",
    "bootstrap/golden-road-portal/_platform/authz-policy.json": "X-replace-me",
    "bootstrap/golden-road-portal/.github/workflows/ci.yml": "ORG_PLACEHOLDER",
    "bootstrap/golden-road-portal/.github/workflows/promote.yml": "ORG_PLACEHOLDER",
    "bootstrap/golden-road-portal/.github/workflows/security-scans.yml": "ORG_PLACEHOLDER",
    "bootstrap/golden-road-portal/.github/workflows/scorecard.yml": "ORG_PLACEHOLDER",
    "bootstrap/golden-road-portal/.github/CODEOWNERS": "ORG_PLACEHOLDER",
    "bootstrap/scorecard-factory/intake/ORG_PLACEHOLDER/golden-road-portal.yaml": "ORG_PLACEHOLDER",
    "bootstrap/scorecard-factory/.github/CODEOWNERS": "ORG_PLACEHOLDER",
    "bootstrap/scorecard-factory/.github/workflows/factory-check.yml": "ORG_PLACEHOLDER",
    "bootstrap/scorecard-factory/.github/workflows/scorecard.yml": "ORG_PLACEHOLDER",
    "bootstrap/scorecard-factory/.github/workflows/scorecard-poll.yml": "ORG_PLACEHOLDER",
    "bootstrap/scorecard-factory/.github/workflows/scorecard-assess.yml": "ORG_PLACEHOLDER",
    "bootstrap/scorecard-factory/config/scorecard.yaml": "llm.replace-me.invalid",
    "bootstrap/scorecard-factory/intake/ORG_PLACEHOLDER/hello-app.yaml": "ORG_PLACEHOLDER",
    "bootstrap/golden-road-agents/README.md": "ORG_PLACEHOLDER",
    "bootstrap/golden-road-agents/.github/CODEOWNERS": "ORG_PLACEHOLDER",
    "bootstrap/golden-road-agents/stub/golden-road/fetch_skill.py": "ORG_PLACEHOLDER",
    "bootstrap/golden-road-agents/INSTALL_FOR_AGENTS.md": "ORG_PLACEHOLDER",
    "bootstrap/golden-road-agents/skill/install_builder.py": "ORG_PLACEHOLDER",
    "bootstrap/golden-road-agents/skill/templates/workflows/ci.yml": "ORG_PLACEHOLDER",
    "bootstrap/golden-road-agents/skill/templates/workflows/promote.yml": "ORG_PLACEHOLDER",
    "bootstrap/golden-road-agents/skill/templates/workflows/scorecard.yml": "ORG_PLACEHOLDER",
    "bootstrap/golden-road-agents/skill/templates/workflows/security-scans.yml": "ORG_PLACEHOLDER",
}

RULESET_REL = Path(".github/rulesets/main.json")
FACTORY_REVIEW_RULESET_REL = Path(".github/rulesets/main-review.json")  # the factory's pull_request rule lives in its own ruleset
PROD_ENV_REL = Path(".github/environment-prod.json")


def _relaxed_ruleset(path: Path) -> bytes:
    ruleset = json.loads(read_regular(path).decode("utf-8"))
    rules = [r for r in ruleset.get("rules", []) if isinstance(r, dict) and r.get("type") == "pull_request"]
    if len(rules) != 1 or not isinstance(rules[0].get("parameters"), dict):
        raise ValueError(f"{path} must hold exactly one pull_request rule")
    rules[0]["parameters"].update(required_approving_review_count=0, require_code_owner_review=False,
                                  require_last_push_approval=False)
    return (json.dumps(ruleset, indent=2) + "\n").encode("utf-8")


def single_approver_files(app: Path, factory: Path | None = None, agents: Path | None = None, portal: Path | None = None) -> dict[Path, bytes]:
    """New content for the files that --single-approver relaxes: hello-app's ruleset and prod environment (and the portal's, when the
    tree has it), and the review ruleset of the scorecard factory (main-review) and the main ruleset of golden-road-agents when the tree has them. Raises ValueError
    if a file is not as expected."""
    files = {}
    for target in (app, portal):
        if target is None or (target is not app and not is_real_dir(target)):
            continue
        ruleset_path, env_path = target / RULESET_REL, target / PROD_ENV_REL
        files[ruleset_path] = _relaxed_ruleset(ruleset_path)
        env = json.loads(read_regular(env_path).decode("utf-8"))
        if not isinstance(env, dict) or "prevent_self_review" not in env:
            raise ValueError(f"{env_path} has no prevent_self_review")
        env["prevent_self_review"] = False
        files[env_path] = (json.dumps(env, indent=2) + "\n").encode("utf-8")
    for repo, rel in ((factory, FACTORY_REVIEW_RULESET_REL), (agents, RULESET_REL)):
        if repo is not None and is_real_dir(repo):
            files[repo / rel] = _relaxed_ruleset(repo / rel)
    return files


SCORECARD_CONTEXT = "scorecard / assessed"
# In central mode the platform's scorecard callers are not needed: the factory's poller assesses every ready PR. configure.py removes them.
SCORECARD_CALLERS = ("hello-app/.github/workflows/scorecard.yml", "golden-road-portal/.github/workflows/scorecard.yml",
                     "golden-road-agents/skill/templates/workflows/scorecard.yml")  # under bootstrap/
_ASSESSED_ENTRY = re.compile(r'("context"\s*:\s*"scorecard / assessed"\s*,\s*"integration_id"\s*:\s*)\d+')
APP_ID = re.compile(r"[1-9][0-9]{0,9}")


def scorecard_ruleset(data: bytes, integration_id: int) -> bytes:
    """The ruleset with `scorecard / assessed` required from the scorecard App (its integration id) instead of the Actions app.
    Raises ValueError unless exactly one required check is named `scorecard / assessed`. A file written in the usual
    `{"context": ..., "integration_id": ...}` form keeps its formatting; any other form is rewritten as JSON."""
    ruleset = json.loads(data.decode("utf-8"))
    entries = [c for rule in ruleset.get("rules", []) if isinstance(rule, dict) and rule.get("type") == "required_status_checks"
               for c in (rule.get("parameters") or {}).get("required_status_checks", []) if isinstance(c, dict) and c.get("context") == SCORECARD_CONTEXT]
    if len(entries) != 1:
        raise ValueError(f"the ruleset must require {SCORECARD_CONTEXT!r} exactly once (it has {len(entries)})")
    text = data.decode("utf-8")
    if len(_ASSESSED_ENTRY.findall(text)) == 1:
        return _ASSESSED_ENTRY.sub(lambda m: f"{m.group(1)}{integration_id}", text).encode("utf-8")
    entries[0]["integration_id"] = integration_id
    return (json.dumps(ruleset, indent=2) + "\n").encode("utf-8")


def valid_host(host: str) -> bool:
    labels = host.split(".")
    return (len(host) <= 253 and len(labels) >= 2 and all(DNS_LABEL.fullmatch(x) for x in labels)
            and HOST_TLD.fullmatch(labels[-1]) is not None)


def valid_label(label: str) -> bool:
    key, sep, value = label.partition("=")
    if not sep or not LABEL_VALUE.fullmatch(value):
        return False
    prefix, slash, name = key.rpartition("/")
    if slash and not (prefix and len(prefix) <= 253 and all(DNS_LABEL.fullmatch(x) for x in prefix.split("."))):
        return False
    return LABEL_NAME.fullmatch(name) is not None


def default_issuer(host: str) -> str | None:
    """The Actions OIDC issuer of github.com and of a ghe.com tenant; None for any other host (it must be given)."""
    if host == DEFAULT_HOST:
        return DEFAULT_ISSUER
    tenant = GHE_TENANT.fullmatch(host)
    return f"https://token.actions.{tenant.group(1)}.ghe.com" if tenant else None


def github_identity(platform: dict, host: str | None, issuer: str | None) -> tuple[str, str]:
    """The platform's GitHub host and Actions OIDC issuer. The stub's trusted signer depends on them, so once recorded they
    can't change. Raises ValueError with what to do."""
    recorded_host, recorded_issuer = platform.get("github_host"), platform.get("actions_oidc_issuer")
    host = (host or recorded_host or DEFAULT_HOST).lower()
    issuer = issuer or (recorded_issuer if recorded_host == host else None) or default_issuer(host)
    if issuer is None:
        raise ValueError(f"pass --cert-oidc-issuer for {host}: only github.com and *.ghe.com have a known Actions OIDC issuer")
    if recorded_host is not None and (recorded_host, recorded_issuer) != (host, issuer):
        raise ValueError(f"the GitHub host is already {recorded_host} (issuer {recorded_issuer}); "
                         "restore with git checkout -- bootstrap platform to change it")
    return host, issuer


def host_changes(root: Path, host: str, issuer: str, changes: dict[Path, bytes]) -> dict[Path, bytes]:
    """The kit's files with the host and issuer filled in; nothing for a tree without the kit. Each file must still name
    github.com and its issuer exactly as often as HOST_SITES says. Raises ValueError otherwise."""
    if not is_real_dir(root / "bootstrap/golden-road-agents"):
        return {}
    out = {}
    for rel, (hosts, issuers) in HOST_SITES.items():
        path = root / rel
        text = (changes.get(path) or read_regular(path)).decode("utf-8")
        if text.count(DEFAULT_HOST) != hosts or text.count(DEFAULT_ISSUER) != issuers:
            raise ValueError(f"{rel} must name {DEFAULT_HOST} {hosts} times and {DEFAULT_ISSUER} {issuers} times")
        new = text.replace(DEFAULT_ISSUER, "\x00").replace(DEFAULT_HOST, host).replace("\x00", issuer)
        if new != text:
            out[path] = new.encode("utf-8")
    return out


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Replace a regular file's content without following links; keeps its permission bits."""
    mode = stat.S_IMODE(os.lstat(path).st_mode)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".configure-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def read_regular(path: Path) -> bytes:
    """Read a regular file's bytes, refusing symlinks."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        return handle.read()


def is_real_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def find_placeholders(bootstrap: Path, org: str) -> dict[Path, bytes]:
    """New content for every regular file containing the placeholder. Never follows symlinks."""
    changes = {}
    replacement = org.encode("ascii")
    for dirpath, dirnames, filenames in os.walk(bootstrap, followlinks=False):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            path = Path(dirpath) / name
            if not stat.S_ISREG(os.lstat(path).st_mode):
                continue
            try:
                data = read_regular(path)
            except OSError as exc:
                sys.exit(f"cannot read {path}: {exc}")
            if PLACEHOLDER not in data:
                continue
            try:
                data.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                sys.exit(f"cannot rewrite {path}: it contains {PLACEHOLDER.decode()} but is not valid UTF-8")
            changes[path] = data.replace(PLACEHOLDER, replacement)
    return changes


def placeholder_dirs(bootstrap: Path) -> list[Path]:
    """Directories named ORG_PLACEHOLDER (the factory's per-owner record directories), deepest first. Never follows symlinks."""
    found = []
    for dirpath, dirnames, _ in os.walk(bootstrap, followlinks=False):
        dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
        found += [Path(dirpath) / d for d in dirnames if d == PLACEHOLDER.decode()]
    return sorted(found, key=lambda p: len(p.parts), reverse=True)


def scorecard_config(bootstrap: Path, url: str, changes: dict[Path, bytes]) -> dict[Path, bytes]:
    """The factory config with its gateway URL filled in, or nothing when it already has this URL (or the tree has no
    factory). Raises ValueError otherwise."""
    path = bootstrap / SCORECARD_CONFIG
    if not is_real_dir(bootstrap / SCORECARD_CONFIG.parts[0]):
        return {}
    text = (changes.get(path) or read_regular(path)).decode("utf-8")
    if SCORECARD_URL_PLACEHOLDER in text:
        if text.count(SCORECARD_URL_PLACEHOLDER) != 1:
            raise ValueError(f"{path} names the placeholder gateway URL more than once")
        new = text.replace(SCORECARD_URL_PLACEHOLDER, url)
        loadable(new)
        return {path: new.encode("utf-8")}
    import yaml

    loadable(text)
    current = yaml.safe_load(text)["agent"]["gateway"]["url"]
    if current != url:
        raise ValueError(f"the scorecard gateway URL is already {current}; restore with git checkout -- bootstrap platform to change it")
    return {}


def loadable(config_text: str) -> None:
    """Load the candidate config with the scorecard's own loader (from a temporary factory directory), so a config it would
    refuse stops configure.py before anything is written. Raises ValueError with the loader's message."""
    from golden_road.io import InputError
    from scorecard.config import load_config

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "config").mkdir()
        (Path(tmp) / "config/scorecard.yaml").write_text(config_text, encoding="utf-8")
        try:
            load_config(tmp)
        except InputError as exc:
            raise ValueError(f"the scorecard config does not load: {exc}") from exc


BUILDER_APPS = ("hello-app", "golden-road-portal")  # bootstrap apps the builder generates; configure and the version bump regenerate both


def github_hosts(host: str) -> list[str]:
    """The GitHub web and API hosts, the portal's only egress in slice 1 (identity linking)."""
    api = "api.github.com" if host == "github.com" else (f"api.{host}" if host.endswith(".ghe.com") else host)
    return sorted({host, api})


def portal_spec(spec: dict, host: str) -> dict:
    """The portal's spec with its egress set to the GitHub host(s) of this platform."""
    hosts = github_hosts(host)
    if len(hosts) == 1:  # GitHub Enterprise Server: one host serves both
        egress = [{"host": host, "port": 443, "reason": "GitHub API and OAuth: identity linking"}]
    else:
        egress = [{"host": h, "port": 443, "reason": "GitHub OAuth: identity linking" if h == host else "GitHub API: identity linking"} for h in hosts]
    return {**spec, "egress": egress}


def regenerate_apps(root: Path, specs: dict | None = None) -> None:
    """Regenerate every builder app in bootstrap/ from its spec with the tree's own platform config: the last step of a configure
    run, and what scripts/bump-builder-version.py repeats after a version change. Needs root/src importable. Raises BuilderError."""
    from builder.generate import generate

    os.environ["BUILDER_PLATFORM_CONFIG"] = str(root / "platform/platform.json")
    for name in BUILDER_APPS:
        app = root / "bootstrap" / name
        if not is_real_dir(app):
            continue
        spec = (specs or {}).get(name) or json.loads(read_regular(app / "builder-spec.json").decode("utf-8"))
        generate(app, spec)


def main() -> int:
    if sys.argv[1:] == ["--print-placeholder-state"]:
        for rel, marker in PLACEHOLDER_STATE.items():
            print(f"{rel}\t{marker}")
        return 0
    if sys.argv[1:] == ["--print-host-sites"]:
        for rel, (hosts, issuers) in HOST_SITES.items():
            print(f"{rel}\t{hosts}\t{issuers}")
        return 0
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--org", required=True)
    p.add_argument("--gateway-host", required=True)
    p.add_argument("--edge-namespace", required=True)
    p.add_argument("--edge-label", action="append", default=[])
    p.add_argument("--identity-header", required=True)
    p.add_argument("--scorecard-gateway-url", default=None,
                   help="the chat-completions URL the scorecard's review agent calls (default https://<gateway host>/v1/chat/completions)")
    p.add_argument("--single-approver", action="store_true",
                   help="PoC only: no second GitHub account; PRs need no approval and the triggerer may approve a promotion")
    p.add_argument("--github-host", default=None, help="the platform's GitHub host (default github.com; a ghe.com tenant for data residency)")
    p.add_argument("--cert-oidc-issuer", default=None,
                   help="the Actions OIDC issuer of that host (derived for github.com and *.ghe.com; required for any other host, "
                        "and for a host whose issuer an enterprise has customized)")
    p.add_argument("--landing-page-url", default=None,
                   help="the golden-road landing page the AGENTS.md block links to (default https://<github host>/<org>/golden-road-agents)")
    p.add_argument("--approved-models-url", default=None, help="the approved-models policy the AGENTS.md block links to (default: not published yet)")
    p.add_argument("--portal-url", default=None, help="the golden road portal's address (https, no path); golden-road register links to it")
    p.add_argument("--scorecard-mode", choices=("per-app", "central"), default=None,
                   help="per-app (default): each app calls the factory's scorecard workflow. central: the factory's poller assesses every ready PR; "
                        "the rulesets require `scorecard / assessed` from the scorecard App and the apps' scorecard callers are removed")
    p.add_argument("--scorecard-app-id", default=None, help="the scorecard GitHub App's id (the App ID, not the client ID); required with --scorecard-mode central")
    args = p.parse_args()
    if args.scorecard_app_id is not None and (not APP_ID.fullmatch(args.scorecard_app_id) or int(args.scorecard_app_id) >= 2**31):
        sys.exit("invalid --scorecard-app-id (the App ID: a positive whole number below 2147483648)")
    if not ORG.fullmatch(args.org) or len(args.org) > 39:
        sys.exit("invalid --org")
    if not valid_host(args.gateway_host):
        sys.exit("invalid --gateway-host")
    if not DNS_LABEL.fullmatch(args.edge_namespace):
        sys.exit("invalid --edge-namespace")
    if not args.edge_label:
        sys.exit("at least one --edge-label KEY=VALUE is required")
    if not all(valid_label(label) for label in args.edge_label):
        sys.exit("invalid --edge-label (use KEY=VALUE)")
    keys = [label.split("=", 1)[0] for label in args.edge_label]
    if len(set(keys)) != len(keys):
        sys.exit("invalid --edge-label: a key is repeated")
    if any(not label.split("=", 1)[1] for label in args.edge_label):
        sys.exit("invalid --edge-label: the value must not be empty")
    if not HEADER_TOKEN.fullmatch(args.identity_header):
        sys.exit("invalid --identity-header (an HTTP header name: letters, digits, hyphens)")
    if args.github_host is not None and not valid_host(args.github_host.lower()):
        sys.exit("invalid --github-host")
    if args.cert_oidc_issuer is not None:
        issuer_match = ISSUER_URL.fullmatch(args.cert_oidc_issuer)
        if not issuer_match or not valid_host(issuer_match.group(1).lower()):
            sys.exit("invalid --cert-oidc-issuer (an https URL with a host name and an optional path, no query, no trailing slash)")
        # A token's issuer claim is compared as written, so keep one spelling: the host in lowercase.
        args.cert_oidc_issuer = f"https://{issuer_match.group(1).lower()}{issuer_match.group(2)}"
    for flag, value in (("--landing-page-url", args.landing_page_url), ("--approved-models-url", args.approved_models_url)):
        if value is not None:
            link = LINK_URL.fullmatch(value)
            if not link or not valid_host(link.group(1).lower()):
                sys.exit(f"invalid {flag} (an https URL with a host name; no user info, spaces, quotes or brackets)")
    if args.portal_url is not None:
        portal_match = PORTAL_URL.fullmatch(args.portal_url)
        if not portal_match or not valid_host(portal_match.group(1).lower()):
            sys.exit("invalid --portal-url (an https URL with a host name and no path)")
    url = args.scorecard_gateway_url or f"https://{args.gateway_host}/v1/chat/completions"
    match = SCORECARD_URL.fullmatch(url)
    if not match or not valid_host(match.group(1).lower()):
        sys.exit("invalid --scorecard-gateway-url (an https URL with a host name and a path, no user info or fragment)")
    for value in (args.org, args.gateway_host, args.edge_namespace, args.identity_header, url, args.github_host or "",
                  args.cert_oidc_issuer or "", args.landing_page_url or "", args.approved_models_url or "", args.portal_url or "", *args.edge_label):
        if "replace-me" in value.lower():
            sys.exit(f"{value!r} is still a placeholder")

    root = args.root.resolve()
    platform_dir, bootstrap = root / "platform", root / "bootstrap"
    for directory in (platform_dir, bootstrap):
        if not is_real_dir(directory):
            sys.exit(f"{directory} must be a real directory")
    platform_path = platform_dir / "platform.json"
    app = bootstrap / "hello-app"
    spec_path = app / "builder-spec.json"
    for path in (platform_path, spec_path):
        if path.is_symlink() or not path.is_file():
            sys.exit(f"{path} must be a regular file")

    # Phase 1: compute and validate everything in memory. Nothing is written until all of it passes.
    if (root / "src").is_dir():
        sys.path.insert(0, str(root / "src"))
    from builder.config import is_denied_egress, is_model_provider
    from builder.errors import BuilderError
    from builder.spec import check_spec

    platform = json.loads(read_regular(platform_path).decode("utf-8"))
    if is_model_provider(args.gateway_host, {**platform, "model_gateway_hosts": []}):
        sys.exit("--gateway-host is a public model provider; use the platform gateway")
    if is_denied_egress(args.gateway_host, platform):
        sys.exit("--gateway-host is a denied egress host")
    try:
        host, issuer = github_identity(platform, args.github_host, args.cert_oidc_issuer)
    except ValueError as exc:
        sys.exit(str(exc))
    url_host = match.group(1).lower()
    if is_model_provider(url_host, {**platform, "model_gateway_hosts": []}) or is_denied_egress(url_host, platform):
        sys.exit("--scorecard-gateway-url is a public model provider or a denied host; use the platform gateway")

    changes = find_placeholders(bootstrap, args.org)
    first_run = bool(changes)  # host_changes below adds to `changes` on a tree that is already configured
    if changes:
        platform["org"] = args.org
    elif platform.get("org") is None:
        sys.exit("no placeholders remain and no org is recorded in platform/platform.json; "
                 "restore with git checkout -- bootstrap platform, then run again")
    elif platform["org"] != args.org:
        sys.exit(f"bootstrap already configured for org {platform['org']}; "
                 "restore with git checkout -- bootstrap platform to change it")
    if platform.get("github_host") is None:
        try:
            changes.update(host_changes(root, host, issuer, changes))
        except (ValueError, OSError) as exc:
            sys.exit(f"cannot set the GitHub host in the kit: {exc}")
    platform["github_host"], platform["actions_oidc_issuer"] = host, issuer
    # The links are not part of any trust decision, so a later run may change them (a builder release then regenerates every app).
    platform["landing_page_url"] = args.landing_page_url or platform.get("landing_page_url") or f"https://{host}/{args.org}/golden-road-agents"
    if args.approved_models_url:
        platform["approved_models_url"] = args.approved_models_url
    if args.portal_url:
        platform["portal_url"] = args.portal_url.rstrip("/")
    recorded = platform.get("gate2_single_approver", False)
    if type(recorded) is not bool:
        sys.exit("gate2_single_approver in platform/platform.json must be true or false")
    if not first_run and recorded != args.single_approver:
        sys.exit(f"bootstrap already configured {'with' if recorded else 'without'} --single-approver; "
                 "restore with git checkout -- bootstrap platform to change it")
    platform["gate2_single_approver"] = args.single_approver
    from builder.config import scorecard_mode as recorded_scorecard_mode
    try:
        recorded_mode = recorded_scorecard_mode(platform)
    except ValueError as exc:
        sys.exit(str(exc))
    mode = args.scorecard_mode or recorded_mode
    if recorded_mode == "central" and mode != "central":
        sys.exit("bootstrap already configured with --scorecard-mode central; restore with git checkout -- bootstrap platform to change it")
    if mode != "central" and args.scorecard_app_id is not None:
        sys.exit("--scorecard-app-id is only for --scorecard-mode central")
    app_id = int(args.scorecard_app_id) if args.scorecard_app_id is not None else platform.get("scorecard_app_id")
    if mode == "central":
        if app_id is None:
            sys.exit("--scorecard-mode central needs --scorecard-app-id (the scorecard GitHub App's App ID)")
        platform["scorecard_mode"], platform["scorecard_app_id"] = "central", app_id
    platform["model_gateway_hosts"] = [args.gateway_host]
    platform["edge_ingress"]["namespace"] = args.edge_namespace
    platform["edge_ingress"]["pod_labels"] = dict(label.split("=", 1) for label in args.edge_label)
    platform["identity_header"] = args.identity_header

    spec = json.loads(changes.get(spec_path, None) or read_regular(spec_path))
    spec["egress"] = [{"host": args.gateway_host, "port": 443, "reason": "model calls through the platform gateway"}]
    errors, questions, _ = check_spec(spec, app, platform)
    if errors or questions:
        sys.exit(f"hello-app spec does not validate: {errors or questions}")
    portal_app, specs = bootstrap / "golden-road-portal", {"hello-app": spec}
    if is_real_dir(portal_app):
        portal_spec_path = portal_app / "builder-spec.json"
        if portal_spec_path.is_symlink() or not portal_spec_path.is_file():
            sys.exit(f"{portal_spec_path} must be a regular file")
        pspec = portal_spec(json.loads(read_regular(portal_spec_path).decode("utf-8")), host)
        errors, questions, _ = check_spec(pspec, portal_app, platform)
        if errors or questions:
            sys.exit(f"golden-road-portal spec does not validate: {errors or questions}")
        specs["golden-road-portal"] = pspec

    try:
        changes.update(scorecard_config(bootstrap, url, changes))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        sys.exit(f"cannot set the scorecard gateway URL: {exc}")
    renames = placeholder_dirs(bootstrap)
    for directory in renames:
        if (directory.parent / args.org).exists():
            sys.exit(f"cannot rename {directory}: {directory.parent / args.org} already exists")

    if args.single_approver:
        try:
            changes.update(single_approver_files(app, bootstrap / "scorecard-factory", bootstrap / "golden-road-agents", portal_app))
        except (ValueError, OSError) as exc:
            sys.exit(f"cannot apply --single-approver: {exc}")

    removals = []
    if mode == "central":
        for target in (app, portal_app):
            if not is_real_dir(target):
                continue
            ruleset_path = target / RULESET_REL
            try:
                before = changes.get(ruleset_path) or read_regular(ruleset_path)
                after = scorecard_ruleset(before, app_id)
            except (ValueError, OSError) as exc:
                sys.exit(f"cannot pin the scorecard check in {ruleset_path}: {exc}")
            if after != before:
                changes[ruleset_path] = after
        removals = [bootstrap / rel for rel in SCORECARD_CALLERS if os.path.lexists(bootstrap / rel)]
        for path in removals:
            changes.pop(path, None)

    # Phase 2: write, then regenerate.
    write_bytes_atomic(platform_path, (json.dumps(platform, indent=2) + "\n").encode("utf-8"))
    for path, data in changes.items():
        write_bytes_atomic(path, data)
    for path in removals:  # never through a link: unlink removes the link itself
        os.unlink(path)
    for directory in renames:  # deepest first, after the files inside them are written
        try:
            os.rename(directory, directory.parent / args.org)
        except OSError as exc:
            sys.exit(f"cannot rename {directory} to {args.org}: {exc}. The tree is partly configured; "
                     "run `git checkout -- bootstrap platform` and fix the cause before configuring again")
    try:
        regenerate_apps(root, specs)
    except BuilderError as exc:
        sys.exit(f"regenerate failed: {exc.errors or exc.questions}")
    print(f"configured org={args.org} github={host} gateway={args.gateway_host} scorecard gateway={url}; regenerated {', '.join(n for n in BUILDER_APPS if n in specs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

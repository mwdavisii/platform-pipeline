#!/usr/bin/env python3
"""Fill in the org-specific values (org, model gateway host, optional edge selector) and regenerate hello-app.

Usage: scripts/configure.py --org <org> --gateway-host <host> --edge-namespace NS --edge-label KEY=VALUE [...]
                            --identity-header NAME [--single-approver]

--single-approver is for the personal PoC only, for an owner with no second GitHub account. It records
gate2_single_approver in platform/platform.json and relaxes the approval settings of hello-app (no PR approvals
required; the person who triggers a promotion may approve it). Never use it for a real organization.

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
}

RULESET_REL = Path(".github/rulesets/main.json")
PROD_ENV_REL = Path(".github/environment-prod.json")


def single_approver_files(app: Path) -> dict[Path, bytes]:
    """New content for the hello-app files that --single-approver relaxes. Raises ValueError if a file is not as expected."""
    ruleset_path, env_path = app / RULESET_REL, app / PROD_ENV_REL
    ruleset = json.loads(read_regular(ruleset_path).decode("utf-8"))
    rules = [r for r in ruleset.get("rules", []) if isinstance(r, dict) and r.get("type") == "pull_request"]
    if len(rules) != 1 or not isinstance(rules[0].get("parameters"), dict):
        raise ValueError(f"{ruleset_path} must hold exactly one pull_request rule")
    rules[0]["parameters"].update(required_approving_review_count=0, require_code_owner_review=False,
                                  require_last_push_approval=False)
    env = json.loads(read_regular(env_path).decode("utf-8"))
    if not isinstance(env, dict) or "prevent_self_review" not in env:
        raise ValueError(f"{env_path} has no prevent_self_review")
    env["prevent_self_review"] = False
    return {path: (json.dumps(doc, indent=2) + "\n").encode("utf-8") for path, doc in ((ruleset_path, ruleset), (env_path, env))}


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


def main() -> int:
    if sys.argv[1:] == ["--print-placeholder-state"]:
        for rel, marker in PLACEHOLDER_STATE.items():
            print(f"{rel}\t{marker}")
        return 0
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--org", required=True)
    p.add_argument("--gateway-host", required=True)
    p.add_argument("--edge-namespace", required=True)
    p.add_argument("--edge-label", action="append", default=[])
    p.add_argument("--identity-header", required=True)
    p.add_argument("--single-approver", action="store_true",
                   help="PoC only: no second GitHub account; PRs need no approval and the triggerer may approve a promotion")
    args = p.parse_args()
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
    for value in (args.org, args.gateway_host, args.edge_namespace, args.identity_header, *args.edge_label):
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
    from builder.generate import generate
    from builder.spec import check_spec

    platform = json.loads(read_regular(platform_path).decode("utf-8"))
    if is_model_provider(args.gateway_host, {**platform, "model_gateway_hosts": []}):
        sys.exit("--gateway-host is a public model provider; use the platform gateway")
    if is_denied_egress(args.gateway_host, platform):
        sys.exit("--gateway-host is a denied egress host")

    changes = find_placeholders(bootstrap, args.org)
    if changes:
        platform["org"] = args.org
    elif platform.get("org") is None:
        sys.exit("no placeholders remain and no org is recorded in platform/platform.json; "
                 "restore with git checkout -- bootstrap platform, then run again")
    elif platform["org"] != args.org:
        sys.exit(f"bootstrap already configured for org {platform['org']}; "
                 "restore with git checkout -- bootstrap platform to change it")
    recorded = platform.get("gate2_single_approver", False)
    if type(recorded) is not bool:
        sys.exit("gate2_single_approver in platform/platform.json must be true or false")
    if not changes and recorded != args.single_approver:
        sys.exit(f"bootstrap already configured {'with' if recorded else 'without'} --single-approver; "
                 "restore with git checkout -- bootstrap platform to change it")
    platform["gate2_single_approver"] = args.single_approver
    platform["model_gateway_hosts"] = [args.gateway_host]
    platform["edge_ingress"]["namespace"] = args.edge_namespace
    platform["edge_ingress"]["pod_labels"] = dict(label.split("=", 1) for label in args.edge_label)
    platform["identity_header"] = args.identity_header

    spec = json.loads(changes.get(spec_path, None) or read_regular(spec_path))
    spec["egress"] = [{"host": args.gateway_host, "port": 443, "reason": "model calls through the platform gateway"}]
    errors, questions, _ = check_spec(spec, app, platform)
    if errors or questions:
        sys.exit(f"hello-app spec does not validate: {errors or questions}")

    if args.single_approver:
        try:
            changes.update(single_approver_files(app))
        except (ValueError, OSError) as exc:
            sys.exit(f"cannot apply --single-approver: {exc}")

    # Phase 2: write, then regenerate.
    write_bytes_atomic(platform_path, (json.dumps(platform, indent=2) + "\n").encode("utf-8"))
    for path, data in changes.items():
        write_bytes_atomic(path, data)
    os.environ["BUILDER_PLATFORM_CONFIG"] = str(platform_path)
    try:
        generate(app, spec)
    except BuilderError as exc:
        sys.exit(f"regenerate failed: {exc.errors or exc.questions}")
    print(f"configured org={args.org} gateway={args.gateway_host}; regenerated {app}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

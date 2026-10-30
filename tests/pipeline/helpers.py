"""Test helpers that rebuild the committed placeholder state from a possibly configured tree."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


COMMITTED_CODEOWNERS = """\
# Changes under .github/ (workflows, rulesets, environment, dependabot) need a code owner's review.
/.github/ @ORG_PLACEHOLDER/platform-reviewers
# Files that scanners read to ignore findings. The security-scans workflow does not read them, but a code owner reviews them anyway.
/.semgrepignore @ORG_PLACEHOLDER/platform-reviewers
/.trivyignore @ORG_PLACEHOLDER/platform-reviewers
/trivy.yaml @ORG_PLACEHOLDER/platform-reviewers
"""

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_HOST = "github.com"
DEFAULT_ISSUER = "https://token.actions.githubusercontent.com"
CONFIGURE_KEYS = ("org", "github_host", "actions_oidc_issuer", "landing_page_url", "approved_models_url", "portal_url", "scorecard_mode",
                  "scorecard_app_id", "findings_bypass_team")
# The committed findings ruleset (hello-app's and the portal's are the same bytes). Central mode pins the check to the App and
# --findings-bypass-team swaps the actor; the helpers put this text back. A test keeps it equal to the committed files.
COMMITTED_FINDINGS_RULESET = """\
{
  "name": "scorecard-findings",
  "target": "branch",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
  "bypass_actors": [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}],
  "rules": [
    {"type": "required_status_checks", "parameters": {"strict_required_status_checks_policy": false, "required_status_checks": [
      {"context": "scorecard / findings", "integration_id": 15368}
    ]}}
  ]
}
"""
# The committed per-app scorecard caller (hello-app, the portal and the kit template are the same text). Central mode removes the files;
# the helpers put this text back. A test keeps it equal to the committed files whenever the live tree is pristine.
COMMITTED_SCORECARD_CALLER = """\
# The risk scorecard from the platform's factory repo, called as a reusable workflow (a personal account can't require
# workflows with a ruleset). It classifies how much human review a pull request needs, and holds the merge only for high or
# critical findings. The job id is `scorecard`, so the checks are `scorecard / precondition`, `scorecard / assess`,
# `scorecard / score`, `scorecard / report`, `scorecard / level: <level>`, `scorecard / findings` and `scorecard / assessed`.
# The ruleset requires `scorecard / assessed`; the scorecard-findings ruleset requires `scorecard / findings`.
# The `types` are the called workflow's, `edited` included: a retargeted base changes the diff, so it is assessed again.
name: scorecard

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review, edited]

permissions: {}

jobs:
  scorecard:
    permissions:
      contents: read
      packages: read
      checks: read
      statuses: read
      pull-requests: write
      issues: write
      id-token: write
    uses: ORG_PLACEHOLDER/scorecard-factory/.github/workflows/scorecard.yml@main
    secrets:
      SCORECARD_GATEWAY_KEY: ${{ secrets.SCORECARD_GATEWAY_KEY }}
      SCORECARD_GATEWAY_MODEL: ${{ secrets.SCORECARD_GATEWAY_MODEL }}
      SCORECARD_GATEWAY_ACCESS_ID: ${{ secrets.SCORECARD_GATEWAY_ACCESS_ID }}
      SCORECARD_GATEWAY_ACCESS_SECRET: ${{ secrets.SCORECARD_GATEWAY_ACCESS_SECRET }}
"""
SCORECARD_INTEGRATION = re.compile(r'("context"\s*:\s*"scorecard / assessed"\s*,\s*"integration_id"\s*:\s*)\d+')
# The committed text itself, not read from the file: in a configured tree the file holds the org or personal logins.
COMMITTED_AGENTS_CODEOWNERS = """\
# Everything here is what agents download and run, and what the stub trusts: every change needs a platform owner's review.
* @ORG_PLACEHOLDER/platform-reviewers
"""


def repo_platform():
    """The tree's own platform/platform.json: per-app as committed, central when configure.py was run with --scorecard-mode central."""
    return json.loads((REPO_ROOT / "platform/platform.json").read_text())


def scorecard_central():
    """True when the tree reviews centrally: the apps have no scorecard caller and their rulesets pin `scorecard / assessed` to the App."""
    from builder.config import scorecard_mode
    return scorecard_mode(repo_platform()) == "central"


def scorecard_integration_id():
    """The integration id the apps' rulesets pin `scorecard / assessed` to: the Actions app (15368) per app, the scorecard App's id centrally."""
    from builder.config import scorecard_app_id
    return scorecard_app_id(repo_platform()) or 15368


def unconfigure_platform(platform_dir):
    """Reset platform/platform.json to its committed placeholder state, whatever the live tree has been configured to."""
    path = Path(platform_dir) / "platform.json"
    doc = json.loads(path.read_text())
    for key in CONFIGURE_KEYS:
        doc.pop(key, None)
    doc["gate2_single_approver"] = False
    doc["model_gateway_hosts"] = ["llm.replace-me.invalid"]
    doc["edge_ingress"] = {"namespace": "replace-me", "pod_labels": {"app": "replace-me"}}
    doc["identity_header"] = "X-replace-me"
    path.write_text(json.dumps(doc, indent=2) + "\n")


def _restore_scorecard_caller(path):
    """Central mode removes the scorecard caller; put the committed text back when it is missing."""
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(COMMITTED_SCORECARD_CALLER)


def _repin_scorecard(ruleset_path):
    """Central mode pins `scorecard / assessed` to the App; put the Actions app's id back (a file already at 15368 is left alone)."""
    text = ruleset_path.read_text()
    new = SCORECARD_INTEGRATION.sub(r"\g<1>15368", text)
    if new != text:
        ruleset_path.write_text(new)


def _restore_findings_ruleset(app):
    """Central mode pins `scorecard / findings` to the App and --findings-bypass-team swaps the actor; put the committed file back."""
    path = Path(app) / ".github/rulesets/scorecard-findings.json"
    if path.read_text() != COMMITTED_FINDINGS_RULESET:
        path.write_text(COMMITTED_FINDINGS_RULESET)


def _restore_default_approval(app, with_env=True, ruleset_name="main.json"):
    """Put the two-person approval settings back. A file already at the defaults is left byte-for-byte alone."""
    ruleset_path = app / ".github/rulesets" / ruleset_name
    ruleset = json.loads(ruleset_path.read_text())
    pr = next(r for r in ruleset["rules"] if r["type"] == "pull_request")["parameters"]
    defaults = {"required_approving_review_count": 1, "require_code_owner_review": True, "require_last_push_approval": True}
    if any(pr[k] != v for k, v in defaults.items()):
        pr.update(defaults)
        ruleset_path.write_text(json.dumps(ruleset, indent=2) + "\n")
    if not with_env:
        return
    env_path = app / ".github/environment-prod.json"
    env = json.loads(env_path.read_text())
    if env["prevent_self_review"] is not True:
        env["prevent_self_review"] = True
        env_path.write_text(json.dumps(env, indent=2) + "\n")


def unconfigure_hello_app(app_dir):
    """Reset hello-app to the committed placeholder state: org placeholders, default approval settings, and the generated
    files (rebuilt by the builder against the unconfigured platform file, which must sit at <app>/../../platform)."""
    app = Path(app_dir)
    _restore_scorecard_caller(app / ".github/workflows/scorecard.yml")
    _repin_scorecard(app / ".github/rulesets/main.json")
    _restore_findings_ruleset(app)
    for rel in (".github/workflows/ci.yml", ".github/workflows/promote.yml", ".github/workflows/security-scans.yml", ".github/workflows/scorecard.yml"):
        path = app / rel
        path.write_text(re.sub(r"uses: [A-Za-z0-9-]+/(platform-pipeline|security-scans|scorecard-factory)/", r"uses: ORG_PLACEHOLDER/\1/", path.read_text()))
    # The setup doc has the owner replace CODEOWNERS wholesale with personal logins, so restore the committed text itself.
    # The fixture test compares it byte for byte with the committed file whenever the live tree is pristine.
    (app / ".github/CODEOWNERS").write_text(COMMITTED_CODEOWNERS)
    _restore_default_approval(app)
    spec_path = app / "builder-spec.json"
    spec = json.loads(spec_path.read_text())
    spec["egress"][0]["host"] = "llm.replace-me.invalid"
    spec_path.write_text(json.dumps(spec, indent=2) + "\n")
    platform_path = app.parent.parent / "platform/platform.json"
    out = subprocess.run([*builder_cmd(), "generate", str(app)], capture_output=True, text=True,
                         env=builder_env(BUILDER_PLATFORM_CONFIG=str(platform_path)))
    assert out.returncode == 0, out.stdout + out.stderr


COMMITTED_FACTORY_CODEOWNERS = """\
# The factory decides how every app's pull requests are classified, so every change needs a platform owner's review.
* @ORG_PLACEHOLDER/platform-reviewers
# An app's tier and purpose, its accepted findings, its confirmed N/A requirements, and which identity and data connection it runs with, are the security team's call.
/intake/ @ORG_PLACEHOLDER/security
/suppressions/ @ORG_PLACEHOLDER/security
/na/ @ORG_PLACEHOLDER/security
/config/registration.yaml @ORG_PLACEHOLDER/security
"""
ZERO_IMAGE = "ghcr.io/ORG_PLACEHOLDER/golden-road-builder@sha256:" + "0" * 64
SCORECARD_URL_PLACEHOLDER = "https://llm.replace-me.invalid/v1/chat/completions"


def unconfigure_agents(agents_dir, org, host=None, issuer=None):
    """Reset bootstrap/golden-road-agents to the committed placeholder state. configure.py replaced ORG_PLACEHOLDER with `org` (None
    when the live tree is pristine), wrote the GitHub host and OIDC issuer into the files in its HOST_SITES table, and, in
    single-approver mode, relaxed the main ruleset. The setup doc has the owner replace CODEOWNERS with personal logins."""
    agents = Path(agents_dir)
    _restore_scorecard_caller(agents / "skill/templates/workflows/scorecard.yml")
    if issuer and issuer != DEFAULT_ISSUER:
        for path in agents.rglob("*"):
            if path.is_file() and not path.is_symlink() and issuer.encode() in path.read_bytes():
                path.write_bytes(path.read_bytes().replace(issuer.encode(), DEFAULT_ISSUER.encode()))
    if host and host != DEFAULT_HOST:
        for path in agents.rglob("*"):
            if path.is_file() and not path.is_symlink() and host.encode() in path.read_bytes():
                path.write_bytes(path.read_bytes().replace(host.encode(), DEFAULT_HOST.encode()))
    if org:
        for path in agents.rglob("*"):
            if path.is_file() and not path.is_symlink() and org.encode() in path.read_bytes():
                path.write_bytes(path.read_bytes().replace(org.encode(), b"ORG_PLACEHOLDER"))
    (agents / ".github/CODEOWNERS").write_text(COMMITTED_AGENTS_CODEOWNERS)
    _restore_default_approval(agents, with_env=False)


def unconfigure_factory(factory_dir, org):
    """Reset bootstrap/scorecard-factory to the committed placeholder state. `org` is the org the live tree was configured
    for (None when it is pristine): configure.py renamed the per-owner record directories to it."""
    factory = Path(factory_dir)
    # Every workflow (scorecard, scorecard-poll, scorecard-assess): the factory's own org, and the builder digest the setup doc has the owner pin.
    for workflow in sorted((factory / ".github/workflows").glob("*.yml")):
        if workflow.name == "factory-check.yml":
            continue  # handled below: only its builder digest is configured
        text = re.sub(r"[A-Za-z0-9_-]+/scorecard-factory", "ORG_PLACEHOLDER/scorecard-factory", workflow.read_text())
        workflow.write_text(re.sub(r"(?m)^  BUILDER_IMAGE: .*$", f"  BUILDER_IMAGE: {ZERO_IMAGE}", text))
    for action in sorted((factory / ".github/actions").glob("*/action.yml")):
        action.write_text(re.sub(r"uses: [A-Za-z0-9_-]+/scorecard-factory/", "uses: ORG_PLACEHOLDER/scorecard-factory/", action.read_text()))
    (factory / ".github/CODEOWNERS").write_text(COMMITTED_FACTORY_CODEOWNERS)
    config = factory / "config/scorecard.yaml"
    config.write_text(re.sub(r'(?m)^(    url: )".*"$', rf'\1"{SCORECARD_URL_PLACEHOLDER}"', config.read_text()))
    _restore_default_approval(factory, with_env=False, ruleset_name="main-review.json")
    check = factory / ".github/workflows/factory-check.yml"
    if check.exists():
        check.write_text(re.sub(r"(?m)^(  BUILDER_IMAGE: ).*$", rf"\1{ZERO_IMAGE}", check.read_text()))
    if org:
        for kind in ("intake", "na", "suppressions"):
            directory = factory / kind / org
            if directory.is_dir():
                directory.rename(factory / kind / "ORG_PLACEHOLDER")
        for path in (factory / "intake/ORG_PLACEHOLDER").glob("*.yaml"):
            path.write_text(path.read_text().replace(f"repo: {org}/", "repo: ORG_PLACEHOLDER/"))


COMMITTED_DEPLOY_CODEOWNERS = """\
# The platform owns this repo: every release pin, generated file, adapter and policy needs a platform owner's review.
* @ORG_PLACEHOLDER/platform-reviewers
# Which identity and data connection an app runs with, per environment, is security's and analytics' call. CODEOWNERS can't
# own part of a file, so either team may approve a bindings.yaml (the Azure identity and the Databricks connection alike).
/apps/**/bindings.yaml @ORG_PLACEHOLDER/security @ORG_PLACEHOLDER/analytics
# The workflows, rulesets and owners stay the platform's, whatever matches above.
/.github/ @ORG_PLACEHOLDER/platform-reviewers
"""


# bump.yml's token mode: configure.py --bump-workflow-token rewrites these two (spec 2026-10-30, B1); the helpers put the app ones back.
BUMP_APP, BUMP_WORKFLOW = "\n  BUMP_TOKEN: app\n", "\n  BUMP_TOKEN: workflow\n"
BUMP_READ = "\n    permissions:\n      contents: read\n      packages: read\n"
BUMP_WRITE = "\n    permissions:\n      contents: write\n      pull-requests: write\n      actions: write\n      checks: read\n      packages: read\n"


def unconfigured_bump_text(text):
    """bump.yml's text in app mode: what configure.py --bump-workflow-token rewrote (the mode line, the job's permissions) put back.
    Text already in app mode is returned unchanged."""
    return text.replace(BUMP_WORKFLOW, BUMP_APP).replace(BUMP_WRITE, BUMP_READ)


def unconfigure_deploy(deploy_dir, org):
    """Reset bootstrap/golden-road-deploy to the committed placeholder state. configure.py replaced ORG_PLACEHOLDER with `org` (None
    when the live tree is pristine), in single-approver mode relaxed the review ruleset (main-review) and set deploy-check's
    SINGLE_APPROVER_PROMOTIONS to accept, and with --bump-workflow-token set bump.yml's BUMP_TOKEN to workflow and its job's
    permissions to write. The setup doc may have the owner replace CODEOWNERS with personal logins, so the committed
    text is restored, and has the owner pin the workflows' BUILDER_IMAGE digest, so the placeholder digest is restored."""
    deploy = Path(deploy_dir)
    if not deploy.is_dir():
        return
    if org:  # the org only where configure.py put it: a path segment (github.com/<org>/, ghcr.io/<org>/)
        segment = re.compile(rf"(?<=/){re.escape(org)}(?=/)")
        for path in deploy.rglob("*"):
            if path.is_file() and not path.is_symlink():
                text = path.read_text()
                if segment.search(text):
                    path.write_text(segment.sub("ORG_PLACEHOLDER", text))
    (deploy / ".github/CODEOWNERS").write_text(COMMITTED_DEPLOY_CODEOWNERS)
    _restore_default_approval(deploy, with_env=False, ruleset_name="main-review.json")
    # The workflows: the builder digest the setup doc has the owner pin, deploy-check's single-approver setting and bump's token mode.
    for workflow in sorted((deploy / ".github/workflows").glob("*.yml")):
        text = re.sub(r"(?m)^(  BUILDER_IMAGE: ).*$", rf"\g<1>{ZERO_IMAGE}", workflow.read_text())
        text = re.sub(r"(?m)^(  SINGLE_APPROVER_PROMOTIONS: )accept$", r"\g<1>refuse", text)
        workflow.write_text(unconfigured_bump_text(text) if workflow.name == "bump.yml" else text)


def unconfigure_portal(app_dir):
    """Reset bootstrap/golden-road-portal to the committed placeholder state: caller org placeholders, the committed CODEOWNERS,
    the default approval settings, github.com egress, and the generated files against the unconfigured platform file."""
    app = Path(app_dir)
    if not app.is_dir():
        return
    _restore_scorecard_caller(app / ".github/workflows/scorecard.yml")
    _repin_scorecard(app / ".github/rulesets/main.json")
    _restore_findings_ruleset(app)
    for rel in (".github/workflows/ci.yml", ".github/workflows/promote.yml", ".github/workflows/security-scans.yml", ".github/workflows/scorecard.yml"):
        path = app / rel
        path.write_text(re.sub(r"uses: [A-Za-z0-9-]+/(platform-pipeline|security-scans|scorecard-factory)/", r"uses: ORG_PLACEHOLDER/\1/", path.read_text()))
    (app / ".github/CODEOWNERS").write_text(COMMITTED_CODEOWNERS)
    _restore_default_approval(app)
    spec_path = app / "builder-spec.json"
    spec = json.loads(spec_path.read_text())
    spec["egress"] = [{"host": "api.github.com", "port": 443, "reason": "GitHub API: identity linking"},
                      {"host": "github.com", "port": 443, "reason": "GitHub OAuth: identity linking"}]
    spec_path.write_text(json.dumps(spec, indent=2) + "\n")
    platform_path = app.parent.parent / "platform/platform.json"
    out = subprocess.run([*builder_cmd(), "generate", str(app)], capture_output=True, text=True, env=builder_env(BUILDER_PLATFORM_CONFIG=str(platform_path)))
    assert out.returncode == 0, out.stdout + out.stderr


def builder_cmd():
    """Run the builder CLI with the interpreter running the tests, so it works without a console-script install."""
    return [sys.executable, "-m", "builder.cli"]


def builder_env(**extra):
    """An environment in which `builder_cmd()` imports the same packages the tests import."""
    return {"PYTHONPATH": os.pathsep.join(p for p in sys.path if p), **extra}


# ---- the setup docs' edits to the factory ----

# Every factory config field the setup docs (personal-poc.md 13.2 and 13.3, and the other setup doc) tell the owner to edit. The tests that compare the live
# config with the tested sample, or look at its starting values, ignore exactly these. Add a field here when a doc step starts editing it.
SETUP_DOC_EDITS = ("gate1_checks", "reviewers", "agent.model.version", "agent.gateway.url", "agent.gateway.auth",
                   "agent.budget.max_total_tokens", "registry.internal_npm_scopes", "registry.internal_packages", "registry.internal_registries",
                   "paths.suppression_files", "patterns.suppression_markers")


def blank_setup_doc_edits(config, required=()):
    """Set every field the setup docs edit to None; `gate1_checks` keeps drift and preview (the entries the platform's list adds are dropped)."""
    config["gate1_checks"] = [e for e in config["gate1_checks"] if e not in required]
    for dotted in SETUP_DOC_EDITS[1:]:
        *parents, leaf = dotted.split(".")
        node = config
        for key in parents:
            node = node[key]
        node[leaf] = None
    config["agent"]["model"].pop("temperature", None)  # optional: a deployment whose model rejects a temperature sets it to null
    config["agent"]["budget"]["max_run_seconds"] = None  # a deployment may set its own deadline, or 0 for none
    config["agent"]["batching"]["max_turns"] = None  # a deployment may give the model more calls per batch
    config["agent"]["budget"]["max_tokens_per_call"] = None  # a deployment may allow longer replies (a cut-off submission is invalid)
    config["large_change"] = None  # a deployment may set its own large-change limits (T-19)
    config.pop("central", None)  # a deployment that turns on central review adds this section
    return config


def doc_bash_blocks(text):
    return re.findall(r"```bash\n(.*?)```", text, re.S)


def run_doc_block(block, cwd, substitutions):
    """Run a documented command block with bash in `cwd`, after replacing its placeholders. Lines that need the network, a remote or
    a commit (git add/commit/push/tag, gh) are dropped; everything else runs as written."""
    for old, new in substitutions.items():
        block = block.replace(old, new)
    lines = [l for l in block.splitlines() if not re.match(r"\s*(git (add|commit|push|tag)|gh) ", l)]
    return subprocess.run(["bash", "-euo", "pipefail", "-c", "\n".join(lines)], cwd=cwd, capture_output=True, text=True)


def scratch_factory_workspace(root, work, required=()):
    """A scratch copy of what the setup docs' factory edits touch: bootstrap/scorecard-factory and platform/required-checks.json."""
    work = Path(work)
    shutil.copytree(Path(root) / "bootstrap/scorecard-factory", work / "bootstrap/scorecard-factory", ignore=shutil.ignore_patterns("__pycache__"))
    # Start from the committed state, not from a tree the docs' edits were already applied to (an export being checked): the config is the
    # tested sample, which the factory config equals apart from the fields the docs edit, and the code owners are the committed text.
    shutil.copy(Path(root) / "tests/scorecard/fixtures/factory/config/scorecard.yaml", work / "bootstrap/scorecard-factory/config/scorecard.yaml")
    (work / "bootstrap/scorecard-factory/.github/CODEOWNERS").write_text(COMMITTED_FACTORY_CODEOWNERS)
    (work / "platform").mkdir()
    (work / "platform/required-checks.json").write_text(json.dumps({"required": list(required)}))
    return work


FAKE_DOC_GH = """#!{python}
import json, os, sys
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "stdin": sys.stdin.read()}}) + "\\n")
"""


def run_block_with_fake_gh(block, cwd, substitutions, tmp_path):
    """Run a documented block with bash in cwd, with a fake gh first on PATH that logs its arguments and what it read on stdin.
    Unlike run_doc_block, gh lines run (against the fake). Returns (completed process, gh calls)."""
    for old, new in substitutions.items():
        block = block.replace(old, new)
    bin_dir, log = Path(tmp_path) / "fake-gh-bin", Path(tmp_path) / "fake-gh.log"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "gh").write_text(FAKE_DOC_GH.format(python=sys.executable))
    (bin_dir / "gh").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE_GH_LOG": str(log), "HOME": str(tmp_path)}
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", block], cwd=cwd, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return out, calls

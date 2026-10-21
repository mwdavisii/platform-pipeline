"""Test helpers that rebuild the committed placeholder state from a possibly configured tree."""

import json
import os
import re
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


def unconfigure_platform(platform_dir):
    """Reset platform/platform.json to its committed placeholder state, whatever the live tree has been configured to."""
    path = Path(platform_dir) / "platform.json"
    doc = json.loads(path.read_text())
    doc.pop("org", None)
    doc["gate2_single_approver"] = False
    doc["model_gateway_hosts"] = ["llm.replace-me.invalid"]
    doc["edge_ingress"] = {"namespace": "replace-me", "pod_labels": {"app": "replace-me"}}
    doc["identity_header"] = "X-replace-me"
    path.write_text(json.dumps(doc, indent=2) + "\n")


def _restore_default_approval(app):
    """Put the two-person approval settings back. A file already at the defaults is left byte-for-byte alone."""
    ruleset_path = app / ".github/rulesets/main.json"
    ruleset = json.loads(ruleset_path.read_text())
    pr = next(r for r in ruleset["rules"] if r["type"] == "pull_request")["parameters"]
    defaults = {"required_approving_review_count": 1, "require_code_owner_review": True, "require_last_push_approval": True}
    if any(pr[k] != v for k, v in defaults.items()):
        pr.update(defaults)
        ruleset_path.write_text(json.dumps(ruleset, indent=2) + "\n")
    env_path = app / ".github/environment-prod.json"
    env = json.loads(env_path.read_text())
    if env["prevent_self_review"] is not True:
        env["prevent_self_review"] = True
        env_path.write_text(json.dumps(env, indent=2) + "\n")


def unconfigure_hello_app(app_dir):
    """Reset hello-app to the committed placeholder state: org placeholders, default approval settings, and the generated
    files (rebuilt by the builder against the unconfigured platform file, which must sit at <app>/../../platform)."""
    app = Path(app_dir)
    for rel in (".github/workflows/ci.yml", ".github/workflows/promote.yml", ".github/workflows/security-scans.yml"):
        path = app / rel
        path.write_text(re.sub(r"uses: [A-Za-z0-9-]+/(platform-pipeline|security-scans)/", r"uses: ORG_PLACEHOLDER/\1/", path.read_text()))
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


def builder_cmd():
    """Run the builder CLI with the interpreter running the tests, so it works without a console-script install."""
    return [sys.executable, "-m", "builder.cli"]


def builder_env(**extra):
    """An environment in which `builder_cmd()` imports the same packages the tests import."""
    return {"PYTHONPATH": os.pathsep.join(p for p in sys.path if p), **extra}

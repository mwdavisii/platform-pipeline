import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from helpers import builder_cmd, builder_env

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "bootstrap/hello-app"


def test_hello_app_verifies_with_platform_config():
    env = {**os.environ, **builder_env(), "BUILDER_PLATFORM_CONFIG": str(ROOT / "platform/platform.json")}
    out = subprocess.run([*builder_cmd(), "verify", str(APP)], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stdout


def test_callers_point_at_platform_pipeline():
    """Holds both before configure (ORG_PLACEHOLDER) and after it (the real org)."""
    for name in ("ci.yml", "promote.yml"):
        text = (APP / ".github/workflows" / name).read_text()
        assert re.search(r"uses: [A-Za-z0-9_-]+/platform-pipeline/\.github/workflows/golden-road(-promote)?\.yml@main", text)


def test_committed_tree_is_all_or_nothing_configured():
    """Either everything still carries a placeholder, or no placeholder is left anywhere that ships."""
    platform = json.loads((ROOT / "platform/platform.json").read_text())
    configured = platform.get("org") is not None
    leftovers = [p for p in list((ROOT / "bootstrap").rglob("*")) + [ROOT / "platform/platform.json"]
                 if p.is_file() and re.search("replace-me|ORG_PLACEHOLDER", p.read_text(errors="replace"))]
    assert (not leftovers) if configured else bool(leftovers)


def test_ruleset_requires_drift_check():
    doc = json.loads((APP / ".github/rulesets/main.json").read_text())
    checks = [r for r in doc["rules"] if r["type"] == "required_status_checks"][0]
    assert {"context": "golden-road / drift", "integration_id": 15368} in checks["parameters"]["required_status_checks"]


def single_approver_mode():
    """The live tree's recorded Gate 2 mode: False when committed, True after configure.py --single-approver."""
    mode = json.loads((ROOT / "platform/platform.json").read_text())["gate2_single_approver"]
    assert type(mode) is bool
    return mode


def test_environment_prevents_self_review():
    doc = json.loads((APP / ".github/environment-prod.json").read_text())
    assert doc["prevent_self_review"] is (not single_approver_mode()) and doc["reviewers"]


def test_builder_tag_ruleset_is_admin_only():
    doc = json.loads((ROOT / ".github/rulesets/builder-tags.json").read_text())
    assert doc["target"] == "tag" and doc["enforcement"] == "active"
    assert doc["conditions"]["ref_name"]["include"] == ["refs/tags/builder-v*"]
    assert {r["type"] for r in doc["rules"]} == {"creation", "update", "deletion"}
    assert [(b["actor_type"], b["actor_id"]) for b in doc["bypass_actors"]] == [("RepositoryRole", 5)]  # 5 = admin


def test_no_secrets_in_bootstrap():
    for path in (ROOT / "bootstrap").rglob("*"):
        if path.is_file():
            text = path.read_text(errors="replace")
            assert "BEGIN OPENSSH PRIVATE KEY" not in text and "ghp_" not in text, path


def test_enforcement_doc_names_controls():
    text = (ROOT / "docs/enforcement.md").read_text()
    for needle in ("Gate 1", "Gate 2", "authz probe", "prevent self-review", "Deploy owners", "builder-v*", "configure.py"):
        assert needle in text


def run_script(script, *args, path=None):
    """Run a script with a PATH of stubs that fail loudly, so a test can never reach docker, gh, or kubectl."""
    return subprocess.run([str(ROOT / "scripts" / script), *args], capture_output=True, text=True,
                          env={"PATH": path or "/usr/bin:/bin"})


def stub_bin(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("gh", "docker", "kubectl", "git"):
        stub = bin_dir / tool
        stub.write_text(f"#!/bin/sh\necho {tool} \"$@\" >> '{tmp_path}/calls'\nexit 99\n")
        stub.chmod(0o755)
    return f"{bin_dir}:/usr/bin:/bin"


def test_push_bootstrap_refuses_public_and_bad_input(tmp_path):
    path = stub_bin(tmp_path)
    for args in (["org", "hello-app", "--public"], ["org", "hello-app", "public"], ["org", "nope", "--private"],
                 ["org", "../x"], ["org"], ["bad org", "hello-app"], ["org", "hello-app", "--private", "extra"]):
        out = run_script("push-bootstrap.sh", *args, path=path)
        assert out.returncode == 2, (args, out.stderr)
    assert not (tmp_path / "calls").exists()


def test_push_bootstrap_refuses_unconfigured_placeholder(tmp_path):
    """Uses synthesized trees, so it holds whether or not the live tree has been configured."""
    for i, marker in enumerate(("uses: ORG_PLACEHOLDER/platform-pipeline", "host: llm.replace-me.invalid", "ns: replace-me")):
        out, calls = push_tree(tmp_path / str(i), "PRIVATE", readme=marker + "\n")
        assert out.returncode == 2 and "configure.py" in out.stderr, marker
        assert calls == ""


def push_tree(tmp_path, view_output, readme="demo\n", args=(), env=None, owner_type="User", login="org"):
    """A copy of the script next to a placeholder-free bootstrap tree, with stub gh and git that log every call."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    (tree / "bootstrap/demo").mkdir(parents=True)
    (tree / "bootstrap/demo/README.md").write_text(readme)
    script = tree / "scripts/push-bootstrap.sh"
    script.write_text((ROOT / "scripts/push-bootstrap.sh").read_text())
    script.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    (bin_dir / "gh").write_text(f"""#!/bin/sh
echo "gh $*" >> '{calls}'
echo "${{GH_HOST:-unset}}" >> '{calls}.hosts'
case "$*" in
  "api user "*) printf '%s\\n' '{login}' ;;
  "api users/"*) printf '%s\\n' '{owner_type}' ;;
  *"--json visibility"*) printf '%s' '{view_output}' ;;
  *"--json url"*) echo https://example.invalid/org/demo ;;
esac
exit 0
""")
    (bin_dir / "git").write_text(f"#!/bin/sh\necho \"git $*\" >> '{calls}'\nexit 0\n")
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    out = subprocess.run([str(script), "org", "demo", *args], capture_output=True, text=True,
                         env={"PATH": f"{bin_dir}:/usr/bin:/bin", **(env or {})})
    return out, calls.read_text() if calls.exists() else ""


def test_push_bootstrap_never_pushes_when_repo_is_public_or_unknown(tmp_path):
    for i, view in enumerate(("PUBLIC", "", "WEIRD")):
        out, calls = push_tree(tmp_path / str(i), view)
        assert out.returncode != 0, view
        assert not any(l.startswith("git ") and " push " in f"{l} " for l in calls.splitlines()), (view, calls)
        assert "Fix or delete" in out.stderr, view
        assert "gh repo create org/demo --private" in calls


def test_push_bootstrap_pushes_after_private_check(tmp_path):
    out, calls = push_tree(tmp_path, "PRIVATE")
    assert out.returncode == 0, out.stderr
    lines = calls.splitlines()
    assert lines.index("gh repo view org/demo --json visibility --jq .visibility") < next(i for i, l in enumerate(lines) if "push" in l)
    assert not any("--push" in l or "--source" in l for l in lines)


POC = {"GOLDEN_ROAD_PUBLIC_POC": "org"}


def test_push_bootstrap_defaults_to_private_and_guards_public():
    text = (ROOT / "scripts/push-bootstrap.sh").read_text()
    assert 'visibility="${3:---private}"' in text
    assert "GOLDEN_ROAD_PUBLIC_POC" in text and "--jq .type" in text and '"User"' in text and "--jq .login" in text
    assert "GH_HOST=github.com" in text
    assert text.index("--jq .type") < text.index("gh repo create")
    assert text.index("--jq .login") < text.index("gh repo create")


def test_push_bootstrap_public_refused_without_the_owner_opt_in(tmp_path):
    for i, env in enumerate(({}, {"GOLDEN_ROAD_PUBLIC_POC": "yes"}, {"GOLDEN_ROAD_PUBLIC_POC": "ORG"}, {"GOLDEN_ROAD_PUBLIC_POC": "other"},
                              {"GOLDEN_ROAD_PUBLIC_POC": ""}, {"GOLDEN_ROAD_PUBLIC_POC": "org "})):
        out, calls = push_tree(tmp_path / str(i), "PUBLIC", args=["--public"], env=env)
        assert out.returncode == 2 and "GOLDEN_ROAD_PUBLIC_POC" in out.stderr, env
        assert calls == "", "nothing may be called before the opt-in is checked"


@pytest.mark.parametrize("login", ["someone-else", "", "Org2"])
def test_push_bootstrap_public_refused_when_not_the_authenticated_user(tmp_path, login):
    out, calls = push_tree(tmp_path, "PUBLIC", args=["--public"], env=POC, login=login)
    assert out.returncode != 0 and "authenticated" in out.stderr
    assert calls.splitlines() == ["gh api user --jq .login"], calls


@pytest.mark.parametrize("owner_type", ["Organization", "", "user", "Bot", "User extra"])
def test_push_bootstrap_public_refused_for_anything_but_a_user(tmp_path, owner_type):
    out, calls = push_tree(tmp_path, "PUBLIC", args=["--public"], env=POC, owner_type=owner_type)
    assert out.returncode != 0 and "personal" in out.stderr
    assert calls.splitlines() == ["gh api user --jq .login", "gh api users/org --jq .type"], calls  # no create, no push


def test_push_bootstrap_public_for_a_user_creates_verifies_then_pushes(tmp_path):
    out, calls = push_tree(tmp_path, "PUBLIC", args=["--public"], env=POC)
    assert out.returncode == 0, out.stderr
    lines = calls.splitlines()
    who = lines.index("gh api user --jq .login")
    owner = lines.index("gh api users/org --jq .type")
    create = lines.index("gh repo create org/demo --public")
    check = lines.index("gh repo view org/demo --json visibility --jq .visibility")
    push = next(i for i, l in enumerate(lines) if " push " in f"{l} ")
    assert who < owner < create < check < push


def test_push_bootstrap_public_pins_github_com_for_every_gh_call(tmp_path):
    out, calls = push_tree(tmp_path, "PUBLIC", args=["--public"], env={**POC, "GH_HOST": "ghe.example.org"})
    assert out.returncode == 0, out.stderr
    hosts = (tmp_path / "calls.hosts").read_text().split()
    assert hosts and set(hosts) == {"github.com"}, hosts


def test_push_bootstrap_public_request_that_comes_back_private_is_not_pushed(tmp_path):
    for i, view in enumerate(("PRIVATE", "INTERNAL", "")):
        out, calls = push_tree(tmp_path / str(i), view, args=["--public"], env=POC)
        assert out.returncode != 0 and "PUBLIC" in out.stderr, view
        assert not any(" push " in f"{l} " for l in calls.splitlines())


def test_push_bootstrap_private_never_accepts_public_result_even_with_opt_in(tmp_path):
    for flag in ("--private", "--internal"):
        out, calls = push_tree(tmp_path / flag, "PUBLIC", args=[flag], env=POC)
        assert out.returncode != 0 and "Fix or delete" in out.stderr
        assert not any(" push " in f"{l} " for l in calls.splitlines())
        assert "users/" not in calls and "api user" not in calls, "the owner lookups are only for --public"


def test_dry_run_script_is_local_only():
    text = (ROOT / "scripts/local-release-dry-run.sh").read_text()
    for forbidden in ("cosign", "docker push", "gh ", "docker login"):
        assert forbidden not in text
    assert "--network none" in text
    assert run_script("local-release-dry-run.sh", str(ROOT / "no-such-dir")).returncode != 0


def test_enforcement_doc_is_complete_and_matches_the_ruleset():
    text = (ROOT / "docs/enforcement.md").read_text()
    assert "TODO" not in text and "Gate 2 environment step" in text
    # the documented ids must match the checked-in files
    assert json.loads((ROOT / "bootstrap/hello-app/.github/rulesets/main.json").read_text())["rules"][3]["parameters"]["required_status_checks"][0]["integration_id"] == 15368


def test_personal_poc_doc_builder_tag_matches_builder_version():
    version = re.search(r'VERSION = "([^"]+)"', (ROOT / "src/builder/__init__.py").read_text()).group(1)
    assert f"builder-v{version}" in (ROOT / "docs/setup/personal-poc.md").read_text()


def test_enforcement_doc_has_verification_contract():
    text = (ROOT / "docs/enforcement.md").read_text()
    for needle in ("Deploy-owner verification contract", "--certificate-github-workflow-repository",
                   "--certificate-github-workflow-trigger workflow_dispatch", "manifests_sha256", "What is public",
                   "Deferred: build egress", "approver logins", "1.3.6.1.4.1.57264.1.8", "never from a separate input"):
        assert needle in text, needle


def test_personal_poc_doc_matches_code():
    text = (ROOT / "docs/setup/personal-poc.md").read_text()
    for needle in ("PUBLIC, PERSONAL, PROOF OF CONCEPT ONLY", "scripts/export-public-poc.sh", "scripts/configure.py",
                   "GOLDEN_ROAD_PUBLIC_POC=<you>", "--public", "users/<you> --jq .type", "--edge-namespace cloudflared",
                   "--edge-label app=cloudflared", "--identity-header X-Platform-Roles", "<gateway host>",
                   "builder-tags.json", "actor_id\": 5", "integration_id", "15368", "rulesets/main.json",
                   "/users/<you>/packages/container/golden-road-builder", "/users/<you>/packages/container/hello-app",
                   "all outside collaborators", "collaborators/<deputy>", "prevent_self_review", "deployment-branch-policies",
                   '{"required": []}', "waits on nothing beyond the drift check", "PoC gap", "verify-attestation",
                   "--dry-run=server", "--context hydra", "THEIR OWN repos", "CODEOWNERS", "GH_HOST=github.com", "platform-pipeline/actions/permissions/fork-pr-contributor-approval", "never from the release notes", "gh api user --jq .login", "code-owner review", "Comment on the PR", "GITHUB_TOKEN Permissions", "DSSEObj"):
        assert needle in text, needle
    assert "/orgs/" not in text and "permissions/access" not in text, "no organization steps in the personal variant"
    # every numbered step ends in a Check
    for section in re.split(r"\n## ", text)[1:]:
        title = section.splitlines()[0]
        if re.match(r"\d+\.", title) and not title.startswith("9."):
            assert "**Check" in section, title
    assert text.index("--json visibility --jq .visibility") < text.index("git push -u origin main")
    assert "--source . --push" not in text
    assert 'p.add_argument("--identity-header"' in (ROOT / "scripts/configure.py").read_text()


def test_enforcement_doc_describes_the_personal_poc_exception():
    text = (ROOT / "docs/enforcement.md").read_text()
    for needle in ("Personal public PoC", "GOLDEN_ROAD_PUBLIC_POC", "exactly `User`", "export script", "Fork PRs", "anyone can produce valid platform-identity signatures", "code-owner review", "required-checks.json"):
        assert needle in text, needle


def test_hello_app_requires_code_owner_review_for_dot_github():
    ruleset = json.loads((APP / ".github/rulesets/main.json").read_text())
    pr = next(r for r in ruleset["rules"] if r["type"] == "pull_request")
    params = pr["parameters"]
    keys = ("required_approving_review_count", "require_code_owner_review", "require_last_push_approval")
    assert tuple(params[k] for k in keys) == ((0, False, False) if single_approver_mode() else (1, True, True))
    owners = (APP / ".github/CODEOWNERS").read_text().splitlines()
    rules = [l for l in owners if l.strip() and not l.startswith("#")]
    assert any(l.split()[0] == "/.github/" and len(l.split()) >= 2 for l in rules)


def test_root_readme_is_neutral_and_links_the_docs():
    text = (ROOT / "README.md").read_text()
    assert "personal proof of concept" in text and "docs/setup/personal-poc.md" in text and "docs/enforcement.md" in text


def test_ruleset_requires_both_security_scans_checks_from_the_actions_app():
    doc = json.loads((APP / ".github/rulesets/main.json").read_text())
    checks = [r for r in doc["rules"] if r["type"] == "required_status_checks"][0]["parameters"]["required_status_checks"]
    assert checks == [{"context": name, "integration_id": 15368} for name in ("golden-road / drift", "golden-road / preview", "security-scans / semgrep", "security-scans / trivy")]


def test_code_scanning_ruleset_is_a_separate_file_so_a_wrong_field_cannot_break_the_main_ruleset():
    doc = json.loads((APP / ".github/rulesets/code-scanning.json").read_text())
    assert doc["name"] == "code-scanning" and doc["enforcement"] == "active" and doc["target"] == "branch"
    assert [r["type"] for r in doc["rules"]] == ["code_scanning"]
    [tool] = doc["rules"][0]["parameters"]["code_scanning_tools"]
    assert tool["tool"] == "CodeQL" and tool["security_alerts_threshold"] == "high_or_higher"
    assert "code_scanning" not in (APP / ".github/rulesets/main.json").read_text()


def test_dependabot_covers_the_language_and_the_actions():
    import yaml
    doc = yaml.safe_load((APP / ".github/dependabot.yml").read_text())
    assert doc["version"] == 2
    ecosystems = {u["package-ecosystem"]: u for u in doc["updates"]}
    assert set(ecosystems) == {"pip", "github-actions"}, "no docker entry: the Dockerfile and its base image belong to the builder"
    for update in ecosystems.values():
        assert update["directory"] == "/" and update["schedule"]["interval"] == "weekly"
    assert (APP / "requirements.txt").is_file() and not (APP / "package.json").exists(), "hello-app is a Python app, so pip rather than npm"


def test_codeowners_covers_workflows_and_the_files_scanners_read_to_ignore_findings():
    rules = [l.split() for l in (APP / ".github/CODEOWNERS").read_text().splitlines() if l.strip() and not l.startswith("#")]
    owned = {r[0] for r in rules if len(r) >= 2}
    assert {"/.github/", "/.semgrepignore", "/.trivyignore", "/trivy.yaml"} <= owned
    doc = (ROOT / "docs/setup/personal-poc.md").read_text()
    for path in owned:
        assert f"{path} @<you> @<deputy>" in doc, f"the doc's CODEOWNERS step must cover {path}"


def test_personal_poc_doc_covers_the_scans_codeql_dependabot_and_the_known_gap():
    text = (ROOT / "docs/setup/personal-poc.md").read_text()
    for needle in ("--tree security-scans", "## 4. Publish `security-scans` (public)", "code-scanning/default-setup", "default setup",
                   "vulnerability-alerts", "dependabot.yml", "rulesets/code-scanning.json", "platform/required-checks.json",
                   'name: "security-scans / semgrep", app_id: 15368', 'name: "security-scans / trivy", app_id: 15368',
                   "CodeQL check name", "builder-v0.2.1", 'VERSION = "0.2.1"', "jinja2==2.10", "shell=True", "golden-road checks wait",
                   "Scan results can be forged", "signed scan results", "can_approve_pull_request_reviews", "Checks: Read-only", "Dry run first",
                   "GOLDEN_ROAD_SCANS_TRIVY", "rulesets --input .github/rulesets/main.json", "Base-image findings", "Gate 1 on a push"):
        assert needle in text, needle
    steps = [int(m) for m in re.findall(r"^## (\d+)\.", text, re.M)]
    assert steps == list(range(0, 13))
    # the scans repo is published before hello-app is pushed, and the builder is released again only after both exist
    assert text.index("## 4. Publish `security-scans`") < text.index("## 5. Create `hello-app`") < text.index("## 11.") < text.index("## 12.")


def test_enforcement_doc_has_neutral_rows_for_the_scans_and_the_forgery_gap():
    text = (ROOT / "docs/enforcement.md").read_text()
    for needle in ("security-scans / semgrep", "security-scans / trivy", "15368", "forged", "signed scan result", "default setup",
                   "base image", "digest-pinned scan image", "job.workflow_repository"):
        assert needle in text, needle
    ruleset = json.loads((APP / ".github/rulesets/main.json").read_text())
    assert "integration_id" in json.dumps(ruleset)


def test_enforcement_doc_requires_verifiers_to_reject_self_approved_promotions():
    text = (ROOT / "docs/enforcement.md").read_text()
    assert "self_approved" in text and "reject" in text
    contract = text[text.index("## Deploy-owner verification contract"):text.index("## What is public")]
    assert "`self_approved: true`" in contract
    jq = contract[contract.index("promo.jsonl\njq"):]
    jq = jq.split("```")[0]
    assert ".self_approved == false" in jq and ".gate2_single_approver == false" in jq
    assert "`gate2_single_approver: true`" in contract and "earlier v1 records are invalid" in contract


def test_personal_poc_doc_has_the_single_approver_deployment_note():
    text = (ROOT / "docs/setup/personal-poc.md").read_text()
    assert "--single-approver" in text and "Deployment note (PoC only)" in text
    note = text[text.index("Deployment note (PoC only)"):]
    note = note[:note.index("\n\n", note.index("self_approved"))]
    for needle in ("0 approvals", "self_approved: true", "separation of duties", "never use it on a real org", "git checkout -- bootstrap platform"):
        assert needle.lower() in note.lower(), needle
    assert "Single-approver mode: skip" in text and "If you have a deputy" in text
    # the two-person path stays documented
    assert "@<you> @<deputy>" in text and "prevent_self_review\": true" in text

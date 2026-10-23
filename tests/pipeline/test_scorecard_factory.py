"""bootstrap/scorecard-factory: the factory repo template (its config, intake, samples, intake action and
protection), and the hello-app wiring that calls its workflow."""

import copy
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from contracts.schema import validate
from helpers import blank_setup_doc_edits, doc_bash_blocks, run_doc_block, scratch_factory_workspace

ROOT = Path(__file__).resolve().parents[2]
FACTORY = ROOT / "bootstrap/scorecard-factory"
FIXTURE = ROOT / "tests/scorecard/fixtures/factory"
APP = ROOT / "bootstrap/hello-app"
ACTION = FACTORY / ".github/actions/intake/action.yml"
CALLER = APP / ".github/workflows/scorecard.yml"
CALLER_USES = re.compile(r"^[A-Za-z0-9_-]+/scorecard-factory/\.github/workflows/scorecard\.yml@main$")


def load_workflow(path):
    doc = yaml.safe_load(path.read_text())
    doc["on"] = doc.pop(True, doc.get("on"))
    return doc


def intake_owner():
    """The one owner directory under intake/: ORG_PLACEHOLDER until configure.py renames it to the org."""
    [owner] = [p.name for p in (FACTORY / "intake").iterdir() if p.is_dir()]
    return owner


# ---- the config and the records ----

def assert_factory_config_matches_sample(factory, required):
    """The factory's config is the tested sample, except for the fields the setup docs tell the owner to edit (helpers.SETUP_DOC_EDITS)."""
    from scorecard.config import load_config

    live, sample = (blank_setup_doc_edits(load_config(d), required) for d in (factory, FIXTURE))
    assert live == sample, "keep bootstrap/scorecard-factory/config/scorecard.yaml in step with the sample the tests use"


def test_the_factory_config_is_the_tested_sample_config_with_only_the_deployment_values_changed():
    assert_factory_config_matches_sample(FACTORY, json.loads((ROOT / "platform/required-checks.json").read_text())["required"])


def test_the_deployment_values_start_empty_or_as_placeholders():
    from scorecard.config import load_config

    config = load_config(FACTORY)
    url = config["agent"]["gateway"]["url"]
    assert url == "https://llm.replace-me.invalid/v1/chat/completions" or "replace-me" not in url
    if "replace-me" in url:  # the committed state; a configured tree has had the doc's edits (auth, reviewers, scopes, ...) applied
        assert config["agent"]["gateway"]["auth"] == {"scheme": "bearer"}
        assert config["reviewers"] == {"elevated": [], "security_review": []} and config["registry"]["internal_npm_scopes"] == []


def test_gate1_covers_drift_preview_and_every_platform_required_check():
    from scorecard.config import load_config

    config = load_config(FACTORY)
    required = json.loads((ROOT / "platform/required-checks.json").read_text())["required"]
    expected = [{"name": "golden-road / drift", "app_id": 15368}, {"name": "golden-road / preview", "app_id": 15368}, *required]
    assert all(entry in config["gate1_checks"] for entry in expected), "add every required-checks.json entry to gate1_checks"
    assert config["drift_check"] == "golden-road / drift"


def test_the_hello_app_intake_record_loads():
    from scorecard.config import load_intake

    owner = intake_owner()
    intake = load_intake(FACTORY, f"{owner}/hello-app")
    assert intake["tier"] == "moderate" and intake["data_classification"] == "internal"
    assert intake["data_classification"] == json.loads((APP / "builder-spec.json").read_text())["data_classification"]
    assert list((FACTORY / "intake").rglob("*.yaml")) == [FACTORY / f"intake/{owner}/hello-app.yaml"]


def fenced(path, lang):
    [block] = re.findall(rf"```{lang}\n(.*?)```", path.read_text(), re.S)
    return block


def test_the_na_sample_is_a_valid_record_for_a_real_requirement(tmp_path):
    """The README's example, written where the scorecard reads it and loaded with its own loader."""
    from scorecard.asvs import check_ids, load_asvs
    from scorecard.config import load_config, load_confirmed_na

    (tmp_path / "na/example").mkdir(parents=True)
    (tmp_path / "na/example/hello-app.yaml").write_text(fenced(FACTORY / "na/README.md", "yaml"))
    entries = load_confirmed_na(tmp_path, "example/hello-app")
    assert len(entries) == 1
    check_ids(load_asvs(), load_config(FACTORY), entries)


def test_the_suppression_sample_is_a_valid_override_with_a_real_rule_id():
    doc = json.loads(fenced(FACTORY / "suppressions/README.md", "json"))
    assert validate("override.v2", doc) == []
    assert doc["rule_id"] in (ROOT / "src/scorecard/signals/changes.py").read_text()


def test_no_sample_is_a_live_record():
    """The samples live in READMEs, so they never apply to an app: na/ and suppressions/ hold no records yet."""
    assert [p.name for p in (FACTORY / "na").rglob("*") if p.is_file()] == ["README.md"]
    assert [p.name for p in (FACTORY / "suppressions").rglob("*") if p.is_file()] == ["README.md"]


def assert_codeowners_protect_the_records(factory):
    lines = [l.split() for l in (Path(factory) / ".github/CODEOWNERS").read_text().splitlines() if l.strip() and not l.startswith("#")]
    owners = {line[0]: line[1:] for line in lines}
    assert set(owners) == {"*", "/intake/", "/suppressions/", "/na/"}
    assert all(o for o in owners.values())
    if all(o.count("/") == 1 for os_ in owners.values() for o in os_):  # every owner is an @org/team: security must be a different team
        for path in ("/intake/", "/suppressions/", "/na/"):
            assert owners[path] != owners["*"], f"{path} needs the security team, not only platform review"
    else:  # owners are users (a personal account, or single-approver mode): one set of people may own everything, but every path must be listed
        assert not any("/" in o[1:] for os_ in owners.values() for o in os_)


def test_codeowners_and_the_ruleset_protect_main():
    assert_codeowners_protect_the_records(FACTORY)
    ruleset = json.loads((FACTORY / ".github/rulesets/main.json").read_text())
    assert ruleset["enforcement"] == "active" and ruleset["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]
    assert "bypass_actors" not in ruleset
    rules = {r["type"]: r.get("parameters") for r in ruleset["rules"]}
    assert set(rules) == {"deletion", "non_fast_forward", "pull_request", "required_status_checks"}
    single = json.loads((ROOT / "platform/platform.json").read_text())["gate2_single_approver"]  # configure.py --single-approver relaxes this ruleset too
    pr = rules["pull_request"]
    assert (pr["required_approving_review_count"], pr["require_code_owner_review"]) == ((0, False) if single else (1, True))
    assert rules["required_status_checks"]["required_status_checks"] == [{"context": "factory-check", "integration_id": 15368}]


# ---- the intake action ----

def test_the_intake_action_is_one_bash_step_with_no_actions_of_its_own():
    doc = yaml.safe_load(ACTION.read_text())
    assert doc["runs"]["using"] == "composite"
    [s] = doc["runs"]["steps"]
    assert s["shell"] == "bash" and "uses" not in s and s["env"] == {"DEST": "${{ inputs.path }}"}
    assert doc["inputs"] == {"path": {"description": doc["inputs"]["path"]["description"], "required": False, "default": "factory"}}
    assert doc["outputs"]["path"]["value"] == "${{ steps.copy.outputs.path }}"
    assert "${{" not in s["run"] and s["run"].lstrip().startswith("set -euo pipefail")


def make_factory(root):
    """A downloaded factory: the action at .github/actions/intake, plus records for two repos."""
    (root / ".github/actions/intake").mkdir(parents=True)
    (root / ".github/actions/intake/action.yml").write_text(ACTION.read_text())
    files = {"config/scorecard.yaml": "config\n", "intake/o/app.yaml": "app intake\n", "intake/o/other.yaml": "other intake\n",
             "na/o/app.yaml": "app na\n", "na/o/other.yaml": "other na\n", "suppressions/o/app/s1.json": "{}\n",
             "suppressions/o/other/s2.json": "{}\n", "suppressions/README.md": "doc\n"}
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def run_action(tmp_path, repo="o/app", dest="factory", factory=None):
    factory = factory or make_factory(tmp_path / "actions-cache")
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    [s] = yaml.safe_load(ACTION.read_text())["runs"]["steps"]
    env = {"PATH": "/usr/bin:/bin", "DEST": dest, "GITHUB_REPOSITORY": repo, "GITHUB_ACTION_PATH": str(factory / ".github/actions/intake"),
           "GITHUB_OUTPUT": str(tmp_path / "output")}
    out = subprocess.run(["bash", "-c", s["run"]], cwd=work, capture_output=True, text=True, env=env)
    return out, work / dest


def tree(path):
    return sorted(p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file())


def test_the_action_copies_only_the_calling_repo_s_records(tmp_path):
    out, dest = run_action(tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr
    assert tree(dest) == ["config/scorecard.yaml", "intake/o/app.yaml", "na/o/app.yaml", "suppressions/o/app/s1.json"]
    assert (dest / "intake/o/app.yaml").read_text() == "app intake\n"
    assert (tmp_path / "output").read_text() == "path=factory\n"


def test_an_app_with_no_records_gets_only_the_config(tmp_path):
    """No intake is not an error here: the scorecard fires T-8 (security_review) for it."""
    out, dest = run_action(tmp_path, repo="o/new-app")
    assert out.returncode == 0 and tree(dest) == ["config/scorecard.yaml"]


@pytest.mark.parametrize("repo", ["o", "o/..", "../o", "o/a b", "o/a/b", ""])
def test_the_action_refuses_odd_repository_names(tmp_path, repo):
    out, dest = run_action(tmp_path, repo=repo)
    assert out.returncode == 1 and not dest.exists()


@pytest.mark.parametrize("dest", ["../x", "a/b", "", ".", "with space"])
def test_the_action_refuses_odd_destinations(tmp_path, dest):
    out, _ = run_action(tmp_path, dest=dest)
    assert out.returncode == 1


def test_the_action_refuses_an_existing_destination(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "work/factory").mkdir()
    out, _ = run_action(tmp_path)
    assert out.returncode == 1 and "already exists" in out.stdout


@pytest.mark.parametrize("link", ["intake/o/app.yaml", "config/scorecard.yaml", "suppressions/o/app/s1.json", "na/o/other.yaml"])
def test_the_action_refuses_a_factory_with_a_symlink(tmp_path, link):
    factory = make_factory(tmp_path / "actions-cache")
    (factory / link).unlink()
    (factory / link).symlink_to("/etc/hostname")
    out, dest = run_action(tmp_path, factory=factory)
    assert out.returncode == 1 and not dest.exists()


def test_the_action_refuses_a_factory_without_a_config(tmp_path):
    factory = make_factory(tmp_path / "actions-cache")
    (factory / "config/scorecard.yaml").unlink()
    out, _ = run_action(tmp_path, factory=factory)
    assert out.returncode == 1 and "no config/scorecard.yaml" in out.stdout


# ---- the factory's own check ----

FACTORY_CHECK = FACTORY / ".github/workflows/factory-check.yml"
ZERO_DIGEST = "0" * 64


def test_the_factory_check_runs_on_the_factory_s_own_pull_requests_with_read_only_permissions():
    doc = load_workflow(FACTORY_CHECK)
    assert doc["on"] == {"pull_request": {"branches": ["main"]}} and doc["permissions"] == {}
    [(job_id, job)] = doc["jobs"].items()
    assert job["name"] == "factory-check" and job["permissions"] == {"contents": "read", "packages": "read"}
    assert "id-token" not in json.dumps(doc) and "secrets." not in FACTORY_CHECK.read_text()


def test_the_factory_check_refuses_the_placeholder_image_first_and_pins_the_image_like_the_other_workflows():
    doc = load_workflow(FACTORY_CHECK)
    first = doc["jobs"]["check"]["steps"][0]
    assert "BUILDER_IMAGE" in first["run"] and "0{64}" in first["run"] and ZERO_DIGEST not in first["run"] and "exit 1" in first["run"]
    assert "uses" not in first and "if" not in first
    assert re.fullmatch(r"ghcr\.io/[A-Za-z0-9_-]+/golden-road-builder@sha256:[0-9a-f]{64}", doc["env"]["BUILDER_IMAGE"])


def test_the_factory_check_checks_out_without_credentials_and_runs_both_commands_in_the_builder_image():
    steps = load_workflow(FACTORY_CHECK)["jobs"]["check"]["steps"]
    [checkout] = [s for s in steps if s.get("uses", "").startswith("actions/checkout@")]
    assert checkout["with"] == {"persist-credentials": False, "path": "factory"}
    run = "\n".join(s.get("run", "") for s in steps)
    assert '"$BUILDER_IMAGE"' in run and "--network none" in run and "--read-only" in run and '"$PWD/factory:/factory:ro"' in run
    assert "sc gate1-checks --factory /factory --out /tmp/required.json" in run and "scorecard \"$@\"" in run
    assert "sc check-factory --factory /factory" in run
    assert run.index("gate1-checks") < run.index("check-factory")


def test_the_factory_ruleset_requires_the_factory_check():
    ruleset = json.loads((FACTORY / ".github/rulesets/main.json").read_text())
    [rule] = [r for r in ruleset["rules"] if r["type"] == "required_status_checks"]
    assert rule["parameters"]["required_status_checks"] == [{"context": load_workflow(FACTORY_CHECK)["jobs"]["check"]["name"], "integration_id": 15368}]


def test_the_action_refuses_a_dangling_symlink_as_the_destination(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "work/factory").symlink_to(tmp_path / "nowhere")
    out, _ = run_action(tmp_path)
    assert out.returncode == 1 and "already exists" in out.stdout and not (tmp_path / "nowhere").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every directory")
def test_the_action_refuses_a_factory_it_cannot_scan_for_symlinks(tmp_path):
    """A `find` that fails must not read as "no symlinks": a link in a directory the scan can't enter would go unseen."""
    factory = make_factory(tmp_path / "actions-cache")
    hidden = factory / "intake/o/hidden"
    hidden.mkdir()
    (hidden / "link.yaml").symlink_to("/etc/hostname")
    hidden.chmod(0)
    try:
        out, dest = run_action(tmp_path, factory=factory)
    finally:
        hidden.chmod(0o755)
    assert out.returncode == 1 and "can't scan" in out.stdout and not dest.exists()


def test_the_factory_check_times_out_and_says_it_is_not_a_security_control():
    assert load_workflow(FACTORY_CHECK)["jobs"]["check"]["timeout-minutes"] == 10
    header = re.sub(r"\s*\n#\s*", " ", FACTORY_CHECK.read_text().split("name: factory-check")[0])
    assert "guard against typos" in header and "not a security control" in header and "code-owner review" in header


# ---- the README ----

def test_the_readme_names_the_checks_and_the_limits():
    text = (FACTORY / "README.md").read_text()
    for needle in ("`level: <level>`", "`assessed`", "Read the level here", "`oidc` (preferred)", "Anyone with write access to the app repo can read it",
                   "egress is not enforced", "No cache", "BUILDER_IMAGE"):
        assert needle in text, needle


def test_the_readme_lists_the_known_limits_the_reviews_asked_for():
    text = (FACTORY / "README.md").read_text()
    limits = text.split("## Known limits")[1]
    flat = re.sub(r"\s+", " ", limits).replace("Rebuild the builder image", "rebuild the builder image")
    for needle in ("keyed by repository name", "re-created", "repo_id",  # a re-created repo inherits the old record
                   "`factory-check`", "typo", "not a security control",  # the factory's own check
                   "image scanners", "dpkg", "rebuild the builder image", "git security fix",  # the copied git binary
                   "15368", "same name", "forge"):  # the forgeable-check gap
        assert needle in flat, needle
    assert "`factory-digest`" in text or "digest of the factory inputs" in text


def test_the_readme_covers_the_review_round_s_notes():
    text = (FACTORY / "README.md").read_text()
    flat = re.sub(r"\s+", " ", text)
    for needle in (
        "`.../workflows/scorecard.yml@refs/heads/main`", "accept only",  # the gateway binding, exactly
        "`id-token: write`", "unused in key mode",  # the permission that stays in key mode
        "Dependabot secret", "named `SCORECARD_GATEWAY_KEY`", "Dependabot-triggered runs can read the key too",
        "250 commits", "commit messages", "never fetches more",  # the pulls/{n}/commits cap
        "concurrency group", "denial of service",
        "retargeted", "assessed again in full",
        'no value', "`assessed` is the check that matters",
    ):
        assert needle in flat, needle


def test_ci_runs_the_scorecard_docker_tests_with_the_other_docker_tests():
    text = (ROOT / ".github/workflows/test.yml").read_text()
    assert "tests/pipeline/test_scorecard_image_docker.py" in text


# ---- hello-app calls the factory workflow ----

def test_hello_app_calls_the_factory_workflow_on_pull_requests_only():
    doc = load_workflow(CALLER)
    factory = load_workflow(FACTORY / ".github/workflows/scorecard.yml")
    assert doc["on"] == {"pull_request": factory["on"]["pull_request"]}, "the caller's types (edited included) are the workflow's"
    assert "edited" in doc["on"]["pull_request"]["types"]
    assert doc["permissions"] == {}
    [(name, job)] = doc["jobs"].items()
    assert name == "scorecard" and "name" not in job, "the checks are `scorecard / <job>`"
    assert CALLER_USES.match(job["uses"]) or job["uses"].startswith("ORG_PLACEHOLDER/scorecard-factory/")
    assert job["secrets"] == {name: "${{ secrets.%s }}" % name for name in (
        "SCORECARD_GATEWAY_KEY", "SCORECARD_GATEWAY_MODEL", "SCORECARD_GATEWAY_ACCESS_ID", "SCORECARD_GATEWAY_ACCESS_SECRET")}, "never `secrets: inherit`"
    assert "with" not in job and "concurrency" not in doc, "the called workflow sets the concurrency group; the same group here would deadlock"


def test_the_caller_grants_exactly_what_the_called_jobs_ask_for():
    caller = load_workflow(CALLER)["jobs"]["scorecard"]["permissions"]
    called = load_workflow(FACTORY / ".github/workflows/scorecard.yml")["jobs"]
    rank = {"read": 1, "write": 2}
    needed = {}
    for job in called.values():
        for scope, level in job.get("permissions", {}).items():
            needed[scope] = max(needed.get(scope, level), level, key=rank.__getitem__)
    assert caller == needed


def test_main_requires_the_fixed_name_scorecard_check():
    ruleset = json.loads((APP / ".github/rulesets/main.json").read_text())
    required = [r for r in ruleset["rules"] if r["type"] == "required_status_checks"][0]["parameters"]["required_status_checks"]
    assert {"context": "scorecard / assessed", "integration_id": 15368} in required
    assert not [c for c in required if c["context"].startswith("scorecard / ") and c["context"] != "scorecard / assessed"], \
        "the level check's name changes with the level, so only `assessed` can be required"
    called = load_workflow(FACTORY / ".github/workflows/scorecard.yml")["jobs"]
    assert "assessed" in called and "name" not in called["assessed"]


def test_the_scorecard_never_waits_on_itself():
    from scorecard.config import load_config

    names = {c["name"] for c in load_config(FACTORY)["gate1_checks"]}
    assert not [n for n in names if n.startswith("scorecard / ")]


# ---- the setup doc and the enforcement doc ----

def poc_step_13():
    text = (ROOT / "docs/setup/personal-poc.md").read_text()
    return text[text.index("## 13. Risk scorecard"):text.index("## 14. ")]


def test_the_personal_poc_doc_sets_up_the_scorecard_in_order():
    text = (ROOT / "docs/setup/personal-poc.md").read_text()
    for needle in ("## 13. Risk scorecard", "--scorecard-gateway-url https://<scorecard gateway host>/v1/chat/completions",
                   'http.request.method eq "POST" and http.request.uri.path eq "/v1/chat/completions"', "/key/generate", "SCORECARD_GATEWAY_MODEL", "max_budget",
                   "anyone with write access to `hello-app` can read", "prefer GitHub OIDC", "builder-v$version",
                   "grep -rlE '^  BUILDER_IMAGE: '", "GOLDEN_ROAD_SCORECARD_LIVE=1",
                   "GOLDEN_ROAD_SCORECARD_FACTORY=bootstrap/scorecard-factory", "baseline_relative", "scripts/push-bootstrap.sh <you> scorecard-factory --public",
                   "gh secret set SCORECARD_GATEWAY_KEY --repo <you>/hello-app", "gh secret set SCORECARD_GATEWAY_MODEL --repo <you>/hello-app", "gh secret set SCORECARD_GATEWAY_ACCESS_ID --repo <you>/hello-app",
                   "gh secret set SCORECARD_GATEWAY_ACCESS_SECRET --repo <you>/hello-app", "Service Auth", "CF-Access-Client-Id", "scorecard / level: security_review", "scorecard / assessed",
                   "gh pr ready", "T-2", "T-15", "T-16", 'select(.context != "scorecard / assessed")'):
        assert needle in text, needle
    order = ["### 13.1", "### 13.2", "### 13.3", "### 13.4", "### 13.5", "### 13.6", "### 13.7"]
    assert [text.index(h) for h in order] == sorted(text.index(h) for h in order)
    # the factory push needs the pinned digest, and the ruleset requires the check only after the wiring PR is merged
    assert text.index("Pin builder $version") < text.index("scorecard-factory --public") < text.index("rulesets/$id --input")
    assert 'p.add_argument("--scorecard-gateway-url"' in (ROOT / "scripts/configure.py").read_text()


def test_the_personal_poc_doc_names_every_value_the_owner_must_decide():
    step = poc_step_13()
    assert step.index("### Decide these before you start") < step.index("### 13.1")
    for placeholder in ("<scorecard gateway host>", "<reviewer model>", "<model version string>", "<daily budget>", "--single-approver"):
        assert placeholder in step[:step.index("### 13.1")], placeholder
    assert "max_budget: <daily budget>" in step and "<model version string>" in step and "json.dumps" in step
    assert "pinned-1" in step, "the committed stand-in version is named, so it is clear it must change"


def test_the_dependabot_secret_step_and_its_limit_are_documented_in_the_poc_doc():
    step = poc_step_13()
    assert "gh secret set SCORECARD_GATEWAY_KEY --repo <you>/hello-app --app dependabot" in step
    assert "gh secret set SCORECARD_GATEWAY_MODEL --repo <you>/hello-app --app dependabot" in step
    assert "gh secret set SCORECARD_GATEWAY_ACCESS_ID --repo <you>/hello-app --app dependabot" in step
    assert "gh secret set SCORECARD_GATEWAY_ACCESS_SECRET --repo <you>/hello-app --app dependabot" in step
    assert "gh secret list --repo <you>/hello-app --app dependabot" in step
    assert "Dependabot-triggered runs can read" in step and "proof item 10" in step
    assert step.index("--app dependabot") < step.index("  unset SCORECARD_GATEWAY_TOKEN\nelse"), "the key is still set when the second command runs"
    assert 'if [ -n "${SCORECARD_GATEWAY_TOKEN:-}" ]' in step and "redo 13.1 step 3" in step and "exit" not in step[step.index("### 13.5"):step.index("### 13.6")].split("```bash")[1].split("```")[0]


def test_the_poc_doc_sets_the_gateway_model_secret_guarded_and_never_prints_it():
    step = poc_step_13()
    assert "printf 'Gateway model name: '; read -rs SCORECARD_GATEWAY_MODEL; echo" in step and "export SCORECARD_GATEWAY_MODEL" in step
    block = block_with(step, "gh secret set SCORECARD_GATEWAY_MODEL")
    assert block.startswith('if [[ "${SCORECARD_GATEWAY_MODEL:-}" =~ ^[A-Za-z0-9._-]{1,128}$ && "$SCORECARD_GATEWAY_MODEL" != . && "$SCORECARD_GATEWAY_MODEL" != .. ]]; then\n')
    for extra in ("", " --app dependabot"):
        assert f"printf '%s' \"$SCORECARD_GATEWAY_MODEL\" | gh secret set SCORECARD_GATEWAY_MODEL --repo <you>/hello-app{extra}\n" in block
    assert "unset SCORECARD_GATEWAY_MODEL" in block
    assert not re.search(r"(echo|cat|curl)[^\n|]*\$SCORECARD_GATEWAY_MODEL", step), "the value is only ever piped to gh"
    checks = step[step.index("**Check:** `gh secret list --repo <you>/hello-app` lists"):]
    assert checks.index("SCORECARD_GATEWAY_MODEL") < checks.index("Fork PRs")
    for needle in ("`SCORECARD_GATEWAY_TOKEN` and `SCORECARD_GATEWAY_MODEL` from 13.1", "raise `agent.model.version`", "by a factory PR", "must not contain the deployment name",
                   "model: {version: \"<your version string>\"}"):
        assert needle in step, needle
    assert "scorecard-reviewer" not in step, "the docs name no gateway alias: the secret holds the exact name the user's gateway serves"


def test_the_factory_readme_names_the_model_secret_and_the_version_rule():
    text = " ".join((FACTORY / "README.md").read_text().split())
    for needle in ("`SCORECARD_GATEWAY_MODEL`", "organization-sensitive", "raise `agent.model.version`", "\"not assessed\"", "must not contain the deployment name"):
        assert needle in text, needle


def test_the_poc_doc_has_no_heredoc_and_no_bash_only_read_prompt_in_the_scorecard_steps():
    step = poc_step_13()
    assert not re.search(r"<<-?\s*['\"]?\w+", step), "pasting into a terminal can indent a heredoc's end marker"
    assert "read -rs -p" not in step and "read -p" not in step, "zsh's read -p is not a prompt"


def test_the_poc_doc_proof_items_cover_the_execution_notes():
    step = poc_step_13()
    for needle in ("Retarget the base", "`edited`", "Cancel mid-run", "`scorecard / assessed` shows as failed, not missing", "**Dependabot.**",
                   "`id-token`", "Re-run all jobs", "`overwrite: true`"):
        assert needle in step, needle


def test_the_enforcement_doc_names_the_scorecard_controls():
    text = (ROOT / "docs/enforcement.md").read_text()
    for needle in ("scrutiny level", "Require workflows to pass before merging", "`level: <level>`", "`scorecard / assessed`", "`env -i`",
                   "refuses to start when a runner variable is set", "Egress is not enforced"):
        assert needle in text, needle


def test_the_poc_doc_has_the_retarget_base_proof_item():
    step = poc_step_13()
    assert "Check the merge base is in the base branch" in step and "equals the tip of `main`" in step and "A re-run can't clear it" in step and "release/x" in step
    assert "`assess`, `score`, `report` and `level` are skipped" in step and "`scorecard / assessed` runs (it is `always()`) and fails" in step
    assert "ref=ghcr.io/<you>/golden-road-builder@sha256:<digest>" in step[step.index("3. **Loosened"):step.index("4. **A scanner")]


def test_the_readme_says_which_level_is_authoritative_after_a_rerun():
    flat = re.sub(r"\s+", " ", (FACTORY / "README.md").read_text())
    assert "latest `assessed` run's summary" in flat and "newest `level: <level>` check" in flat


def test_the_readme_records_that_edits_spend_tokens_and_can_drain_the_budget():
    flat = re.sub(r"\s+", " ", (FACTORY / "README.md").read_text())
    for needle in ("title or body edit", "bot edits", "daily gateway budget", "fail safe to `elevated` (T-14"):
        assert needle in flat, needle


# ---- the documented setup commands, run on scratch copies ----

POC = ROOT / "docs/setup/personal-poc.md"
REQUIRED_SAMPLE = [{"name": "security-scans / semgrep", "app_id": 15368}, {"name": "security-scans / trivy", "app_id": 15368}]


def block_with(text, needle):
    [block] = [b for b in doc_bash_blocks(text) if needle in b]
    return block


def pin_everywhere(tmp_path, doc_text, subs):
    """Run the doc's pin command on a scratch copy of .github/workflows and bootstrap, then return the copy."""
    import shutil

    shutil.copytree(ROOT / ".github/workflows", tmp_path / ".github/workflows")
    shutil.copytree(ROOT / "bootstrap", tmp_path / "bootstrap", ignore=shutil.ignore_patterns("__pycache__"))
    out = run_doc_block(block_with(doc_text, "xargs sed -i"), tmp_path, subs)
    assert out.returncode == 0, out.stderr
    return tmp_path


def assert_pinned_everywhere(root, ref, doc_text):
    lines = subprocess.run(["grep", "-rh", "^  BUILDER_IMAGE:", ".github/workflows", "bootstrap"], cwd=root, capture_output=True, text=True).stdout.splitlines()
    assert lines and set(lines) == {f"  BUILDER_IMAGE: {ref}"}, lines
    assert re.search(rf"\b{len(lines)} +BUILDER_IMAGE: \$ref", doc_text), f"the doc's Check must expect {len(lines)} lines"
    left = subprocess.run(["grep", "-rlE", r"^  BUILDER_IMAGE: .*@sha256:0{64}$", ".github", "bootstrap"], cwd=root, capture_output=True, text=True)
    assert left.stdout == "", f"a placeholder digest is left in {left.stdout}"


def test_the_documented_pin_command_leaves_no_placeholder_digest_anywhere(tmp_path):
    text = poc_step_13()  # step 14 has its own pin block
    ref = "ghcr.io/acme/golden-road-builder@sha256:" + "ab" * 32
    root = pin_everywhere(tmp_path, text, {"<you>": "acme", "<digest>": "ab" * 32})
    assert_pinned_everywhere(root, ref, text)
    # the factory has two workflows that run the image, and the pin reaches both
    for name in ("scorecard.yml", "factory-check.yml"):
        assert ref in (root / "bootstrap/scorecard-factory/.github/workflows" / name).read_text()


def test_the_documented_factory_edits_pass_the_factory_tests(tmp_path):
    """personal-poc.md 13.2: the Gate 1 entries, the model version and the code owners, run as written on a scratch copy."""
    from scorecard.config import load_config

    work = scratch_factory_workspace(ROOT, tmp_path, REQUIRED_SAMPLE)
    block = block_with(POC.read_text(), "gate1-extra.txt")
    out = run_doc_block(block, work / "bootstrap/..", {"<src>/.venv/bin/python": __import__("sys").executable, "<model version string>": "vendor/model-4&1",
                                                       "<you>": "acme", "<deputy>": "other"})
    assert out.returncode == 0, out.stderr
    factory = work / "bootstrap/scorecard-factory"
    config = load_config(factory)
    assert config["agent"]["model"] == {"version": "vendor/model-4&1"}
    assert [c for c in config["gate1_checks"] if c in REQUIRED_SAMPLE] == REQUIRED_SAMPLE
    assert_factory_config_matches_sample(factory, REQUIRED_SAMPLE)
    assert_codeowners_protect_the_records(factory)


def test_the_poc_doc_makes_the_firewall_rule_before_the_route_and_posts_only():
    step = poc_step_13()
    one = step[step.index("1. **Firewall rule first"):step.index("2. **Model.**")]
    assert one.index("WAF custom rule") < one.index("add the public hostname")
    assert 'http.request.method eq "POST"' in one and "allowed_routes" in step and "<litellm internal host>" in step[:step.index("### 13.1")]


# ---- the intake request form ----

FORM = FACTORY / ".github/ISSUE_TEMPLATE/intake-request.yml"


def test_the_intake_form_is_what_doctor_searches_for():
    from golden_road import doctor
    form = yaml.safe_load(FORM.read_text())
    assert form["labels"] == [doctor.INTAKE_LABEL] and form["title"] == doctor.INTAKE_TITLE_PREFIX
    fields = {f["id"]: f for f in form["body"] if "id" in f}
    assert list(fields) == ["repository", "does", "does-not", "owner", "data-classification"]
    assert all(f["validations"]["required"] is True for f in fields.values())
    schema = json.loads((ROOT / "src/contracts/schemas/builder-spec.v1.json").read_text())
    assert fields["data-classification"]["type"] == "dropdown"
    options = fields["data-classification"]["attributes"]["options"]
    assert options == schema["properties"]["data_classification"]["enum"]
    assert options == json.loads((ROOT / "src/contracts/schemas/intake.v1.json").read_text())["properties"]["data_classification"]["enum"]
    assert "tier" not in fields
    # No item asks about the tier; the only mention, markdown included, is the sentence saying security sets it.
    mentions = [text for item in form["body"] for text in json.dumps(item).replace("\\n", "\n").splitlines() if "tier" in text.lower()]
    allowed = "Security sets the tier and turns this into `intake/<owner>/<repo>.yaml` by pull request."
    assert [m.strip() for m in mentions] == [allowed], mentions


def test_the_readme_names_the_form_and_the_title():
    text = " ".join((FACTORY / "README.md").read_text().split())
    for needle in ("`.github/ISSUE_TEMPLATE/intake-request.yml`", "`Intake request: <owner>/<repo>`", "no label"):
        assert needle in text, needle


def test_the_access_setup_in_the_poc_doc_keeps_the_secret_out_of_argv_and_sets_all_four_secrets_guarded():
    step = poc_step_13()
    assert not re.search(r'-H\s+"[^"\n]*\$SCORECARD_GATEWAY_ACCESS_(SECRET|ID)', step), "a curl -H argument would put the secret in argv"
    assert "-H @<(printf 'CF-Access-Client-Id: %s\\nCF-Access-Client-Secret: %s\\n' \"$SCORECARD_GATEWAY_ACCESS_ID\" \"$SCORECARD_GATEWAY_ACCESS_SECRET\")" in step
    guard = 'if [ -n "${SCORECARD_GATEWAY_ACCESS_ID:-}" ] && [ -n "${SCORECARD_GATEWAY_ACCESS_SECRET:-}" ]; then'
    block = step[step.index(guard):]
    block = block[:block.index("\nfi\n")]
    for name in ("ID", "SECRET"):
        for extra in ("", " --app dependabot"):
            assert f"printf '%s' \"$SCORECARD_GATEWAY_ACCESS_{name}\" | gh secret set SCORECARD_GATEWAY_ACCESS_{name} --repo <you>/hello-app{extra}\n" in block + "\n"
    assert "unset SCORECARD_GATEWAY_ACCESS_ID SCORECARD_GATEWAY_ACCESS_SECRET" in block and "paste it" not in step
    assert "printf 'Access client id: '; read -rs SCORECARD_GATEWAY_ACCESS_ID; echo" in step
    assert "printf 'Access client secret: '; read -rs SCORECARD_GATEWAY_ACCESS_SECRET; echo" in step
    checks = step[step.index("**Check:** `gh secret list --repo <you>/hello-app` lists"):]
    assert checks.index("SCORECARD_GATEWAY_ACCESS_ID") < checks.index("Fork PRs") and "SCORECARD_GATEWAY_ACCESS_SECRET" in checks[:checks.index("Fork PRs")]
    for needle in ("Return 401 response for service auth policies", "empty path", "before the tunnel hostname exists"):
        assert needle in step, needle
    assert "path `/v1/chat/completions`, and a policy" not in step

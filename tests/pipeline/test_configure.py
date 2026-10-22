import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import builder_cmd, builder_env, unconfigure_agents, unconfigure_factory, unconfigure_hello_app, unconfigure_platform

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def workspace(tmp_path):
    shutil.copytree(ROOT / "platform", tmp_path / "platform")
    shutil.copytree(ROOT / "bootstrap", tmp_path / "bootstrap")
    live = json.loads((ROOT / "platform/platform.json").read_text())
    org = live.get("org")
    unconfigure_platform(tmp_path / "platform")
    unconfigure_hello_app(tmp_path / "bootstrap/hello-app")
    unconfigure_factory(tmp_path / "bootstrap/scorecard-factory", org)
    unconfigure_agents(tmp_path / "bootstrap/golden-road-agents", org, live.get("github_host"), live.get("actions_oidc_issuer"))
    return tmp_path


EDGE = ["--edge-namespace", "edge-ns", "--edge-label", "app=edge", "--edge-label", "tier=ingress", "--identity-header", "X-Roles-Test"]
GOOD = ["--org", "example-org", "--gateway-host", "llm.example.org", *EDGE]


def configure(workspace, *args):
    return subprocess.run([sys.executable, str(ROOT / "scripts/configure.py"), "--root", str(workspace), *args],
                          capture_output=True, text=True)


def test_configure_fills_values_and_regenerates(workspace):
    assert "ORG_PLACEHOLDER" in (workspace / "bootstrap/hello-app/.github/workflows/ci.yml").read_text()
    out = configure(workspace, *GOOD)
    assert out.returncode == 0, out.stderr
    platform = json.loads((workspace / "platform/platform.json").read_text())
    assert platform["model_gateway_hosts"] == ["llm.example.org"]
    assert platform["edge_ingress"] == {"namespace": "edge-ns", "pod_labels": {"app": "edge", "tier": "ingress"}}
    assert platform["identity_header"] == "X-Roles-Test"
    for path in (workspace / "bootstrap").rglob("*"):
        if path.is_file():
            assert "replace-me" not in path.read_text(errors="replace"), path
    assert "edge-ns" in (workspace / "bootstrap/hello-app/deploy/base/networkpolicy.yaml").read_text()
    spec = json.loads((workspace / "bootstrap/hello-app/builder-spec.json").read_text())
    assert spec["egress"][0]["host"] == "llm.example.org"
    for path in (workspace / "bootstrap").rglob("*"):
        if path.is_file():
            assert "ORG_PLACEHOLDER" not in path.read_text(errors="replace"), path
    assert "example-org/platform-pipeline" in (workspace / "bootstrap/hello-app/.github/workflows/ci.yml").read_text()
    env = builder_env(BUILDER_PLATFORM_CONFIG=str(workspace / "platform/platform.json"), PATH="")
    verify = subprocess.run([*builder_cmd(), "verify", str(workspace / "bootstrap/hello-app")],
                            capture_output=True, text=True, env=env)
    assert verify.returncode == 0, verify.stdout


@pytest.mark.parametrize("args", [
    ["--org", "bad org", "--gateway-host", "llm.example.org", *EDGE],
    ["--org", "ok", "--gateway-host", "not a host", *EDGE],
    ["--org", "ok", "--gateway-host", "api.openai.com", *EDGE],
])
def test_configure_rejects_bad_values(workspace, args):
    assert configure(workspace, *args).returncode != 0


@pytest.mark.parametrize("drop", ["--edge-namespace", "--edge-label", "--identity-header"])
def test_edge_and_header_flags_are_required(workspace, drop):
    # drop every occurrence of the flag with its value
    args = []
    it = iter(GOOD)
    for a in it:
        if a == drop:
            next(it)
        else:
            args.append(a)
    before = (workspace / "platform/platform.json").read_text()
    out = configure(workspace, *args)
    assert out.returncode != 0
    assert (workspace / "platform/platform.json").read_text() == before


@pytest.mark.parametrize("extra", [
    ["--identity-header", "bad header"], ["--identity-header", "X-Roles\n"], ["--identity-header", "X-replace-me"],
    ["--edge-namespace", "replace-me"], ["--edge-namespace", "Bad_NS"], ["--edge-label", "app=replace-me"],
])
def test_edge_and_header_values_are_validated(workspace, extra):
    args = list(GOOD)
    flag = extra[0]
    idx = args.index(flag)
    args[idx + 1] = extra[1]
    assert configure(workspace, *args).returncode != 0


@pytest.mark.parametrize("labels", [["app=a", "app=b"], ["app="], ["app=a", "tier="]])
def test_edge_labels_reject_repeated_keys_and_empty_values(workspace, labels):
    args = ["--org", "ok", "--gateway-host", "llm.example.org", "--edge-namespace", "edge-ns", "--identity-header", "X-Roles-Test"]
    for label in labels:
        args += ["--edge-label", label]
    before = (workspace / "platform/platform.json").read_text()
    assert configure(workspace, *args).returncode != 0
    assert (workspace / "platform/platform.json").read_text() == before


PLACEHOLDER = re.compile("replace-me|ORG_PLACEHOLDER")


def placeholder_state(root):
    out = subprocess.run([sys.executable, str(root / "scripts/configure.py"), "--print-placeholder-state"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return dict(line.split("\t", 1) for line in out.stdout.splitlines())


def test_placeholder_state_names_every_file_that_holds_a_placeholder(workspace):
    """The export refuses a tree unless these files still hold their placeholders, so the list must not drift."""
    state = placeholder_state(ROOT)
    holders = {p.relative_to(workspace).as_posix() for d in ("platform", "bootstrap") for p in (workspace / d).rglob("*")
               if p.is_file() and PLACEHOLDER.search(p.read_text(errors="replace"))}
    assert set(state) == holders
    for rel, marker in state.items():
        assert marker in (workspace / rel).read_text(), rel
    out = configure(workspace, *GOOD)
    assert out.returncode == 0, out.stderr
    for rel, marker in state.items():
        # configure.py renames the factory's ORG_PLACEHOLDER record directories to the org
        assert marker not in (workspace / rel.replace("/ORG_PLACEHOLDER/", "/example-org/")).read_text(), f"configure left {marker} in {rel}"


RULESET = "bootstrap/hello-app/.github/rulesets/main.json"
PROD_ENV = "bootstrap/hello-app/.github/environment-prod.json"
RESTORE = "git checkout -- bootstrap platform"


def pr_params(root):
    rules = json.loads((root / RULESET).read_text())["rules"]
    return next(r for r in rules if r["type"] == "pull_request")["parameters"]


FACTORY_RULESET = "bootstrap/scorecard-factory/.github/rulesets/main.json"


def factory_pr_params(root):
    rules = json.loads((root / FACTORY_RULESET).read_text())["rules"]
    return next(r for r in rules if r["type"] == "pull_request")["parameters"]


def live_tree_is_pristine():
    return "org" not in json.loads((ROOT / "platform/platform.json").read_text())


def test_unconfigured_workspace_matches_the_committed_placeholder_state(workspace):
    """The fixture must rebuild the committed state, whatever mode the live tree was configured in."""
    state = placeholder_state(ROOT)
    assert json.loads((workspace / "platform/platform.json").read_text())["gate2_single_approver"] is False
    assert pr_params(workspace) == {"required_approving_review_count": 1, "dismiss_stale_reviews_on_push": True,
                                    "require_code_owner_review": True, "require_last_push_approval": True,
                                    "required_review_thread_resolution": False}
    assert json.loads((workspace / PROD_ENV).read_text())["prevent_self_review"] is True
    for rel, marker in state.items():
        assert marker in (workspace / rel).read_text(), rel
    verify = subprocess.run([*builder_cmd(), "verify", str(workspace / "bootstrap/hello-app")], capture_output=True, text=True,
                            env=builder_env(BUILDER_PLATFORM_CONFIG=str(workspace / "platform/platform.json"), PATH=""))
    assert verify.returncode == 0, verify.stdout
    if not live_tree_is_pristine():
        return  # a configured tree has no committed placeholder content to compare byte for byte
    for rel in (*state, RULESET, PROD_ENV, "bootstrap/hello-app/.builder.lock"):
        assert (workspace / rel).read_bytes() == (ROOT / rel).read_bytes(), rel


def test_without_the_flag_approval_settings_are_untouched(workspace):
    before = {rel: (workspace / rel).read_text() for rel in (RULESET, PROD_ENV)}
    assert configure(workspace, *GOOD).returncode == 0
    assert {rel: (workspace / rel).read_text() for rel in (RULESET, PROD_ENV)} == before
    assert json.loads((workspace / "platform/platform.json").read_text())["gate2_single_approver"] is False
    assert pr_params(workspace)["required_approving_review_count"] == 1
    assert json.loads((workspace / PROD_ENV).read_text())["prevent_self_review"] is True


def test_single_approver_flag_relaxes_review_rules_and_records_the_mode(workspace):
    ruleset_before = json.loads((workspace / RULESET).read_text())
    env_before = json.loads((workspace / PROD_ENV).read_text())
    out = configure(workspace, *GOOD, "--single-approver")
    assert out.returncode == 0, out.stderr
    assert json.loads((workspace / "platform/platform.json").read_text())["gate2_single_approver"] is True
    ruleset = json.loads((workspace / RULESET).read_text())
    params = pr_params(workspace)
    assert (params["required_approving_review_count"], params["require_code_owner_review"], params["require_last_push_approval"]) == (0, False, False)
    # Nothing else changes: the rest of the pull_request rule, the other rules, and the top level.
    ruleset_before["rules"][2]["parameters"].update(required_approving_review_count=0, require_code_owner_review=False, require_last_push_approval=False)
    assert ruleset == ruleset_before
    assert [r["type"] for r in ruleset["rules"]] == ["deletion", "non_fast_forward", "pull_request", "required_status_checks"]
    env = json.loads((workspace / PROD_ENV).read_text())
    assert env == {**env_before, "prevent_self_review": False}
    assert env["reviewers"] == [{"type": "User", "id": 0}]
    # the generated app still verifies against the new platform config
    verify = subprocess.run([*builder_cmd(), "verify", str(workspace / "bootstrap/hello-app")], capture_output=True, text=True,
                            env=builder_env(BUILDER_PLATFORM_CONFIG=str(workspace / "platform/platform.json"), PATH=""))
    assert verify.returncode == 0, verify.stdout


def test_single_approver_flag_relaxes_the_factory_ruleset_the_same_way(workspace):
    before = json.loads((workspace / FACTORY_RULESET).read_text())
    keys = ("required_approving_review_count", "require_code_owner_review", "require_last_push_approval")
    assert tuple(factory_pr_params(workspace)[k] for k in keys) == (1, True, True)
    assert configure(workspace, *GOOD, "--single-approver").returncode == 0
    after = json.loads((workspace / FACTORY_RULESET).read_text())
    assert tuple(factory_pr_params(workspace)[k] for k in keys) == (0, False, False)
    next(r for r in before["rules"] if r["type"] == "pull_request")["parameters"].update(required_approving_review_count=0, require_code_owner_review=False, require_last_push_approval=False)
    assert after == before, "only the approval settings change; the required scorecard-factory check and the rest stay"


def test_the_default_mode_leaves_the_factory_ruleset_alone(workspace):
    before = (workspace / FACTORY_RULESET).read_bytes()
    assert configure(workspace, *GOOD).returncode == 0
    assert (workspace / FACTORY_RULESET).read_bytes() == before


def test_a_factory_ruleset_that_is_not_as_expected_stops_single_approver_before_anything_changes(workspace):
    doc = json.loads((workspace / FACTORY_RULESET).read_text())
    doc["rules"] = [r for r in doc["rules"] if r["type"] != "pull_request"]
    (workspace / FACTORY_RULESET).write_text(json.dumps(doc))
    before = {p: p.read_bytes() for d in ("platform", "bootstrap") for p in (workspace / d).rglob("*") if p.is_file()}
    out = configure(workspace, *GOOD, "--single-approver")
    assert out.returncode != 0 and "--single-approver" in out.stderr and FACTORY_RULESET in out.stderr
    assert {p: p.read_bytes() for d in ("platform", "bootstrap") for p in (workspace / d).rglob("*") if p.is_file()} == before


def test_rerunning_with_the_same_mode_is_fine(workspace):
    for flags in ([], ["--single-approver"]):
        ws = workspace / ("single" if flags else "two")
        shutil.copytree(workspace / "platform", ws / "platform")
        shutil.copytree(workspace / "bootstrap", ws / "bootstrap")
        assert configure(ws, *GOOD, *flags).returncode == 0
        snapshot = {p.relative_to(ws): p.read_bytes() for d in ("platform", "bootstrap") for p in (ws / d).rglob("*") if p.is_file()}
        again = configure(ws, *GOOD, *flags)
        assert again.returncode == 0, again.stderr
        assert {p.relative_to(ws): p.read_bytes() for d in ("platform", "bootstrap") for p in (ws / d).rglob("*") if p.is_file()} == snapshot


@pytest.mark.parametrize("first,second", [([], ["--single-approver"]), (["--single-approver"], [])])
def test_switching_modes_after_configure_is_refused(workspace, first, second):
    assert configure(workspace, *GOOD, *first).returncode == 0
    snapshot = {p: p.read_bytes() for d in ("platform", "bootstrap") for p in (workspace / d).rglob("*") if p.is_file()}
    out = configure(workspace, *GOOD, *second)
    assert out.returncode != 0
    assert "single-approver" in out.stderr and RESTORE in out.stderr
    assert {p: p.read_bytes() for d in ("platform", "bootstrap") for p in (workspace / d).rglob("*") if p.is_file()} == snapshot


@pytest.mark.parametrize("break_file", ["two_pr_rules", "no_prevent_self_review"])
def test_single_approver_with_unexpected_files_changes_nothing(workspace, break_file):
    """The files are only inspected by --single-approver, so this fails after the org and spec checks, in phase 1."""
    if break_file == "two_pr_rules":
        doc = json.loads((workspace / RULESET).read_text())
        doc["rules"].append(next(r for r in doc["rules"] if r["type"] == "pull_request"))
        (workspace / RULESET).write_text(json.dumps(doc))
    else:
        doc = json.loads((workspace / PROD_ENV).read_text())
        del doc["prevent_self_review"]
        (workspace / PROD_ENV).write_text(json.dumps(doc))
    before = {p: p.read_bytes() for d in ("platform", "bootstrap") for p in (workspace / d).rglob("*") if p.is_file()}
    out = configure(workspace, *GOOD, "--single-approver")
    assert out.returncode != 0 and "--single-approver" in out.stderr
    assert {p: p.read_bytes() for d in ("platform", "bootstrap") for p in (workspace / d).rglob("*") if p.is_file()} == before


def test_default_approval_files_are_not_placeholder_state():
    """They hold no placeholder: the export check must not mistake default content for a configured tree.
    The live tree's approval settings follow its recorded mode: strict by default, relaxed exactly when single-approver."""
    state = placeholder_state(ROOT)
    assert RULESET not in state and PROD_ENV not in state
    single = json.loads((ROOT / "platform/platform.json").read_text())["gate2_single_approver"]
    assert type(single) is bool
    params, env = pr_params(ROOT), json.loads((ROOT / PROD_ENV).read_text())
    keys = ("required_approving_review_count", "require_code_owner_review", "require_last_push_approval")
    assert FACTORY_RULESET not in state
    assert tuple(factory_pr_params(ROOT)[k] for k in keys) == tuple(params[k] for k in keys)
    if single:
        assert tuple(params[k] for k in keys) == (0, False, False)
        assert env["prevent_self_review"] is False
    else:
        assert tuple(params[k] for k in keys) == (1, True, True)
        assert env["prevent_self_review"] is True


# ---- the scorecard factory ----

FACTORY_CONFIG = "bootstrap/scorecard-factory/config/scorecard.yaml"


def factory_url(root):
    import yaml

    return yaml.safe_load((root / FACTORY_CONFIG).read_text())["agent"]["gateway"]["url"]


def test_configure_fills_the_factory_and_renames_its_record_directories(workspace):
    out = configure(workspace, *GOOD)
    assert out.returncode == 0, out.stderr
    factory = workspace / "bootstrap/scorecard-factory"
    assert factory_url(workspace) == "https://llm.example.org/v1/chat/completions"
    assert not (factory / "intake/ORG_PLACEHOLDER").exists()
    assert "repo: example-org/hello-app" in (factory / "intake/example-org/hello-app.yaml").read_text()
    from scorecard.config import load_config, load_intake

    assert load_config(factory)["agent"]["gateway"]["url"] == "https://llm.example.org/v1/chat/completions"
    assert load_intake(factory, "example-org/hello-app")["repo"] == "example-org/hello-app"


def test_configure_points_the_factory_workflow_at_the_org(workspace):
    assert configure(workspace, *GOOD).returncode == 0
    workflow = (workspace / "bootstrap/scorecard-factory/.github/workflows/scorecard.yml").read_text()
    assert "ORG_PLACEHOLDER" not in workflow
    assert "uses: example-org/scorecard-factory/.github/actions/intake@main" in workflow
    assert "github.repository != 'example-org/scorecard-factory'" in workflow
    assert "BUILDER_IMAGE: ghcr.io/example-org/golden-road-builder@sha256:" + "0" * 64 in workflow, "the digest is pinned by hand after the builder release"


def test_configure_points_the_hello_app_caller_at_the_factory(workspace):
    assert configure(workspace, *GOOD).returncode == 0
    caller = (workspace / "bootstrap/hello-app/.github/workflows/scorecard.yml").read_text()
    assert "uses: example-org/scorecard-factory/.github/workflows/scorecard.yml@main" in caller


def test_the_scorecard_gateway_url_can_differ_from_the_app_gateway(workspace):
    url = "https://llm-ci.example.org/v1/chat/completions"
    assert configure(workspace, *GOOD, "--scorecard-gateway-url", url).returncode == 0
    assert factory_url(workspace) == url
    assert json.loads((workspace / "platform/platform.json").read_text())["model_gateway_hosts"] == ["llm.example.org"]
    assert configure(workspace, *GOOD, "--scorecard-gateway-url", url).returncode == 0, "re-running with the same URL is fine"
    out = configure(workspace, *GOOD)
    assert out.returncode != 0 and "already" in out.stderr and factory_url(workspace) == url


@pytest.mark.parametrize("url", ["http://llm.example.org/v1/chat/completions", "https://user@llm.example.org/v1", "https://llm.example.org",
                                 "https://llm.example.org/v1#x", "https://llm.replace-me.invalid/v1/chat/completions",
                                 'https://llm.example.org/v1"x', "https://api.openai.com/v1/chat/completions", "https://not_a_host/v1"])
def test_a_bad_scorecard_gateway_url_changes_nothing(workspace, url):
    before = (workspace / FACTORY_CONFIG).read_text()
    assert configure(workspace, *GOOD, "--scorecard-gateway-url", url).returncode != 0
    assert (workspace / FACTORY_CONFIG).read_text() == before
    assert (workspace / "bootstrap/scorecard-factory/intake/ORG_PLACEHOLDER").is_dir()


def test_a_factory_config_the_scorecard_would_refuse_changes_nothing(workspace):
    """The candidate config is loaded with the scorecard's own loader before anything is written."""
    config = workspace / FACTORY_CONFIG
    config.write_text(config.read_text().replace("'^\\s*@\\w+\\.(skip|skipif|xfail)\\b'", "'(unclosed'"))
    assert "(unclosed" in config.read_text()
    before = {p: p.read_bytes() for d in ("platform", "bootstrap") for p in (workspace / d).rglob("*") if p.is_file()}
    out = configure(workspace, *GOOD)
    assert out.returncode != 0 and "scorecard" in out.stderr and "invalid regex" in out.stderr
    assert {p: p.read_bytes() for d in ("platform", "bootstrap") for p in (workspace / d).rglob("*") if p.is_file()} == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root can rename in a read-only directory")
def test_a_rename_that_fails_partway_says_how_to_restore(workspace):
    locked = workspace / "bootstrap/scorecard-factory/intake"
    locked.chmod(0o555)
    try:
        out = configure(workspace, *GOOD)
    finally:
        locked.chmod(0o755)
    assert out.returncode != 0 and "rename" in out.stderr and RESTORE in out.stderr


AGENTS_RULESET = "bootstrap/golden-road-agents/.github/rulesets/main.json"


def test_single_approver_flag_relaxes_the_agents_ruleset_the_same_way(workspace):
    before = json.loads((workspace / AGENTS_RULESET).read_text())
    assert configure(workspace, *GOOD, "--single-approver").returncode == 0
    after = json.loads((workspace / AGENTS_RULESET).read_text())
    next(r for r in before["rules"] if r["type"] == "pull_request")["parameters"].update(
        required_approving_review_count=0, require_code_owner_review=False, require_last_push_approval=False)
    assert after == before


def test_the_default_mode_leaves_the_agents_ruleset_alone(workspace):
    before = (workspace / AGENTS_RULESET).read_bytes()
    assert configure(workspace, *GOOD).returncode == 0
    assert (workspace / AGENTS_RULESET).read_bytes() == before


AGENTS_REL = Path("bootstrap/golden-road-agents")
ISSUER = "https://token.actions.githubusercontent.com"


def load_kit(path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"kit_{path.stem}_{abs(hash(str(path)))}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_agents_code_owners_text_in_helpers_is_the_committed_file():
    from helpers import COMMITTED_AGENTS_CODEOWNERS
    live = (ROOT / "bootstrap/golden-road-agents/.github/CODEOWNERS").read_text()
    if "ORG_PLACEHOLDER" in live:  # a pristine tree; a configured one holds the org or personal logins
        assert live == COMMITTED_AGENTS_CODEOWNERS


def test_the_host_and_issuer_default_to_github_com_and_are_recorded(workspace):
    before = {rel: (workspace / rel).read_text() for rel in host_sites()}
    out = configure(workspace, *GOOD)
    assert out.returncode == 0, out.stderr
    platform = json.loads((workspace / "platform/platform.json").read_text())
    assert platform["github_host"] == "github.com" and platform["actions_oidc_issuer"] == ISSUER
    for rel, text in before.items():
        assert (workspace / rel).read_text() == text.replace("ORG_PLACEHOLDER", "example-org"), rel


def test_a_ghe_com_tenant_rewrites_the_kit_and_derives_the_issuer(workspace):
    out = configure(workspace, *GOOD, "--github-host", "acme.ghe.com")
    assert out.returncode == 0, out.stderr
    platform = json.loads((workspace / "platform/platform.json").read_text())
    assert platform["github_host"] == "acme.ghe.com" and platform["actions_oidc_issuer"] == "https://token.actions.acme.ghe.com"
    agents = workspace / AGENTS_REL
    for script in ("stub/golden-road/fetch_skill.py", "skill/install_builder.py"):
        module = load_kit(agents / script)
        assert (module.HOST, module.OIDC_ISSUER) == ("acme.ghe.com", "https://token.actions.acme.ghe.com"), script
    install = (agents / "INSTALL_FOR_AGENTS.md").read_text()
    assert "gh auth status --hostname acme.ghe.com" in install and "--cert-oidc-issuer https://token.actions.acme.ghe.com" in install
    assert "https://acme.ghe.com/example-org/golden-road-agents/.github/workflows/release-skill.yml@refs/tags/<tag>" in install
    assert "gh api --hostname acme.ghe.com " in (agents / "README.md").read_text()
    for path in agents.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            assert "github.com" not in path.read_text(errors="replace") and "githubusercontent" not in path.read_text(errors="replace"), path


def test_another_host_needs_an_explicit_issuer(workspace):
    before = (workspace / "platform/platform.json").read_text()
    out = configure(workspace, *GOOD, "--github-host", "ghe.example.org")
    assert out.returncode != 0 and "--cert-oidc-issuer" in out.stderr
    assert (workspace / "platform/platform.json").read_text() == before
    out = configure(workspace, *GOOD, "--github-host", "ghe.example.org", "--cert-oidc-issuer", "https://ghe.example.org/_services/token")
    assert out.returncode == 0, out.stderr
    assert load_kit(workspace / AGENTS_REL / "skill/install_builder.py").OIDC_ISSUER == "https://ghe.example.org/_services/token"


@pytest.mark.parametrize("extra", [
    ["--github-host", "not a host"], ["--github-host", "github.replace-me.invalid"], ["--github-host", "localhost"],
    ["--cert-oidc-issuer", "http://token.example.org"], ["--cert-oidc-issuer", "https://token.example.org/?a=b"],
    ["--cert-oidc-issuer", "https://user@token.example.org"],
])
def test_bad_hosts_and_issuers_change_nothing(workspace, extra):
    before = (workspace / "platform/platform.json").read_text()
    out = configure(workspace, *GOOD, *extra)
    assert out.returncode != 0
    assert (workspace / "platform/platform.json").read_text() == before


def test_changing_the_host_after_configure_is_refused_and_changes_nothing(workspace):
    assert configure(workspace, *GOOD, "--github-host", "acme.ghe.com").returncode == 0
    snapshot = {p: p.read_bytes() for p in (workspace / "bootstrap").rglob("*") if p.is_file()}
    for extra in (["--github-host", "other.ghe.com"], ["--cert-oidc-issuer", "https://token.example.org"]):
        out = configure(workspace, *GOOD, *extra)
        assert out.returncode != 0 and "git checkout -- bootstrap platform" in out.stderr, extra
    assert configure(workspace, *GOOD).returncode == 0, "re-running without the flags keeps the recorded host"
    assert {p: p.read_bytes() for p in (workspace / "bootstrap").rglob("*") if p.is_file()} == snapshot


def test_a_kit_file_that_names_the_host_an_unexpected_number_of_times_stops_before_anything_changes(workspace):
    install = workspace / AGENTS_REL / "INSTALL_FOR_AGENTS.md"
    install.write_text(install.read_text() + "\nSee https://github.com/elsewhere.\n")
    before = (workspace / "platform/platform.json").read_text()
    out = configure(workspace, *GOOD, "--github-host", "acme.ghe.com")
    assert out.returncode != 0 and "INSTALL_FOR_AGENTS.md" in out.stderr
    assert (workspace / "platform/platform.json").read_text() == before


def test_single_approver_cannot_flip_on_an_older_configured_tree(workspace):
    assert configure(workspace, *GOOD).returncode == 0
    path = workspace / "platform/platform.json"
    older = {k: v for k, v in json.loads(path.read_text()).items() if k not in ("github_host", "actions_oidc_issuer")}
    path.write_text(json.dumps(older, indent=2) + "\n")
    snapshot = {p: p.read_bytes() for p in (workspace / "bootstrap").rglob("*") if p.is_file()}
    out = configure(workspace, *GOOD, "--single-approver", "--github-host", "acme.ghe.com")
    assert out.returncode != 0 and "--single-approver" in out.stderr
    assert json.loads(path.read_text()) == older
    assert {p: p.read_bytes() for p in (workspace / "bootstrap").rglob("*") if p.is_file()} == snapshot


def test_the_issuer_host_is_lowercased_and_a_trailing_slash_is_refused(workspace):
    before = (workspace / "platform/platform.json").read_text()
    out = configure(workspace, *GOOD, "--github-host", "ghe.example.org", "--cert-oidc-issuer", "https://ghe.example.org/_services/token/")
    assert out.returncode != 0 and (workspace / "platform/platform.json").read_text() == before
    out = configure(workspace, *GOOD, "--github-host", "ghe.example.org", "--cert-oidc-issuer", "https://GHE.Example.org/_services/token")
    assert out.returncode == 0, out.stderr
    assert json.loads((workspace / "platform/platform.json").read_text())["actions_oidc_issuer"] == "https://ghe.example.org/_services/token"


def host_sites():
    out = subprocess.run([sys.executable, str(ROOT / "scripts/configure.py"), "--print-host-sites"], capture_output=True, text=True, check=True)
    return {line.split("\t")[0]: tuple(int(n) for n in line.split("\t")[1:]) for line in out.stdout.splitlines()}


def test_host_sites_name_every_kit_file_that_holds_the_host_or_the_issuer(workspace):
    sites = host_sites()
    agents = workspace / AGENTS_REL
    found = {}
    for path in agents.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            text = path.read_text(errors="replace")
            counts = (text.count("github.com"), text.count(ISSUER))
            if counts != (0, 0):
                found[str(path.relative_to(workspace))] = counts
    assert found == sites


def test_the_landing_page_defaults_to_the_kit_repo_and_reaches_hello_app_s_block(workspace):
    assert configure(workspace, *GOOD).returncode == 0
    platform = json.loads((workspace / "platform/platform.json").read_text())
    assert platform["landing_page_url"] == "https://github.com/example-org/golden-road-agents" and "approved_models_url" not in platform
    assert "<https://github.com/example-org/golden-road-agents>" in (workspace / "bootstrap/hello-app/AGENTS.md").read_text()


def test_the_links_can_be_given_and_changed_later(workspace):
    landing, models = "https://intranet.example.org/golden-road?x=1", "https://intranet.example.org/ai/models"
    assert configure(workspace, *GOOD, "--landing-page-url", landing).returncode == 0
    out = configure(workspace, *GOOD, "--approved-models-url", models)
    assert out.returncode == 0, out.stderr
    platform = json.loads((workspace / "platform/platform.json").read_text())
    assert (platform["landing_page_url"], platform["approved_models_url"]) == (landing, models), "a later run keeps the recorded landing page"
    agents = (workspace / "bootstrap/hello-app/AGENTS.md").read_text()
    assert f"<{landing}>" in agents and f"<{models}>" in agents


@pytest.mark.parametrize("flag", ["--landing-page-url", "--approved-models-url"])
@pytest.mark.parametrize("url", ["http://intranet.example.org/x", "https://intranet.example.org/a b", "https://u@intranet.example.org/",
                                 "https://intranet.example.org/(x)", "https://llm.replace-me.invalid/x",
                                 "https://intranet.example.org/a-->b", "https://intranet.example.org/a>b", "https://intranet.example.org/[a](b)", "https://intranet.example.org/a\nb", 'https://intranet.example.org/"a"', "https://intranet.example.org/<!--"])
def test_bad_links_change_nothing(workspace, flag, url):
    before = (workspace / "platform/platform.json").read_text()
    assert configure(workspace, *GOOD, flag, url).returncode != 0
    assert (workspace / "platform/platform.json").read_text() == before

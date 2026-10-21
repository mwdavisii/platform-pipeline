import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import builder_cmd, builder_env, unconfigure_hello_app, unconfigure_platform

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def workspace(tmp_path):
    shutil.copytree(ROOT / "platform", tmp_path / "platform")
    shutil.copytree(ROOT / "bootstrap", tmp_path / "bootstrap")
    unconfigure_platform(tmp_path / "platform")
    unconfigure_hello_app(tmp_path / "bootstrap/hello-app")
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
        assert marker not in (workspace / rel).read_text(), f"configure left {marker} in {rel}"


RULESET = "bootstrap/hello-app/.github/rulesets/main.json"
PROD_ENV = "bootstrap/hello-app/.github/environment-prod.json"
RESTORE = "git checkout -- bootstrap platform"


def pr_params(root):
    rules = json.loads((root / RULESET).read_text())["rules"]
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
    if single:
        assert tuple(params[k] for k in keys) == (0, False, False)
        assert env["prevent_self_review"] is False
    else:
        assert tuple(params[k] for k in keys) == (1, True, True)
        assert env["prevent_self_review"] is True

"""configure.py --scorecard-mode central: the App's id is pinned in the rulesets, the scorecard callers go, and nothing else moves.
Per-app (the default) records nothing and changes nothing."""
import hashlib
import json
import os
import re
from pathlib import Path

import pytest

from helpers import COMMITTED_SCORECARD_CALLER, unconfigure_agents, unconfigure_factory, unconfigure_hello_app, unconfigure_platform, unconfigure_portal
from test_configure import GOOD, configure, live_tree_is_pristine, workspace  # noqa: F401 - workspace is the fixture

ROOT = Path(__file__).resolve().parents[2]
CENTRAL = ["--scorecard-mode", "central", "--scorecard-app-id", "123456"]
RULESETS = ("bootstrap/hello-app/.github/rulesets/main.json", "bootstrap/golden-road-portal/.github/rulesets/main.json")
CALLERS = ("bootstrap/hello-app/.github/workflows/scorecard.yml", "bootstrap/golden-road-portal/.github/workflows/scorecard.yml",
           "bootstrap/golden-road-agents/skill/templates/workflows/scorecard.yml")
FACTORY_CALLER = "bootstrap/scorecard-factory/.github/workflows/scorecard.yml"


def snapshot(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}


def contexts(workspace, rel):
    rules = json.loads((workspace / rel).read_text())["rules"]
    checks = next(r for r in rules if r["type"] == "required_status_checks")["parameters"]["required_status_checks"]
    return {c["context"]: c["integration_id"] for c in checks}


def platform_of(workspace):
    return json.loads((workspace / "platform/platform.json").read_text())


def test_the_default_is_per_app_and_records_nothing(workspace):
    assert configure(workspace, *GOOD).returncode == 0
    platform = platform_of(workspace)
    assert "scorecard_mode" not in platform and "scorecard_app_id" not in platform
    for rel in RULESETS:
        assert set(contexts(workspace, rel).values()) == {15368}
    for rel in CALLERS:
        assert (workspace / rel).is_file()


def test_central_records_the_mode_and_app_id_pins_the_rulesets_and_removes_the_callers(workspace):
    out = configure(workspace, *GOOD, *CENTRAL)
    assert out.returncode == 0, out.stderr
    platform = platform_of(workspace)
    assert platform["scorecard_mode"] == "central" and platform["scorecard_app_id"] == 123456
    for rel in RULESETS:
        got = contexts(workspace, rel)
        assert got["scorecard / assessed"] == 123456
        assert {k: v for k, v in got.items() if k != "scorecard / assessed"} == {k: 15368 for k in got if k != "scorecard / assessed"} and len(got) == 5
    for rel in CALLERS:
        assert not (workspace / rel).exists(), rel
    assert (workspace / FACTORY_CALLER).is_file(), "the factory's own reusable workflow stays (C11)"
    assert "example-org" in (workspace / "bootstrap/scorecard-factory/.github/workflows/scorecard.yml").read_text()


def test_the_pin_changes_only_that_entry_and_keeps_the_file_s_formatting(workspace):
    before = {rel: (workspace / rel).read_text() for rel in RULESETS}
    assert configure(workspace, *GOOD, *CENTRAL).returncode == 0
    for rel in RULESETS:
        # whitespace-tolerant: the unconfigured fixture keeps the committed one-line entry, a configured tree's file is indented
        pin = re.compile(r'("context"\s*:\s*"scorecard / assessed"\s*,\s*"integration_id"\s*:\s*)15368')
        assert len(pin.findall(before[rel])) == 1
        assert (workspace / rel).read_text() == pin.sub(r"\g<1>123456", before[rel])


@pytest.mark.parametrize("extra", [["--scorecard-mode", "central"], ["--scorecard-app-id", "123456"], ["--scorecard-mode", "per-app", "--scorecard-app-id", "5"]])
def test_a_mode_without_an_id_or_an_id_without_central_changes_nothing(workspace, extra):
    before = snapshot(workspace)
    out = configure(workspace, *GOOD, *extra)
    assert out.returncode != 0 and snapshot(workspace) == before


@pytest.mark.parametrize("app_id", ["0", "-1", "abc", "2147483648", "", "1.5", "+7", " 7"])
def test_bad_app_ids_change_nothing(workspace, app_id):
    before = snapshot(workspace)
    out = configure(workspace, *GOOD, "--scorecard-mode", "central", f"--scorecard-app-id={app_id}")
    assert out.returncode != 0 and snapshot(workspace) == before


def test_per_app_to_central_on_a_configured_tree_is_allowed(workspace):
    assert configure(workspace, *GOOD).returncode == 0
    out = configure(workspace, *GOOD, *CENTRAL)
    assert out.returncode == 0, out.stderr
    assert platform_of(workspace)["scorecard_mode"] == "central" and contexts(workspace, RULESETS[0])["scorecard / assessed"] == 123456
    assert not (workspace / CALLERS[0]).exists()


def test_central_back_to_per_app_is_refused_with_the_restore_hint(workspace):
    assert configure(workspace, *GOOD, *CENTRAL).returncode == 0
    before = snapshot(workspace)
    out = configure(workspace, *GOOD, "--scorecard-mode", "per-app")
    assert out.returncode != 0 and "git checkout -- bootstrap platform" in out.stderr and snapshot(workspace) == before


def test_a_rerun_without_the_flag_keeps_central(workspace):
    assert configure(workspace, *GOOD, *CENTRAL).returncode == 0
    before = snapshot(workspace)
    out = configure(workspace, *GOOD)
    assert out.returncode == 0, out.stderr
    assert platform_of(workspace)["scorecard_mode"] == "central" and platform_of(workspace)["scorecard_app_id"] == 123456
    assert snapshot(workspace) == before and contexts(workspace, RULESETS[1])["scorecard / assessed"] == 123456


def test_the_app_id_can_change_in_central_mode(workspace):
    assert configure(workspace, *GOOD, *CENTRAL).returncode == 0
    out = configure(workspace, *GOOD, "--scorecard-mode", "central", "--scorecard-app-id", "777")
    assert out.returncode == 0, out.stderr
    assert platform_of(workspace)["scorecard_app_id"] == 777
    for rel in RULESETS:
        assert contexts(workspace, rel)["scorecard / assessed"] == 777


def test_central_with_single_approver_keeps_both_ruleset_changes(workspace):
    out = configure(workspace, *GOOD, "--single-approver", *CENTRAL)
    assert out.returncode == 0, out.stderr
    for rel in RULESETS:
        doc = json.loads((workspace / rel).read_text())
        pr = next(r for r in doc["rules"] if r["type"] == "pull_request")["parameters"]
        assert (pr["required_approving_review_count"], pr["require_code_owner_review"], pr["require_last_push_approval"]) == (0, False, False)
        assert contexts(workspace, rel)["scorecard / assessed"] == 123456


@pytest.mark.parametrize("edit", ["remove", "duplicate"])
def test_a_ruleset_without_exactly_one_scorecard_check_stops_central_before_anything_changes(workspace, edit):
    path = workspace / RULESETS[0]
    doc = json.loads(path.read_text())
    checks = next(r for r in doc["rules"] if r["type"] == "required_status_checks")["parameters"]["required_status_checks"]
    entry = next(c for c in checks if c["context"] == "scorecard / assessed")
    if edit == "remove":
        checks.remove(entry)
    else:
        checks.append(dict(entry))
    path.write_text(json.dumps(doc, indent=2) + "\n")  # format-agnostic: a configured tree's rulesets are indented, the source's are one-line
    before = snapshot(workspace)
    out = configure(workspace, *GOOD, *CENTRAL)
    assert out.returncode != 0 and "scorecard / assessed" in out.stderr and snapshot(workspace) == before


def test_the_helpers_restore_a_central_tree_to_the_committed_state(workspace):
    assert configure(workspace, *GOOD, *CENTRAL).returncode == 0
    live = platform_of(workspace)
    unconfigure_platform(workspace / "platform")
    unconfigure_hello_app(workspace / "bootstrap/hello-app")
    unconfigure_portal(workspace / "bootstrap/golden-road-portal")
    unconfigure_factory(workspace / "bootstrap/scorecard-factory", "example-org")
    unconfigure_agents(workspace / "bootstrap/golden-road-agents", "example-org", live.get("github_host"), live.get("actions_oidc_issuer"))
    platform = platform_of(workspace)
    assert "scorecard_mode" not in platform and "scorecard_app_id" not in platform
    for rel in RULESETS:
        assert set(contexts(workspace, rel).values()) == {15368}
    for rel in CALLERS:
        assert (workspace / rel).read_text() == COMMITTED_SCORECARD_CALLER, rel
    if live_tree_is_pristine():
        for rel in (*CALLERS, *RULESETS, FACTORY_CALLER):
            assert (workspace / rel).read_bytes() == (ROOT / rel).read_bytes(), rel


def test_the_committed_scorecard_caller_text_in_helpers_is_the_committed_file():
    if not live_tree_is_pristine():
        pytest.skip("a configured tree holds the org, not the placeholder")
    for rel in CALLERS:
        assert (ROOT / rel).read_text() == COMMITTED_SCORECARD_CALLER, rel


def test_central_mode_is_off_in_the_committed_tree():
    if not live_tree_is_pristine():
        pytest.skip("a configured tree may have been switched to central on purpose")
    platform = json.loads((ROOT / "platform/platform.json").read_text())
    assert "scorecard_mode" not in platform and "scorecard_app_id" not in platform
    for rel in RULESETS:
        assert os.path.exists(ROOT / rel)
        assert set(contexts(ROOT, rel).values()) == {15368}

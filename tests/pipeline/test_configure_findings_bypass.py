"""configure.py --findings-bypass-team: on an org, the platform team overrides the findings block, not the app's repo admins."""
import json

import pytest

from test_configure import GOOD, configure, workspace  # noqa: F401 - workspace is the fixture
from test_configure_scorecard_mode import CENTRAL, FINDINGS, contexts, platform_of, snapshot


def actors(workspace, rel):
    return json.loads((workspace / rel).read_text())["bypass_actors"]


def test_the_default_keeps_the_repo_admin(workspace):
    assert configure(workspace, *GOOD).returncode == 0
    for rel in FINDINGS:
        assert actors(workspace, rel) == [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}]
    assert "findings_bypass_team" not in platform_of(workspace)


def test_a_team_replaces_the_actor_and_is_recorded(workspace):
    out = configure(workspace, *GOOD, "--findings-bypass-team", "platform-admins")
    assert out.returncode == 0, out.stderr
    for rel in FINDINGS:
        assert actors(workspace, rel) == [{"actor_id": 0, "actor_type": "Team", "bypass_mode": "pull_request"}]
        assert contexts(workspace, rel) == {"scorecard / findings": 15368}
    assert platform_of(workspace)["findings_bypass_team"] == "platform-admins"


def test_main_keeps_no_bypass_actor_with_a_team(workspace):
    assert configure(workspace, *GOOD, "--findings-bypass-team", "platform-admins").returncode == 0
    for app in ("hello-app", "golden-road-portal"):
        assert not json.loads((workspace / "bootstrap" / app / ".github/rulesets/main.json").read_text()).get("bypass_actors"), app


@pytest.mark.parametrize("slug", ["Platform", "a b", "-x", "x" * 101, "a`b", "", "replace-me", "team-replace-me"])
def test_a_bad_slug_changes_nothing(workspace, slug):
    before = snapshot(workspace)
    assert configure(workspace, *GOOD, "--findings-bypass-team", slug).returncode != 0 and snapshot(workspace) == before


def test_single_approver_refuses_a_team(workspace):
    before = snapshot(workspace)
    out = configure(workspace, *GOOD, "--single-approver", "--findings-bypass-team", "platform-admins")
    assert out.returncode != 0 and "--single-approver" in out.stderr and snapshot(workspace) == before


@pytest.mark.parametrize("edit,flags", [
    ({"findings_bypass_team": "platform-admins"}, ["--single-approver"]),
    ({"gate2_single_approver": True}, ["--findings-bypass-team", "platform-admins"]),
], ids=["recorded-team-then-single-approver", "recorded-single-approver-then-team"])
def test_a_team_and_single_approver_are_refused_together_even_when_one_is_recorded(workspace, edit, flags):
    path = workspace / "platform/platform.json"
    path.write_text(json.dumps({**platform_of(workspace), **edit}, indent=2) + "\n")
    before = snapshot(workspace)
    out = configure(workspace, *GOOD, *flags)
    assert out.returncode != 0 and "--single-approver" in out.stderr and snapshot(workspace) == before


def test_another_team_on_a_configured_tree_is_refused_with_the_restore_hint(workspace):
    assert configure(workspace, *GOOD, "--findings-bypass-team", "platform-admins").returncode == 0
    before = snapshot(workspace)
    out = configure(workspace, *GOOD, "--findings-bypass-team", "other")
    assert out.returncode != 0 and "git checkout -- bootstrap platform" in out.stderr and snapshot(workspace) == before
    assert configure(workspace, *GOOD).returncode == 0 and platform_of(workspace)["findings_bypass_team"] == "platform-admins"
    assert snapshot(workspace) == before, "a rerun keeps the team and changes nothing"


def test_team_and_central_together(workspace):
    assert configure(workspace, *GOOD, *CENTRAL, "--findings-bypass-team", "platform-admins").returncode == 0
    for rel in FINDINGS:
        assert contexts(workspace, rel) == {"scorecard / findings": 123456} and actors(workspace, rel)[0]["actor_type"] == "Team"


def test_a_team_then_central_keeps_both(workspace):
    assert configure(workspace, *GOOD, "--findings-bypass-team", "platform-admins").returncode == 0
    assert configure(workspace, *GOOD, *CENTRAL).returncode == 0
    for rel in FINDINGS:
        assert contexts(workspace, rel) == {"scorecard / findings": 123456} and actors(workspace, rel)[0]["actor_type"] == "Team"

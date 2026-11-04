"""The portal's writer deletes a decommissioned app's adapters by the names golden-road compose gives them (spec 2026-11-02 L6)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bootstrap/golden-road-portal"))

from golden_road.deploy import adapter_name as deploy_name  # noqa: E402
from golden_road.deploy import adapter_name_or_none  # noqa: E402
from portal.writer.allowlist import adapter_name as writer_name  # noqa: E402


@pytest.mark.parametrize("owner, repo, env", [("acme", "notes", "test"), ("Acme", "My_App.v2", "prod"), ("o", "x" * 70, "prod"),
                                              ("acme", "a" * 40 + "b" * 20, "test"), ("mwdavisii", "hello-app", "prod")])
def test_the_writer_names_adapters_as_deploy_does(owner, repo, env):
    assert writer_name(owner, repo, env) == deploy_name(owner, repo, env)


@pytest.mark.parametrize("owner, repo, env", [("-acme", "notes", "test"), ("_acme", "notes", "prod"), (".acme", "notes", "test")])
def test_a_name_deploy_refuses_is_none_for_the_writer(owner, repo, env):
    assert writer_name(owner, repo, env) is None and adapter_name_or_none(owner, repo, env) is None


@pytest.mark.parametrize("owner, repo, env", [("acme", "notes", "test"), ("Acme", "My_App.v2", "prod")])
def test_the_writer_takes_compose_s_adapters_for_its_own_and_another_app_s_for_not(owner, repo, env):
    """The writer deletes an adapter by the app's name only if it reconciles ./apps/<owner>/<repo>/<env> (adapter_not_ours otherwise)."""
    from golden_road.deploy import adapters
    from portal.writer.lifecycle import _adapter_is_for
    d = f"apps/{owner}/{repo}/{env}"
    for path, text in adapters(owner, repo, env, "ns-x", repo_url="https://github.com/acme/golden-road-deploy").items():
        adapter_dir = path.rsplit("/", 1)[0]
        assert _adapter_is_for(text.encode(), adapter_dir, d)
        assert not _adapter_is_for(text.encode(), adapter_dir, f"apps/{owner}-x/{repo}/{env}")

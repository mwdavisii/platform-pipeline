import json
from pathlib import Path

import pytest

from builder.config import gate2_single_approver, is_model_provider, load_base_images, load_platform, mcp_gateways, platform_sha256, skills_repo


def test_platform_config_has_required_keys():
    platform = load_platform()
    for key in ("model_gateway_hosts", "model_provider_hosts", "denied_egress_hosts", "edge_ingress", "dns", "identity_header", "resources_tiers"):
        assert key in platform


def test_base_images_are_digest_pinned():
    for app_type, image in load_base_images().items():
        assert "@sha256:" in image["build"], app_type
        assert "@sha256:" in image["runtime"], app_type
        assert image["uid"] != 0


def test_model_provider_detection():
    platform = load_platform()
    assert is_model_provider("api.openai.com", platform)
    assert is_model_provider("myorg.openai.azure.com", platform)
    assert is_model_provider("bedrock-runtime.us-east-1.amazonaws.com", platform)
    assert not is_model_provider("litellm.internal", platform)
    assert not is_model_provider("api.github.com", platform)


def test_platform_override_changes_hash(tmp_path, monkeypatch):
    default_hash = platform_sha256()
    override = load_platform()
    override["identity_header"] = "X-Other"
    path = tmp_path / "platform.json"
    path.write_text(json.dumps(override), encoding="utf-8")
    monkeypatch.setenv("BUILDER_PLATFORM_CONFIG", str(path))
    assert load_platform()["identity_header"] == "X-Other"
    assert platform_sha256() != default_hash


def test_denied_egress_hosts_are_globbed():
    from builder.config import is_denied_egress

    platform = load_platform()
    assert is_denied_egress("metadata.google.internal", platform)
    assert is_denied_egress("instance-data.us-west-2.compute.internal", platform)
    assert is_denied_egress("metadata.eu-west-1.internal", platform)
    assert not is_denied_egress("litellm.internal", platform)


def test_more_model_providers_detected():
    platform = load_platform()
    for host in (
        "bedrock.us-east-1.amazonaws.com",
        "bedrock-agent-runtime.us-east-1.amazonaws.com",
        "openrouter.ai",
        "api.perplexity.ai",
        "api.fireworks.ai",
        "api-inference.huggingface.co",
        "router.huggingface.co",
        "abc.endpoints.huggingface.cloud",
    ):
        assert is_model_provider(host, platform), host


def test_in_cluster_egress_defaults_to_empty():
    assert load_platform()["in_cluster_egress"] == {}


def test_gate2_single_approver_is_false_by_default_and_in_the_platform_file():
    assert gate2_single_approver({}) is False
    assert gate2_single_approver({"gate2_single_approver": True}) is True
    assert "gate2_single_approver" not in load_platform()
    root = json.loads((Path(__file__).resolve().parents[2] / "platform/platform.json").read_text())
    assert type(root["gate2_single_approver"]) is bool  # False when committed; configure.py --single-approver sets True


@pytest.mark.parametrize("value", [1, 0, "true", None, [], {}])
def test_gate2_single_approver_must_be_a_strict_boolean(value):
    with pytest.raises(ValueError):
        gate2_single_approver({"gate2_single_approver": value})


# ---- central review: the scorecard mode ----

def test_scorecard_mode_defaults_to_per_app():
    from builder.config import scorecard_mode
    assert scorecard_mode({}) == "per-app"
    assert scorecard_mode({"scorecard_mode": "central"}) == "central"
    root = json.loads((Path(__file__).resolve().parents[2] / "platform/platform.json").read_text())
    assert "scorecard_mode" not in load_platform()  # the builder's packaged copy is the committed per-app one in every tree
    if scorecard_mode(root) == "central":  # a tree configured with --scorecard-mode central records the mode and the App's id
        assert root["scorecard_mode"] == "central" and isinstance(root["scorecard_app_id"], int)
    else:
        assert "scorecard_mode" not in root or root["scorecard_mode"] == "per-app"
        assert "scorecard_app_id" not in root  # per-app when committed; configure.py --scorecard-mode central records it


def test_central_mode_needs_a_positive_app_id():
    from builder.config import scorecard_app_id
    assert scorecard_app_id({"scorecard_mode": "central", "scorecard_app_id": 5}) == 5
    for bad in ({"scorecard_app_id": 0}, {"scorecard_app_id": True}, {"scorecard_app_id": "5"}, {"scorecard_app_id": -1}, {"scorecard_app_id": 2**31}, {}):
        with pytest.raises(ValueError):
            scorecard_app_id({"scorecard_mode": "central", **bad})


def test_per_app_mode_has_no_app_id():
    from builder.config import scorecard_app_id
    assert scorecard_app_id({}) is None
    assert scorecard_app_id({"scorecard_mode": "per-app", "scorecard_app_id": 5}) is None


@pytest.mark.parametrize("value", ["", "Central", "both", None, True, 1])
def test_an_unknown_mode_is_refused(value):
    from builder.config import scorecard_mode
    with pytest.raises(ValueError):
        scorecard_mode({"scorecard_mode": value})


def test_the_committed_platform_configs_carry_the_policy_placeholder():
    """The builder's own copy is always the placeholder; platform/platform.json is too until configure.py has run (org recorded),
    and after that it holds a valid recorded link."""
    from builder.config import classification_policy_url
    from contracts.classification import POLICY_PLACEHOLDER
    root = Path(__file__).resolve().parents[2]
    assert json.loads((root / "src/builder/data/platform.json").read_text())["classification_policy_url"] == POLICY_PLACEHOLDER
    live = json.loads((root / "platform/platform.json").read_text())
    if live.get("org") is None:
        assert live["classification_policy_url"] == POLICY_PLACEHOLDER
    else:
        assert classification_policy_url(live) == live["classification_policy_url"]


def test_the_policy_url_is_the_recorded_link_or_the_placeholder_and_a_bad_one_is_an_error():
    from builder.config import classification_policy_url
    from contracts.classification import POLICY_PLACEHOLDER
    assert classification_policy_url({"classification_policy_url": "https://wiki.example.org/p?x=1"}) == "https://wiki.example.org/p?x=1"
    assert classification_policy_url({}) == POLICY_PLACEHOLDER
    for bad in ("http://x.example.org", 7, "https://wiki.example.org/a b", "https://wiki.example.org/\"x"):
        with pytest.raises(ValueError, match="classification_policy_url"):
            classification_policy_url({"classification_policy_url": bad})


def test_the_platform_names_two_gateways_and_the_catalog():
    assert mcp_gateways({"mcp_gateways": {"test": "mcp-test.example.com", "prod": "mcp.example.com"}}) == {
        "test": "mcp-test.example.com", "prod": "mcp.example.com"}
    assert mcp_gateways({}) == {}
    assert skills_repo({"skills_repo": "acme/golden-road-skills"}) == "acme/golden-road-skills" and skills_repo({}) is None
    assert mcp_gateways(load_platform()) == {"test": "mcp-test.example.com", "prod": "mcp.example.com"}


@pytest.mark.parametrize("bad", [{"test": "x"}, {"test": "mcp.example.com", "dev": "y.example.com"}, ["mcp.example.com"], {"test": 1},
                                 {"test": "mcp.example.com\n"}, {"test": "mcp.example.com", "prod": "mcp.example.com"}])
def test_a_bad_gateway_map_is_refused(bad):
    with pytest.raises(ValueError, match="mcp_gateways"):
        mcp_gateways({"mcp_gateways": bad})


@pytest.mark.parametrize("bad", ["acme", "acme/x/y", 1, "acme/skills\n", ""])
def test_a_bad_skills_repo_is_refused(bad):
    with pytest.raises(ValueError, match="skills_repo"):
        skills_repo({"skills_repo": bad})

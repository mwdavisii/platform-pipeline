"""An agent app's environment (spec 2026-11-17 A5, decided while planning items 10 and 11): the gateway key from its fixed Secret Server
path, in every environment, with or without a binding; nothing changes for any other app."""

import hashlib

import pytest
import yaml

from golden_road.bindings import agent_key_name, agent_key_path
from golden_road.compose import ComposeError, compose
from golden_road.render import render

IMAGE = "ghcr.io/example-org/agent-calendar@sha256:" + "b" * 64
KEY = {"secretKey": "AGENT_GATEWAY_KEY", "remoteRef": {"key": "/golden-road/agent-gateway-keys/agent-example-org--agent-calendar-test",
                                                         "property": "key"}}
MCP = {"gateway": "mcp-test.example.com", "servers": [{"server_id": "notes", "name": "Notes", "verified_by": "mock-cloudflare"}]}
BINDING = {"schema": "binding/v2", "repo": "example-org/agent-calendar", "environment": "test", "linked_by": "p_" + "a" * 32,
           "linked_at": "2026-11-17T15:00:00Z", "request": "r_" + "b" * 32, "mcp": MCP}


def agent_manifests(app, image=IMAGE):
    """The golden app's manifests with AGENT_GATEWAY_URL in its env, as the builder renders an agent's."""
    objects = [o for o in yaml.safe_load_all(render(app, image, "notes-api")) if o]
    objects[1]["spec"]["template"]["spec"]["containers"][0]["env"].append({"name": "AGENT_GATEWAY_URL", "value": "https://llm.example.org/v1"})
    return "---\n".join(yaml.safe_dump(o, sort_keys=False) for o in objects)


@pytest.fixture
def signed(app):
    return agent_manifests(app)


def docs(text):
    return [o for o in yaml.safe_load_all(text) if o]


def env_of(objects):
    return objects[2 if objects[0]["kind"] == "Namespace" else 1]["spec"]["template"]["spec"]["containers"][0]["env"]


def test_the_key_s_name_and_path_follow_the_repo_and_environment():
    assert agent_key_name("Example-Org/Agent-Calendar", "prod") == "agent-example-org--agent-calendar-prod"
    assert agent_key_path("example-org/agent-calendar", "test") == KEY["remoteRef"]["key"]
    with pytest.raises(ValueError, match="no gateway key name"):
        agent_key_name("-bad/repo", "test")


def test_an_agent_without_a_binding_gets_the_key_alone(signed):
    out = docs(compose(signed, IMAGE, None, repo="example-org/agent-calendar", environment="test"))
    assert [o["kind"] for o in out] == ["ServiceAccount", "Deployment", "Service", "NetworkPolicy", "CiliumNetworkPolicy", "ExternalSecret"]
    assert env_of(out)[-1] == {"name": "AGENT_GATEWAY_KEY", "valueFrom": {"secretKeyRef": {"name": "notes-api-delinea", "key": "AGENT_GATEWAY_KEY"}}}
    assert out[-1]["metadata"]["name"] == "notes-api-delinea" and out[-1]["spec"]["data"] == [KEY]


def test_the_key_comes_after_the_mcp_token(signed):
    out = docs(compose(signed, IMAGE, BINDING, repo="example-org/agent-calendar", environment="test"))
    data = out[-1]["spec"]["data"]
    assert [d["secretKey"] for d in data] == ["CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET", "AGENT_GATEWAY_KEY"]
    assert [e["name"] for e in env_of(out)][-3:] == ["CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET", "AGENT_GATEWAY_KEY"]


@pytest.mark.parametrize("kw", [{}, {"repo": "example-org/agent-calendar"}, {"environment": "test"},
                                {"repo": "example-org/agent-calendar", "environment": "staging"}],
                         ids=["neither", "no-environment", "no-repo", "unknown-environment"])
def test_an_agent_s_manifests_need_the_repo_and_environment(signed, kw):
    with pytest.raises(ComposeError, match="needs its repo and environment"):
        compose(signed, IMAGE, None, **kw)


def test_an_agent_repo_with_no_gateway_key_name_is_refused(signed):
    with pytest.raises(ComposeError, match="no gateway key name"):
        compose(signed, IMAGE, None, repo="-bad/agent-calendar", environment="test")


def test_a_secret_can_t_take_an_agent_name(signed):
    binding = {**BINDING, "secrets": [{"env": "AGENT_GATEWAY_KEY", "secret_id": 1042, "field_slug": "password",
                                       "secret_name": "x", "folder_id": 17, "verified_by": "mock-secret-server"}]}
    binding.pop("mcp")
    with pytest.raises(ComposeError, match="AGENT_"):
        compose(signed, IMAGE, binding, repo="example-org/agent-calendar", environment="test")


SECRET = {"env": "NOTES_DB_PASSWORD", "secret_id": 1042, "secret_name": "notes-db", "folder_id": 17, "field_slug": "password",
          "verified_by": "mock-secret-server"}
# sha256 of what compose wrote at dd60c86 (before compose knew agents) for the golden app rendered with IMAGE, namespace
# agent-calendar-test: the manifests themselves, then no binding, then BINDING with SECRET added (secrets and MCP servers).
BEFORE_AGENTS = {"manifests": "1ec58b3d0ed438109c767afd1f7b80a48d36d682e748e5776dc08e69a57c9c9e",
                 "none": "b4de675647aec3ce9d4ea298d1046dbce531e63d837d5b5ed0c51f0b65a2b18c",
                 "secrets_and_mcp": "653def278afe9143d59ceda378204e5d206df07cf418eff3eeb72123416d1d76"}


@pytest.mark.parametrize("kw", [{}, {"repo": "example-org/agent-calendar", "environment": "test"}], ids=["no-kwargs", "kwargs"])
def test_any_other_app_composes_exactly_as_before(app, kw):
    """Byte for byte what compose wrote before it knew agents, with or without the repo and environment."""
    plain = render(app, IMAGE, "notes-api")
    assert hashlib.sha256(plain.encode()).hexdigest() == BEFORE_AGENTS["manifests"], "the golden app's render changed: re-pin"
    for name, binding in (("none", None), ("secrets_and_mcp", {**BINDING, "secrets": [SECRET]})):
        out = compose(plain, IMAGE, binding, namespace="agent-calendar-test", **kw)
        assert hashlib.sha256(out.encode()).hexdigest() == BEFORE_AGENTS[name], name


def test_the_key_s_name_needs_a_platform_environment():
    with pytest.raises(ValueError, match="no gateway key name"):
        agent_key_name("example-org/agent-calendar", "staging")


IDENTITY = {"client_id": "11111111-2222-3333-4444-555555555555", "tenant_id": "66666666-7777-8888-9999-000000000000",
            "directory_id": "sp-agent-calendar", "verified_by": "mock-directory"}
GRAPH_RULE = {"toFQDNs": [{"matchName": "graph.microsoft.com"}], "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}]}


def _egress(out):
    return next(o for o in out if o["kind"] == "CiliumNetworkPolicy")["spec"]["egress"]


def test_an_agent_with_an_identity_may_reach_graph(signed):
    """graph_read (spec 2026-11-17 A6; the final review's I1): an agent with an Azure identity bound gets graph.microsoft.com on 443,
    after the identity's sign-in host."""
    out = docs(compose(signed, IMAGE, {**BINDING, "azure_identity": IDENTITY}, repo="example-org/agent-calendar", environment="test"))
    hosts = [r["toFQDNs"][0]["matchName"] for r in _egress(out) if "toFQDNs" in r and r["toFQDNs"] and "matchName" in r["toFQDNs"][0]]
    assert GRAPH_RULE in _egress(out) and hosts.index("login.microsoftonline.com") < hosts.index("graph.microsoft.com")
    assert _egress(out).count(GRAPH_RULE) == 1


def test_graph_egress_is_an_agent_s_with_an_identity_only(app, signed):
    agent_alone = docs(compose(signed, IMAGE, BINDING, repo="example-org/agent-calendar", environment="test"))
    assert GRAPH_RULE not in _egress(agent_alone), "no identity, no graph_read"
    plain = render(app, IMAGE, "notes-api")
    other = docs(compose(plain, IMAGE, {**BINDING, "azure_identity": IDENTITY}, repo="example-org/agent-calendar", environment="test"))
    assert GRAPH_RULE not in _egress(other), "any other app's egress is as before"

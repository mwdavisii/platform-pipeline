"""The runtime's settings (spec 2026-11-17 "The runtime", Start): what each binding sets, and a missing or bad one named, never its value."""

import pytest
from runtime_fakes import BASE_ENV, CLIENT, TENANT

from agent_runtime.settings import SettingError, from_env

MCP = {"MCP_URL": "https://mcp-test.example.com/mcp", "MCP_SERVERS": "docs,notes", "CF_ACCESS_CLIENT_ID": "id.access",
       "CF_ACCESS_CLIENT_SECRET": "secret-SENTINEL"}
AZURE = {"AZURE_CLIENT_ID": CLIENT, "AZURE_TENANT_ID": TENANT, "AZURE_FEDERATED_TOKEN_FILE": "/var/run/secrets/azure/tokens/azure-identity-token"}
DATABRICKS = {"DATABRICKS_HOST": "adb-1111111111111111.1.azuredatabricks.net", "DATABRICKS_HTTP_PATH": "/sql/1.0/warehouses/0000notes0000",
              "DATABRICKS_CATALOG": "notes", "DATABRICKS_SCHEMA": "public"}


def test_the_gateway_alone_gives_no_tools():
    settings = from_env(BASE_ENV)
    assert (settings.gateway_url, settings.gateway_key) == ("https://llm.example.org/v1", "sk-gateway-SENTINEL")
    assert (settings.mcp, settings.azure, settings.databricks, settings.skills_dir) == (None, None, None, None)


def test_every_binding_is_read():
    settings = from_env({**BASE_ENV, **MCP, **AZURE, **DATABRICKS, "SKILLS_DIR": "/skills", "AZURE_AUTHORITY_HOST": "https://login.microsoftonline.us"})
    assert settings.mcp.url == MCP["MCP_URL"] and settings.mcp.servers == ("docs", "notes")
    assert settings.azure.authority == "https://login.microsoftonline.us/" and settings.azure.tenant_id == TENANT
    assert (settings.databricks.warehouse_id, settings.databricks.catalog, settings.databricks.schema) == ("0000notes0000", "notes", "public")
    assert settings.skills_dir == "/skills"
    assert "SENTINEL" not in repr(settings)


def test_the_authority_defaults_to_entra():
    assert from_env({**BASE_ENV, **AZURE}).azure.authority == "https://login.microsoftonline.com/"


@pytest.mark.parametrize("env, name", [
    ({"AGENT_GATEWAY_KEY": "k"}, "AGENT_GATEWAY_URL is not set"),
    ({"AGENT_GATEWAY_URL": "https://llm.example.org/v1"}, "AGENT_GATEWAY_KEY is not set"),
    ({**BASE_ENV, "AGENT_GATEWAY_URL": "ftp://llm.example.org"}, "AGENT_GATEWAY_URL is not an http(s) URL"),
    ({**BASE_ENV, "AGENT_GATEWAY_URL": "https://user@llm.example.org/v1"}, "AGENT_GATEWAY_URL is not an http(s) URL"),
    ({**BASE_ENV, "AGENT_GATEWAY_URL": "https://llm.example.org/v1?x=1"}, "AGENT_GATEWAY_URL is not an http(s) URL"),
    ({**BASE_ENV, "AGENT_GATEWAY_KEY": "two words"}, "AGENT_GATEWAY_KEY has spaces or line breaks"),
    ({**BASE_ENV, "AGENT_GATEWAY_KEY": "key-SENTINEL\n"}, "AGENT_GATEWAY_KEY has spaces or line breaks"),
    ({**BASE_ENV, **{k: v for k, v in MCP.items() if k != "CF_ACCESS_CLIENT_SECRET"}}, "CF_ACCESS_CLIENT_SECRET is not set"),
    ({**BASE_ENV, **MCP, "MCP_URL": "http://mcp-test.example.com/mcp"}, "MCP_URL is not an https URL"),
    ({**BASE_ENV, **MCP, "MCP_SERVERS": ""}, "MCP_SERVERS is not set"),
    ({**BASE_ENV, **AZURE, "AZURE_TENANT_ID": "not-a-guid"}, "AZURE_TENANT_ID is not a GUID"),
    ({**BASE_ENV, **{k: v for k, v in AZURE.items() if k != "AZURE_FEDERATED_TOKEN_FILE"}}, "AZURE_FEDERATED_TOKEN_FILE is not set"),
    ({**BASE_ENV, **AZURE, "AZURE_AUTHORITY_HOST": "http://login.example.test"}, "AZURE_AUTHORITY_HOST is not an https URL"),
    ({**BASE_ENV, **AZURE, "AZURE_AUTHORITY_HOST": "https://login.example.test"}, "AZURE_AUTHORITY_HOST is not an Entra sign-in host"),
    ({**BASE_ENV, **AZURE, "AZURE_AUTHORITY_HOST": "https://login.microsoftonline.com.example.test"},
     "AZURE_AUTHORITY_HOST is not an Entra sign-in host"),
    ({**BASE_ENV, **AZURE, "AZURE_AUTHORITY_HOST": "https://login.microsoftonline.com:8443"}, "AZURE_AUTHORITY_HOST is not an Entra sign-in host"),
    ({**BASE_ENV, **AZURE, "AZURE_AUTHORITY_HOST": "https://login.microsoftonline.com/x"}, "AZURE_AUTHORITY_HOST is not an Entra sign-in host"),
    ({**BASE_ENV, **MCP, "CF_ACCESS_CLIENT_ID": "id SENTINEL"}, "CF_ACCESS_CLIENT_ID has spaces or line breaks"),
    ({**BASE_ENV, **MCP, "CF_ACCESS_CLIENT_SECRET": "secret-SENTINEL\n"}, "CF_ACCESS_CLIENT_SECRET has spaces or line breaks"),
    ({**BASE_ENV, **MCP, "CF_ACCESS_CLIENT_SECRET": "secret-SENTINEL\u00e9"}, "CF_ACCESS_CLIENT_SECRET is not ASCII"),
    ({**BASE_ENV, "AGENT_GATEWAY_KEY": "sk-SENTINEL\u00e9"}, "AGENT_GATEWAY_KEY is not ASCII"),
    ({**BASE_ENV, **AZURE, "AZURE_CLIENT_ID": "not-a-guid"}, "AZURE_CLIENT_ID is not a GUID"),
    ({**BASE_ENV, **DATABRICKS}, "DATABRICKS_HOST is set without AZURE_CLIENT_ID"),
    ({**BASE_ENV, **AZURE, **DATABRICKS, "DATABRICKS_HOST": "https://adb.example.test"}, "DATABRICKS_HOST is not a host name"),
    ({**BASE_ENV, **AZURE, **DATABRICKS, "DATABRICKS_HTTP_PATH": "/other"}, "DATABRICKS_HTTP_PATH is not /sql/1.0/warehouses/<id>"),
])
def test_a_missing_or_bad_setting_is_named_and_its_value_never_shown(env, name):
    with pytest.raises(SettingError) as caught:
        from_env(env)
    assert str(caught.value).startswith(name)
    assert "SENTINEL" not in str(caught.value) and "not-a-guid" not in str(caught.value)


def test_http_is_accepted_for_the_gateway():
    assert from_env({**BASE_ENV, "AGENT_GATEWAY_URL": "http://mock:9090/v1/"}).gateway_url == "http://mock:9090/v1"


@pytest.mark.parametrize("host", ["https://login.microsoftonline.com", "https://login.microsoftonline.us/", "https://login.chinacloudapi.cn",
                                  "https://login.partner.microsoftonline.cn"])
def test_each_entra_cloud_s_sign_in_host_is_accepted(host):
    assert from_env({**BASE_ENV, **AZURE, "AZURE_AUTHORITY_HOST": host}).azure.authority == host.rstrip("/") + "/"

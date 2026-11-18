"""The runtime's settings, from the environment the deploy composes (spec 2026-11-17 "The runtime"): the gateway (always: the builder
writes AGENT_GATEWAY_URL into the signed manifests and compose adds AGENT_GATEWAY_KEY), and whatever the environment's binding sets
(MCP servers, the Azure identity, Databricks, skills). A missing or bad setting is a SettingError naming it, never its value."""

import dataclasses as dc
import re

URL = re.compile(r"(https?)://([A-Za-z0-9.-]+)(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?")
HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")
GUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
WAREHOUSE = re.compile(r"/sql/1\.0/warehouses/([A-Za-z0-9]+)")
ENTRA = "https://login.microsoftonline.com/"
# Entra's sign-in hosts (public, US Government, China, and the China partner cloud): the federated token goes nowhere else.
AUTHORITIES = frozenset({"https://login.microsoftonline.com", "https://login.microsoftonline.us", "https://login.chinacloudapi.cn",
                         "https://login.partner.microsoftonline.cn"})


class SettingError(Exception):
    """A setting that is missing or bad: the message names the setting, never its value."""


@dc.dataclass(frozen=True)
class Mcp:
    url: str
    servers: tuple
    client_id: str = dc.field(repr=False)
    client_secret: str = dc.field(repr=False)


@dc.dataclass(frozen=True)
class Azure:
    client_id: str
    tenant_id: str
    token_file: str
    authority: str  # ends in "/"


@dc.dataclass(frozen=True)
class Databricks:
    host: str
    warehouse_id: str
    catalog: str | None
    schema: str | None


@dc.dataclass(frozen=True)
class Settings:
    gateway_url: str
    gateway_key: str = dc.field(repr=False)
    mcp: Mcp | None = None
    azure: Azure | None = None
    databricks: Databricks | None = None
    skills_dir: str | None = None


def _need(env, name: str) -> str:
    value = env.get(name, "")
    if not value:
        raise SettingError(f"{name} is not set")
    return value


def _credential(env, name: str) -> str:
    """A key or token: set, ASCII, one word."""
    value = _need(env, name)
    if any(c.isspace() for c in value):
        raise SettingError(f"{name} has spaces or line breaks")
    if not value.isascii():
        raise SettingError(f"{name} is not ASCII")
    return value


def from_env(env) -> Settings:
    url = _need(env, "AGENT_GATEWAY_URL").rstrip("/")
    if not URL.fullmatch(url):
        raise SettingError("AGENT_GATEWAY_URL is not an http(s) URL")
    key = _credential(env, "AGENT_GATEWAY_KEY")
    mcp = None
    if env.get("MCP_URL"):
        if not env["MCP_URL"].startswith("https://") or not URL.fullmatch(env["MCP_URL"]):
            raise SettingError("MCP_URL is not an https URL")
        servers = tuple(s for s in env.get("MCP_SERVERS", "").split(",") if s)
        if not servers:
            raise SettingError("MCP_SERVERS is not set")
        mcp = Mcp(env["MCP_URL"], servers, _credential(env, "CF_ACCESS_CLIENT_ID"),
                  _credential(env, "CF_ACCESS_CLIENT_SECRET"))
    azure = None
    if env.get("AZURE_CLIENT_ID"):
        client_id, tenant = env["AZURE_CLIENT_ID"], _need(env, "AZURE_TENANT_ID")
        if not GUID.fullmatch(client_id):
            raise SettingError("AZURE_CLIENT_ID is not a GUID")
        if not GUID.fullmatch(tenant):
            raise SettingError("AZURE_TENANT_ID is not a GUID")
        authority = (env.get("AZURE_AUTHORITY_HOST") or ENTRA).rstrip("/")
        if not authority.startswith("https://") or not URL.fullmatch(authority):
            raise SettingError("AZURE_AUTHORITY_HOST is not an https URL")
        if authority not in AUTHORITIES:
            raise SettingError("AZURE_AUTHORITY_HOST is not an Entra sign-in host")
        azure = Azure(client_id, tenant, _need(env, "AZURE_FEDERATED_TOKEN_FILE"), authority + "/")
    databricks = None
    if env.get("DATABRICKS_HOST"):
        if azure is None:
            raise SettingError("DATABRICKS_HOST is set without AZURE_CLIENT_ID (the connection signs in as the app's identity)")
        if not HOST.fullmatch(env["DATABRICKS_HOST"]):
            raise SettingError("DATABRICKS_HOST is not a host name")
        found = WAREHOUSE.fullmatch(_need(env, "DATABRICKS_HTTP_PATH"))
        if not found:
            raise SettingError("DATABRICKS_HTTP_PATH is not /sql/1.0/warehouses/<id>")
        databricks = Databricks(env["DATABRICKS_HOST"], found.group(1), env.get("DATABRICKS_CATALOG") or None,
                                env.get("DATABRICKS_SCHEMA") or None)
    return Settings(url, key, mcp, azure, databricks, env.get("SKILLS_DIR") or None)

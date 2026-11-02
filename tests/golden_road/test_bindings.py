import pytest
from golden_road.bindings import AZURE_LOGIN, SKILLS_DIR, effects, mcp_secret_path, mcp_token_name

AZ = {"directory_id": "sp-notes", "tenant_id": "00000000-0000-4000-8000-00000000a2e0",
      "client_id": "11111111-1111-4111-8111-111111111111", "verified_by": "mock-directory"}
DBX = {"directory_id": "wh-notes", "host": "adb-1111111111111111.1.azuredatabricks.net", "http_path": "/sql/1.0/warehouses/0000notes0000",
       "catalog": "notes", "schema": "public", "verified_by": "mock-directory"}


def test_no_binding_adds_nothing():
    none = {"client_id": None, "env": [], "egress": [], "secrets": [], "mcp": None, "skills": []}
    assert effects(None) == none
    assert effects({"schema": "binding/v1", "repo": "acme/notes"}) == none


def test_identity_only():
    assert effects({"azure_identity": AZ}) == {
        "client_id": AZ["client_id"],
        "env": [("AZURE_CLIENT_ID", AZ["client_id"]), ("AZURE_TENANT_ID", AZ["tenant_id"])],
        "egress": [AZURE_LOGIN], "secrets": [], "mcp": None, "skills": []}


def test_identity_and_databricks():
    out = effects({"azure_identity": AZ, "databricks": DBX})
    assert out["env"] == [("AZURE_CLIENT_ID", AZ["client_id"]), ("AZURE_TENANT_ID", AZ["tenant_id"]),
                          ("DATABRICKS_HOST", DBX["host"]), ("DATABRICKS_HTTP_PATH", DBX["http_path"]),
                          ("DATABRICKS_CATALOG", "notes"), ("DATABRICKS_SCHEMA", "public")]
    assert out["egress"] == sorted([AZURE_LOGIN, DBX["host"]])


def test_catalog_and_schema_are_optional():
    dbx = {k: v for k, v in DBX.items() if k not in ("catalog", "schema")}
    names = [n for n, _ in effects({"azure_identity": AZ, "databricks": dbx})["env"]]
    assert "DATABRICKS_CATALOG" not in names and "DATABRICKS_SCHEMA" not in names


def test_databricks_without_identity_adds_nothing():
    assert effects({"databricks": DBX})["env"] == []


def test_each_call_returns_a_fresh_result():
    effects(None)["env"].append(("X", "y"))
    assert effects(None)["env"] == []


S = {"env": "NOTES_DB_PASSWORD", "secret_id": 1042, "secret_name": "notes-db", "folder_id": 17, "field_slug": "password",
     "verified_by": "mock-secret-server"}


def test_secrets_are_an_effect_without_an_identity():
    fx = effects({"secrets": [S]})
    assert fx["client_id"] is None and fx["env"] == [] and fx["egress"] == []
    assert fx["secrets"] == [("NOTES_DB_PASSWORD", 1042, "password")]


def test_secrets_are_an_effect_with_an_identity_too():
    assert effects({"azure_identity": AZ, "secrets": [S]})["secrets"] == [("NOTES_DB_PASSWORD", 1042, "password")]


def test_secrets_are_sorted_by_env_name():
    fx = effects({"secrets": [{**S, "env": "B"}, {**S, "env": "A", "secret_id": 7}]})
    assert [e for e, _, _ in fx["secrets"]] == ["A", "B"]


def test_no_secrets_is_an_empty_list():
    assert effects(None)["secrets"] == [] and effects({"secrets": []})["secrets"] == []


MCP = {"gateway": "mcp-test.example.com", "servers": [{"server_id": "tickets-x", "name": "T", "verified_by": "mock-cloudflare"},
                                                     {"server_id": "notes", "name": "Notes", "verified_by": "mock-cloudflare"}]}
BASE = {"repo": "Acme/My.Notes", "environment": "test"}


def test_the_token_is_named_from_the_repo_and_environment():
    assert mcp_token_name("Acme/My.Notes", "prod") == "cf-mcp-acme--my.notes-prod"
    assert mcp_secret_path("acme/notes", "test") == "/golden-road/mcp-tokens/cf-mcp-acme--notes-test"


@pytest.mark.parametrize("one, other", [("acme/a.b", "acme/a-b"), ("acme/b-c", "acme-b/c"), ("acme/a_b", "acme/a-b"),
                                        ("a/b--c-d", "a/b--c"), ("acme/notes", "Acme/Notes-test")])
def test_two_repos_never_share_a_token(one, other):
    assert mcp_token_name(one, "test") != mcp_token_name(other, "test")
    assert mcp_token_name("acme/a.b", "test") == "cf-mcp-acme--a.b-test" and mcp_token_name("acme-b/c", "test") == "cf-mcp-acme-b--c-test"


def test_the_token_path_has_no_space():
    assert " " not in mcp_secret_path("Acme/My.Notes", "test")


@pytest.mark.parametrize("repo", ["a--b/c", "-acme/notes", "acme-/notes", "ac_me/notes", "ac.me/notes", "a" * 40 + "/notes", "acme/", "acme"])
def test_an_owner_github_would_refuse_has_no_token(repo):
    with pytest.raises(ValueError, match="owner"):
        mcp_token_name(repo, "test")


def test_mcp_servers_are_plain_env_egress_and_a_token_without_an_identity():
    fx = effects({**BASE, "mcp": MCP})
    assert fx["client_id"] is None
    assert fx["env"] == [("MCP_URL", "https://mcp-test.example.com/mcp"), ("MCP_SERVERS", "notes,tickets-x")]
    assert fx["egress"] == ["mcp-test.example.com"]
    assert fx["mcp"] == {"gateway": "mcp-test.example.com", "url": "https://mcp-test.example.com/mcp", "servers": ["notes", "tickets-x"],
                         "token": "cf-mcp-acme--my.notes-test", "secret_path": "/golden-road/mcp-tokens/cf-mcp-acme--my.notes-test"}


def test_mcp_env_follows_the_identity_s_and_the_gateway_joins_its_egress():
    fx = effects({**BASE, "azure_identity": AZ, "databricks": DBX, "mcp": MCP, "skills": [
        {"name": "a", "commit": "2" * 40, "verified_by": "mock-skills-catalog"}]})
    assert [n for n, _ in fx["env"]] == ["AZURE_CLIENT_ID", "AZURE_TENANT_ID", "DATABRICKS_HOST", "DATABRICKS_HTTP_PATH",
                                         "DATABRICKS_CATALOG", "DATABRICKS_SCHEMA", "MCP_URL", "MCP_SERVERS", "SKILLS_DIR"]
    assert fx["egress"] == sorted([AZURE_LOGIN, DBX["host"], "mcp-test.example.com"])


def test_an_mcp_without_servers_adds_nothing():
    fx = effects({**BASE, "mcp": {"gateway": "mcp-test.example.com", "servers": []}})
    assert fx["env"] == [] and fx["egress"] == [] and fx["mcp"] is None


def test_skills_are_sorted_pins_and_set_skills_dir():
    fx = effects({**BASE, "skills": [{"name": "b", "commit": "1" * 40, "verified_by": "mock-skills-catalog"},
                                     {"name": "a", "commit": "2" * 40, "verified_by": "mock-skills-catalog"}]})
    assert fx["skills"] == [("a", "2" * 40), ("b", "1" * 40)] and fx["env"] == [("SKILLS_DIR", SKILLS_DIR)] and SKILLS_DIR == "/skills"


def test_no_mcp_or_skills_is_none_and_empty():
    assert effects(None)["mcp"] is None and effects(None)["skills"] == []

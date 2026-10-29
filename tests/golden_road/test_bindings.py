from golden_road.bindings import AZURE_LOGIN, effects

AZ = {"directory_id": "sp-notes", "tenant_id": "00000000-0000-4000-8000-00000000a2e0",
      "client_id": "11111111-1111-4111-8111-111111111111", "verified_by": "mock-directory"}
DBX = {"directory_id": "wh-notes", "host": "adb-1111111111111111.1.azuredatabricks.net", "http_path": "/sql/1.0/warehouses/0000notes0000",
       "catalog": "notes", "schema": "public", "verified_by": "mock-directory"}


def test_no_binding_adds_nothing():
    assert effects(None) == {"client_id": None, "env": [], "egress": []}
    assert effects({"schema": "binding/v1", "repo": "acme/notes"}) == {"client_id": None, "env": [], "egress": []}


def test_identity_only():
    assert effects({"azure_identity": AZ}) == {
        "client_id": AZ["client_id"],
        "env": [("AZURE_CLIENT_ID", AZ["client_id"]), ("AZURE_TENANT_ID", AZ["tenant_id"])],
        "egress": [AZURE_LOGIN]}


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

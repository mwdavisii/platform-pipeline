import copy

import pytest

from contracts.schema import validate

GOOD = {
    "schema": "binding/v1",
    "repo": "acme/notes",
    "azure_identity": {"directory_id": "sp-notes", "tenant_id": "00000000-0000-4000-8000-00000000a2e0",
                       "client_id": "11111111-1111-4111-8111-111111111111", "verified_by": "mock-directory"},
    "databricks": {"directory_id": "wh-notes", "host": "adb-1111111111111111.1.azuredatabricks.net",
                   "http_path": "/sql/1.0/warehouses/0000notes0000", "catalog": "notes", "schema": "public", "verified_by": "mock-directory"},
    "linked_by": "p_" + "a" * 32,
    "linked_at": "2026-10-29T15:00:00Z",
    "request": "r_" + "b" * 32,
}


def test_a_full_binding_is_valid():
    assert validate("binding.v1", GOOD) == []


def test_identity_only_and_unlinked_are_valid():
    identity_only = {k: v for k, v in GOOD.items() if k != "databricks"}
    unlinked = {k: v for k, v in GOOD.items() if k not in ("azure_identity", "databricks")}
    assert validate("binding.v1", identity_only) == [] and validate("binding.v1", unlinked) == []


@pytest.mark.parametrize("path, value", [
    (("azure_identity", "client_id"), "not-a-guid"),
    (("azure_identity", "tenant_id"), "11111111-1111-4111-8111-11111111111"),
    (("azure_identity", "verified_by"), "me"),
    (("databricks", "host"), "evil.example.com"),
    (("databricks", "host"), "adb-1.1.azuredatabricks.net.evil.com"),
    (("databricks", "http_path"), "/sql/1.0/warehouses/../x"),
    (("databricks", "catalog"), "notes; drop"),
    (("repo",), "acme/notes\nx"),
    (("linked_at",), "2026-10-29"),
])
def test_bad_values_are_refused(path, value):
    doc = copy.deepcopy(GOOD)
    target = doc
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert validate("binding.v1", doc)


def test_databricks_needs_an_azure_identity():
    doc = {k: v for k, v in GOOD.items() if k != "azure_identity"}
    assert validate("binding.v1", doc)


def test_unknown_keys_are_refused():
    assert validate("binding.v1", {**GOOD, "token": "x"})
    assert validate("binding.v1", {**GOOD, "databricks": {**GOOD["databricks"], "token": "x"}})


def test_the_gate_record_may_name_a_binding_or_none():
    import json
    from pathlib import Path
    schema = json.loads((Path(__file__).resolve().parents[2] / "src/contracts/schemas/gate-record.v1.json").read_text())
    assert "binding" in schema["properties"] and "binding" not in schema["required"]


def test_a_trailing_newline_or_an_impossible_instant_is_refused():
    doc = copy.deepcopy(GOOD)
    doc["databricks"]["host"] += "\n"
    assert validate("binding.v1", doc)
    assert validate("binding.v1", {**GOOD, "linked_at": "2026-02-30T15:00:00Z"})


V2 = {"schema": "binding/v2", "repo": "acme/notes", "environment": "test", "secrets": [],
      "linked_by": "p_" + "0" * 32, "linked_at": "2026-10-29T15:00:00Z", "request": "r_" + "0" * 32}
SECRET = {"env": "NOTES_DB_PASSWORD", "secret_id": 1042, "secret_name": "notes-db", "folder_id": 17, "field_slug": "password",
          "verified_by": "mock-secret-server"}


def with_secrets(*items):
    doc = copy.deepcopy(V2)
    doc["secrets"] = list(items)
    return doc


def test_a_binding_may_carry_secret_references():
    assert validate("binding.v2", with_secrets(SECRET)) == []
    assert validate("binding.v2", V2) == []  # every document written before this still validates


def test_secrets_need_no_azure_identity():
    assert validate("binding.v2", with_secrets(SECRET)) == []


@pytest.mark.parametrize("change", [
    {"env": "notes_db"}, {"env": "1ABC"}, {"env": "A\n"}, {"secret_id": 0}, {"secret_id": "1042"}, {"secret_id": True},
    {"folder_id": -1}, {"field_slug": "Password"}, {"field_slug": "-x"}, {"field_slug": "a" * 65}, {"secret_name": ""},
    {"secret_name": "a" * 201}, {"secret_name": "a\nb"}, {"verified_by": "mock-directory"}, {"value": "x"},
    {"secret_name": "a" * 129}, {"secret_name": " notes"}, {"secret_name": "notes "}, {"secret_name": "notes<script>"},
    {"secret_name": "notes/db"}, {"secret_name": "notes\tdb"}, {"secret_name": "{{ x }}"}, {"secret_name": "-notes"},
])
def test_a_bad_secret_reference_is_refused(change):
    item = {**SECRET, **change}
    assert validate("binding.v2", with_secrets(item))


def test_at_most_20_secrets_and_env_names_are_unique():
    many = [{**SECRET, "env": f"S{i}"} for i in range(21)]
    assert validate("binding.v2", with_secrets(*many))
    assert validate("binding.v2", with_secrets(*many[:20])) == []
    assert validate("binding.v2", with_secrets(SECRET, {**SECRET, "secret_id": 1050}))  # the same env twice


@pytest.mark.parametrize("name", ["notes-db", "a", "Notes DB.v2_prod", "a" * 128, "1042"])
def test_a_secret_server_like_name_is_accepted(name):
    assert validate("binding.v2", with_secrets({**SECRET, "secret_name": name})) == []

import copy

import pytest
from contracts.schema import validate

HEX = "a" * 64
RELEASE = {"schema": "deploy-release/v1", "repo": "acme/notes", "environment": "test", "namespace": "notes-test",
           "release": "test-20261029132351-42fa829", "image": f"ghcr.io/acme/notes@sha256:{HEX}",
           "manifests_sha256": HEX, "gate_record_sha256": HEX}
BINDING = {"schema": "binding/v2", "repo": "acme/notes", "environment": "test", "linked_by": "p_" + "0" * 32,
           "linked_at": "2026-10-29T15:00:00Z", "request": "r_" + "0" * 32,
           "azure_identity": {"directory_id": "sp-notes", "tenant_id": "00000000-0000-4000-8000-00000000a2e0",
                              "client_id": "11111111-1111-4111-8111-111111111111", "verified_by": "mock-directory"},
           "secrets": []}


def test_a_good_release_and_binding_validate():
    assert validate("deploy-release.v1", RELEASE) == []
    assert validate("binding.v2", BINDING) == []
    assert validate("binding.v2", {k: v for k, v in BINDING.items() if k != "secrets"}) == []  # secrets is optional


@pytest.mark.parametrize("key,value", [
    ("environment", "staging"), ("namespace", "Notes-test"), ("namespace", "notes.app-test"), ("namespace", "-notes-test"),
    ("namespace", "notes-test-"), ("namespace", "n" * 59 + "-test"), ("namespace", "notes-test\n"),
    ("namespace", "notes"), ("namespace", "notes-prod"), ("namespace", "test"),
    ("release", "prod-x"), ("release", "test-20261029132351-42fa829\n"), ("image", "ghcr.io/acme/notes:latest"),
    ("manifests_sha256", "A" * 64), ("gate_record_sha256", "a" * 63), ("repo", "acme/notes\n"), ("repo", "acme"),
])
def test_a_bad_release_is_refused(key, value):
    doc = dict(RELEASE, **{key: value})
    assert validate("deploy-release.v1", doc)


@pytest.mark.parametrize("key", list(RELEASE))
def test_every_release_field_is_required(key):
    assert validate("deploy-release.v1", {k: v for k, v in RELEASE.items() if k != key})


def test_a_release_takes_no_extra_field():
    assert validate("deploy-release.v1", dict(RELEASE, extra=1))


def test_a_prod_release_needs_a_prod_tag_and_a_prod_namespace():
    prod = dict(RELEASE, environment="prod", release="prod-20261029140000-42fa829", namespace="notes-prod")
    assert validate("deploy-release.v1", prod) == []
    assert validate("deploy-release.v1", dict(prod, release=RELEASE["release"])) != []
    assert validate("deploy-release.v1", dict(prod, namespace="notes-test")) != []  # a namespace per environment: <repo>-<env>
    assert validate("deploy-release.v1", dict(RELEASE, release="prod-20261029140000-42fa829")) != []
    assert validate("deploy-release.v1", dict(RELEASE, namespace="n" * 58 + "-test")) == []  # 63, the longest label


def test_binding_v2_needs_an_environment_and_keeps_v1_rules():
    assert validate("binding.v2", {k: v for k, v in BINDING.items() if k != "environment"})
    assert validate("binding.v2", dict(BINDING, environment="staging"))
    assert validate("binding.v2", dict(BINDING, schema="binding/v1"))
    doc = copy.deepcopy(BINDING); doc["databricks"] = {"directory_id": "wh", "host": "adb-1.1.azuredatabricks.net",
                                                       "http_path": "/sql/1.0/warehouses/abc", "verified_by": "mock-directory"}
    assert validate("binding.v2", doc) == []
    del doc["azure_identity"]
    assert validate("binding.v2", doc)  # Databricks only with an Azure identity, as in v1
    assert validate("binding.v2", dict(BINDING, secrets=[{"env": "X"}]))  # a secret reference needs every field


@pytest.mark.parametrize("stamp", ["2026-02-30T12:00:00Z", "2026-10-29T24:00:00Z"])
def test_binding_v2_linked_at_must_be_a_real_instant(stamp):
    assert validate("binding.v2", dict(BINDING, linked_at=stamp))

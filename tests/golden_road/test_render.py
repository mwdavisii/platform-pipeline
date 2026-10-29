import shutil
from pathlib import Path

import pytest
import yaml

from golden_road.io import InputError
from golden_road.render import render

GOLDEN = Path(__file__).resolve().parents[1] / "builder/golden/python-fastapi"
REF = "ghcr.io/org/notes-api@sha256:" + "b" * 64


@pytest.fixture
def tree(tmp_path):
    shutil.copytree(GOLDEN, tmp_path / "app")
    return tmp_path / "app"


def docs(text):
    return [d for d in yaml.safe_load_all(text) if d]


def test_render_pins_digest_and_keeps_every_object(tree):
    out = render(tree, REF, "notes-api")
    objects = docs(out)
    assert [o["kind"] for o in objects] == ["ServiceAccount", "Deployment", "Service", "NetworkPolicy", "CiliumNetworkPolicy"]
    [container] = objects[1]["spec"]["template"]["spec"]["containers"]
    assert container["image"] == REF
    assert "notes-api\n" not in out.split("image:")[1][:80]
    assert all("namespace" not in o["metadata"] for o in objects)


@pytest.mark.parametrize("ref", ["ghcr.io/org/notes-api:latest", "ghcr.io/org/notes-api@sha256:abc", "notes-api", "ghcr.io/org/x@sha256:" + "b" * 64 + "\n"])
def test_bad_image_ref(tree, ref):
    with pytest.raises(InputError):
        render(tree, ref, "notes-api")


def test_overlay_refused(tree):
    (tree / "deploy" / "overlays").mkdir()
    with pytest.raises(InputError):
        render(tree, REF, "notes-api")


def test_extra_resource_refused(tree):
    k = tree / "deploy/base/kustomization.yaml"
    doc = yaml.safe_load(k.read_text())
    doc["resources"].append("extra.yaml")
    k.write_text(yaml.safe_dump(doc))
    (tree / "deploy/base/extra.yaml").write_text("kind: ClusterRoleBinding\n")
    with pytest.raises(InputError):
        render(tree, REF, "notes-api")


def test_second_container_refused(tree):
    d = tree / "deploy/base/deployment.yaml"
    doc = yaml.safe_load(d.read_text())
    doc["spec"]["template"]["spec"]["containers"].append({"name": "sidecar", "image": "evil:latest"})
    d.write_text(yaml.safe_dump(doc))
    with pytest.raises(InputError):
        render(tree, REF, "notes-api")


def test_symlinked_manifest_refused(tree, tmp_path):
    target = tmp_path / "outside.yaml"
    target.write_text((tree / "deploy/base/service.yaml").read_text())
    (tree / "deploy/base/service.yaml").unlink()
    (tree / "deploy/base/service.yaml").symlink_to(target)
    with pytest.raises(InputError):
        render(tree, REF, "notes-api")


def test_wrong_app_name_refused(tree):
    with pytest.raises(InputError):
        render(tree, REF, "other-app")


def _edit_deployment(tree, fn):
    d = tree / "deploy/base/deployment.yaml"
    doc = yaml.safe_load(d.read_text())
    fn(doc["spec"]["template"]["spec"])
    d.write_text(yaml.safe_dump(doc))


@pytest.mark.parametrize("key,value", [
    ("initContainers", [{"name": "i", "image": "evil:latest"}]),
    ("ephemeralContainers", [{"name": "e", "image": "evil:latest"}]),
    ("volumes", [{"name": "v", "image": {"reference": "evil:latest"}}]),
])
def test_other_image_carriers_refused(tree, key, value):
    _edit_deployment(tree, lambda pod: pod.__setitem__(key, value))
    with pytest.raises(InputError):
        render(tree, REF, "notes-api")


def test_image_in_another_kind_refused(tree):
    p = tree / "deploy/base/service.yaml"
    doc = yaml.safe_load(p.read_text())
    doc["kind"] = "Job"
    doc["spec"] = {"template": {"spec": {"containers": [{"name": "x", "image": "evil:latest"}]}}}
    p.write_text(yaml.safe_dump(doc))
    with pytest.raises(InputError):
        render(tree, REF, "notes-api")


def test_image_hidden_in_other_object_refused(tree):
    p = tree / "deploy/base/service.yaml"
    doc = yaml.safe_load(p.read_text())
    doc["metadata"]["annotations"] = {"x": {"image": "evil:latest"}}
    p.write_text(yaml.safe_dump(doc))
    with pytest.raises(InputError):
        render(tree, REF, "notes-api")


def test_yaml_anchors_and_aliases_refused(tree):
    d = tree / "deploy/base/deployment.yaml"
    text = d.read_text().replace("name: app\n", "name: &n app\n", 1)
    assert "&n" in text
    d.write_text(text)
    with pytest.raises(InputError):
        render(tree, REF, "notes-api")


def test_namespace_refused(tree):
    p = tree / "deploy/base/service.yaml"
    doc = yaml.safe_load(p.read_text())
    doc["metadata"]["namespace"] = "kube-system"
    p.write_text(yaml.safe_dump(doc))
    with pytest.raises(InputError):
        render(tree, REF, "notes-api")


def test_wrong_kind_in_slot_refused(tree):
    p = tree / "deploy/base/service.yaml"
    doc = yaml.safe_load(p.read_text())
    doc["kind"] = "ClusterRoleBinding"
    p.write_text(yaml.safe_dump(doc))
    with pytest.raises(InputError):
        render(tree, REF, "notes-api")


def test_missing_deploy_is_an_input_error(tmp_path):
    with pytest.raises(InputError):
        render(tmp_path, REF, "notes-api")


def test_output_has_no_yaml_aliases(tree):
    out = render(tree, REF, "notes-api")
    assert "&id" not in out and "*id" not in out
    assert out.count(REF) == 2  # header comment + image


def test_missing_deploy_and_base_have_accurate_messages(tmp_path, tree):
    with pytest.raises(InputError, match="deploy/base not found; run builder generate"):
        render(tmp_path, REF, "notes-api")
    shutil.rmtree(tree / "deploy/base")
    with pytest.raises(InputError, match="deploy/base not found; run builder generate"):
        render(tree, REF, "notes-api")


import hashlib  # noqa: E402

from golden_road.render import load_binding  # noqa: E402

BINDING = {"schema": "binding/v1", "repo": "acme/notes", "linked_by": "p_" + "a" * 32, "linked_at": "2026-10-29T15:00:00Z",
           "request": "r_" + "b" * 32,
           "azure_identity": {"directory_id": "sp-notes", "tenant_id": "00000000-0000-4000-8000-00000000a2e0",
                              "client_id": "11111111-1111-4111-8111-111111111111", "verified_by": "mock-directory"},
           "databricks": {"directory_id": "wh-notes", "host": "adb-1111111111111111.1.azuredatabricks.net",
                          "http_path": "/sql/1.0/warehouses/0000notes0000", "catalog": "notes", "schema": "public",
                          "verified_by": "mock-directory"}}
SNAPSHOT = Path(__file__).resolve().parent / "fixtures/render-python-fastapi.yaml"


def test_render_without_a_binding_is_byte_for_byte_what_it_was(tree):
    # The snapshot is render()'s output for the python-fastapi golden before bindings existed. If the golden itself changes
    # (a builder change), regenerate it from a render without a binding and check the diff is only the golden's.
    assert render(tree, REF, "notes-api") == SNAPSHOT.read_text()
    assert render(tree, REF, "notes-api", binding=None) == SNAPSHOT.read_text()


def test_render_with_a_binding_adds_the_identity_env_and_egress(tree):
    sa, deployment, _, _, cilium = docs(render(tree, REF, "notes-api", binding=BINDING))
    assert sa["metadata"]["annotations"] == {"azure.workload.identity/client-id": "11111111-1111-4111-8111-111111111111"}
    assert deployment["spec"]["template"]["metadata"]["labels"]["azure.workload.identity/use"] == "true"
    env = {e["name"]: e.get("value") for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["AZURE_TENANT_ID"] == "00000000-0000-4000-8000-00000000a2e0" and env["DATABRICKS_SCHEMA"] == "public"
    assert env["AZURE_CLIENT_ID"] == "11111111-1111-4111-8111-111111111111" and env["PORT"] == "8080"
    fqdns = [r["toFQDNs"][0]["matchName"] for r in cilium["spec"]["egress"] if "toFQDNs" in r]
    assert "login.microsoftonline.com" in fqdns and "adb-1111111111111111.1.azuredatabricks.net" in fqdns
    for rule in cilium["spec"]["egress"]:
        if "toFQDNs" in rule and rule["toFQDNs"][0]["matchName"] in ("login.microsoftonline.com", "adb-1111111111111111.1.azuredatabricks.net"):
            assert rule == {"toFQDNs": [{"matchName": rule["toFQDNs"][0]["matchName"]}],
                            "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}]}


def test_a_binding_keeps_every_rule_the_builder_generated(tree):
    before = docs(render(tree, REF, "notes-api"))
    after = docs(render(tree, REF, "notes-api", binding=BINDING))
    egress = before[4]["spec"]["egress"]
    assert "rules" in egress[0]["toPorts"][0] and egress[0]["toPorts"][0]["rules"]["dns"]  # the DNS rule toFQDNs relies on
    assert after[4]["spec"]["egress"][:len(egress)] == egress
    assert after[4]["spec"]["endpointSelector"] == before[4]["spec"]["endpointSelector"]
    assert after[2:4] == before[2:4]  # the Service and the NetworkPolicy are untouched
    assert after[1]["spec"]["selector"] == before[1]["spec"]["selector"]


def test_a_host_the_spec_already_reaches_on_443_is_not_added_twice(tree):
    p = tree / "deploy/base/cilium-egress.yaml"
    doc = yaml.safe_load(p.read_text())
    doc["spec"]["egress"].append({"toFQDNs": [{"matchName": "login.microsoftonline.com"}], "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}]})
    p.write_text(yaml.safe_dump(doc, sort_keys=False))
    cilium = docs(render(tree, REF, "notes-api", binding=BINDING))[4]
    assert [r["toFQDNs"][0]["matchName"] for r in cilium["spec"]["egress"] if "toFQDNs" in r].count("login.microsoftonline.com") == 1


def test_identity_only_adds_no_databricks(tree):
    identity_only = {k: v for k, v in BINDING.items() if k != "databricks"}
    _, deployment, _, _, cilium = docs(render(tree, REF, "notes-api", binding=identity_only))
    names = [e["name"] for e in deployment["spec"]["template"]["spec"]["containers"][0]["env"]]
    assert "AZURE_CLIENT_ID" in names and not [n for n in names if n.startswith("DATABRICKS_")]
    assert "adb-1111111111111111.1.azuredatabricks.net" not in [r["toFQDNs"][0]["matchName"] for r in cilium["spec"]["egress"] if "toFQDNs" in r]


def test_an_unlinked_binding_adds_nothing(tree):
    unlinked = {k: v for k, v in BINDING.items() if k not in ("azure_identity", "databricks")}
    assert render(tree, REF, "notes-api", binding=unlinked) == render(tree, REF, "notes-api")


def test_a_binding_env_name_the_spec_already_uses_is_refused(tree):
    _edit_deployment(tree, lambda pod: pod["containers"][0]["env"].append({"name": "AZURE_CLIENT_ID", "value": "x"}))
    with pytest.raises(InputError, match="AZURE_CLIENT_ID"):
        render(tree, REF, "notes-api", binding=BINDING)


def test_a_service_account_annotation_already_set_is_refused(tree):
    p = tree / "deploy/base/serviceaccount.yaml"
    doc = yaml.safe_load(p.read_text())
    doc["metadata"]["annotations"] = {"azure.workload.identity/client-id": "22222222-2222-4222-8222-222222222222"}
    p.write_text(yaml.safe_dump(doc, sort_keys=False))
    with pytest.raises(InputError, match="azure.workload.identity"):
        render(tree, REF, "notes-api", binding=BINDING)


def test_load_binding_checks_schema_and_repo(tmp_path):
    path = tmp_path / "binding.yaml"
    path.write_text(yaml.safe_dump(BINDING))
    assert load_binding(path, "acme/notes") == BINDING
    with pytest.raises(InputError, match="for acme/notes, not acme/other"):
        load_binding(path, "acme/other")
    path.write_text(yaml.safe_dump({**BINDING, "azure_identity": {**BINDING["azure_identity"], "client_id": "x"}}))
    with pytest.raises(InputError, match="binding"):
        load_binding(path, "acme/notes")


def test_load_binding_refuses_a_repo_that_differs_only_in_case(tmp_path):
    # The release fetched bindings/<owner>/<repo>.yaml for exactly this name, and factory-check holds the file's repo to its
    # path exactly; a case-only difference means neither held, so it is refused rather than guessed at.
    path = tmp_path / "binding.yaml"
    path.write_text(yaml.safe_dump(BINDING))
    with pytest.raises(InputError, match="not Acme/Notes"):
        load_binding(path, "Acme/Notes")


@pytest.mark.parametrize("text", [
    "",
    "[1, 2]\n",
    "schema: binding/v1\n---\nschema: binding/v1\n",
    "{not yaml",
    '{"message": "Not Found", "documentation_url": "https://docs.github.com"}\n',
])
def test_load_binding_refuses_anything_but_one_binding(tmp_path, text):
    path = tmp_path / "binding.yaml"
    path.write_text(text)
    with pytest.raises(InputError):
        load_binding(path, "acme/notes")


def test_load_binding_refuses_a_line_break_in_a_value(tmp_path):
    path = tmp_path / "binding.yaml"
    bad = {**BINDING, "databricks": {**BINDING["databricks"], "catalog": "notes\n- name: X"}}
    path.write_text(yaml.safe_dump(bad))
    with pytest.raises(InputError, match="binding"):
        load_binding(path, "acme/notes")


def test_load_binding_refuses_a_symlink_or_a_directory(tmp_path):
    real = tmp_path / "real.yaml"
    real.write_text(yaml.safe_dump(BINDING))
    link = tmp_path / "binding.yaml"
    link.symlink_to(real)
    with pytest.raises(InputError):
        load_binding(link, "acme/notes")
    (tmp_path / "dir.yaml").mkdir()
    with pytest.raises(InputError):
        load_binding(tmp_path / "dir.yaml", "acme/notes")


def test_load_binding_refuses_anchors(tmp_path):
    path = tmp_path / "binding.yaml"
    path.write_text(yaml.safe_dump(BINDING).replace("catalog: notes", "catalog: &c notes"))
    with pytest.raises(InputError, match="anchors"):
        load_binding(path, "acme/notes")


def test_load_binding_refuses_a_large_file(tmp_path):
    path = tmp_path / "binding.yaml"
    path.write_text(yaml.safe_dump(BINDING) + "#" * 70_000 + "\n")
    with pytest.raises(InputError, match="too large"):
        load_binding(path, "acme/notes")


def test_the_snapshot_fixture_is_what_its_hash_says():
    # Guards the fixture itself against an accidental edit along with render.py.
    assert hashlib.sha256(SNAPSHOT.read_bytes()).hexdigest() == "b1646db56a035645ef4f4a1130ee4f430c6462712b94fecd3de63e71cecd31a9"


def _edit(tree, name, fn):
    p = tree / "deploy/base" / name
    doc = yaml.safe_load(p.read_text())
    fn(doc)
    p.write_text(yaml.safe_dump(doc, sort_keys=False))


IDENTITY_KEYS = [
    ("serviceaccount.yaml", lambda d: d["metadata"].setdefault("annotations", {}).update({"azure.workload.identity/client-id": "x"})),
    ("serviceaccount.yaml", lambda d: d["metadata"].setdefault("annotations", {}).update({"azure.workload.identity/tenant-id": "x"})),
    ("serviceaccount.yaml", lambda d: d["metadata"]["labels"].update({"azure.workload.identity/use": "true"})),
    ("deployment.yaml", lambda d: d["spec"]["template"]["metadata"]["labels"].update({"azure.workload.identity/use": "true"})),
    ("deployment.yaml", lambda d: d["spec"]["template"]["metadata"].setdefault("annotations", {}).update(
        {"azure.workload.identity/service-account-token-expiration": "3600"})),
]


@pytest.mark.parametrize("name,fn", IDENTITY_KEYS, ids=["sa-client-id", "sa-tenant-id", "sa-label", "pod-label", "pod-annotation"])
@pytest.mark.parametrize("with_binding", [True, False])
def test_a_deploy_base_that_switches_the_identity_on_itself_is_refused(tree, name, fn, with_binding):
    _edit(tree, name, fn)
    with pytest.raises(InputError, match="azure.workload.identity/.*only the platform's binding may set"):
        render(tree, REF, "notes-api", binding=BINDING if with_binding else None)


@pytest.mark.parametrize("name,fn,what", [
    ("serviceaccount.yaml", lambda d: d["metadata"].__setitem__("annotations", None), "ServiceAccount's metadata.annotations must be a mapping"),
    ("serviceaccount.yaml", lambda d: d["metadata"].__setitem__("annotations", ["a"]), "ServiceAccount's metadata.annotations must be a mapping"),
    ("deployment.yaml", lambda d: d["spec"]["template"]["metadata"].__setitem__("labels", None), "pod template metadata.labels must be a mapping"),
    ("deployment.yaml", lambda d: d["spec"]["template"].__setitem__("metadata", None), "pod template metadata must be a mapping"),
], ids=["sa-annotations-null", "sa-annotations-list", "pod-labels-null", "pod-metadata-null"])
@pytest.mark.parametrize("with_binding", [True, False])
def test_metadata_that_is_not_a_mapping_has_its_own_error(tree, name, fn, what, with_binding):
    _edit(tree, name, fn)
    with pytest.raises(InputError, match=what):
        render(tree, REF, "notes-api", binding=BINDING if with_binding else None)

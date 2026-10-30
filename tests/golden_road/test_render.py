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

SNAPSHOT = Path(__file__).resolve().parent / "fixtures/render-python-fastapi.yaml"


def test_render_is_byte_for_byte_what_it_was(tree):
    # The snapshot is render()'s output for the python-fastapi golden before bindings existed, and render takes no binding again
    # from builder 0.4.0 (golden-road compose applies one per environment). If the golden itself changes (a builder change),
    # regenerate it from a render and check the diff is only the golden's.
    assert render(tree, REF, "notes-api") == SNAPSHOT.read_text()


def test_render_takes_no_binding(tree):
    with pytest.raises(TypeError):
        render(tree, REF, "notes-api", binding=None)


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
def test_a_deploy_base_that_switches_the_identity_on_itself_is_refused(tree, name, fn):
    _edit(tree, name, fn)
    with pytest.raises(InputError, match="azure.workload.identity/.*only the platform's binding may set"):
        render(tree, REF, "notes-api")


@pytest.mark.parametrize("name,fn,what", [
    ("serviceaccount.yaml", lambda d: d["metadata"].__setitem__("annotations", None), "ServiceAccount's metadata.annotations must be a mapping"),
    ("serviceaccount.yaml", lambda d: d["metadata"].__setitem__("annotations", ["a"]), "ServiceAccount's metadata.annotations must be a mapping"),
    ("deployment.yaml", lambda d: d["spec"]["template"]["metadata"].__setitem__("labels", None), "pod template metadata.labels must be a mapping"),
    ("deployment.yaml", lambda d: d["spec"]["template"].__setitem__("metadata", None), "pod template metadata must be a mapping"),
], ids=["sa-annotations-null", "sa-annotations-list", "pod-labels-null", "pod-metadata-null"])
def test_metadata_that_is_not_a_mapping_has_its_own_error(tree, name, fn, what):
    _edit(tree, name, fn)
    with pytest.raises(InputError, match=what):
        render(tree, REF, "notes-api")

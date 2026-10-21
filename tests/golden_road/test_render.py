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

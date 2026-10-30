"""The portal writer's binding PR and golden-road-deploy, across trees (spec 2026-10-29 GitOps deploy repo, correction 3): the two
files the writer PUTs, laid out with the environment's release.yaml and base/manifests.yaml, are exactly what `golden-road compose`
writes, so deploy-check's drift check (golden-road deploy-verify) passes on the writer's PR."""

import base64
import json
import sys
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bootstrap/golden-road-portal"))
sys.path.insert(0, str(ROOT / "bootstrap/golden-road-portal/tests"))

from golden_road import deploy  # noqa: E402
from golden_road.compose import kustomization  # noqa: E402
from portal_fakes import GROUPS, ISSUER, T0, binding_request, fake_directory, make_settings, seed, transport, writer_config  # noqa: E402
from test_portal_writer_run import MANIFESTS, NOT_FOUND, bgh_for_write, file_answer, puts, release_doc  # noqa: E402

from portal.github.client import GitHub  # noqa: E402
from portal.roles import roles_for  # noqa: E402
from portal.store.memory import MemoryStore  # noqa: E402
from portal.secretserver_mock import load_mock  # noqa: E402
from portal.writer.run import run_once  # noqa: E402


@pytest.fixture
def world():
    store, settings = MemoryStore(), make_settings()
    people = {}
    for who, groups in GROUPS.items():
        if groups is not None:
            people[who] = store.sign_in(issuer=ISSUER, subject=f"sub-{who}", display_name=who.title(), email=None, groups=groups,
                                        roles=roles_for(groups, settings.role_groups), now=T0, max_age=settings.session_max)[0]
    seed(store, people, T0)  # acme/notes's repo admins, as the collector saw them
    return store, people


def written(store, people, environment, databricks=True, identity=True, secrets=()):
    """The writer's first pass on one binding for `environment`: {path: bytes} of what it PUT, and the release.yaml it read. With
    `secrets` the writer re-checks them against the mock Secret Server, as the platform account."""
    req = binding_request(store, people, T0, environment=environment, databricks=databricks, identity=identity, secrets=secrets)
    d = f"apps/acme/notes/{environment}"
    namespace, tag = f"notes-{environment}", f"{environment}-20261029120000-abcdef1"
    release = (f"# Written by the bump workflow from the published release {tag}.\n"
               + yaml.safe_dump(release_doc(environment=environment, namespace=namespace, release=tag), sort_keys=False)).encode()
    gh = bgh_for_write(release=file_answer(release))
    D = "/repos/acme/golden-road-deploy/contents"
    for name, answer in (("release.yaml", file_answer(release)), ("base/manifests.yaml", file_answer(MANIFESTS)),
                         ("bindings.yaml", NOT_FOUND), ("composed.yaml", NOT_FOUND)):
        gh.routes[("GET", "api.github.com", f"{D}/{d}/{name}")] = [answer]
    for name in ("bindings.yaml", "composed.yaml"):
        gh.routes[("PUT", "api.github.com", f"{D}/{d}/{name}")] = [(201, {"commit": {"sha": ("4" if name == "bindings.yaml" else "2") * 40}})]
    run_once(store, GitHub("https://api.github.com", httpx.Client(transport=transport(gh)), lambda: "ghs_" + "w" * 36),
             writer_config(directory=fake_directory(), secret_server=load_mock()), T0)
    assert store.get_request(req.id).state == "pr_open", store.get_request(req.id).failure_reason
    return {path.removeprefix(f"{D}/"): base64.b64decode(body["content"]) for path, body in puts(gh)}, release


def lay_out(root, environment, files, release):
    d = root / f"apps/acme/notes/{environment}"
    (d / "base").mkdir(parents=True)
    (d / "release.yaml").write_bytes(release)
    (d / "base/manifests.yaml").write_bytes(MANIFESTS)
    (d / "kustomization.yaml").write_text(kustomization(f"notes-{environment}"))  # bump.yml's, unchanged by a binding
    for path, data in files.items():
        (root / path).write_bytes(data)
    return d


@pytest.mark.parametrize("environment", ["test", "prod"])
@pytest.mark.parametrize("databricks", [True, False])
def test_the_writer_s_two_files_pass_deploy_verify(world, tmp_path, environment, databricks):
    store, people = world
    files, release = written(store, people, environment, databricks)
    assert sorted(files) == [f"apps/acme/notes/{environment}/bindings.yaml", f"apps/acme/notes/{environment}/composed.yaml"]
    d = lay_out(tmp_path, environment, files, release)
    assert deploy.verify_environment(d) == []
    before = (d / "composed.yaml").read_bytes()
    deploy.compose_environment(d)
    assert (d / "composed.yaml").read_bytes() == before, "golden-road compose writes the same bytes"


SECRETS = [(1042, "password", "NOTES_DB_PASSWORD"), (1050, "username", "SMTP_USER")]


@pytest.mark.parametrize("environment", ["test", "prod"])
@pytest.mark.parametrize("identity", [True, False])
def test_the_writer_s_files_with_secrets_pass_deploy_verify(world, tmp_path, environment, identity):
    """Spec 2026-10-29 Delinea secrets: the writer's bindings.yaml with secrets and its composed.yaml with the ExternalSecret are what
    golden-road compose writes, and deploy-verify's ExternalSecret check (store, target, keys) holds."""
    store, people = world
    files, release = written(store, people, environment, identity=identity, secrets=SECRETS)
    d = lay_out(tmp_path, environment, files, release)
    binding = yaml.safe_load((d / "bindings.yaml").read_bytes())
    assert [(s["secret_id"], s["field_slug"], s["env"]) for s in binding["secrets"]] == sorted(SECRETS, key=lambda s: s[2])
    objects = list(yaml.safe_load_all((d / "composed.yaml").read_bytes()))
    external = [o for o in objects if o["kind"] == "ExternalSecret"]
    assert objects[0]["kind"] == "Namespace" and objects[-1] is external[0] and len(external) == 1
    assert external[0]["metadata"]["name"] == "notes-api-delinea"
    assert external[0]["spec"]["secretStoreRef"] == {"kind": "ClusterSecretStore", "name": "delinea-secret-server"}
    assert {(e["secretKey"], e["remoteRef"]["key"], e["remoteRef"]["property"]) for e in external[0]["spec"]["data"]} == {
        ("NOTES_DB_PASSWORD", "1042", "password"), ("SMTP_USER", "1050", "username")}
    env = next(o for o in objects if o["kind"] == "Deployment")["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {"name": "SMTP_USER", "valueFrom": {"secretKeyRef": {"name": "notes-api-delinea", "key": "SMTP_USER"}}} in env
    assert any(e["name"] == "AZURE_CLIENT_ID" for e in env) == identity
    assert deploy.verify_environment(d) == []
    before = (d / "composed.yaml").read_bytes()
    deploy.compose_environment(d)
    assert (d / "composed.yaml").read_bytes() == before, "golden-road compose writes the same bytes"
    assert b"<mock>" not in before and b"<mock>" not in (d / "bindings.yaml").read_bytes()


def test_a_hand_edited_secret_in_the_writer_s_output_is_what_deploy_verify_refuses(world, tmp_path):
    """The ExternalSecret check is real: a composed.yaml pointing a secret at another id fails deploy-verify."""
    store, people = world
    files, release = written(store, people, "test", secrets=SECRETS)
    d = lay_out(tmp_path, "test", files, release)
    text = (d / "composed.yaml").read_text()
    assert "key: '1042'" in text or 'key: "1042"' in text
    (d / "composed.yaml").write_text(text.replace("'1042'", "'2001'").replace('"1042"', '"2001"'))
    assert deploy.verify_environment(d) != []


def test_a_stale_composed_yaml_is_what_deploy_verify_would_refuse(world, tmp_path):
    """The check is real: had the writer written only bindings.yaml, deploy-check would fail the PR."""
    store, people = world
    files, release = written(store, people, "test")
    d = lay_out(tmp_path, "test", {p: b for p, b in files.items() if p.endswith("bindings.yaml")}, release)
    (d / "composed.yaml").write_text(json.dumps("not what compose writes"))
    assert any("composed.yaml differs" in p for p in deploy.verify_environment(d))


def test_the_writer_s_caps_are_golden_road_s():
    from golden_road.render import MAX_FILE_BYTES
    from portal.writer import run
    assert (run.MAX_FILE_BYTES, run.MAX_BINDING_BYTES) == (MAX_FILE_BYTES, deploy.MAX_BINDING_BYTES)

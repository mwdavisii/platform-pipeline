"""The portal writer's binding PR and golden-road-deploy, across trees (spec 2026-10-29 GitOps deploy repo, correction 3): the files
the writer PUTs, laid out with the environment's release.yaml and base/manifests.yaml, are exactly what `golden-road compose`
writes, so deploy-check's drift check (golden-road deploy-verify) passes on the writer's PR. With MCP servers and skills (spec
2026-10-31) that is three files, cloudflare-access.yaml the third, and the skills ConfigMap built from the portal's catalog is the
one deploy-verify builds from the bundled catalog (the same bytes)."""

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
from portal_fakes import (GROUPS, ISSUER, T0, binding_request, fake_directory, make_settings, mock_cloudflare, seed, transport,  # noqa: E402
                          writer_config)
from test_portal_writer_run import MANIFESTS, NOT_FOUND, bgh_for_write, file_answer, puts, release_doc  # noqa: E402

from portal.github.client import GitHub  # noqa: E402
from portal.roles import roles_for  # noqa: E402
from portal.store.memory import MemoryStore  # noqa: E402
from portal.secretserver_mock import load_mock  # noqa: E402
from portal.skills_catalog import MOCK as CATALOG_MOCK, load_mock as load_skills_mock  # noqa: E402
from portal.writer.run import run_once  # noqa: E402


@pytest.fixture
def world():
    store, settings = MemoryStore(), make_settings()
    people = {}
    for who, groups in GROUPS.items():
        if groups is not None:
            people[who] = store.sign_in(issuer=ISSUER, subject=f"sub-{who}", display_name=who.title(), email=f"{who}@example.test",
                                        groups=groups,
                                        roles=roles_for(groups, settings.role_groups), now=T0, max_age=settings.session_max)[0]
    seed(store, people, T0)  # acme/notes's repo admins, as the collector saw them
    return store, people


def written(store, people, environment, databricks=True, identity=True, secrets=(), mcp=(), skills=(), lifecycle=None):
    """The writer's first pass on one binding for `environment`: {path: bytes} of what it PUT, and the release.yaml it read. With
    `secrets` the writer re-checks them against the mock Secret Server, as the platform account; with `mcp` (server ids) and
    `skills` ((name, commit)) against the mock Cloudflare and the portal's skills catalog. With `lifecycle` (bytes) the environment
    is suspended (spec 2026-11-02 rule 5)."""
    req = binding_request(store, people, T0, environment=environment, databricks=databricks, identity=identity, secrets=secrets,
                          mcp=mcp, skills=skills)
    d = f"apps/acme/notes/{environment}"
    namespace, tag = f"notes-{environment}", f"{environment}-20261029120000-abcdef1"
    release = (f"# Written by the bump workflow from the published release {tag}.\n"
               + yaml.safe_dump(release_doc(environment=environment, namespace=namespace, release=tag), sort_keys=False)).encode()
    gh = bgh_for_write(release=file_answer(release))
    D = "/repos/acme/golden-road-deploy/contents"
    for name, answer in (("release.yaml", file_answer(release)), ("base/manifests.yaml", file_answer(MANIFESTS)),
                         ("bindings.yaml", NOT_FOUND), ("composed.yaml", NOT_FOUND), ("cloudflare-access.yaml", NOT_FOUND),
                         ("lifecycle.yaml", NOT_FOUND if lifecycle is None else file_answer(lifecycle))):
        gh.routes[("GET", "api.github.com", f"{D}/{d}/{name}")] = [answer]
    # the pull request fixture's head is "2" * 40: the last file written
    for name, sha in (("bindings.yaml", "4"), ("composed.yaml", "6" if mcp else "2"), ("cloudflare-access.yaml", "2")):
        gh.routes[("PUT", "api.github.com", f"{D}/{d}/{name}")] = [(201, {"commit": {"sha": sha * 40}})]
    run_once(store, GitHub("https://api.github.com", httpx.Client(transport=transport(gh)), lambda: "ghs_" + "w" * 36),
             writer_config(directory=fake_directory(), secret_server=load_mock(), cloudflare=mock_cloudflare(),
                           skills_catalog=load_skills_mock()), T0)
    assert store.get_request(req.id).state == "pr_open", store.get_request(req.id).failure_reason
    return {path.removeprefix(f"{D}/"): base64.b64decode(body["content"]) for path, body in puts(gh)}, release


def lay_out(root, environment, files, release, lifecycle=None):
    d = root / f"apps/acme/notes/{environment}"
    (d / "base").mkdir(parents=True)
    (d / "release.yaml").write_bytes(release)
    if lifecycle is not None:
        (d / "lifecycle.yaml").write_bytes(lifecycle)
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


def test_a_prod_binding_waits_for_the_review_the_deploy_repo_requires():
    """Spec 2026-10-30, P1: the writer never merges a prod binding, so its PR meets main-review like anyone's: CODEOWNERS gives every
    bindings.yaml to security and analytics, and main-review asks for a code owner's review (none in a single-approver tree)."""
    from portal.writer.allowlist import ENVIRONMENTS, REVIEWED
    assert REVIEWED == ("prod",) and set(REVIEWED) <= set(ENVIRONMENTS)
    deploy_repo = ROOT / "bootstrap/golden-road-deploy"
    assert any(line.startswith("/apps/**/bindings.yaml ") for line in (deploy_repo / ".github/CODEOWNERS").read_text().splitlines())
    review = json.loads((deploy_repo / ".github/rulesets/main-review.json").read_text())
    [rule] = [r for r in review["rules"] if r["type"] == "pull_request"]
    single = json.loads((ROOT / "platform/platform.json").read_text()).get("gate2_single_approver") is True
    assert rule["parameters"]["require_code_owner_review"] is not single


MAIN = json.loads(CATALOG_MOCK.read_text(encoding="utf-8"))["main"]


@pytest.mark.parametrize("environment", ["test", "prod"])
@pytest.mark.parametrize("identity", [True, False])
def test_the_writer_s_three_files_with_mcp_and_skills_pass_deploy_verify(world, tmp_path, environment, identity):
    """Spec 2026-10-31: notes on the environment's gateway and incident-triage at the catalog's main, with and without an Azure
    identity. The writer's files are exactly bindings.yaml, composed.yaml and cloudflare-access.yaml; deploy-verify passes (the
    gateway is platform.json's, the ExternalSecret's MCP entries name the token's path, the skills ConfigMap is rebuilt from the
    bundled catalog), and golden-road compose rewrites all three byte for byte."""
    store, people = world
    files, release = written(store, people, environment, identity=identity, mcp=["notes"], skills=[("incident-triage", MAIN)])
    d = f"apps/acme/notes/{environment}"
    assert sorted(files) == [f"{d}/bindings.yaml", f"{d}/cloudflare-access.yaml", f"{d}/composed.yaml"]
    d = lay_out(tmp_path, environment, files, release)
    assert deploy.verify_environment(d) == []
    access = yaml.safe_load((d / "cloudflare-access.yaml").read_bytes())
    assert access["servers"] == ["notes"] and access["gateway"] == ("mcp-test.example.com" if environment == "test" else "mcp.example.com")
    assert access["token"]["secret_path"] == f"/golden-road/mcp-tokens/cf-mcp-acme--notes-{environment}"
    objects = list(yaml.safe_load_all((d / "composed.yaml").read_bytes()))
    [config_map] = [o for o in objects if o["kind"] == "ConfigMap"]
    assert config_map["metadata"]["name"] == "notes-api-skills" and "incident-triage__checklists__sev1.md" in config_map["data"]
    [external] = [o for o in objects if o["kind"] == "ExternalSecret"]
    assert [e["secretKey"] for e in external["spec"]["data"]] == ["CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET"]
    env = next(o for o in objects if o["kind"] == "Deployment")["spec"]["template"]["spec"]["containers"][0]["env"]
    assert any(e["name"] == "AZURE_CLIENT_ID" for e in env) == identity and {"name": "SKILLS_DIR", "value": "/skills"} in env
    before = {name: (d / name).read_bytes() for name in ("composed.yaml", "cloudflare-access.yaml")}
    deploy.compose_environment(d)
    assert {name: (d / name).read_bytes() for name in before} == before, "golden-road compose writes the same bytes"


def test_a_hand_edited_access_file_from_the_writer_is_what_deploy_verify_refuses(world, tmp_path):
    store, people = world
    files, release = written(store, people, "test", identity=False, mcp=["notes"])
    d = lay_out(tmp_path, "test", files, release)
    assert deploy.verify_environment(d) == []
    text = (d / "cloudflare-access.yaml").read_text()
    (d / "cloudflare-access.yaml").write_text(text.replace("- notes", "- finance-ledger"))
    assert deploy.verify_environment(d) != []


def test_a_bindings_yaml_the_writer_wrote_with_mcp_is_refused_on_a_platform_with_another_gateway(world, tmp_path, monkeypatch):
    """A portal configured with other gateway hosts than platform.json's fails deploy-check, never silently (conflict 2)."""
    store, people = world
    files, release = written(store, people, "test", identity=False, mcp=["notes"])
    d = lay_out(tmp_path, "test", files, release)
    monkeypatch.setattr(deploy, "_platform_gateways", lambda: {"test": "mcp-other.example.com", "prod": "mcp.example.com"})
    assert any("gateway" in p for p in deploy.verify_environment(d))


@pytest.mark.parametrize("environment", ["test", "prod"])
def test_a_binding_the_writer_writes_while_suspended_passes_deploy_verify_at_zero(world, tmp_path, environment):
    """Spec 2026-11-02 rule 5: the writer composes the binding with the environment's lifecycle.yaml, as deploy-verify does, so a
    suspended app's binding PR passes deploy-check and keeps the Deployment at zero replicas."""
    store, people = world
    life = yaml.safe_dump({"schema": "lifecycle/v1", "repo": "acme/notes", "environment": environment, "state": "suspended",
                           "task": "SCTASK0010001", "reason": "Critical vulnerability; suspended until patched",
                           "requested_by": "p_" + "a" * 32, "requested_at": "2026-11-02T22:00:00Z", "request": "r_" + "d" * 32},
                          sort_keys=False).encode()
    files, release = written(store, people, environment, lifecycle=life)
    assert sorted(files) == [f"apps/acme/notes/{environment}/bindings.yaml", f"apps/acme/notes/{environment}/composed.yaml"]
    d = lay_out(tmp_path, environment, files, release, lifecycle=life)
    assert deploy.verify_environment(d) == []
    objects = list(yaml.safe_load_all((d / "composed.yaml").read_bytes()))
    assert next(o for o in objects if o["kind"] == "Deployment")["spec"]["replicas"] == 0
    before = (d / "composed.yaml").read_bytes()
    deploy.compose_environment(d)
    assert (d / "composed.yaml").read_bytes() == before, "golden-road compose writes the same bytes"

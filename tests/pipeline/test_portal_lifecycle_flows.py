"""The portal writer's lifecycle PRs and golden-road-deploy, across trees (spec 2026-11-02): a real deploy tree laid out by golden-road
compose, loaded into RepoWorld, changed by the writer's passes, then written back and checked by golden-road deploy-verify. What the
writer leaves is what deploy-check passes: a suspend's composed.yaml is compose's own, a resume brings back the reviewed bytes, and a
decommission's deploy PR leaves no environment and no adapter."""

import hashlib
import sys
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bootstrap/golden-road-portal"))
sys.path.insert(0, str(ROOT / "bootstrap/golden-road-portal/tests"))

from golden_road import deploy  # noqa: E402
from golden_road.cli import main  # noqa: E402
from portal_fakes import (FIXTURES, GROUPS, INTAKE_V2, ISSUER, T0, RepoWorld, binding_request, fake_directory, lifecycle_app,  # noqa: E402
                          lifecycle_request, make_settings, seed, transport, writer_config)

from portal.github.client import GitHub  # noqa: E402
from portal.roles import roles_for  # noqa: E402
from portal.store.memory import MemoryStore  # noqa: E402
from portal.writer.run import run_once  # noqa: E402

URL = "https://github.com/acme/golden-road-deploy"
DEPLOY, FACTORY = "acme/golden-road-deploy", "acme/scorecard-factory"
MANIFESTS = (FIXTURES / "deploy/manifests.yaml").read_bytes()  # golden-road render's output, as bump pins it
IMAGE = "ghcr.io/acme/notes@sha256:" + "b" * 64


@pytest.fixture
def people():
    store, settings = MemoryStore(), make_settings()
    found = {}
    for who, groups in GROUPS.items():
        if groups is not None:
            found[who] = store.sign_in(issuer=ISSUER, subject=f"sub-{who}", display_name=who.title(), email=f"{who}@example.test",
                                       groups=groups, roles=roles_for(groups, settings.role_groups), now=T0, max_age=settings.session_max)[0]
    seed(store, found, T0)
    return store, found


def lay_out_tree(root):
    """golden-road-deploy as bump leaves it: the Flux source, apps/.gitkeep, and acme/notes in test and prod (release.yaml and
    base/manifests.yaml), with composed.yaml, kustomization.yaml and the adapters written by golden-road compose. {path: bytes}."""
    source = root / "clusters/home/flux/gitrepository.yaml"
    source.parent.mkdir(parents=True)
    source.write_text(yaml.safe_dump({"apiVersion": "source.toolkit.fluxcd.io/v1", "kind": "GitRepository",
                                      "metadata": {"name": "golden-road-deploy", "namespace": "flux-system"},
                                      "spec": {"interval": "5m", "url": URL, "ref": {"branch": "main"}}}))
    (root / "apps").mkdir()
    (root / "apps/.gitkeep").write_bytes(b"")
    for env in ("test", "prod"):
        d = root / f"apps/acme/notes/{env}"
        (d / "base").mkdir(parents=True)
        (d / "base/manifests.yaml").write_bytes(MANIFESTS)
        release = {"schema": "deploy-release/v1", "repo": "acme/notes", "environment": env, "namespace": f"notes-{env}",
                   "release": f"{env}-20261029120000-abcdef1", "image": IMAGE, "manifests_sha256": hashlib.sha256(MANIFESTS).hexdigest(),
                   "gate_record_sha256": "c" * 64}
        (d / "release.yaml").write_text(yaml.safe_dump(release, sort_keys=False))
        assert main(["compose", "--root", str(root), f"apps/acme/notes/{env}"]) == 0
    return read_tree(root)


def read_tree(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(Path(root).rglob("*")) if p.is_file()}


def write_tree(root, files):
    root.mkdir()
    for path, raw in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(raw)
    return root


def loaded(tmp_path):
    world = RepoWorld()
    original = lay_out_tree(tmp_path / "laid-out")
    world.add_repo(DEPLOY, original)
    world.add_repo(FACTORY, {"intake/acme/notes.yaml": INTAKE_V2("acme/notes")})
    world.add_repo("acme/notes", {"README.md": b"# app\n"})
    return world, original


def passes(store, world, n, cfg=None):
    gh = GitHub("https://api.github.com", httpx.Client(transport=transport(world)), lambda: "ghs_" + "w" * 36)
    for _ in range(n):
        run_once(store, gh, cfg or writer_config(directory=fake_directory()), T0)


def replicas(raw):
    return next(o for o in yaml.safe_load_all(raw) if o and o["kind"] == "Deployment")["spec"]["replicas"]


def verified(tmp_path, name, files):
    root = write_tree(tmp_path / name, files)
    return root, deploy.verify_tree(root, expect_url=URL)


def suspended(store, people, world):
    req = lifecycle_request(store, people, T0)
    passes(store, world, 3)
    assert store.get_request(req.id).state == "merged", store.lifecycle_steps(req.id)
    return req


def test_a_suspend_leaves_a_tree_deploy_verify_passes_and_compose_rewrites_byte_for_byte(people, tmp_path):
    store, found = people
    world, _ = loaded(tmp_path)
    suspended(store, found, world)
    root, result = verified(tmp_path, "suspended", world.files(DEPLOY))
    assert result == {"environments": 2, "problems": [], "warnings": []}
    for env in ("test", "prod"):
        d = root / f"apps/acme/notes/{env}"
        before = (d / "composed.yaml").read_bytes()
        assert replicas(before) == 0 and (d / "lifecycle.yaml").is_file()
        deploy.compose_environment(d)
        assert (d / "composed.yaml").read_bytes() == before, "the writer's composed.yaml is golden-road compose's own"


def test_a_resume_brings_back_each_composed_yaml_byte_for_byte(people, tmp_path):
    store, found = people
    world, original = loaded(tmp_path)
    suspended(store, found, world)
    resume = lifecycle_request(store, found, T0, action="resume", task="SCTASK0010002")
    passes(store, world, 3)
    assert store.get_request(resume.id).state == "merged"
    root, result = verified(tmp_path, "resumed", world.files(DEPLOY))
    assert result == {"environments": 2, "problems": [], "warnings": []}
    assert world.files(DEPLOY) == original, "lifecycle.yaml gone, and each composed.yaml the tree's original"


def test_a_decommission_s_deploy_pr_leaves_no_environment_and_no_adapter(people, tmp_path):
    store, found = people
    world, _ = loaded(tmp_path)
    suspended(store, found, world)  # a suspended app decommissioned: its lifecycle.yaml goes too
    req = lifecycle_request(store, found, T0, action="decommission", task="SCTASK0010003")
    cfg = writer_config(directory=fake_directory(), lifecycle_app=lifecycle_app(world))
    passes(store, world, 1, cfg)
    world.merge(DEPLOY, 2)
    passes(store, world, 1, cfg)
    assert store.lifecycle_steps(req.id)[0].state == "done" and store.lifecycle_state("acme/notes").state == "decommissioned"
    _, result = verified(tmp_path, "decommissioned", world.files(DEPLOY))
    assert result == {"environments": 0, "problems": [], "warnings": []}


def test_a_binding_written_while_suspended_passes_deploy_verify_at_zero(people, tmp_path):
    """Rule 5 across trees: a binding PR on a suspended tree composes with lifecycle.yaml, and deploy-verify passes on its head."""
    store, found = people
    world, _ = loaded(tmp_path)
    suspended(store, found, world)
    req = binding_request(store, found, T0)
    passes(store, world, 1)
    after = store.get_request(req.id)
    assert after.state == "pr_open", after.failure_reason
    root = write_tree(tmp_path / "bound", world.files(DEPLOY, after.branch))
    d = root / "apps/acme/notes/test"
    assert (d / "bindings.yaml").is_file() and deploy.verify_environment(d) == []
    assert replicas((d / "composed.yaml").read_bytes()) == 0
    assert deploy.verify_tree(root, expect_url=URL)["problems"] == []

"""The thin controller adapters: one Flux Kustomization (home cluster, suspended) and one Argo CD Application (office cluster, manual
sync) per app and environment, both pointing at the plain Kustomize directory. Mock only: generated and checked, never applied."""

import json
import shutil

import pytest
import yaml
from golden_road.cli import main
from golden_road.deploy import adapter_name, adapters, deploy_repo_url, verify_tree
from golden_road.io import InputError
from test_deploy_dir import env_dir, write_gitrepository

URL = "https://github.com/acme/golden-road-deploy"
FLUX, ARGO = "clusters/home/flux/acme-notes-test.yaml", "clusters/office/argo/acme-notes-test.yaml"


def test_flux_is_suspended_and_argo_has_no_automated_sync():
    files = adapters("acme", "notes", "test", "notes-test", repo_url=URL)
    assert sorted(files) == sorted([FLUX, ARGO])
    flux = yaml.safe_load(files[FLUX])
    argo = yaml.safe_load(files[ARGO])
    assert flux["apiVersion"] == "kustomize.toolkit.fluxcd.io/v1" and flux["metadata"] == {"name": "acme-notes-test", "namespace": "flux-system"}
    assert flux["kind"] == "Kustomization" and flux["spec"]["suspend"] is True and flux["spec"]["prune"] is True
    assert flux["spec"]["path"] == "./apps/acme/notes/test" and flux["spec"]["targetNamespace"] == "notes-test"
    assert flux["spec"]["sourceRef"] == {"kind": "GitRepository", "name": "golden-road-deploy"}
    assert argo["apiVersion"] == "argoproj.io/v1alpha1" and argo["metadata"] == {"name": "acme-notes-test", "namespace": "argocd"}
    assert argo["kind"] == "Application" and "automated" not in argo["spec"].get("syncPolicy", {})
    assert argo["spec"]["source"] == {"repoURL": URL, "targetRevision": "main", "path": "apps/acme/notes/test"}
    assert argo["spec"]["destination"] == {"server": "https://kubernetes.default.svc", "namespace": "notes-test"}


def test_the_adapters_are_the_same_bytes_every_time():
    assert adapters("acme", "notes", "prod", "notes-prod", repo_url=URL) == adapters("acme", "notes", "prod", "notes-prod", repo_url=URL)


@pytest.mark.parametrize("owner,repo,env,want", [
    ("acme", "notes", "test", "acme-notes-test"),
    ("Acme", "My.Notes_App", "prod", "acme-my-notes-app-prod"),
])
def test_the_name_is_owner_repo_env_lowercased(owner, repo, env, want):
    assert adapter_name(owner, repo, env) == want


def test_a_long_name_is_truncated_to_63_with_a_hash_suffix():
    long_a, long_b = "a" * 70, "a" * 69 + "b"
    a, b = adapter_name("acme", long_a, "test"), adapter_name("acme", long_b, "test")
    assert len(a) == 63 and len(b) == 63 and a != b
    assert a.startswith("acme-aaaa") and a[-7] == "-" and all(c in "0123456789abcdef" for c in a[-6:])


@pytest.mark.parametrize("args", [("acme", "notes", "staging", "notes-staging"), ("acme", "no/tes", "test", "notes-test"),
                                  ("acme", "notes", "test", "Notes_Test"), ("ac me", "notes", "test", "notes-test")])
def test_bad_inputs_are_refused(args):
    with pytest.raises(InputError):
        adapters(*args, repo_url=URL)


@pytest.mark.parametrize("url", ["http://github.com/acme/golden-road-deploy", "https://github.com/acme/other", "https://github.com/acme/golden-road-deploy.git",
                                 "https://github.com/acme/golden-road-deploy/x", "https://github.com/a b/golden-road-deploy"])
def test_a_bad_repo_url_is_refused(url):
    with pytest.raises(InputError):
        adapters("acme", "notes", "test", "notes-test", repo_url=url)


def test_the_repo_url_comes_from_the_gitrepository(tmp_path):
    write_gitrepository(tmp_path, URL)
    assert deploy_repo_url(tmp_path) == URL


@pytest.mark.parametrize("edit", [lambda d: d["spec"].update(ref={"branch": "dev"}), lambda d: d["metadata"].update(name="x"),
                                  lambda d: d.update(kind="OCIRepository"), lambda d: d["spec"].update(url="https://evil.example/acme/x")])
def test_a_gitrepository_that_is_not_this_repo_on_main_is_refused(tmp_path, edit):
    path = write_gitrepository(tmp_path, URL)
    doc = yaml.safe_load(path.read_text()); edit(doc); path.write_text(yaml.safe_dump(doc))
    with pytest.raises(InputError, match="gitrepository.yaml"):
        deploy_repo_url(tmp_path)


def test_compose_writes_the_adapters_and_verify_checks_them(tmp_path, app, capsys):
    env_dir(tmp_path, app)
    assert main(["compose", "--root", str(tmp_path), "apps/acme/notes/test"]) == 0
    assert json.loads(capsys.readouterr().out)["written"] == ["apps/acme/notes/test/composed.yaml", "apps/acme/notes/test/kustomization.yaml", FLUX, ARGO]
    want = adapters("acme", "notes", "test", "notes-test", repo_url=URL)
    assert {rel: (tmp_path / rel).read_text() for rel in want} == want
    assert verify_tree(tmp_path) == {"environments": 1, "problems": []}


@pytest.mark.parametrize("rel", [FLUX, ARGO])
def test_verify_reports_a_missing_adapter(tmp_path, app, rel):
    env_dir(tmp_path, app)
    main(["compose", "--root", str(tmp_path), "apps/acme/notes/test"])
    (tmp_path / rel).unlink()
    [problem] = verify_tree(tmp_path)["problems"]
    assert rel in problem and "missing" in problem and "apps/acme/notes/test" in problem


@pytest.mark.parametrize("rel,old,new", [(FLUX, "suspend: true", "suspend: false"), (ARGO, "namespace: argocd", "namespace: argocd\n  labels: {x: y}")])
def test_verify_reports_a_hand_edited_adapter(tmp_path, app, rel, old, new):
    """A resumed Kustomization in git, or an Application that gained an automated sync, is drift: resuming is done by hand on the cluster."""
    env_dir(tmp_path, app)
    main(["compose", "--root", str(tmp_path), "apps/acme/notes/test"])
    text = (tmp_path / rel).read_text()
    assert old in text
    (tmp_path / rel).write_text(text.replace(old, new))
    problems = verify_tree(tmp_path)["problems"]
    assert any(rel in p and "differs" in p for p in problems)
    assert all(rel in p for p in problems)  # a resumed Kustomization is also reported as not off by default


def test_verify_reports_a_resumed_kustomization_as_not_off_by_default(tmp_path, app):
    env_dir(tmp_path, app)
    main(["compose", "--root", str(tmp_path), "apps/acme/notes/test"])
    (tmp_path / FLUX).write_text((tmp_path / FLUX).read_text().replace("suspend: true", "suspend: false"))
    assert any(FLUX in p and "spec.suspend: true" in p for p in verify_tree(tmp_path)["problems"])


def test_verify_reports_an_automated_argo_sync(tmp_path, app):
    env_dir(tmp_path, app)
    main(["compose", "--root", str(tmp_path), "apps/acme/notes/test"])
    doc = yaml.safe_load((tmp_path / ARGO).read_text())
    doc["spec"]["syncPolicy"] = {"automated": {"prune": True}}
    (tmp_path / ARGO).write_text(yaml.safe_dump(doc))
    assert any(ARGO in p and "differs" in p for p in verify_tree(tmp_path)["problems"])


@pytest.mark.parametrize("d", ["clusters/home/flux", "clusters/office/argo"])
def test_verify_reports_an_adapter_for_no_environment(tmp_path, app, d):
    env_dir(tmp_path, app)
    main(["compose", "--root", str(tmp_path), "apps/acme/notes/test"])
    (tmp_path / d / "acme-gone-prod.yaml").write_text("kind: x\n")
    [problem] = verify_tree(tmp_path)["problems"]
    assert f"{d}/acme-gone-prod.yaml" in problem and "no environment" in problem


def test_verify_reports_a_symlinked_adapter(tmp_path, app):
    env_dir(tmp_path, app)
    main(["compose", "--root", str(tmp_path), "apps/acme/notes/test"])
    target = tmp_path / "elsewhere.yaml"
    (tmp_path / FLUX).rename(target)
    (tmp_path / FLUX).symlink_to(target)
    assert any(FLUX in p and "symlink" in p for p in verify_tree(tmp_path)["problems"])


def test_verify_reports_two_environments_with_one_adapter_name(tmp_path, app):
    env_dir(tmp_path, app)
    other = env_dir(tmp_path / "x", app)
    target = tmp_path / "apps/acme/notes.x/test"  # acme-notes-x-test
    target.parent.mkdir(parents=True); other.rename(target)
    twin = tmp_path / "apps/acme/notes_x/test"  # also acme-notes-x-test
    twin.parent.mkdir(parents=True)
    shutil.copytree(target, twin)
    for d, repo, ns in ((target, "acme/notes.x", "notes-x-test"), (twin, "acme/notes_x", "notes-x2-test")):
        release = yaml.safe_load((d / "release.yaml").read_text())
        (d / "release.yaml").write_text(yaml.safe_dump({**release, "repo": repo, "namespace": ns}))
    # compose refuses either of the two (see below), so this tree is what a hand-made commit would hold
    assert main(["compose", "--root", str(tmp_path), "apps/acme/notes.x/test"]) == 2
    problems = verify_tree(tmp_path)["problems"]
    assert any("acme-notes-x-test" in p and "apps/acme/notes.x/test" in p and "apps/acme/notes_x/test" in p for p in problems)


def test_compose_without_the_gitrepository_writes_nothing(tmp_path, app, capsys):
    d = env_dir(tmp_path, app)
    (tmp_path / "clusters/home/flux/gitrepository.yaml").unlink()
    assert main(["compose", "--root", str(tmp_path), "apps/acme/notes/test"]) == 2
    assert "gitrepository.yaml" in json.loads(capsys.readouterr().err)["error"]
    assert not (d / "composed.yaml").exists()


def test_verify_without_the_gitrepository_says_so(tmp_path, app):
    env_dir(tmp_path, app)
    main(["compose", "--root", str(tmp_path), "apps/acme/notes/test"])
    (tmp_path / "clusters/home/flux/gitrepository.yaml").unlink()
    assert any("gitrepository.yaml" in p for p in verify_tree(tmp_path)["problems"])


@pytest.mark.parametrize("owner,repo", [("-acme", "notes"), ("_acme", "notes"), (".acme", "notes")])
def test_a_name_that_is_not_a_dns_label_is_refused(owner, repo):
    with pytest.raises(InputError, match="DNS-1123"):
        adapter_name(owner, repo, "test")


def _twin(tmp_path, app, repo, namespace):
    other = env_dir(tmp_path / ("x-" + repo), app)
    target = tmp_path / "apps/acme" / repo / "test"
    target.parent.mkdir(parents=True); other.rename(target)
    release = yaml.safe_load((target / "release.yaml").read_text())
    (target / "release.yaml").write_text(yaml.safe_dump({**release, "repo": f"acme/{repo}", "namespace": namespace}))
    return target


def test_compose_refuses_an_adapter_name_another_environment_has(tmp_path, app, capsys):
    """acme/notes.app and acme/notes-app both name acme-notes-app-test: compose never overwrites the other's adapters."""
    first = _twin(tmp_path, app, "notes-app", "notes-app-test")
    write_gitrepository(tmp_path)
    assert main(["compose", "--root", str(tmp_path), "apps/acme/notes-app/test"]) == 0
    capsys.readouterr()
    before = {rel: (tmp_path / rel).read_bytes() for rel in ("clusters/home/flux/acme-notes-app-test.yaml", "clusters/office/argo/acme-notes-app-test.yaml")}
    second = _twin(tmp_path, app, "notes.app", "notes-app2-test")
    assert main(["compose", "--root", str(tmp_path), "apps/acme/notes.app/test"]) == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert "acme-notes-app-test" in error and "apps/acme/notes-app/test" in error and "apps/acme/notes.app/test" in error
    assert {rel: (tmp_path / rel).read_bytes() for rel in before} == before
    assert not (second / "composed.yaml").exists() and (first / "composed.yaml").exists()

import hashlib
import shutil
import subprocess

import pytest
import yaml
from golden_road.deploy import compose_environment, default_namespace, load_binding_v2, load_release, verify_environment
from golden_road.io import InputError
from golden_road.render import render

IMAGE = "ghcr.io/acme/notes@sha256:" + "b" * 64
IDENTITY = {"directory_id": "sp-notes", "tenant_id": "00000000-0000-4000-8000-00000000a2e0",
            "client_id": "11111111-1111-4111-8111-111111111111", "verified_by": "mock-directory"}


def binding_v2(environment="test", repo="acme/notes", **extra):
    return {"schema": "binding/v2", "repo": repo, "environment": environment, "linked_by": "p_" + "a" * 32,
            "linked_at": "2026-10-29T15:00:00Z", "request": "r_" + "b" * 32, "azure_identity": IDENTITY, **extra}


def write_gitrepository(root, url="https://github.com/acme/golden-road-deploy"):
    """The deploy repo's Flux source, which names the repo the adapters point at."""
    path = root / "clusters/home/flux/gitrepository.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"apiVersion": "source.toolkit.fluxcd.io/v1", "kind": "GitRepository",
                                    "metadata": {"name": "golden-road-deploy", "namespace": "flux-system"},
                                    "spec": {"interval": "5m", "url": url, "ref": {"branch": "main"}}}))
    return path


def env_dir(tmp_path, app, binding=None, environment="test"):
    write_gitrepository(tmp_path)
    d = tmp_path / "apps/acme/notes" / environment
    (d / "base").mkdir(parents=True)
    signed = render(app, IMAGE, "notes-api")
    (d / "base/manifests.yaml").write_text(signed)
    release = {"schema": "deploy-release/v1", "repo": "acme/notes", "environment": environment, "namespace": f"notes-{environment}",
               "release": f"{environment}-20261029132351-42fa829", "image": IMAGE,
               "manifests_sha256": hashlib.sha256(signed.encode()).hexdigest(), "gate_record_sha256": "a" * 64}
    (d / "release.yaml").write_text(yaml.safe_dump(release))
    if binding is not None:
        (d / "bindings.yaml").write_text(yaml.safe_dump(binding))
    return d


def docs(text):
    return [o for o in yaml.safe_load_all(text) if o]


def test_compose_writes_composed_and_kustomization_and_verify_passes(tmp_path, app):
    d = env_dir(tmp_path, app)
    assert compose_environment(d) == [d / "composed.yaml", d / "kustomization.yaml"]
    assert (d / "composed.yaml").is_file() and (d / "kustomization.yaml").is_file()
    assert yaml.safe_load((d / "kustomization.yaml").read_text())["namespace"] == "notes-test"
    assert verify_environment(d) == []


def test_a_binding_is_composed_in(tmp_path, app):
    d = env_dir(tmp_path, app, binding=binding_v2())
    compose_environment(d)
    sa = docs((d / "composed.yaml").read_text())[1]  # after the Namespace
    assert sa["metadata"]["annotations"]["azure.workload.identity/client-id"] == IDENTITY["client_id"]
    assert verify_environment(d) == []


def test_compose_twice_writes_the_same_bytes(tmp_path, app):
    d = env_dir(tmp_path, app, binding=binding_v2())
    compose_environment(d)
    first = (d / "composed.yaml").read_bytes()
    compose_environment(d)
    assert (d / "composed.yaml").read_bytes() == first


def test_verify_reports_a_stale_composed_file(tmp_path, app):
    d = env_dir(tmp_path, app); compose_environment(d)
    (d / "composed.yaml").write_text((d / "composed.yaml").read_text() + "# edited\n")
    assert verify_environment(d) == ["apps/acme/notes/test/composed.yaml differs from what golden-road compose writes"]


def test_verify_reports_a_binding_changed_without_recomposing(tmp_path, app):
    d = env_dir(tmp_path, app); compose_environment(d)
    (d / "bindings.yaml").write_text(yaml.safe_dump(binding_v2()))
    assert verify_environment(d) == ["apps/acme/notes/test/composed.yaml differs from what golden-road compose writes"]


def test_verify_reports_missing_generated_files_and_never_writes(tmp_path, app):
    d = env_dir(tmp_path, app)
    problems = verify_environment(d)
    assert any("composed.yaml is missing" in p for p in problems) and any("kustomization.yaml is missing" in p for p in problems)
    assert not (d / "composed.yaml").exists() and not (d / "kustomization.yaml").exists()


def test_the_base_must_match_the_release_s_hash(tmp_path, app):
    d = env_dir(tmp_path, app)
    (d / "base/manifests.yaml").write_text((d / "base/manifests.yaml").read_text() + "\n")
    with pytest.raises(InputError, match="manifests_sha256"):
        compose_environment(d)
    assert any("manifests_sha256" in p for p in verify_environment(d))
    assert not (d / "composed.yaml").exists()


def test_the_path_must_match_the_records(tmp_path, app):
    d = env_dir(tmp_path, app, environment="test")
    text = (d / "release.yaml").read_text().replace("environment: test", "environment: prod").replace("release: test-", "release: prod-")
    text = text.replace("namespace: notes-test", "namespace: notes-prod")
    (d / "release.yaml").write_text(text)
    with pytest.raises(InputError, match="environment"):
        compose_environment(d)


def test_the_repo_must_match_the_path_exactly(tmp_path, app):
    d = env_dir(tmp_path, app)
    (d / "release.yaml").write_text((d / "release.yaml").read_text().replace("repo: acme/notes", "repo: Acme/Notes"))
    with pytest.raises(InputError, match="repo"):
        load_release(d)


def test_an_invalid_release_is_refused(tmp_path, app):
    d = env_dir(tmp_path, app)
    (d / "release.yaml").write_text((d / "release.yaml").read_text().replace("namespace: notes-test", "namespace: Notes-test"))
    with pytest.raises(InputError, match="deploy-release/v1"):
        compose_environment(d)


def test_an_extra_file_is_reported(tmp_path, app):
    d = env_dir(tmp_path, app); compose_environment(d)
    (d / "patch.yaml").write_text("x: 1\n")
    assert any("patch.yaml" in p for p in verify_environment(d))
    with pytest.raises(InputError, match="patch.yaml"):
        compose_environment(d)


def test_an_extra_directory_is_reported(tmp_path, app):
    d = env_dir(tmp_path, app); compose_environment(d)
    (d / "patches").mkdir(); (d / "patches/egress.yaml").write_text("x: 1\n")
    assert any("patches" in p for p in verify_environment(d))


@pytest.mark.parametrize("binding,match", [
    (binding_v2(repo="acme/other"), "repo"),
    (binding_v2(environment="prod"), "environment"),
])
def test_a_binding_for_another_repo_or_environment_is_refused(tmp_path, app, binding, match):
    d = env_dir(tmp_path, app, binding=binding)
    with pytest.raises(InputError, match=match):
        compose_environment(d)
    assert any(match in p for p in verify_environment(d))


def test_a_binding_v1_is_refused(tmp_path, app):
    v1 = {k: v for k, v in binding_v2().items() if k != "environment"}
    v1["schema"] = "binding/v1"
    d = env_dir(tmp_path, app, binding=v1)
    with pytest.raises(InputError, match="binding/v2"):
        compose_environment(d)


def test_an_invalid_binding_is_refused(tmp_path, app):
    d = env_dir(tmp_path, app, binding=binding_v2(secrets=[{"env": "X"}]))
    with pytest.raises(InputError, match="binding/v2"):
        load_binding_v2(d, load_release(d))


def test_no_binding_file_is_no_binding(tmp_path, app):
    d = env_dir(tmp_path, app)
    assert load_binding_v2(d, load_release(d)) is None


@pytest.mark.parametrize("name", ["release.yaml", "bindings.yaml", "base/manifests.yaml", "composed.yaml"])
def test_a_symlink_anywhere_is_refused(tmp_path, app, name):
    d = env_dir(tmp_path, app, binding=binding_v2()); compose_environment(d)
    real = tmp_path / "outside"
    real.write_bytes((d / name).read_bytes())
    (d / name).unlink(); (d / name).symlink_to(real)
    assert any("symlink" in p for p in verify_environment(d))
    with pytest.raises(InputError, match="symlink"):
        compose_environment(d)


def test_a_symlinked_base_directory_is_refused(tmp_path, app):
    d = env_dir(tmp_path, app)
    shutil.move(d / "base", tmp_path / "real-base")
    (d / "base").symlink_to(tmp_path / "real-base")
    with pytest.raises(InputError, match="symlink"):
        compose_environment(d)


def test_a_symlinked_environment_directory_is_refused(tmp_path, app):
    d = env_dir(tmp_path, app)
    shutil.move(d, tmp_path / "real-env")
    d.symlink_to(tmp_path / "real-env")
    with pytest.raises(InputError, match="symlink"):
        compose_environment(d)


def test_a_release_over_1_mb_is_refused(tmp_path, app):
    d = env_dir(tmp_path, app)
    (d / "release.yaml").write_text((d / "release.yaml").read_text() + "#" * 1_000_001 + "\n")
    with pytest.raises(InputError, match="too large"):
        compose_environment(d)


def test_a_base_over_1_mb_is_refused(tmp_path, app):
    d = env_dir(tmp_path, app)
    (d / "base/manifests.yaml").write_text((d / "base/manifests.yaml").read_text() + "#" * 1_000_001 + "\n")
    with pytest.raises(InputError, match="too large"):
        compose_environment(d)


def test_a_size_error_names_the_file_by_its_path_in_the_environment(tmp_path, app):
    d = env_dir(tmp_path, app)
    (d / "base/manifests.yaml").write_text("#" * 1_000_001 + "\n")
    with pytest.raises(InputError, match="^apps/acme/notes/test: base/manifests.yaml is too large"):
        compose_environment(d)
    (d / "base/manifests.yaml").unlink()
    with pytest.raises(InputError, match="base/manifests.yaml must be a regular file"):
        compose_environment(d)


@pytest.mark.parametrize("name", ["release.yaml", "bindings.yaml"])
def test_duplicate_keys_in_the_records_are_refused(tmp_path, app, name):
    d = env_dir(tmp_path, app, binding=binding_v2())
    text = (d / name).read_text()
    (d / name).write_text(text + "repo: acme/notes\n")
    with pytest.raises(InputError, match=f"{name}.*duplicate key"):
        compose_environment(d)


def test_a_large_binding_is_refused(tmp_path, app):
    d = env_dir(tmp_path, app, binding=binding_v2())
    (d / "bindings.yaml").write_text((d / "bindings.yaml").read_text() + "#" * 70_000 + "\n")
    with pytest.raises(InputError, match="too large"):
        compose_environment(d)


def test_a_base_the_builder_did_not_make_is_refused(tmp_path, app):
    d = env_dir(tmp_path, app)
    text = (d / "base/manifests.yaml").read_text() + "---\nkind: ExternalSecret\nmetadata: {name: x}\n"
    (d / "base/manifests.yaml").write_text(text)
    release = yaml.safe_load((d / "release.yaml").read_text())
    (d / "release.yaml").write_text(yaml.safe_dump({**release, "manifests_sha256": hashlib.sha256(text.encode()).hexdigest()}))
    with pytest.raises(InputError, match="kinds"):
        compose_environment(d)


@pytest.mark.skipif(shutil.which("kustomize") is None, reason="kustomize is not installed")
def test_kustomize_builds_the_environment(tmp_path, app):
    d = env_dir(tmp_path, app, binding=binding_v2()); compose_environment(d)
    out = subprocess.run(["kustomize", "build", str(d)], check=True, capture_output=True, text=True).stdout
    objects = docs(out)
    namespaces = [o for o in objects if o["kind"] == "Namespace"]
    assert namespaces == [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "notes-test"}}]
    rest = [o for o in objects if o["kind"] != "Namespace"]
    assert len(rest) == 5 and all(o["metadata"]["namespace"] == "notes-test" for o in rest)


def test_composed_yaml_creates_the_environment_s_namespace_first(tmp_path, app):
    """Flux's targetNamespace and an Argo Application without CreateNamespace don't create it, so a manual resume or sync would fail."""
    d = env_dir(tmp_path, app); compose_environment(d)
    objects = docs((d / "composed.yaml").read_text())
    assert objects[0] == {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "notes-test"}}
    assert [o["kind"] for o in objects[1:]] == ["ServiceAccount", "Deployment", "Service", "NetworkPolicy", "CiliumNetworkPolicy"]


def test_verify_reports_a_composed_yaml_without_its_namespace(tmp_path, app):
    d = env_dir(tmp_path, app); compose_environment(d)
    text = (d / "composed.yaml").read_text()
    header, rest = text.split("\n", 1)
    (d / "composed.yaml").write_text(header + "\n" + rest.split("---\n", 1)[1])
    assert any("composed.yaml" in p and "differs" in p for p in verify_environment(d))


@pytest.mark.parametrize("repo,env,want", [
    ("acme/notes", "test", "notes-test"), ("acme/hello-app", "prod", "hello-app-prod"), ("Acme/Hello.App", "test", "hello-app-test"),
    ("acme/my_app", "prod", "my-app-prod"), ("acme/_x.", "test", "x-test"), ("acme/" + "a" * 58, "test", "a" * 58 + "-test"),
])
def test_the_namespace_convention_is_repo_dash_env(repo, env, want):
    assert default_namespace(repo, env) == want


@pytest.mark.parametrize("repo,env", [("acme/" + "a" * 59, "test"), ("acme/...", "test"), ("acme/notes", "staging"), ("notes", "test")])
def test_a_repo_with_no_valid_namespace_is_refused(repo, env):
    with pytest.raises(InputError):
        default_namespace(repo, env)


SECRET = {"env": "NOTES_DB_PASSWORD", "secret_id": 1042, "secret_name": "notes-db", "folder_id": 17, "field_slug": "password",
          "verified_by": "mock-secret-server"}


def edit_composed(d, fn):
    objects = docs((d / "composed.yaml").read_text())
    fn(objects)
    (d / "composed.yaml").write_text("---\n".join(yaml.safe_dump(o, sort_keys=False) for o in objects))


def test_a_composed_external_secret_must_match_the_binding(tmp_path, app):
    d = env_dir(tmp_path, app, binding=binding_v2(secrets=[SECRET]))
    compose_environment(d)
    assert docs((d / "composed.yaml").read_text())[-1]["kind"] == "ExternalSecret"
    assert verify_environment(d) == []
    text = (d / "composed.yaml").read_text()
    assert "key: '1042'" in text
    (d / "composed.yaml").write_text(text.replace("key: '1042'", "key: '2001'"))
    problems = verify_environment(d)
    assert any("composed.yaml differs" in p for p in problems)          # drift catches it
    assert any("ExternalSecret" in p and "2001" in p for p in problems)  # and the explicit check names it


def external_secret(objects):
    return next(o for o in objects if o["kind"] == "ExternalSecret")


@pytest.mark.parametrize("edit", [
    lambda o: external_secret(o)["spec"]["secretStoreRef"].update(kind="SecretStore"),
    lambda o: external_secret(o)["spec"]["secretStoreRef"].update(name="other-store"),
    lambda o: external_secret(o)["spec"]["data"].append({"secretKey": "X", "remoteRef": {"key": "9", "property": "p"}}),
    lambda o: external_secret(o)["spec"]["data"][0]["remoteRef"].update(property="username"),
    lambda o: external_secret(o)["spec"]["target"].update(name="someone-else"),
    lambda o: external_secret(o)["metadata"].update(name="other-app-delinea"),
    lambda o: external_secret(o)["spec"].__setitem__("data", "nope"),
    lambda o: o.append(dict(external_secret(o))),
    lambda o: o.remove(external_secret(o)),
    lambda o: o.append({"apiVersion": "external-secrets.io/v1", "kind": "SecretStore", "metadata": {"name": "mine"}, "spec": {}}),
    lambda o: o.append({"apiVersion": "external-secrets.io/v1", "kind": "ClusterSecretStore", "metadata": {"name": "mine"}}),
    lambda o: external_secret(o)["spec"].__setitem__("dataFrom", [{"find": {"name": {"regexp": ".*"}}}]),
    lambda o: external_secret(o)["spec"]["target"].__setitem__("template", {"data": {"X": "{{ .other }}"}}),
    lambda o: external_secret(o)["spec"]["target"].update(creationPolicy="Merge"),
    lambda o: external_secret(o)["metadata"].__setitem__("namespace", "kube-system"),
    lambda o: external_secret(o).update(apiVersion="external-secrets.io/v1beta1"),
    lambda o: external_secret(o)["spec"].update(refreshInterval="1s"),
    lambda o: external_secret(o).__setitem__("status", {}),
], ids=["store-kind", "store-name", "extra-key", "other-field", "target", "name", "data-not-list", "two", "none",
        "secret-store", "cluster-secret-store", "data-from", "template", "creation-policy", "namespace", "api-version",
        "refresh", "extra-top-key"])
def test_an_external_secret_outside_the_rules_is_reported(tmp_path, app, edit):
    d = env_dir(tmp_path, app, binding=binding_v2(secrets=[SECRET]))
    compose_environment(d)
    edit_composed(d, edit)
    problems = verify_environment(d)
    assert any("composed.yaml differs" in p for p in problems)
    assert any("composed.yaml:" in p and ("ExternalSecret" in p or "SecretStore" in p) for p in problems), problems


def test_an_external_secret_without_secrets_in_the_binding_is_reported(tmp_path, app):
    d = env_dir(tmp_path, app, binding=binding_v2(secrets=[SECRET]))
    compose_environment(d)
    the_secret = external_secret(docs((d / "composed.yaml").read_text()))
    (d / "bindings.yaml").write_text(yaml.safe_dump(binding_v2()))
    compose_environment(d)
    edit_composed(d, lambda o: o.append(the_secret))
    assert any("ExternalSecret" in p and "no secrets" in p for p in verify_environment(d))


def test_no_secrets_means_no_external_secret_problem(tmp_path, app):
    d = env_dir(tmp_path, app, binding=binding_v2())
    compose_environment(d)
    assert verify_environment(d) == []


@pytest.mark.parametrize("env", ["NODE_OPTIONS", "LD_PRELOAD", "PATH", "PYTHONPATH", "HOME", "PORT", "SECRETSERVER_X", "AZURE_TENANT_ID"])
def test_a_secret_env_the_builder_reserves_is_refused(tmp_path, app, env):
    d = env_dir(tmp_path, app, binding=binding_v2(secrets=[{**SECRET, "env": env}]))
    with pytest.raises(InputError, match=env):
        compose_environment(d)
    problems = verify_environment(d)
    assert any(env in p for p in problems), problems


def test_a_reserved_secret_env_in_a_hand_edited_binding_is_caught_by_verify(tmp_path, app):
    d = env_dir(tmp_path, app, binding=binding_v2(secrets=[SECRET]))
    compose_environment(d)
    (d / "bindings.yaml").write_text(yaml.safe_dump(binding_v2(secrets=[{**SECRET, "env": "LD_PRELOAD"}])))
    assert any("LD_PRELOAD" in p and "reserved" in p for p in verify_environment(d))

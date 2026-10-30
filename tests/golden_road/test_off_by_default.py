"""golden_road.off_by_default: golden-road-deploy never reconciles or denies on its own, read from every YAML document."""

import subprocess
import sys
from pathlib import Path

import pytest
from golden_road.off_by_default import problems

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "src/golden_road/off_by_default.py"
FLUX, ARGO = "clusters/home/flux/x.yaml", "clusters/office/argo/x.yaml"
POLICY = "policy/cluster-image-policy.yaml"
GOOD_POLICY = "apiVersion: policy.sigstore.dev/v1beta1\nkind: ClusterImagePolicy\nspec:\n  mode: warn\n"

GOOD_STORE = ("apiVersion: external-secrets.io/v1\nkind: ClusterSecretStore\nmetadata: {name: delinea-secret-server}\nspec:\n"
              "  conditions:\n    - namespaceRegexes: [\"^[a-z0-9-]+-(test|prod)$\"]\n  provider:\n"
              "    secretserver:\n      serverURL: https://secretserver.example.invalid\n"
              "      username: {secretRef: {name: golden-road-eso, key: username, namespace: external-secrets}}\n"
              "      password: {secretRef: {name: golden-road-eso, key: password, namespace: external-secrets}}\n")

BAD = {
    "argo-flow": {ARGO: "kind: Application\nspec: {syncPolicy: {automated: {}}}\n"},
    "argo-block": {ARGO: "kind: Application\nspec:\n  syncPolicy:\n    automated:\n      prune: true\n"},
    "argo-sync-not-a-map": {ARGO: "kind: Application\nspec:\n  syncPolicy: automated\n"},
    "applicationset": {ARGO: "kind: ApplicationSet\nspec: {}\n"},
    "flux-unsuspended": {FLUX: "kind: Kustomization\nspec:\n  suspend: false\n"},
    "flux-no-suspend": {FLUX: "kind: Kustomization\nspec:\n  prune: true\n"},
    "flux-suspend-string": {FLUX: "kind: Kustomization\nspec:\n  suspend: 'true'\n"},
    "flux-multidoc": {FLUX: "kind: Kustomization\nspec:\n  suspend: true\n---\nkind: Kustomization\nspec:\n  prune: true\n"},
    "flux-flow-and-stray-line": {FLUX: "kind: Kustomization\nspec: {suspend: false}\n  suspend: true\n"},
    "flux-duplicate-key": {FLUX: "kind: Kustomization\nspec:\n  suspend: false\n  suspend: true\n"},
    "flux-alias": {FLUX: "a: &t true\nkind: Kustomization\nspec:\n  suspend: *t\n"},
    "flux-yml": {"clusters/home/flux/x.yml": "kind: Kustomization\nspec:\n  suspend: false\n"},
    "flux-elsewhere": {"clusters/home/x.yaml": "kind: Kustomization\nspec:\n  suspend: false\n"},
    "flux-under-apps": {"apps/acme/notes/test/composed.yaml": "kind: Kustomization\napiVersion: kustomize.toolkit.fluxcd.io/v1\nspec: {}\n"},
    "gitrepo-smuggle": {"clusters/home/flux/gitrepository.yaml": "kind: GitRepository\n---\nkind: Kustomization\nspec:\n  suspend: false\n"},
    "policy-2nd-doc": {POLICY: GOOD_POLICY + "---\nkind: ClusterImagePolicy\nspec:\n  mode: enforce\n"},
    "policy-other-file": {"policy/p2.yaml": "kind: ClusterImagePolicy\nspec:\n  mode: enforce\n"},
    "policy-no-mode": {POLICY: "kind: ClusterImagePolicy\nspec: {}\n"},
    "clusters-other-file": {"clusters/home/flux/notes.txt": "x\n"},
    "clusters-nested": {"clusters/home/flux/sub/x.yaml": "kind: GitRepository\n"},
    "not-yaml": {"policy/x.yaml": "a: [\n"},
    "secret-store-in-an-app": {"apps/acme/notes/test/composed.yaml": "apiVersion: external-secrets.io/v1\nkind: SecretStore\nspec: {}\n"},
    "secret-store-in-external-secrets": {"external-secrets/cluster-secret-store.yaml": "kind: SecretStore\nspec: {}\n"},
    "cluster-secret-store-in-an-app": {"apps/acme/notes/test/composed.yaml": "kind: ClusterSecretStore\nmetadata: {name: x}\n"},
    "cluster-secret-store-in-policy": {POLICY: GOOD_POLICY + "---\nkind: ClusterSecretStore\nmetadata: {name: x}\n"},
    "cluster-secret-store-elsewhere-in-es": {"external-secrets/cilium-egress.yaml": "kind: ClusterSecretStore\nmetadata: {name: x}\n"},
    "external-secret-in-policy": {"policy/es.yaml": "kind: ExternalSecret\nmetadata: {name: x}\n"},
    "external-secret-in-base": {"apps/acme/notes/test/base/manifests.yaml": "kind: ExternalSecret\nmetadata: {name: x}\n"},
    "external-secret-in-a-staging-env": {"apps/acme/notes/staging/composed.yaml": "kind: ExternalSecret\nmetadata: {name: x}\n"},
    "external-secret-in-external-secrets": {"external-secrets/cluster-secret-store.yaml": "kind: ExternalSecret\nmetadata: {name: x}\n"},
    "external-secrets-other-file": {"external-secrets/extra.yaml": "kind: ConfigMap\n"},
    "external-secrets-nested": {"external-secrets/sub/README.md": "x\n"},
    "store-password-value": {"external-secrets/cluster-secret-store.yaml": (
        "kind: ClusterSecretStore\nmetadata: {name: delinea-secret-server}\nspec:\n  provider:\n    secretserver:\n"
        "      username: {secretRef: {name: golden-road-eso, key: username, namespace: external-secrets}}\n"
        "      password: {value: hunter2}\n")},
    "store-username-value-beside-ref": {"external-secrets/cluster-secret-store.yaml": (
        "kind: ClusterSecretStore\nmetadata: {name: delinea-secret-server}\nspec:\n  provider:\n    secretserver:\n"
        "      username: {value: eso, secretRef: {name: golden-road-eso, key: username, namespace: external-secrets}}\n"
        "      password: {secretRef: {name: golden-road-eso, key: password, namespace: external-secrets}}\n")},
    "flux-points-at-external-secrets": {FLUX: "kind: Kustomization\nspec:\n  suspend: true\n  path: ./external-secrets\n"},
    "argo-points-at-external-secrets": {ARGO: "kind: Application\nspec:\n  source: {path: external-secrets}\n"},
    "argo-sources-at-external-secrets": {ARGO: "kind: Application\nspec:\n  sources: [{path: ./external-secrets/}]\n"},
    "flux-root-path": {FLUX: "kind: Kustomization\nspec:\n  suspend: true\n  path: ./\n"},
    "flux-dot-path": {FLUX: "kind: Kustomization\nspec:\n  suspend: true\n  path: .\n"},
    "flux-no-path": {FLUX: "kind: Kustomization\nspec:\n  suspend: true\n"},
    "flux-parent-path": {FLUX: "kind: Kustomization\nspec:\n  suspend: true\n  path: ./apps/../..\n"},
    "flux-apps-dotdot": {FLUX: "kind: Kustomization\nspec:\n  suspend: true\n  path: ./apps/..\n"},
    "flux-absolute-root": {FLUX: "kind: Kustomization\nspec:\n  suspend: true\n  path: /\n"},
    "argo-root-path": {ARGO: "kind: Application\nspec:\n  source: {repoURL: x, path: .}\n"},
    "argo-no-path": {ARGO: "kind: Application\nspec:\n  source: {repoURL: x}\n"},
    "argo-sources-root": {ARGO: "kind: Application\nspec:\n  sources: [{repoURL: x, path: apps/acme/notes/test}, {repoURL: x, path: ./}]\n"},
    "argo-recurse": {ARGO: "kind: Application\nspec:\n  source: {path: apps/acme, directory: {recurse: true}}\n"},
    "argo-sources-recurse": {ARGO: "kind: Application\nspec:\n  sources: [{path: apps/acme, directory: {recurse: true}}]\n"},
    "kustomize-resource-root": {"apps/acme/notes/test/kustomization.yaml": (
        "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources: [composed.yaml, ../../../..]\n")},
    "kustomize-resource-above-root": {"apps/acme/notes/test/kustomization.yaml": (
        "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources: [../../../../..]\n")},
    "kustomize-remote-double-slash": {"apps/acme/notes/test/kustomization.yaml": (
        "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n"
        "resources: ['https://github.com/acme/golden-road-deploy//external-secrets?ref=main']\n")},
    "kustomize-remote-query": {"apps/acme/notes/test/kustomization.yaml": (
        "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n"
        "resources: ['github.com/acme/golden-road-deploy?path=external-secrets']\n")},
    "store-two-documents": {"external-secrets/cluster-secret-store.yaml": GOOD_STORE + "---\nkind: ConfigMap\nmetadata: {name: x}\n"},
    "store-no-conditions": {"external-secrets/cluster-secret-store.yaml": GOOD_STORE.replace(
        "  conditions:\n    - namespaceRegexes: [\"^[a-z0-9-]+-(test|prod)$\"]\n", "")},
    "store-any-namespace": {"external-secrets/cluster-secret-store.yaml": GOOD_STORE.replace("^[a-z0-9-]+-(test|prod)$", ".*")},
    "store-other-name": {"external-secrets/cluster-secret-store.yaml": GOOD_STORE.replace("name: delinea-secret-server", "name: other")},
    "store-other-kind": {"external-secrets/cluster-secret-store.yaml": "kind: ConfigMap\nmetadata: {name: delinea-secret-server}\n"},
    "store-ref-namespace": {"external-secrets/cluster-secret-store.yaml": GOOD_STORE.replace(
        "key: password, namespace: external-secrets", "key: password, namespace: kube-system")},
    "store-ref-no-namespace": {"external-secrets/cluster-secret-store.yaml": GOOD_STORE.replace(
        "key: username, namespace: external-secrets", "key: username")},
    "cluster-external-secret": {"policy/x.yaml": "kind: ClusterExternalSecret\nmetadata: {name: x}\n"},
    "push-secret": {"apps/acme/notes/test/composed.yaml": "kind: PushSecret\nmetadata: {name: x}\n"},
    "a-secret": {"apps/acme/notes/test/composed.yaml": "kind: Secret\nmetadata: {name: x}\nstringData: {password: x}\n"},
    "a-secret-in-external-secrets": {"external-secrets/cilium-egress.yaml": "kind: Secret\nmetadata: {name: golden-road-eso}\n"},
    "kustomize-resource-external-secrets": {"apps/acme/notes/test/kustomization.yaml": (
        "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources: [composed.yaml, ../../../../external-secrets]\n")},
}


def tree(tmp_path, files):
    for rel, text in {POLICY: GOOD_POLICY, "clusters/home/flux/README.md": "x\n", **files}.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    return tmp_path


@pytest.mark.parametrize("name", list(BAD))
def test_anything_that_would_reconcile_or_deny_is_refused(tmp_path, name):
    found = problems(tree(tmp_path, BAD[name]))
    assert found and all(any(p.startswith(rel + ":") for rel in BAD[name]) for p in found), found


def test_the_off_by_default_shapes_pass(tmp_path):
    files = {FLUX: "apiVersion: kustomize.toolkit.fluxcd.io/v1\nkind: Kustomization\nspec:\n  suspend: true\n  path: ./apps/acme/notes/test\n",
             ARGO: "kind: Application\nspec:\n  project: default\n",
             "clusters/office/argo/y.yaml": "kind: Application\nspec:\n  syncPolicy:\n    syncOptions: [CreateNamespace=false]\n",
             "clusters/home/flux/gitrepository.yaml": "kind: GitRepository\n",
             "apps/acme/notes/test/kustomization.yaml": "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nnamespace: n\n",
             ".github/workflows/x.yml": "on: push\njobs: {}\n"}
    assert problems(tree(tmp_path, files)) == []


def test_git_is_skipped_and_symlinks_are_refused(tmp_path):
    root = tree(tmp_path, {".git/x.yaml": "kind: Kustomization\n"})
    assert problems(root) == []
    (root / "policy/link.yaml").symlink_to(root / POLICY)
    assert problems(root) == ["policy/link.yaml: is a symlink"]


def test_the_committed_deploy_repo_passes():
    assert problems(ROOT / "bootstrap/golden-road-deploy") == []


def test_it_runs_standalone_in_isolated_mode(tmp_path):
    """The export loads it by path with python3 -I: no package imports, nothing from the working directory."""
    root = tree(tmp_path / "t", BAD["flux-yml"])
    run = lambda d: subprocess.run([sys.executable, "-I", str(MODULE), str(d)], capture_output=True, text=True, cwd=tmp_path)
    out = run(root)
    assert out.returncode == 1 and "clusters/home/flux/x.yml" in out.stdout
    assert run(ROOT / "bootstrap/golden-road-deploy").returncode == 0




def test_the_documented_external_secrets_files_and_a_composed_external_secret_pass(tmp_path):
    files = {"external-secrets/README.md": "x\n", "external-secrets/cluster-secret-store.yaml": GOOD_STORE,
             "external-secrets/cilium-egress.yaml": "apiVersion: cilium.io/v2\nkind: CiliumNetworkPolicy\nmetadata: {namespace: external-secrets}\n",
             "apps/acme/notes/prod/composed.yaml": "kind: Namespace\n---\napiVersion: external-secrets.io/v1\nkind: ExternalSecret\n"
                                                   "metadata: {name: notes-api-delinea}\n"}
    assert problems(tree(tmp_path, files)) == []


def test_argo_without_recurse_and_paths_into_apps_pass(tmp_path):
    files = {ARGO: "kind: Application\nspec:\n  source: {repoURL: x, path: apps/acme/notes/test, directory: {recurse: false}}\n",
             "clusters/office/argo/y.yaml": "kind: Application\nspec:\n  sources: [{repoURL: x, path: ./apps/acme/notes/prod/}]\n",
             "apps/acme/notes/test/kustomization.yaml": "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources: [composed.yaml]\n"}
    assert problems(tree(tmp_path, files)) == []

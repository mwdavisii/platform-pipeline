"""golden-road-deploy stays off by default: nothing in the repo may reconcile or deny on its own. Every YAML document of every .yaml
and .yml file is parsed (not grepped, so flow style, a second document or a stray line can't hide one), and:

- a Flux Kustomization (any kind Kustomization except Kustomize's own kustomization.yaml) has spec.suspend true;
- an Argo CD Application has no spec.syncPolicy.automated, and there is no ApplicationSet;
- a Sigstore ClusterImagePolicy has spec.mode warn;
- clusters/ holds only README.md files, clusters/home/flux/gitrepository.yaml, and .yaml adapters directly in clusters/home/flux/
  and clusters/office/argo/;
- External Secrets: a SecretStore, ClusterExternalSecret, PushSecret or Secret is refused anywhere; the one ClusterSecretStore,
  delinea-secret-server, is the only document of external-secrets/cluster-secret-store.yaml and takes its credential only from a
  secretRef in namespace external-secrets (never a value), and its spec.conditions lets only an app environment's namespace
  (<repo>-test or <repo>-prod) use it; an ExternalSecret appears only in an environment's composed.yaml
  (golden-road compose writes it);
- external-secrets/ holds only README.md, cluster-secret-store.yaml and cilium-egress.yaml, documented and applied by hand, so no
  Flux or Argo CD path reaches that directory or the repo root above it (a missing path is the root), no Argo CD source recurses,
  no kustomization entry reaches it (a remote URL is split on /, // and ?), and no other path-like value outside it names it.

A file that isn't valid YAML, names a key twice or is a symlink is refused too: what can't be read can't be checked. Standalone
(the standard library and PyYAML only), so the public export can run it with `python3 -I` on the staged tree, and deploy-verify
runs the same check in deploy-check."""

import os
import posixpath
import re
import sys
from pathlib import Path

import yaml

MAX_BYTES = 4 * 1024 * 1024
KUSTOMIZE_CONFIG = "kustomize.config.k8s.io/"  # kustomization.yaml: the Kustomize entry point, not a Flux object
ADAPTER_DIRS = ("clusters/home/flux", "clusters/office/argo")
EXTERNAL_SECRETS_DIR = "external-secrets"
EXTERNAL_SECRETS_FILES = {"README.md", "cluster-secret-store.yaml", "cilium-egress.yaml"}
CLUSTER_SECRET_STORE_FILE = "external-secrets/cluster-secret-store.yaml"
CLUSTER_SECRET_STORE_NAME = "delinea-secret-server"  # golden_road.compose.EXTERNAL_SECRET_STORE; this file imports nothing of the package
CLUSTER_SECRET_STORE_CONDITIONS = [{"namespaceRegexes": ["^[a-z0-9-]+-(test|prod)$"]}]  # only <repo>-<env> namespaces use the store
REFUSED_KINDS = {
    "SecretStore": "secrets come only from the platform's ClusterSecretStore",
    "ClusterExternalSecret": "an ExternalSecret comes only from golden-road compose, one per environment",
    "PushSecret": "nothing here writes to Secret Server",
    "Secret": "a Secret's value is never in git",
}
COMPOSED = re.compile(r"^apps/[^/]+/[^/]+/(test|prod)/composed\.yaml$")


class _DuplicateKey(yaml.YAMLError):
    pass


class _Alias(yaml.YAMLError):
    pass


class _UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            self.flatten_mapping(node)
            seen = []
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=True)
                if key in seen:
                    raise _DuplicateKey(f"duplicate key {str(key)[:80]!r}")
                seen.append(key)
        return super().construct_mapping(node, deep)


def _get(doc, *keys):
    for key in keys:
        if not isinstance(doc, dict):
            return None
        doc = doc.get(key)
    return doc


def document_problems(doc) -> list[str]:
    """What keeps one parsed document from being off by default."""
    if not isinstance(doc, dict):
        return []
    kind, api = doc.get("kind"), doc.get("apiVersion")
    if kind == "Kustomization" and not (isinstance(api, str) and api.startswith(KUSTOMIZE_CONFIG)):
        if _get(doc, "spec", "suspend") is not True:
            return ["a Flux Kustomization must have spec.suspend: true"]
    elif kind == "Application":
        sync = _get(doc, "spec", "syncPolicy")
        if sync is not None and (not isinstance(sync, dict) or "automated" in sync):
            return ["an Argo CD Application must have no automated sync (spec.syncPolicy.automated)"]
    elif kind == "ApplicationSet":
        return ["an Argo CD ApplicationSet generates Applications this check can't see; golden-road-deploy holds none"]
    elif kind == "ClusterImagePolicy":
        if _get(doc, "spec", "mode") != "warn":
            return ["a ClusterImagePolicy must have spec.mode: warn"]
    return []


def _strings(node):
    """Every string value (not key) at any depth."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)


def _names_external_secrets(value: str) -> bool:
    """A path that names external-secrets/: one of its segments is exactly the directory (./external-secrets, ../../external-secrets/,
    external-secrets/x). The API group external-secrets.io and a bare namespace name are not paths and pass, unless a key that holds a
    path holds them (see _applied_paths)."""
    return EXTERNAL_SECRETS_DIR in value.strip().split("/") and "/" in value.strip()


REMOTE = re.compile(r"^([a-z][a-z0-9+.-]*://|git@|github\.com/|gitlab\.com/|bitbucket\.org/)", re.IGNORECASE)


def _applies_external_secrets(value: str, base: str) -> bool:
    """Whether a path Flux, Argo CD or Kustomize would apply reaches external-secrets/: it is the directory or inside it, or it is the
    repo's root or above it (which holds the directory). `base` is the directory the path is relative to. A remote URL is split on
    /, // and ? (kustomize's repo//path?ref= form) and refused if any part is the directory."""
    value = value.strip()
    if REMOTE.match(value):
        return EXTERNAL_SECRETS_DIR in re.split(r"//|/|\?|&|=", value)
    if value.startswith("/"):
        return True  # absolute: the repo root, as Flux and Argo read it
    norm = posixpath.normpath(posixpath.join(base, value)) if value else posixpath.normpath(base or ".")
    return norm == "." or norm == ".." or norm.startswith("../") or norm.split("/")[0] == EXTERNAL_SECRETS_DIR


def _is_flux_kustomization(doc) -> bool:
    api = doc.get("apiVersion")
    return doc.get("kind") == "Kustomization" and not (isinstance(api, str) and api.startswith(KUSTOMIZE_CONFIG))


def _applied_paths(rel: str, doc) -> list:
    """(path, base) for what a document tells Flux, Argo CD or Kustomize to apply. A Flux Kustomization's spec.path and an Argo CD
    source's path are relative to the repo root, and a missing one is the root itself; a kustomization.yaml's resources, components
    and bases are relative to its own directory."""
    found = []
    if _is_flux_kustomization(doc):
        path = _get(doc, "spec", "path")
        found.append((path if isinstance(path, str) else "", ""))
    if doc.get("kind") == "Application":
        sources = _get(doc, "spec", "sources")
        sources = (sources if isinstance(sources, list) else []) + ([_get(doc, "spec", "source")] if _get(doc, "spec", "source") is not None else [])
        for source in sources:
            path = _get(source, "path")
            found.append((path if isinstance(path, str) else "", ""))
    if (doc.get("kind") == "Kustomization" and not _is_flux_kustomization(doc)) or doc.get("kind") is None:
        for key in ("resources", "components", "bases"):
            items = doc.get(key)
            found += [(v, posixpath.dirname(rel)) for v in (items if isinstance(items, list) else []) if isinstance(v, str)]
    return found


def _recurses(doc) -> bool:
    """An Argo CD source with directory.recurse: true applies every directory below its path, so it is refused outright."""
    if doc.get("kind") != "Application":
        return False
    sources = _get(doc, "spec", "sources")
    sources = (sources if isinstance(sources, list) else []) + [_get(doc, "spec", "source")]
    return any(_get(source, "directory", "recurse") not in (None, False) for source in sources)


def _secret_refs(node):
    """Every secretRef mapping at any depth."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "secretRef":
                yield value
            yield from _secret_refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _secret_refs(item)


def _credential_problems(doc) -> list[str]:
    found = []
    if _get(doc, "metadata", "name") != CLUSTER_SECRET_STORE_NAME:
        found.append(f"the ClusterSecretStore must be named {CLUSTER_SECRET_STORE_NAME}, the store every composed ExternalSecret names")
    provider = _get(doc, "spec", "provider")
    server = _get(provider, "secretserver")
    if not isinstance(provider, dict) or list(provider) != ["secretserver"] or not isinstance(server, dict):
        return found + ["the ClusterSecretStore's provider must be secretserver alone"]
    bad = [key for key in ("username", "password") if not (isinstance(server.get(key), dict) and list(server[key]) == ["secretRef"]
                                                           and isinstance(server[key]["secretRef"], dict))]
    found += [f"the ClusterSecretStore's {key} must be only a secretRef (a credential is never in git)" for key in bad]
    if any(_get(ref, "namespace") != EXTERNAL_SECRETS_DIR for ref in _secret_refs(doc)):
        found.append(f"every secretRef in the ClusterSecretStore must name namespace {EXTERNAL_SECRETS_DIR}")
    if _get(doc, "spec", "conditions") != CLUSTER_SECRET_STORE_CONDITIONS:
        found.append("the ClusterSecretStore's spec.conditions must be exactly namespaceRegexes [\"^[a-z0-9-]+-(test|prod)$\"]: only an "
                     "app environment's namespace may use it")
    return found


def placement_problems(rel: str, doc) -> list[str]:
    """What keeps one parsed document from being where golden-road-deploy allows it (External Secrets objects and Secrets, and
    anything that would apply external-secrets/: a Flux, Argo CD or Kustomize path that reaches it or the repo root, an Argo CD
    recurse, or any path-like value naming it in a Kubernetes object, one with a kind; a workflow's text may name the directory)."""
    if not isinstance(doc, dict):
        return []
    kind, found = doc.get("kind"), []
    if kind in REFUSED_KINDS:
        found.append(f"a {kind} is refused anywhere: {REFUSED_KINDS[kind]}")
    elif kind == "ClusterSecretStore":
        if rel != CLUSTER_SECRET_STORE_FILE:
            found.append(f"a ClusterSecretStore may only be in {CLUSTER_SECRET_STORE_FILE}")
        else:
            found += _credential_problems(doc)
    elif kind == "ExternalSecret" and not COMPOSED.fullmatch(rel):
        found.append("an ExternalSecret may only be in an environment's composed.yaml, where golden-road compose writes it")
    if _recurses(doc):
        found.append("an Argo CD source may not set directory.recurse: it would apply every directory below its path")
    if not rel.startswith(EXTERNAL_SECRETS_DIR + "/"):
        if any(_applies_external_secrets(v, base) for v, base in _applied_paths(rel, doc)) \
                or ("kind" in doc and any(_names_external_secrets(v) for v in _strings(doc))):
            found.append("nothing may point at external-secrets/ or the repo root that holds it: it is documented and applied by hand")
    return found


def _file_problems(rel: str, docs: list) -> list[str]:
    """Rules about a whole file rather than one document."""
    if rel == CLUSTER_SECRET_STORE_FILE and not (len(docs) == 1 and isinstance(docs[0], dict) and docs[0].get("kind") == "ClusterSecretStore"):
        return [f"{CLUSTER_SECRET_STORE_FILE} must be exactly one document, the ClusterSecretStore {CLUSTER_SECRET_STORE_NAME}"]
    return []


def _layout_problem(rel: str) -> str | None:
    """Why a file under clusters/ is not one golden-road-deploy holds, or None."""
    parts = rel.split("/")
    if parts[-1] == "README.md" or rel == "clusters/home/flux/gitrepository.yaml":
        return None
    if "/".join(parts[:-1]) in ADAPTER_DIRS and parts[-1].endswith(".yaml"):
        return None
    return "clusters/ holds only README.md, clusters/home/flux/gitrepository.yaml and .yaml adapters in clusters/home/flux/ and clusters/office/argo/"


def problems(root) -> list[str]:
    """Every problem, as "<path from root>: <why>", sorted by path. .git/ is skipped."""
    root = Path(root)
    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        for name in list(dirnames):
            if (here / name).is_symlink():
                found.append(f"{(here / name).relative_to(root).as_posix()}: is a symlink")
        dirnames[:] = sorted(d for d in dirnames if not (here / d).is_symlink() and not (here == root and d == ".git"))
        for name in sorted(filenames):
            path = here / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                found.append(f"{rel}: is a symlink")
                continue
            if rel.startswith("clusters/"):
                why = _layout_problem(rel)
                if why:
                    found.append(f"{rel}: {why}")
            if rel.startswith(EXTERNAL_SECRETS_DIR + "/") and rel.removeprefix(EXTERNAL_SECRETS_DIR + "/") not in EXTERNAL_SECRETS_FILES:
                found.append(f"{rel}: external-secrets/ holds only README.md, cluster-secret-store.yaml and cilium-egress.yaml")
            if not name.endswith((".yaml", ".yml")):
                continue
            if path.stat().st_size > MAX_BYTES:
                found.append(f"{rel}: is too large to check")
                continue
            try:
                text = path.read_text(encoding="utf-8")
                for event in yaml.parse(text, Loader=yaml.SafeLoader):
                    if isinstance(event, yaml.AliasEvent):
                        raise _Alias("YAML aliases are refused")  # an alias could hide a value from a reader; nothing here needs one
                docs = list(yaml.load_all(text, Loader=_UniqueKeyLoader))
            except (yaml.YAMLError, UnicodeDecodeError, RecursionError) as exc:
                found.append(f"{rel}: is not YAML this check can read ({type(exc).__name__}: {str(exc).splitlines()[0][:120] if str(exc) else ''})")
                continue
            found += [f"{rel}: {why}" for why in _file_problems(rel, docs)]
            for n, doc in enumerate(docs, 1):
                for why in document_problems(doc) + placement_problems(rel, doc):
                    found.append(f"{rel}: {why}" + (f" (document {n})" if len(docs) > 1 else ""))
    return sorted(found)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: off_by_default.py <golden-road-deploy directory>", file=sys.stderr)
        return 2
    if not Path(argv[0]).is_dir():
        print(f"{argv[0]} is not a directory", file=sys.stderr)
        return 2
    found = problems(argv[0])
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())

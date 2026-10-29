"""Deploy-ready manifests: the builder's deploy/base, with the app image pinned to the signed digest.

Equivalent to `kustomize build` on a base that only lists resources, done in Python so the pipeline
needs no extra binary. Anything that isn't exactly the builder's layout is refused, and the output is
dumped from the objects that were checked, so the only image it can name is the digest it was given.
"""

import re
from pathlib import Path

import yaml

from builder.render.k8s import RESOURCES
from contracts.schema import validate

from .bindings import effects
from .io import InputError

IMAGE_REF = re.compile(r"^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$")
KUSTOMIZATION = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "resources": RESOURCES}
KINDS = ["ServiceAccount", "Deployment", "Service", "NetworkPolicy", "CiliumNetworkPolicy"]
MAX_FILE_BYTES = 1_000_000
MAX_BINDING_BYTES = 65_536  # a binding is a few hundred bytes; the release fetches it with the same cap
WORKLOAD_IDENTITY_ANNOTATION = "azure.workload.identity/client-id"
WORKLOAD_IDENTITY_LABEL = "azure.workload.identity/use"
WORKLOAD_IDENTITY_PREFIX = "azure.workload.identity/"


class _NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def _read_yaml(path: Path, max_bytes: int = MAX_FILE_BYTES):
    if path.is_symlink() or not path.is_file():
        raise InputError(f"{path.name} must be a regular file")
    if path.stat().st_size > max_bytes:
        raise InputError(f"{path.name} is too large")
    try:
        text = path.read_text(encoding="utf-8")
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
                raise InputError(f"{path.name} uses YAML anchors or aliases, which are not rendered")
        docs = [d for d in yaml.safe_load_all(text) if d is not None]
    except (yaml.YAMLError, UnicodeDecodeError, RecursionError) as exc:
        raise InputError(f"{path.name} is not valid YAML") from exc
    if len(docs) != 1 or not isinstance(docs[0], dict):
        raise InputError(f"{path.name} must contain exactly one object")
    return docs[0]


def _images(node) -> list:
    """Every value under an `image` key, at any depth."""
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "image":
                found.append(value)
            found.extend(_images(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_images(item))
    return found


def load_binding(path, repo: str) -> dict:
    """The app's binding as the release fetched it from the factory: one YAML object, no anchors, a valid binding/v1 for exactly
    this repo. The release checks it here itself rather than trusting that factory-check ran on the factory's main (a factory can
    pin a builder older than bindings). The release fetched the file at bindings/<repo>.yaml for this exact name and factory-check
    holds a binding's repo to its path exactly, so a name that differs, even only in case, is refused rather than guessed at."""
    doc = _read_yaml(Path(path), MAX_BINDING_BYTES)
    errors = validate("binding.v1", doc)
    if errors:
        raise InputError(f"the binding is not a valid binding/v1: {errors[0]}"[:300])
    if doc["repo"] != repo:
        raise InputError(f"the binding is for {doc['repo']}, not {repo}")
    return doc


def _refuse_workload_identity_keys(objects: list) -> None:
    """Only the platform's binding switches the workload identity on, so deploy/base may carry no azure.workload.identity/ key
    in the ServiceAccount's or the pod template's labels or annotations, with or without a binding. Reads only."""
    template = objects[1]["spec"]["template"]
    metadata = template.get("metadata", {}) if isinstance(template, dict) else None
    if not isinstance(metadata, dict):
        raise InputError("the Deployment's pod template metadata must be a mapping")
    for meta, what in ((objects[0]["metadata"], "the ServiceAccount's"), (metadata, "the Deployment's pod template")):
        for key in ("annotations", "labels"):
            if key not in meta:
                continue
            if not isinstance(meta[key], dict):
                raise InputError(f"{what} metadata.{key} must be a mapping")
            taken = sorted(k for k in meta[key] if isinstance(k, str) and k.startswith(WORKLOAD_IDENTITY_PREFIX))
            if taken:
                raise InputError(f"{what} {key} carry {', '.join(taken)}, which only the platform's binding may set")


def _apply_binding(objects: list, binding) -> None:
    """Add the binding's workload identity, env and egress to the checked objects. The Azure Workload Identity webhook on the
    cluster injects the projected token; the platform adds only the annotation, the label and plain values. Egress uses the
    builder's own FQDN rule shape for declared egress, after the rules it generated (the DNS rule toFQDNs needs among them)."""
    _refuse_workload_identity_keys(objects)
    fx = effects(binding)
    if fx["client_id"] is None:
        return
    service_account, deployment, cilium = objects[0], objects[1], objects[4]
    service_account["metadata"].setdefault("annotations", {})[WORKLOAD_IDENTITY_ANNOTATION] = fx["client_id"]
    deployment["spec"]["template"].setdefault("metadata", {}).setdefault("labels", {})[WORKLOAD_IDENTITY_LABEL] = "true"
    env = deployment["spec"]["template"]["spec"]["containers"][0].setdefault("env", [])
    if not isinstance(env, list):
        raise InputError("the app container's env is not a list")
    taken = {e.get("name") for e in env if isinstance(e, dict)} & {name for name, _ in fx["env"]}
    if taken:
        raise InputError(f"the Deployment already sets {', '.join(sorted(taken))}, which only the platform's binding may set")
    env.extend({"name": name, "value": value} for name, value in fx["env"])
    spec = cilium.get("spec")
    rules = spec.setdefault("egress", []) if isinstance(spec, dict) else None
    if not isinstance(rules, list):
        raise InputError("the CiliumNetworkPolicy has no egress list")
    for host in fx["egress"]:
        rule = {"toFQDNs": [{"matchName": host}], "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}]}
        if rule not in rules:
            rules.append(rule)


def render(root, image_ref: str, app_name: str, binding=None) -> str:
    if not isinstance(image_ref, str) or not IMAGE_REF.fullmatch(image_ref):
        raise InputError("the image must be a registry reference pinned by sha256 digest")
    deploy = Path(root) / "deploy"
    if not (deploy / "base").is_dir():
        raise InputError("deploy/base not found; run builder generate")
    if deploy.is_symlink() or not deploy.is_dir() or sorted(p.name for p in deploy.iterdir()) != ["base"]:
        raise InputError("deploy/ must contain only the generated base; overlays are not rendered")
    base = deploy / "base"
    if base.is_symlink() or not base.is_dir() or sorted(p.name for p in base.iterdir()) != sorted([*RESOURCES, "kustomization.yaml"]):
        raise InputError("deploy/base must contain exactly the builder's generated files")
    if _read_yaml(base / "kustomization.yaml") != KUSTOMIZATION:
        raise InputError("deploy/base/kustomization.yaml is not the builder's")
    objects = [_read_yaml(base / name) for name in RESOURCES]
    if [o.get("kind") for o in objects] != KINDS:
        raise InputError("deploy/base must contain exactly the builder's kinds, one per file")
    if any(not isinstance(o.get("metadata"), dict) or "namespace" in o["metadata"] for o in objects):
        raise InputError("deploy/base objects must not set a namespace")
    deployment = objects[1]
    try:
        pod = deployment["spec"]["template"]["spec"]
        containers = pod.get("containers", [])
        valid = (not pod.get("initContainers") and not pod.get("ephemeralContainers") and isinstance(containers, list)
                 and len(containers) == 1 and containers[0].get("name") == "app" and containers[0].get("image") == app_name)
    except (KeyError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise InputError("the Deployment must have exactly one container named app using the app's image")
    containers[0]["image"] = image_ref
    _apply_binding(objects, binding)
    if _images(objects) != [image_ref]:
        raise InputError("the manifests must name no image other than the app's")
    header = f"# Rendered by golden-road from deploy/base, image pinned to {image_ref}\n"
    return header + "---\n".join(yaml.dump(o, Dumper=_NoAliasDumper, sort_keys=False, default_flow_style=False) for o in objects)

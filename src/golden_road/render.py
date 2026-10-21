"""Deploy-ready manifests: the builder's deploy/base, with the app image pinned to the signed digest.

Equivalent to `kustomize build` on a base that only lists resources, done in Python so the pipeline
needs no extra binary. Anything that isn't exactly the builder's layout is refused, and the output is
dumped from the objects that were checked, so the only image it can name is the digest it was given.
"""

import re
from pathlib import Path

import yaml

from builder.render.k8s import RESOURCES

from .io import InputError

IMAGE_REF = re.compile(r"^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$")
KUSTOMIZATION = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "resources": RESOURCES}
KINDS = ["ServiceAccount", "Deployment", "Service", "NetworkPolicy", "CiliumNetworkPolicy"]
MAX_FILE_BYTES = 1_000_000


class _NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def _read_yaml(path: Path):
    if path.is_symlink() or not path.is_file():
        raise InputError(f"{path.name} must be a regular file")
    if path.stat().st_size > MAX_FILE_BYTES:
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


def render(root, image_ref: str, app_name: str) -> str:
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
    if _images(objects) != [image_ref]:
        raise InputError("the manifests must name no image other than the app's")
    header = f"# Rendered by golden-road from deploy/base, image pinned to {image_ref}\n"
    return header + "---\n".join(yaml.dump(o, Dumper=_NoAliasDumper, sort_keys=False, default_flow_style=False) for o in objects)

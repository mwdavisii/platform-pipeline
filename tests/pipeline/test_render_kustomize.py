import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from golden_road.render import render

ROOT = Path(__file__).resolve().parents[2]
REF = "ghcr.io/org/notes-api@sha256:" + "b" * 64
pytestmark = pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl not installed")


def key(o):
    return (o["kind"], o["metadata"]["name"])


def test_render_matches_kustomize_build(tmp_path):
    shutil.copytree(ROOT / "tests/builder/golden/python-fastapi/deploy/base", tmp_path / "base")
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "kustomization.yaml").write_text(yaml.safe_dump({
        "resources": ["../base"],
        "images": [{"name": "notes-api", "newName": "ghcr.io/org/notes-api", "digest": "sha256:" + "b" * 64}],
    }))
    kustomized = subprocess.run(["kubectl", "kustomize", str(overlay)], check=True, capture_output=True, text=True).stdout
    ours = render(ROOT / "tests/builder/golden/python-fastapi", REF, "notes-api")
    a = sorted((d for d in yaml.safe_load_all(kustomized) if d), key=key)
    b = sorted((d for d in yaml.safe_load_all(ours) if d), key=key)
    assert a == b

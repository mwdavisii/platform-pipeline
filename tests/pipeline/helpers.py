"""Test helpers that rebuild the committed placeholder state from a possibly configured tree."""

import json
import os
import re
import sys
from pathlib import Path


def unconfigure_platform(platform_dir):
    """Reset platform/platform.json to its committed placeholder state, whatever the live tree has been configured to."""
    path = Path(platform_dir) / "platform.json"
    doc = json.loads(path.read_text())
    doc.pop("org", None)
    doc["model_gateway_hosts"] = ["llm.replace-me.invalid"]
    doc["edge_ingress"] = {"namespace": "replace-me", "pod_labels": {"app": "replace-me"}}
    doc["identity_header"] = "X-replace-me"
    path.write_text(json.dumps(doc, indent=2) + "\n")


def unconfigure_hello_app(app_dir):
    """Reset the hello-app caller workflows and spec egress to their placeholder state."""
    app = Path(app_dir)
    for name in ("ci.yml", "promote.yml"):
        path = app / ".github/workflows" / name
        path.write_text(re.sub(r"uses: [A-Za-z0-9-]+/platform-pipeline", "uses: ORG_PLACEHOLDER/platform-pipeline", path.read_text()))
    spec_path = app / "builder-spec.json"
    spec = json.loads(spec_path.read_text())
    spec["egress"][0]["host"] = "llm.replace-me.invalid"
    spec_path.write_text(json.dumps(spec, indent=2) + "\n")


def builder_cmd():
    """Run the builder CLI with the interpreter running the tests, so it works without a console-script install."""
    return [sys.executable, "-m", "builder.cli"]


def builder_env(**extra):
    """An environment in which `builder_cmd()` imports the same packages the tests import."""
    return {"PYTHONPATH": os.pathsep.join(p for p in sys.path if p), **extra}

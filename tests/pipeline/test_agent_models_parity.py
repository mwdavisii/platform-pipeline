"""The portal's model list is the platform's (decided while planning: item 5): the portal can't read platform.json."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_portal_s_models_are_platform_json_s():
    portal = json.loads((ROOT / "bootstrap/golden-road-portal/portal/agent_models.json").read_text())
    assert portal["schema"] == "agent-models/v1"
    for rel in ("platform/platform.json", "src/builder/data/platform.json"):
        assert portal["models"] == json.loads((ROOT / rel).read_text())["agent_models"], rel

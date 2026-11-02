"""The 0.6.0 defaults and readers that live twice, kept equal (final review of 0.6.0): the placeholder gateways in the portal, in
configure.py and in the unconfigured platform.json; and the skills catalog fixture read by the portal and by golden_road."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bootstrap/golden-road-portal"))

from golden_road.skills_catalog import MockCatalog  # noqa: E402
from portal.settings import DEFAULT_GATEWAYS  # noqa: E402
from portal.skills_catalog import load_mock  # noqa: E402


def unconfigured_platform() -> dict:
    """platform/platform.json as committed (configure.py rewrites the working copy of a configured tree, never its commit)."""
    out = subprocess.run(["git", "-C", str(ROOT), "show", "HEAD:platform/platform.json"], capture_output=True, text=True)
    return json.loads(out.stdout if out.returncode == 0 else (ROOT / "platform/platform.json").read_text())


def test_the_placeholder_gateways_agree():
    spec = importlib.util.spec_from_file_location("configure_under_test", ROOT / "scripts/configure.py")
    configure = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(configure)
    assert DEFAULT_GATEWAYS == configure.MCP_GATEWAY_PLACEHOLDERS == unconfigured_platform()["mcp_gateways"]


def test_both_catalog_readers_give_the_same_skills_for_every_name_and_commit():
    fixture = json.loads((ROOT / "src/golden_road/data/mock_skills_catalog.json").read_text())
    ours, portal = MockCatalog.load(), load_mock()
    pairs = [(name, commit) for commit, tree in fixture["commits"].items() for name in tree["skills"]]
    assert len(pairs) >= 4
    for name, commit in pairs:
        meta, files = ours.skill(name, commit)
        skill = portal.skill(name, commit)
        assert skill is not None, (name, commit)
        assert (list(skill.environments), skill.files) == (list(meta["environments"]), files), (name, commit)
        assert "skill.yaml" not in files

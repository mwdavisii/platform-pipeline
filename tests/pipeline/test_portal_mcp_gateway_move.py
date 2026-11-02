"""When the platform moves an environment's MCP gateway (platform.json, and the portal's PORTAL_MCP_GATEWAY_*), a binding still naming
the old one fails deploy-check. The portal clears it: one confirmed Remove drops every server on the old gateway and records the new
one, and that binding passes deploy-verify's gateway check against a platform.json naming the new host."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bootstrap/golden-road-portal"))
sys.path.insert(0, str(ROOT / "bootstrap/golden-road-portal/tests"))

from golden_road import deploy  # noqa: E402
from golden_road.io import InputError  # noqa: E402
from portal_fakes import T0, fake_directory  # noqa: E402

from portal.binding_record import next_binding  # noqa: E402

NEW = {"test": "mcp-new.example.com", "prod": "mcp.example.com"}
COMMON = dict(repo="acme/notes", org="acme", directory=fake_directory(), person_id="p_" + "a" * 32, request_id="r_" + "b" * 32, now=T0,
              environment="test")
OLD_BINDING = {"schema": "binding/v2", "repo": "acme/notes", "environment": "test", "secrets": [],
               "mcp": {"gateway": "mcp-test.example.com", "servers": [{"server_id": "notes", "name": "Notes", "verified_by": "mock-cloudflare"},
                                                                       {"server_id": "wiki", "name": "Wiki", "verified_by": "mock-cloudflare"}]},
               "linked_by": "p_" + "a" * 32, "linked_at": "2026-10-23T12:00:00Z", "request": "r_" + "c" * 32}


@pytest.fixture(autouse=True)
def moved_platform(monkeypatch):
    monkeypatch.setattr(deploy, "_platform_gateways", lambda: dict(NEW))


def test_the_old_binding_fails_and_the_portal_s_removal_passes():
    with pytest.raises(InputError, match="mcp gateway is mcp-test.example.com"):
        deploy._mcp_problems(OLD_BINDING)
    doc = next_binding(current=OLD_BINDING, **COMMON, remove_mcp="notes", mcp_gateway=NEW["test"])
    assert doc["mcp"] == {"gateway": "mcp-new.example.com", "servers": []}   # both servers, in one request
    deploy._mcp_problems(doc)

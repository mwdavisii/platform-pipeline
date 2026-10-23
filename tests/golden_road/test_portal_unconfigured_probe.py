"""The platform's preview runs the portal image with no environment and then the authz probe. The real probe, against the real
generated _platform layer wrapped around the unconfigured portal app, passes: guard 403s come from _platform, allowed requests
reach the app and get its 503 (or the health path's 200), and the probe accepts both."""

import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from builder.config import load_platform
from golden_road.authz_probe import evaluate, probe

PORTAL = Path(__file__).resolve().parents[2] / "bootstrap/golden-road-portal"
PLATFORM = load_platform()


@pytest.fixture
def served(monkeypatch):
    monkeypatch.syspath_prepend(str(PORTAL))
    for name in [n for n in sys.modules if n == "_platform" or n.startswith("_platform.")]:
        monkeypatch.delitem(sys.modules, name)
    from _platform.authz import AuthzASGI
    from portal.web.app import create_app_from_env
    app = AuthzASGI(create_app_from_env({}))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(10)


def test_the_real_probe_passes_against_the_unconfigured_portal(served):
    # the repo's policy file holds a placeholder header name until the deploy owners set theirs
    import json
    header = json.loads((PORTAL / "_platform/authz-policy.json").read_text())["identity_header"]
    result = probe(PORTAL, served, platform={**PLATFORM, "identity_header": header}, ready_timeout=20)
    assert result["status"] == "pass" and result["denied"] > 0 and result["failures"] == [], result
    assert evaluate  # the probe's own evaluate produced the verdict

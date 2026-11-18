"""builder-spec/v1 for an agent (spec 2026-11-17 "Builder and platform changes"): its fixed fields and its agent.yaml."""

import json
import shutil
from pathlib import Path

import pytest

from builder.config import load_platform
from builder.spec import check_spec

FIXTURE = Path(__file__).parent / "fixtures/agent"
PLATFORM = load_platform()


@pytest.fixture
def agent(tmp_path):
    root = tmp_path / "agent-calendar"
    shutil.copytree(FIXTURE, root)
    return root, json.loads((root / "builder-spec.json").read_text())


def test_the_fixture_passes(agent):
    root, spec = agent
    assert check_spec(spec, root, PLATFORM) == ([], [], [])


@pytest.mark.parametrize("change, words", [
    ({"entrypoint": "main.py"}, "an agent's entrypoint is agent.yaml"),
    ({"port": 8000}, "an agent listens on port 8080"),
    ({"health_path": "/"}, "an agent's health_path is /healthz"),
    ({"egress": [{"host": "api.example.org", "port": 443, "reason": "weather data"}]}, "an agent has no egress of its own"),
    ({"env": [{"name": "WEATHER_KEY", "secret": True}]}, "an agent has no env of its own"),
    ({"server": "uvicorn"}, "server only applies to python apps"),
    ({"authz": {"roles": [{"name": "r", "access": "read"}], "default": "deny", "rules": [{"methods": ["GET"], "path": "/", "roles": ["r"]}]}},
     "authz does not apply to an agent"),
])
def test_an_agent_s_fields_are_fixed(agent, change, words):
    root, spec = agent
    errors, _, _ = check_spec({**spec, **change}, root, PLATFORM)
    assert any(words in e for e in errors), errors


def test_a_missing_health_path_is_refused(agent):
    root, spec = agent
    spec.pop("health_path")
    assert any("health_path is /healthz" in e for e in check_spec(spec, root, PLATFORM)[0])


def test_agent_yaml_is_validated_with_the_platform_s_models(agent):
    root, spec = agent
    (root / "agent.yaml").write_text((root / "agent.yaml").read_text().replace("chat-default", "other-model"))
    errors, _, _ = check_spec(spec, root, PLATFORM)
    assert errors == ["agent.yaml: model is not one of the platform's models (chat-default)"]


def test_a_missing_or_linked_agent_yaml_is_refused(agent, tmp_path):
    root, spec = agent
    (root / "agent.yaml").unlink()
    assert check_spec(spec, root, PLATFORM)[0] == ["agent.yaml not found (it must be a regular file at the repo root)"]
    (tmp_path / "elsewhere.yaml").write_text((FIXTURE / "agent.yaml").read_text())
    (root / "agent.yaml").symlink_to(tmp_path / "elsewhere.yaml")
    assert check_spec(spec, root, PLATFORM)[0] == ["agent.yaml not found (it must be a regular file at the repo root)"]


def test_a_platform_without_the_runtime_refuses_every_agent(agent):
    root, spec = agent
    platform = {k: v for k, v in PLATFORM.items() if k != "images"}
    assert any("names no agent runtime (images.agent.runtime)" in e for e in check_spec(spec, root, platform)[0])


def test_the_reserved_agent_prefix_is_refused_for_any_app(notes_spec, python_app):
    notes_spec["env"] = [{"name": "AGENT_GATEWAY_KEY", "secret": True}]
    assert any("env name AGENT_GATEWAY_KEY is reserved" in e for e in check_spec(notes_spec, python_app, PLATFORM)[0])


@pytest.mark.parametrize("raw", [b"? [a]\n: x\n", b"? {a: 1}\n: x\n"])
def test_a_key_that_isn_t_text_is_a_clean_error(agent, raw):
    root, spec = agent
    (root / "agent.yaml").write_bytes(raw)
    assert check_spec(spec, root, PLATFORM)[0] == ["agent.yaml has a key that isn't text"]


def test_agent_yaml_is_read_only_up_to_its_limit(agent, monkeypatch):
    from contracts.agent_file import MAX_BYTES
    root, spec = agent
    (root / "agent.yaml").write_bytes(b"#" * (MAX_BYTES * 64))
    reads, real_open = [], Path.open

    class Counted:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()

        def read(self, size=-1):
            reads.append(size)
            return self.handle.read(size)

    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail(f"read_bytes reads all of {self.name}"))
    monkeypatch.setattr(Path, "open", lambda self, *a, **k: Counted(real_open(self, *a, **k)) if self.name == "agent.yaml" else real_open(self, *a, **k))
    assert check_spec(spec, root, PLATFORM)[0] == ["agent.yaml is larger than 16 KiB"]
    assert reads == [MAX_BYTES + 1]


@pytest.mark.parametrize("hosts", [[], ["Not A Host"], "llm.example.org", [1]])
def test_an_agent_needs_a_model_gateway_host(agent, hosts):
    root, spec = agent
    errors = check_spec(spec, root, {**PLATFORM, "model_gateway_hosts": hosts})[0]
    assert errors == ["model_gateway_hosts in the platform config must name the model gateway's host first (an agent's model calls go there)"]

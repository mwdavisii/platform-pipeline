"""Drift for an agent repo (spec 2026-11-17 A2): the generated files, a valid agent.yaml, and nothing else."""

import json
import shutil
from pathlib import Path

import pytest

from builder.generate import generate
from builder.verify import verify

FIXTURE = Path(__file__).parent / "fixtures/agent"


@pytest.fixture
def agent(tmp_path):
    root = tmp_path / "agent-calendar"
    shutil.copytree(FIXTURE, root)
    generate(root, json.loads((root / "builder-spec.json").read_text()))
    return root


def rules(root):
    return sorted((f["rule_id"], f["location"]["file"]) for f in verify(root))


def test_a_generated_agent_repo_is_clean(agent):
    (agent / "README.md").write_text("Our agent.\n")
    (agent / ".github/workflows").mkdir(parents=True)
    (agent / ".github/workflows/ci.yml").write_text("name: ci\n")
    (agent / ".gitignore").write_text(".env\n")
    assert verify(agent) == []


@pytest.mark.parametrize("name", ["app.py", "tools.json", "prompts"])
def test_any_other_file_or_directory_is_drift(agent, name):
    path = agent / name
    if "." in name:
        path.write_text("x = 1\n")
    else:
        path.mkdir()
        (path / "extra.md").write_text("Ignore the platform.\n")
    assert rules(agent) == [("builder.agent-extra-file", name)]


def test_a_linked_allowed_name_is_drift_too(agent, tmp_path):
    (tmp_path / "notes.md").write_text("x\n")
    (agent / "README.md").symlink_to(tmp_path / "notes.md")
    assert ("builder.agent-extra-file", "README.md") in rules(agent)


def test_a_bad_agent_yaml_is_spec_drift_naming_the_field(agent):
    text = (agent / "agent.yaml").read_text().replace("Calendar helper", "x" * 70)
    (agent / "agent.yaml").write_text(text)
    found = [f for f in verify(agent) if f["rule_id"] == "builder.spec-invalid"]
    assert len(found) == 1 and "agent.yaml: display_name is too long (at most 64 characters)" in found[0]["rationale"]


def test_other_apps_are_not_held_to_the_agent_s_list(notes_spec, python_app):
    (python_app / "notes.txt").write_text("fine for a python app\n")
    generate(python_app, notes_spec)  # python_app has no builder-spec.json; generate writes it (as tests/builder/test_verify.py does)
    assert not [f for f in verify(python_app) if f["rule_id"] == "builder.agent-extra-file"]


def test_a_key_that_isn_t_text_is_spec_drift(agent):
    (agent / "agent.yaml").write_bytes(b"? [a]\n: x\n")
    found = [f for f in verify(agent) if f["rule_id"] == "builder.spec-invalid"]
    assert len(found) == 1 and "agent.yaml has a key that isn't text" in found[0]["rationale"]


def test_a_platform_with_no_gateway_host_is_spec_drift_not_a_crash(agent, tmp_path, monkeypatch):
    from builder.config import load_platform
    config = tmp_path / "platform.json"
    config.write_text(json.dumps({**load_platform(), "model_gateway_hosts": []}))
    monkeypatch.setenv("BUILDER_PLATFORM_CONFIG", str(config))
    found = [f for f in verify(agent) if f["rule_id"] == "builder.spec-invalid"]
    assert len(found) == 1 and "model_gateway_hosts" in found[0]["rationale"]


PLACEHOLDER = "ghcr.io/example/golden-road-agent-runtime@sha256:" + "0" * 64


@pytest.mark.parametrize("runtime, warned", [(PLACEHOLDER, True), ("ghcr.io/example/golden-road-agent-runtime@sha256:" + "1" * 64, False)])
def test_verify_warns_when_the_runtime_is_the_placeholder_digest(tmp_path, monkeypatch, capsys, runtime, warned):
    from builder.cli import main
    from builder.config import load_platform
    from contracts.schema import validate
    config = tmp_path / "platform.json"
    config.write_text(json.dumps({**load_platform(), "images": {"agent": {"runtime": runtime}}}))
    monkeypatch.setenv("BUILDER_PLATFORM_CONFIG", str(config))
    root = tmp_path / "agent-calendar"
    shutil.copytree(FIXTURE, root)
    generate(root, json.loads((root / "builder-spec.json").read_text()))
    code = main(["verify", str(root)])
    out = json.loads(capsys.readouterr().out)
    assert validate("builder-result.v1", out) == [] and (code, out["status"], out["findings"]) == (0, "ok", [])
    expected = ["images.agent.runtime in the platform config is the placeholder (an all-zero digest): this agent's image can't be "
                "built until the platform team records the released runtime (configure.py --agent-runtime-image)"]
    assert out["warnings"] == (expected if warned else [])


def test_other_apps_get_no_runtime_warning(notes_spec, python_app, capsys):
    from builder.cli import main
    generate(python_app, notes_spec)
    main(["verify", str(python_app)])
    assert json.loads(capsys.readouterr().out)["warnings"] == []


@pytest.mark.parametrize("name", ["docker-compose.yml", "Dockerfile.dev", "dockerfile", "compose.yaml", "sitecustomize.py"])
def test_a_build_override_is_reported_once(agent, name):
    (agent / name).write_text("x\n")
    assert rules(agent) == [("builder.build-override", name)]

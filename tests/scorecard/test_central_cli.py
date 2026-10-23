"""scorecard poll: the command the factory's poller runs. Off unless the factory config has `central`."""
import json
import shutil
from pathlib import Path

import pytest
import yaml

from scorecard.cli import main

FACTORY = Path(__file__).resolve().parents[2] / "bootstrap/scorecard-factory"
URL = "https://github.com/o/scorecard-factory/actions/runs/1"


def args(factory, out, **over):
    values = {"--factory": str(factory), "--factory-repo": "o/scorecard-factory", "--workflow": "scorecard-assess.yml", "--ref": "main",
              "--run-url": URL, "--out": str(out), **over}
    return ["poll", *[x for kv in values.items() for x in kv]]


@pytest.fixture
def central_factory(tmp_path):
    shutil.copytree(FACTORY / "config", tmp_path / "f/config")
    path = tmp_path / "f/config/scorecard.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["central"] = {"max_in_flight": 4, "assess_timeout_minutes": 360}
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return tmp_path / "f"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("SCORECARD_APP_ID", "GOLDEN_ROAD_APP_KEY", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def test_poll_is_inert_without_central_settings(tmp_path, capsys):
    out = tmp_path / "poll.json"
    assert main(args(FACTORY, out)) == 0
    assert json.loads(out.read_text()) == {"central": "off"}
    assert json.loads(capsys.readouterr().out) == {"central": "off"}


def test_poll_needs_the_app_id_and_key_when_central_is_on(central_factory, tmp_path, monkeypatch, capsys):
    out = tmp_path / "poll.json"
    assert main(args(central_factory, out)) == 1 and not out.exists()
    assert "SCORECARD_APP_ID" in json.loads(capsys.readouterr().err)["error"]
    monkeypatch.setenv("SCORECARD_APP_ID", "abc")
    assert main(args(central_factory, out)) == 1 and not out.exists()
    monkeypatch.setenv("SCORECARD_APP_ID", "123")
    assert main(args(central_factory, out)) == 1 and not out.exists()
    assert "GOLDEN_ROAD_APP_KEY" in capsys.readouterr().err


def test_poll_exits_1_when_any_repo_errored(central_factory, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SCORECARD_APP_ID", "123")
    monkeypatch.setenv("GOLDEN_ROAD_APP_KEY", "k")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    seen = {}

    def fake(factory, **kw):
        seen.update(kw)
        return {"central": "on", "in_flight": 0, "max_in_flight": 4, "actions": [], "errors": ["x"]}
    monkeypatch.setattr("scorecard.central.poll", fake)
    out = tmp_path / "poll.json"
    assert main(args(central_factory, out)) == 1
    assert json.loads(out.read_text())["errors"] == ["x"]
    assert seen["app_id"] == 123 and seen["factory_repo"] == "o/scorecard-factory" and seen["run_url"] == URL
    assert json.loads(capsys.readouterr().out) == {"dispatched": 0, "errors": 1}


def test_poll_exits_0_when_nothing_errored(central_factory, tmp_path, monkeypatch):
    monkeypatch.setenv("SCORECARD_APP_ID", "123")
    monkeypatch.setenv("GOLDEN_ROAD_APP_KEY", "k")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setattr("scorecard.central.poll", lambda factory, **kw: {"central": "on", "actions": [{"action": "dispatch"}], "errors": []})
    assert main(args(central_factory, tmp_path / "poll.json")) == 0


def test_poll_refuses_an_output_directory(central_factory, tmp_path, capsys):
    assert main(args(central_factory, tmp_path)) == 1
    assert "is a directory" in capsys.readouterr().err


@pytest.mark.parametrize("url", ["http://github.com/x", "https://user@github.com/x", "https://github.com/a b", "https://github.com/x?y=1",
                                 "javascript:alert(1)", "https://github.com"])
def test_poll_refuses_an_odd_run_url(central_factory, tmp_path, monkeypatch, url):
    monkeypatch.setenv("SCORECARD_APP_ID", "123")
    monkeypatch.setenv("GOLDEN_ROAD_APP_KEY", "k")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setattr("scorecard.central.poll", lambda factory, **kw: pytest.fail("polled"))
    out = tmp_path / "poll.json"
    assert main(args(central_factory, out, **{"--run-url": url})) == 1 and not out.exists()

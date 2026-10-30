"""golden-road deploy-pins: every environment's release.yaml as JSON, for the deploy repo's workflows (they read it with jq; the
runner has no YAML reader they can trust). It reads only, and refuses the whole tree when any release.yaml is not valid."""

import json

import yaml
from golden_road.cli import main
from test_deploy_dir import env_dir  # the helpers from test_deploy_dir.py


def test_every_pin_is_written_by_environment_path(tmp_path, app):
    env_dir(tmp_path, app); env_dir(tmp_path, app, environment="prod")
    out = tmp_path / "pins.json"
    assert main(["deploy-pins", "--root", str(tmp_path), "--output", str(out)]) == 0
    pins = json.loads(out.read_text())
    assert sorted(pins) == ["apps/acme/notes/prod", "apps/acme/notes/test"]
    want = yaml.safe_load((tmp_path / "apps/acme/notes/test/release.yaml").read_text())
    assert pins["apps/acme/notes/test"] == want and pins["apps/acme/notes/prod"]["release"].startswith("prod-")


def test_an_empty_repo_has_no_pins(tmp_path):
    (tmp_path / "apps").mkdir(); (tmp_path / "apps/.gitkeep").write_text("")
    assert main(["deploy-pins", "--root", str(tmp_path), "--output", str(tmp_path / "pins.json")]) == 0
    assert json.loads((tmp_path / "pins.json").read_text()) == {}


def test_an_invalid_release_refuses_the_tree_and_writes_nothing(tmp_path, app, capsys):
    d = env_dir(tmp_path, app)
    release = yaml.safe_load((d / "release.yaml").read_text())
    (d / "release.yaml").write_text(yaml.safe_dump({**release, "image": "not an image"}))
    out = tmp_path / "pins.json"
    out.write_text("{}")  # a stale result from an earlier run is removed, never left for a later step
    assert main(["deploy-pins", "--root", str(tmp_path), "--output", str(out)]) == 2
    assert not out.exists() and "apps/acme/notes/test" in json.loads(capsys.readouterr().err)["error"]


def test_an_environment_without_a_release_is_refused(tmp_path, app, capsys):
    d = env_dir(tmp_path, app)
    (d / "release.yaml").unlink()
    assert main(["deploy-pins", "--root", str(tmp_path), "--output", str(tmp_path / "pins.json")]) == 2
    assert "release.yaml" in json.loads(capsys.readouterr().err)["error"]


def test_a_stray_path_under_apps_is_refused(tmp_path, app):
    env_dir(tmp_path, app)
    (tmp_path / "apps/acme/notes/staging").mkdir()
    assert main(["deploy-pins", "--root", str(tmp_path), "--output", str(tmp_path / "pins.json")]) == 2

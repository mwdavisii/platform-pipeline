"""golden-road deploy-pin: what bump.yml writes for one environment from a published release's two assets: release.yaml (the tag,
the image from the gate record, both assets' sha256, and the namespace kept from the current pin or <repo>-<env> for a new one) and
base/manifests.yaml, byte for byte. golden-road compose then writes the generated files. Offline; nothing is fetched."""

import hashlib
import json

import pytest
import yaml
from golden_road.cli import main
from golden_road.record import build_gate_record
from golden_road.render import render

IMAGE = "ghcr.io/acme/notes-api@sha256:" + "b" * 64
TEST_TAG, PROD_TAG = "test-20261029132351-42fa829", "prod-20261029150000-42fa829"


@pytest.fixture
def assets(tmp_path, app):
    text = render(app, IMAGE, "notes-api")
    record = build_gate_record(repo="acme/notes", commit="42fa829" + "0" * 33, image=IMAGE,
                               builder_image="ghcr.io/acme/golden-road-builder@sha256:" + "e" * 64,
                               drift={"status": "pass", "counts": {"by_severity": {s: 0 for s in ("critical", "high", "medium", "low", "info")}, "by_layer": {}},
                                      "blocking": [], "overrides_applied": []},
                               checks={"passed": True, "required_checks": []}, authz={"status": "not-declared"}, created="2026-10-29T13:23:51Z",
                               release=TEST_TAG, manifests_sha256=hashlib.sha256(text.encode()).hexdigest())
    d = tmp_path / "in"
    d.mkdir()
    (d / "manifests.yaml").write_text(text)
    (d / "gate-record.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    root = tmp_path / "deploy"
    (root / "apps").mkdir(parents=True)
    return {"root": root, "dir": d, "text": text, "record": record}


def pin(assets, env="test", tag=TEST_TAG, repo="acme/notes"):
    return main(["deploy-pin", "--root", str(assets["root"]), f"apps/{repo}/{env}", "--release", tag,
                 "--manifests", str(assets["dir"] / "manifests.yaml"), "--gate-record", str(assets["dir"] / "gate-record.json")])


def release(assets, env="test", repo="acme/notes"):
    return yaml.safe_load((assets["root"] / f"apps/{repo}/{env}/release.yaml").read_text())


def test_a_new_environment_gets_its_pin_and_base(assets, capsys):
    assert pin(assets) == 0
    assert json.loads(capsys.readouterr().out) == {"written": ["apps/acme/notes/test/release.yaml", "apps/acme/notes/test/base/manifests.yaml"]}
    gate_bytes = (assets["dir"] / "gate-record.json").read_bytes()
    assert release(assets) == {"schema": "deploy-release/v1", "repo": "acme/notes", "environment": "test", "namespace": "notes-test",
                               "release": TEST_TAG, "image": IMAGE, "manifests_sha256": hashlib.sha256(assets["text"].encode()).hexdigest(),
                               "gate_record_sha256": hashlib.sha256(gate_bytes).hexdigest()}
    assert (assets["root"] / "apps/acme/notes/test/base/manifests.yaml").read_text() == assets["text"]


def test_the_written_pin_composes_and_verifies(assets, capsys):
    from test_deploy_dir import write_gitrepository
    write_gitrepository(assets["root"])
    assert pin(assets) == 0
    assert main(["compose", "--root", str(assets["root"]), "apps/acme/notes/test"]) == 0
    capsys.readouterr()
    assert main(["deploy-verify", "--root", str(assets["root"])]) == 0


def test_an_existing_namespace_is_kept(assets):
    assert pin(assets) == 0
    path = assets["root"] / "apps/acme/notes/test/release.yaml"
    path.write_text(yaml.safe_dump({**release(assets), "namespace": "chosen-by-hand-test"}))
    assert pin(assets) == 0 and release(assets)["namespace"] == "chosen-by-hand-test"


def test_prod_pins_the_promoted_tag_with_the_promoted_gate_record(assets):
    assert pin(assets, env="prod", tag=PROD_TAG) == 0
    assert release(assets, "prod")["release"] == PROD_TAG and release(assets, "prod")["namespace"] == "notes-prod"


@pytest.mark.parametrize("change,needle", [
    (lambda a: (a["dir"] / "manifests.yaml").write_text(a["text"] + "# swapped\n"), "manifests_sha256"),
    (lambda a: (a["dir"] / "gate-record.json").write_text(json.dumps({**a["record"], "status": "fail"})), "pass"),
    (lambda a: (a["dir"] / "gate-record.json").write_text(json.dumps({**a["record"], "repo": "acme/other"})), "acme/other"),
    (lambda a: (a["dir"] / "gate-record.json").write_text(json.dumps({**a["record"], "release": "test-20261029000000-42fa829"})), "test-20261029000000-42fa829"),
    (lambda a: (a["dir"] / "gate-record.json").write_text("{}"), "gate-record/v1"),
    (lambda a: (a["dir"] / "gate-record.json").write_text("not json"), "gate-record.json"),
], ids=["manifests-swapped", "failed-gate", "other-repo", "other-release", "not-a-gate-record", "not-json"])
def test_a_release_that_does_not_hold_together_is_refused_and_nothing_is_written(assets, capsys, change, needle):
    change(assets)
    assert pin(assets) == 2
    assert needle in json.loads(capsys.readouterr().err)["error"]
    assert not (assets["root"] / "apps/acme").exists()


@pytest.mark.parametrize("env,tag", [("test", PROD_TAG), ("prod", TEST_TAG), ("prod", "prod-20261029150000-1234567"), ("test", "test-1")])
def test_a_tag_that_is_not_this_environments_is_refused(assets, env, tag):
    assert pin(assets, env=env, tag=tag) == 2


def test_a_repo_name_with_no_valid_namespace_says_to_set_it_by_hand(assets, capsys):
    long = "acme/" + "n" * 70
    record = {**assets["record"], "repo": long}
    (assets["dir"] / "gate-record.json").write_text(json.dumps(record))
    assert pin(assets, repo=long) == 2
    assert "by hand" in json.loads(capsys.readouterr().err)["error"]


@pytest.mark.parametrize("bad", ["apps/acme/notes", "apps/acme/../x/test", "../apps/acme/notes/test", "apps/acme/notes/staging"])
def test_only_an_environment_path_is_accepted(assets, bad):
    assert main(["deploy-pin", "--root", str(assets["root"]), bad, "--release", TEST_TAG, "--manifests", str(assets["dir"] / "manifests.yaml"),
                 "--gate-record", str(assets["dir"] / "gate-record.json")]) == 2


def test_a_symlinked_environment_is_refused(assets, tmp_path):
    (assets["root"] / "apps/acme").mkdir()
    (tmp_path / "elsewhere").mkdir()
    (assets["root"] / "apps/acme/notes").symlink_to(tmp_path / "elsewhere")
    assert pin(assets) == 2 and list((tmp_path / "elsewhere").iterdir()) == []

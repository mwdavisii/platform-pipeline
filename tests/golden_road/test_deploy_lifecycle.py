import json
import shutil

import pytest
import yaml
from golden_road import deploy
from golden_road.cli import main
from golden_road.deploy import (LIFECYCLE, compose_environment, intake_decommissioned, load_lifecycle, load_release, plan_environment,
                                verify_environment, verify_tree)
from golden_road.io import InputError
from test_deploy_dir import binding_v2, env_dir

SUSPENDED = {"schema": "lifecycle/v1", "repo": "acme/notes", "environment": "test", "state": "suspended", "task": "SCTASK0010001",
             "reason": "Critical vulnerability; suspended until patched", "requested_by": "p_" + "a" * 32,
             "requested_at": "2026-11-02T22:00:00Z", "request": "r_" + "d" * 32}


def suspend(d, **change):
    doc = {**SUSPENDED, "environment": d.name, **change}
    (d / LIFECYCLE).write_text(yaml.safe_dump(doc, sort_keys=False))
    return doc


def replicas(d):
    return next(o for o in yaml.safe_load_all((d / "composed.yaml").read_text()) if o and o["kind"] == "Deployment")["spec"]["replicas"]


@pytest.mark.parametrize("binding", [False, True])
def test_a_suspended_environment_composes_to_zero_and_verifies(tmp_path, app, binding):
    d = env_dir(tmp_path, app, binding_v2() if binding else None)
    compose_environment(d)
    assert replicas(d) == 1
    suspend(d)
    assert any("composed.yaml differs" in p for p in verify_environment(d)), "a lifecycle.yaml without its recompose is drift"
    compose_environment(d)
    assert replicas(d) == 0 and verify_environment(d) == []
    (d / LIFECYCLE).unlink()
    assert any("composed.yaml differs" in p for p in verify_environment(d))
    compose_environment(d)
    assert replicas(d) == 1 and verify_environment(d) == []


def test_a_pin_of_a_suspended_environment_stays_at_zero(tmp_path, app):
    """Rule 5: bump recomposes with lifecycle.yaml (it calls plan_environment)."""
    d = env_dir(tmp_path, app)
    suspend(d)
    planned = plan_environment(d, strict_gateway=False)
    composed = planned[d / "composed.yaml"]
    assert next(o for o in yaml.safe_load_all(composed) if o and o["kind"] == "Deployment")["spec"]["replicas"] == 0


@pytest.mark.parametrize("change, match", [
    ({"repo": "acme/other"}, "lifecycle.yaml's repo is acme/other, not acme/notes"),
    ({"environment": "prod"}, "lifecycle.yaml's environment is prod, not test"),
    ({"schema": "lifecycle/v2"}, "lifecycle.yaml is not a valid lifecycle/v1"),
    ({"state": "active"}, "lifecycle.yaml is not a valid lifecycle/v1")])
def test_a_lifecycle_file_that_is_not_its_directory_s_is_refused(tmp_path, app, change, match):
    d = env_dir(tmp_path, app)
    suspend(d, **change)
    with pytest.raises(InputError, match=match):
        load_lifecycle(d, load_release(d))
    assert any(match in p for p in verify_environment(d))


def test_a_symlinked_or_duplicate_key_lifecycle_file_is_refused(tmp_path, app):
    d = env_dir(tmp_path, app)
    (d / LIFECYCLE).write_text("schema: lifecycle/v1\nschema: lifecycle/v1\n")
    assert any("duplicate key" in p for p in verify_environment(d))
    (d / LIFECYCLE).unlink()
    (tmp_path / "elsewhere.yaml").write_text(yaml.safe_dump(SUSPENDED))
    (d / LIFECYCLE).symlink_to(tmp_path / "elsewhere.yaml")
    assert any("symlink" in p for p in verify_environment(d))
    with pytest.raises(InputError, match="lifecycle.yaml is a symlink"):
        load_lifecycle(d, load_release(d))


def test_no_lifecycle_file_is_a_running_environment(tmp_path, app):
    d = env_dir(tmp_path, app)
    assert load_lifecycle(d, load_release(d)) is None


def test_removing_a_whole_app_with_its_adapters_passes(tmp_path, app):
    """L6: one PR deletes apps/<owner>/<repo>/ and both adapters of each environment; what is left verifies."""
    for env in ("test", "prod"):
        env_dir(tmp_path, app, environment=env)
        assert main(["compose", "--root", str(tmp_path), f"apps/acme/notes/{env}"]) == 0
    assert verify_tree(tmp_path)["problems"] == []
    shutil.rmtree(tmp_path / "apps/acme")
    (tmp_path / "apps/.gitkeep").write_text("")
    for env in ("test", "prod"):
        (tmp_path / f"clusters/home/flux/acme-notes-{env}.yaml").unlink()
        (tmp_path / f"clusters/office/argo/acme-notes-{env}.yaml").unlink()
    assert verify_tree(tmp_path) == {"environments": 0, "problems": [], "warnings": []}


def test_removing_the_app_but_leaving_an_adapter_is_refused(tmp_path, app):
    env_dir(tmp_path, app)
    main(["compose", "--root", str(tmp_path), "apps/acme/notes/test"])
    shutil.rmtree(tmp_path / "apps/acme")
    (tmp_path / "apps/.gitkeep").write_text("")
    (tmp_path / "clusters/office/argo/acme-notes-test.yaml").unlink()
    assert verify_tree(tmp_path)["problems"] == ["clusters/home/flux/acme-notes-test.yaml is an adapter for no environment in apps/; remove it"]


RECORD = "schema: intake/v2\nrepo: acme/notes\n"
GONE = {"at": "2026-11-02T22:30:00Z", "task": "SCTASK0010001", "by": "p_" + "b" * 32, "request": "r_" + "d" * 32}


def test_the_intake_record_s_decommissioned_key_is_read():
    assert intake_decommissioned(RECORD.encode(), "acme/notes") is None
    raw = (RECORD + yaml.safe_dump({"decommissioned": GONE})).encode()
    assert intake_decommissioned(raw, "acme/notes") == GONE


@pytest.mark.parametrize("raw, match", [(b"- a list\n", "not one mapping"), (b"schema: intake/v2\nrepo: acme/other\n", "is for acme/other"),
                                        (b"a: [unclosed\n", "not YAML"), (b"\xff\xfe", "not UTF-8"),
                                        (RECORD.encode() + b"decommissioned: yes\n", "decommissioned is not"),
                                        (RECORD.encode() + b"decommissioned: null\n", "decommissioned is not"),
                                        (RECORD.encode() + b"decommissioned:\n", "decommissioned is not"),
                                        (RECORD.encode() + b"g: &g {at: x}\ndecommissioned: *g\n", "anchors or aliases"),
                                        (RECORD.encode() + b"g: &g {at: x}\ndecommissioned: {<<: *g}\n", "anchors or aliases")])
def test_an_unreadable_record_fails_closed(raw, match):
    with pytest.raises(InputError, match=match):
        intake_decommissioned(raw, "acme/notes")


def test_deploy_intake_prints_the_key(tmp_path, capsys):
    path = tmp_path / "intake.yaml"
    path.write_text(RECORD + yaml.safe_dump({"decommissioned": GONE}))
    assert main(["deploy-intake", "--intake", str(path), "--repo", "acme/notes"]) == 0
    assert json.loads(capsys.readouterr().out) == {"repo": "acme/notes", "decommissioned": GONE}
    path.write_text(RECORD)
    assert main(["deploy-intake", "--intake", str(path), "--repo", "acme/notes"]) == 0
    assert json.loads(capsys.readouterr().out) == {"repo": "acme/notes", "decommissioned": None}
    assert main(["deploy-intake", "--intake", str(path), "--repo", "acme/other"]) == 2

import json
import os
import shutil

import pytest

from builder import VERSION
from builder.errors import EXIT_INVALID, EXIT_NEEDS_INPUT, BuilderError
from builder.generate import generate
from builder.lock import canonical_json, sha256_text


def tree(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_generate_writes_files_lock_and_spec(notes_spec, python_app):
    result = generate(python_app, notes_spec)
    assert ".builder.lock" in result["files"]
    for rel in ("Dockerfile", ".dockerignore", "deploy/base/deployment.yaml", "_platform/entry.py"):
        assert (python_app / rel).is_file(), rel
    lock = json.loads((python_app / ".builder.lock").read_text())
    assert lock["builder_version"] == VERSION
    assert lock["spec_sha256"] == sha256_text(canonical_json(notes_spec))
    assert lock["files"]["Dockerfile"] == sha256_text((python_app / "Dockerfile").read_text())
    assert ".builder.lock" not in lock["files"]
    assert json.loads((python_app / "builder-spec.json").read_text()) == notes_spec


def test_generate_is_deterministic(notes_spec, python_app, tmp_path):
    other = tmp_path / "copy"
    shutil.copytree(python_app, other)
    generate(python_app, notes_spec)
    generate(other, notes_spec)
    assert tree(python_app) == tree(other)
    first = tree(python_app)
    generate(python_app, notes_spec)
    assert tree(python_app) == first


def test_removing_authz_removes_platform_files(notes_spec, python_app):
    generate(python_app, notes_spec)
    del notes_spec["authz"]
    (python_app / "app.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    result = generate(python_app, notes_spec)
    assert "_platform/entry.py" in result["removed"]
    assert not (python_app / "_platform" / "entry.py").exists()


def test_stale_lock_cannot_delete_outside_root(notes_spec, python_app, tmp_path):
    outside = tmp_path / "victim.txt"
    outside.write_text("keep me")
    (python_app / ".builder.lock").write_text(json.dumps({"files": {"../victim.txt": "x"}}))
    generate(python_app, notes_spec)
    assert outside.read_text() == "keep me"


def test_invalid_spec_raises_exit_2(notes_spec, python_app):
    notes_spec["egress"] = [{"host": "api.openai.com", "port": 443, "reason": "model calls"}]
    with pytest.raises(BuilderError) as err:
        generate(python_app, notes_spec)
    assert err.value.exit_code == EXIT_INVALID
    assert not (python_app / "Dockerfile").exists()


def test_needs_input_raises_exit_3(notes_spec, python_app):
    (python_app / "requirements.txt").unlink()
    with pytest.raises(BuilderError) as err:
        generate(python_app, notes_spec)
    assert err.value.exit_code == EXIT_NEEDS_INPUT
    assert err.value.questions


def test_forged_lock_cannot_delete_app_files_or_dotdot(notes_spec, python_app):
    keep = python_app / "app.py"
    before = keep.read_text()
    (python_app / ".builder.lock").write_text(json.dumps({"files": {
        "app.py": "x", "deploy/base/../../app.py": "x", "requirements.txt": "x", "bad\nname": "x", "a\\b": "x"}}))
    result = generate(python_app, notes_spec)
    assert keep.read_text() == before and (python_app / "requirements.txt").is_file()
    assert result["removed"] == []


def test_stale_lock_symlink_is_left_alone(notes_spec, python_app):
    generate(python_app, notes_spec)
    link = python_app / "_platform" / "old.py"
    link.symlink_to(python_app / "app.py")
    lock_edit(python_app, lambda lock: lock["files"].__setitem__("_platform/old.py", "x"))
    generate(python_app, notes_spec)
    assert link.is_symlink() and (python_app / "app.py").is_file()


def lock_edit(root, mutate):
    lock = json.loads((root / ".builder.lock").read_text())
    mutate(lock)
    (root / ".builder.lock").write_text(json.dumps(lock))


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}


def test_platform_linked_to_root_is_refused_and_user_files_untouched(notes_spec, python_app):
    for name, text in {"__init__.py": "# user\n", "entry.py": "USER CODE\n", "settings.py": "USER SETTINGS\n"}.items():
        (python_app / name).write_text(text)
    os.symlink(".", python_app / "_platform")
    (python_app / ".builder.lock").write_text(json.dumps({"files": {"_platform/settings.py": "x", "_platform/app.py": "y"}}))
    before = snapshot(python_app)
    with pytest.raises(BuilderError) as err:
        generate(python_app, notes_spec)
    assert err.value.exit_code == EXIT_INVALID and err.value.errors
    assert snapshot(python_app) == before


def test_deploy_base_linked_into_source_is_refused(notes_spec, python_app):
    (python_app / "src").mkdir()
    (python_app / "src/service.yaml").write_text("user file\n")
    (python_app / "deploy").mkdir()
    os.symlink("../src", python_app / "deploy/base")
    before = snapshot(python_app)
    with pytest.raises(BuilderError) as err:
        generate(python_app, notes_spec)
    assert err.value.exit_code == EXIT_INVALID
    assert snapshot(python_app) == before
    assert not (python_app / "Dockerfile").exists()


def test_output_that_is_a_directory_is_refused_before_any_write(notes_spec, python_app):
    generate(python_app, notes_spec)
    (python_app / "deploy/base/service.yaml").unlink()
    (python_app / "deploy/base/service.yaml").mkdir()
    notes_spec["port"] = 9090
    with pytest.raises(BuilderError) as err:
        generate(python_app, notes_spec)
    assert err.value.exit_code == EXIT_INVALID
    assert "9090" not in (python_app / "deploy/base/deployment.yaml").read_text()


def test_stale_file_edited_since_generation_is_kept(notes_spec, python_app):
    generate(python_app, notes_spec)
    entry = python_app / "_platform/entry.py"
    entry.write_text(entry.read_text() + "# mine\n")
    del notes_spec["authz"]
    (python_app / "app.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    assert "_platform/entry.py" not in generate(python_app, notes_spec)["removed"]
    assert entry.is_file()


def test_stale_file_without_builder_header_or_unknown_name_is_kept(notes_spec, python_app):
    generate(python_app, notes_spec)
    mine = python_app / "_platform/authz.py"
    mine.write_text("print('mine')\n")
    other = python_app / "_platform/notes.txt"
    other.write_text("hello\n")
    lock_edit(python_app, lambda lock: lock["files"].update({
        "_platform/authz.py": sha256_text("print('mine')\n"), "_platform/notes.txt": sha256_text("hello\n")}))
    del notes_spec["authz"]
    (python_app / "app.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    assert generate(python_app, notes_spec)["removed"] and mine.is_file() and other.is_file()


def test_failure_midway_keeps_old_lock_and_reports_exit_2(notes_spec, python_app, monkeypatch):
    generate(python_app, notes_spec)
    old_lock = (python_app / ".builder.lock").read_bytes()
    notes_spec["port"] = 9090
    real, calls = os.replace, []

    def flaky(src, dst):
        calls.append(dst)
        if len(calls) == 3:
            raise OSError(28, "No space left on device")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(BuilderError) as err:
        generate(python_app, notes_spec)
    assert err.value.exit_code == EXIT_INVALID
    assert (python_app / ".builder.lock").read_bytes() == old_lock
    assert not [p for p in python_app.rglob(".builder-tmp-*")]


def test_stale_files_survive_a_failed_write(notes_spec, python_app, monkeypatch):
    generate(python_app, notes_spec)
    del notes_spec["authz"]
    (python_app / "app.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda s, d: (_ for _ in ()).throw(OSError(5, "io")) if str(d).endswith("deployment.yaml") else real(s, d))
    with pytest.raises(BuilderError):
        generate(python_app, notes_spec)
    assert (python_app / "_platform/entry.py").is_file()


def test_hardlinked_output_is_not_written_through(notes_spec, python_app, tmp_path):
    outside = tmp_path / "victim.txt"
    outside.write_text("keep me")
    os.link(outside, python_app / "Dockerfile")
    generate(python_app, notes_spec)
    assert outside.read_text() == "keep me"


def test_result_lists_spec_file(notes_spec, python_app):
    assert "builder-spec.json" in generate(python_app, notes_spec)["files"]


def test_symlinked_output_is_replaced_not_followed(notes_spec, python_app, tmp_path):
    outside = tmp_path / "victim.txt"
    outside.write_text("keep me")
    (python_app / "Dockerfile").symlink_to(outside)
    generate(python_app, notes_spec)
    assert outside.read_text() == "keep me"
    assert not (python_app / "Dockerfile").is_symlink()


@pytest.mark.parametrize("link", ["deploy", "_platform"])
def test_symlinked_output_directory_outside_root_is_refused(notes_spec, python_app, tmp_path, link):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (python_app / link).symlink_to(outside)
    with pytest.raises(BuilderError) as err:
        generate(python_app, notes_spec)
    assert err.value.exit_code in (EXIT_INVALID, EXIT_NEEDS_INPUT)
    assert list(outside.iterdir()) == []
    assert not (python_app / "Dockerfile").exists()


def test_missing_data_classification_raises_exit_2(notes_spec, python_app):
    del notes_spec["data_classification"]
    with pytest.raises(BuilderError) as err:
        generate(python_app, notes_spec)
    assert err.value.exit_code == EXIT_INVALID
    assert any("data_classification" in e for e in err.value.errors)
    assert not (python_app / "Dockerfile").exists()

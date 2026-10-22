"""write_callers.py writes the four caller workflows, with the app's name, into an app folder, and nothing else."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from golden_road import doctor

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "bootstrap/golden-road-agents/skill"
NAMES = list(doctor.CALLERS)
OWNER = re.compile(r"[A-Za-z0-9_-]+/(platform-pipeline|scorecard-factory|security-scans)/")


def configured(tmp_path, org="example-org"):
    """The skill folder as configure.py leaves it for org."""
    dest = tmp_path / "skill"
    shutil.copytree(SKILL, dest, ignore=shutil.ignore_patterns("__pycache__"))
    for path in (dest / "templates/workflows").iterdir():
        path.write_text(OWNER.sub(rf"{org}/\1/", path.read_text()))
    return dest


def write(skill, cwd, *args):
    out = subprocess.run([sys.executable, "-I", str(skill / "write_callers.py"), *args], cwd=cwd, capture_output=True, text=True)
    return out.returncode, json.loads(out.stdout)


@pytest.fixture
def app(tmp_path):
    folder = tmp_path / "app"
    folder.mkdir()
    (folder / "builder-spec.json").write_text(json.dumps({"app_name": "notes-api"}))
    return folder


def test_writes_the_four_callers_with_the_app_name_and_doctor_accepts_them(tmp_path, app):
    skill = configured(tmp_path)
    code, doc = write(skill, app)
    assert code == 0 and doc["written"] == NAMES and doc["unchanged"] == [] and doc["app_name"] == "notes-api"
    assert "app_name: notes-api" in (app / ".github/workflows/ci.yml").read_text()
    for name, template in doctor.CALLERS.items():
        parsed = yaml.safe_load((app / ".github/workflows" / name).read_text())
        assert any(job.get("uses") == template.format(org="example-org") for job in parsed["jobs"].values()), name
        assert (parsed[True] if True in parsed else parsed["on"]) == doctor.CALLER_TRIGGERS[name]
    assert b"\r\n" not in (app / ".github/workflows/ci.yml").read_bytes()


def test_a_second_run_changes_nothing_and_an_edited_caller_is_overwritten(tmp_path, app):
    skill = configured(tmp_path)
    write(skill, app)
    code, doc = write(skill, app, str(app))
    assert code == 0 and doc["written"] == [] and doc["unchanged"] == NAMES
    ci = app / ".github/workflows/ci.yml"
    ci.write_text(ci.read_text().replace("name: ci", "name: ci  # edited"))  # still the platform's: overwritten
    code, doc = write(skill, app)
    assert code == 0 and doc["written"] == ["ci.yml"] and "edited" not in ci.read_text()


@pytest.mark.parametrize("name", NAMES)
def test_refuses_the_users_own_workflow_with_a_platform_name_and_writes_nothing(tmp_path, app, name):
    skill = configured(tmp_path)
    (app / ".github/workflows").mkdir(parents=True)
    (app / ".github/workflows" / name).write_text("name: mine\non: push\njobs: {}\n")
    code, doc = write(skill, app)
    assert code == 3 and doc["status"] == "error"
    assert f"rename your workflow {name}; the platform owns this name" in doc["message"]
    assert sorted(p.name for p in (app / ".github/workflows").iterdir()) == [name]
    assert (app / ".github/workflows" / name).read_text() == "name: mine\non: push\njobs: {}\n"


def test_a_caller_that_names_the_wrong_platform_repo_is_not_the_platforms(tmp_path, app):
    skill = configured(tmp_path)
    write(skill, app)
    ci = app / ".github/workflows/ci.yml"
    ci.write_text("jobs:\n  x:\n    uses: example-org/scorecard-factory/.github/workflows/scorecard.yml@main\n")
    assert write(skill, app)[0] == 3


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read and write anything")
def test_an_unreadable_or_unwritable_target_exits_3_with_json(tmp_path, app):
    skill = configured(tmp_path)
    write(skill, app)
    ci = app / ".github/workflows/ci.yml"
    ci.write_text(ci.read_text() + "# edit\n")
    ci.chmod(0)
    try:
        code, doc = write(skill, app)
    finally:
        ci.chmod(0o644)
    assert code == 3 and doc["status"] == "error" and doc["fix"]
    (app / ".github/workflows/ci.yml").write_text("# platform-pipeline edit\n")
    (app / ".github/workflows").chmod(0o555)
    try:
        code, doc = write(skill, app)
    finally:
        (app / ".github/workflows").chmod(0o755)
    assert code == 3 and doc["status"] == "error" and doc["fix"]


@pytest.mark.parametrize("spec", ['{"app_name": "Bad Name"}', '{"app_name": "x\\nmore"}', '{"app_name": 7}', "[]", "not json", None],
                         ids=["bad-name", "newline", "not-a-string", "list", "not-json", "missing"])
def test_refuses_a_missing_or_bad_spec(tmp_path, app, spec):
    skill = configured(tmp_path)
    if spec is None:
        (app / "builder-spec.json").unlink()
    else:
        (app / "builder-spec.json").write_text(spec)
    code, doc = write(skill, app)
    assert code == 3 and doc["status"] == "error" and not (app / ".github").exists()


def test_refuses_a_linked_folder_or_caller_before_writing_anything(tmp_path, app):
    skill = configured(tmp_path)
    (tmp_path / "elsewhere").mkdir()
    (app / ".github").symlink_to(tmp_path / "elsewhere")
    assert write(skill, app)[0] == 3 and not any((tmp_path / "elsewhere").iterdir())
    (app / ".github").unlink()
    (app / ".github/workflows").mkdir(parents=True)
    (app / ".github/workflows/scorecard.yml").symlink_to(tmp_path / "target.yml")
    assert write(skill, app)[0] == 3
    assert not (tmp_path / "target.yml").exists() and not (app / ".github/workflows/ci.yml").exists()


def test_refuses_unconfigured_templates(tmp_path, app):
    skill = configured(tmp_path, org="ORG_" + "PLACEHOLDER")
    code, doc = write(skill, app)
    assert code == 3 and "configure.py" in doc["fix"] and not (app / ".github").exists()


@pytest.mark.parametrize("args", [["a", "b"], ["--force"]])
def test_usage(tmp_path, app, args):
    assert write(configured(tmp_path), app, *args)[0] == 2


@pytest.mark.parametrize("text", [
    "# copied from org/platform-pipeline/.github/workflows/golden-road.yml\nname: mine\njobs: {}\n",
    "name: mine /platform-pipeline/\njobs:\n  x:\n    steps:\n      - run: echo /platform-pipeline/\n",
    "jobs:\n  x:\n    steps:\n      - uses: example-org/platform-pipeline/.github/actions/x@main\n",
], ids=["comment", "name-and-run", "a-step-not-a-job"])
def test_a_workflow_that_only_mentions_the_platform_repo_is_the_users_own(tmp_path, app, text):
    skill = configured(tmp_path)
    (app / ".github/workflows").mkdir(parents=True)
    (app / ".github/workflows/ci.yml").write_text(text)
    code, doc = write(skill, app)
    assert code == 3 and "rename your workflow ci.yml" in doc["message"]
    assert (app / ".github/workflows/ci.yml").read_text() == text


def test_a_job_that_calls_the_platform_workflow_is_the_platforms_even_when_quoted(tmp_path, app):
    skill = configured(tmp_path)
    (app / ".github/workflows").mkdir(parents=True)
    (app / ".github/workflows/ci.yml").write_text("jobs:\n  x:\n    uses: 'example-org/platform-pipeline/.github/workflows/golden-road.yml@v1'  # old pin\n")
    code, doc = write(skill, app)
    assert code == 0 and "ci.yml" in doc["written"]

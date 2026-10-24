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
# The per-app scorecard caller as committed (a test keeps it equal to the bundle's template when the tree has one): central mode
# removes the template from the bundle, and these tests are about the callers written from a full bundle.
SCORECARD_TEMPLATE = Path(__file__).resolve().parent / "fixtures/scorecard.yml"


def configured(tmp_path, org="example-org"):
    """The skill folder as configure.py leaves it for org, with all four templates: a tree configured for central review has no
    scorecard.yml in its bundle, so that one comes from the fixture."""
    dest = tmp_path / "skill"
    shutil.copytree(SKILL, dest, ignore=shutil.ignore_patterns("__pycache__"))
    if not (dest / "templates/workflows/scorecard.yml").exists():
        shutil.copy(SCORECARD_TEMPLATE, dest / "templates/workflows/scorecard.yml")
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


# ---- central review: the bundle may carry no scorecard caller ----

def central_bundle(tmp_path):
    """The skill as a central-mode configure.py leaves it: no templates/workflows/scorecard.yml. Returns (skill, the platform's scorecard.yml text)."""
    skill = configured(tmp_path)
    template = skill / "templates/workflows/scorecard.yml"
    text = template.read_text().replace("__APP_NAME__", "notes-api")
    template.unlink()
    return skill, text


def test_a_bundle_without_the_scorecard_template_writes_three_callers_and_removes_the_platform_s_scorecard_caller(tmp_path, app):
    skill, platform_scorecard = central_bundle(tmp_path)
    (app / ".github/workflows").mkdir(parents=True)
    (app / ".github/workflows/scorecard.yml").write_text(platform_scorecard)
    code, doc = write(skill, app)
    assert code == 0, doc
    assert doc["written"] == ["ci.yml", "promote.yml", "security-scans.yml"] and doc["removed"] == ["scorecard.yml"]
    assert not (app / ".github/workflows/scorecard.yml").exists()
    assert sorted(p.name for p in (app / ".github/workflows").iterdir()) == ["ci.yml", "promote.yml", "security-scans.yml"]
    code, doc = write(skill, app)
    assert code == 0 and doc["written"] == [] and doc["removed"] == [] and doc["unchanged"] == ["ci.yml", "promote.yml", "security-scans.yml"]


def test_a_user_owned_scorecard_yml_is_left_alone_when_the_bundle_has_no_template(tmp_path, app):
    skill, _ = central_bundle(tmp_path)
    (app / ".github/workflows").mkdir(parents=True)
    (app / ".github/workflows/scorecard.yml").write_text("name: mine\non: push\njobs: {}\n")
    code, doc = write(skill, app)
    assert code == 0 and doc["removed"] == [] and (app / ".github/workflows/scorecard.yml").read_text() == "name: mine\non: push\njobs: {}\n"


def test_a_linked_scorecard_yml_is_never_removed_through_the_link(tmp_path, app):
    skill, text = central_bundle(tmp_path)
    (tmp_path / "target.yml").write_text(text)
    (app / ".github/workflows").mkdir(parents=True)
    (app / ".github/workflows/scorecard.yml").symlink_to(tmp_path / "target.yml")
    code, doc = write(skill, app)
    assert code == 3 and doc["status"] == "error" and (tmp_path / "target.yml").read_text() == text
    assert not (app / ".github/workflows/ci.yml").exists(), "nothing was written"


def test_a_full_bundle_still_writes_four(tmp_path, app):
    code, doc = write(configured(tmp_path), app)
    assert code == 0 and doc["written"] == NAMES and doc["removed"] == []


@pytest.mark.parametrize("missing", ["ci.yml", "promote.yml", "security-scans.yml"])
def test_a_bundle_missing_a_required_template_exits_3_and_writes_nothing(tmp_path, app, missing):
    skill = configured(tmp_path)
    (skill / "templates/workflows" / missing).unlink()
    code, doc = write(skill, app)
    assert code == 3 and missing in doc["message"] and not (app / ".github").exists()

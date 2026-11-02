import json
import os
import subprocess
from pathlib import Path

import pytest

from golden_road.skills_catalog import ENV, MOCK, CatalogError, GitCatalog, MockCatalog, default_catalog

DOC = json.loads(Path(MOCK).read_text())
OLD, MAIN = "2c7d1e9f0a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d", DOC["main"]


def test_the_mock_reads_a_skill_at_a_commit_without_its_skill_yaml():
    cat = MockCatalog.load()
    assert cat.name == "mock-skills-catalog"
    meta, files = cat.skill("incident-triage", MAIN)
    assert meta["environments"] == ["test", "prod"] and sorted(files) == ["SKILL.md", "checklists/sev1.md"]
    assert sorted(cat.skill("incident-triage", OLD)[1]) == ["SKILL.md"]


def test_each_read_is_a_fresh_copy():
    cat = MockCatalog.load()
    cat.skill("incident-triage", MAIN)[1]["SKILL.md"] = "changed"
    assert cat.skill("incident-triage", MAIN)[1]["SKILL.md"] != "changed"


@pytest.mark.parametrize("name, commit", [("release-notes", OLD), ("nope", MAIN), ("incident-triage", "f" * 40),
                                          ("../incident-triage", MAIN), ("incident-triage", "HEAD")])
def test_a_skill_not_at_that_commit_is_refused(name, commit):
    with pytest.raises(CatalogError, match="not in the catalog"):
        MockCatalog.load().skill(name, commit)


@pytest.mark.parametrize("edit", [
    lambda d: d.update(schema="x"), lambda d: d.update(main="abc"), lambda d: d.update(main="e" * 40),
    lambda d: d["commits"].update({"HEAD": {"skills": {}}}),
    lambda d: d["commits"][MAIN]["skills"]["release-notes"].update({"skill.yaml": "schema: skill/v1\nname: other\n"}),
    lambda d: d["commits"][MAIN]["skills"]["release-notes"].pop("skill.yaml"),
    lambda d: d["commits"][MAIN]["skills"]["release-notes"].update({"skill.yaml": "[unclosed"}),
    lambda d: d["commits"][MAIN]["skills"].update({"Bad_Name": d["commits"][MAIN]["skills"]["release-notes"]}),
    lambda d: d["commits"][MAIN]["skills"]["release-notes"].update({"SKILL.md": 7}),
])
def test_a_bad_mock_file_is_refused(tmp_path, edit):
    doc = json.loads(json.dumps(DOC))
    edit(doc)
    (tmp_path / "c.json").write_text(json.dumps(doc))
    with pytest.raises(CatalogError):
        MockCatalog.load(tmp_path / "c.json")


def test_a_mock_file_naming_a_key_twice_is_refused(tmp_path):
    (tmp_path / "c.json").write_text(Path(MOCK).read_text().replace('"schema": "mock-skills-catalog/v1",',
                                                                    '"schema": "mock-skills-catalog/v1", "schema": "mock-skills-catalog/v1",'))
    with pytest.raises(CatalogError, match="twice"):
        MockCatalog.load(tmp_path / "c.json")


def run_git(repo):
    return lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()  # noqa: E731


def commit_all(repo, on_main=True):
    """Commit everything; on_main also moves refs/remotes/origin/main there, as a fetch of the merged catalog would."""
    run = run_git(repo)
    run("add", "-A")
    run("-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "catalog")
    head = run("rev-parse", "HEAD")
    if on_main:
        run("update-ref", "refs/remotes/origin/main", head)
    return head


def git_catalog(tmp_path):
    """The fixture's main commit as a real git repo: the catalog layout skills/<name>/..."""
    repo = tmp_path / "catalog"
    for name, files in DOC["commits"][MAIN]["skills"].items():
        for path, text in files.items():
            (repo / "skills" / name / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / "skills" / name / path).write_text(text)
    run_git(repo)("init", "-q")
    return repo, commit_all(repo)


def test_the_git_catalog_reads_the_same_skill_at_its_commit(tmp_path):
    repo, head = git_catalog(tmp_path)
    cat = GitCatalog(repo)
    assert cat.name == "skills-catalog"
    meta, files = cat.skill("incident-triage", head)
    assert meta["environments"] == ["test", "prod"]
    assert files == {k: v for k, v in DOC["commits"][MAIN]["skills"]["incident-triage"].items() if k != "skill.yaml"}
    with pytest.raises(CatalogError, match="not in the catalog"):
        cat.skill("incident-triage", "f" * 40)
    with pytest.raises(CatalogError, match="not in the catalog"):
        cat.skill("nope", head)


def test_the_git_catalog_reads_the_commit_not_the_working_tree(tmp_path):
    repo, head = git_catalog(tmp_path)
    (repo / "skills/incident-triage/SKILL.md").write_text("edited after the commit")
    assert "edited" not in GitCatalog(repo).skill("incident-triage", head)[1]["SKILL.md"]


@pytest.mark.parametrize("name, commit", [("../skills", "0" * 40), ("incident-triage", "--output=/tmp/x"), ("incident-triage", "HEAD")])
def test_the_git_catalog_checks_its_arguments_before_any_call(tmp_path, name, commit):
    with pytest.raises(CatalogError, match="not in the catalog"):
        GitCatalog(tmp_path).skill(name, commit)  # tmp_path is not a repo: a git call would fail differently


def test_a_commit_on_an_unmerged_branch_is_not_in_the_catalog(tmp_path):
    repo, _ = git_catalog(tmp_path)
    run_git(repo)("checkout", "-qb", "side")
    (repo / "skills/incident-triage/SKILL.md").write_text("# Not reviewed\n")
    side = commit_all(repo, on_main=False)
    with pytest.raises(CatalogError, match="not in the catalog"):
        GitCatalog(repo).skill("incident-triage", side)
    assert GitCatalog(repo, main_ref="refs/heads/side").skill("incident-triage", side)[1]["SKILL.md"] == "# Not reviewed\n"


def test_a_tree_is_not_a_commit(tmp_path):
    repo, _ = git_catalog(tmp_path)
    tree = run_git(repo)("rev-parse", "HEAD^{tree}")
    with pytest.raises(CatalogError, match="not in the catalog"):
        GitCatalog(repo).skill("incident-triage", tree)


def test_a_checkout_without_main_has_nothing_in_the_catalog(tmp_path):
    repo, head = git_catalog(tmp_path)
    run_git(repo)("update-ref", "-d", "refs/remotes/origin/main")
    with pytest.raises(CatalogError, match="not in the catalog"):
        GitCatalog(repo).skill("incident-triage", head)


@pytest.mark.parametrize("ref", ["main", "--output=x", "refs/../x", "refs/heads/a b"])
def test_the_main_ref_is_a_ref(tmp_path, ref):
    with pytest.raises(CatalogError, match="ref"):
        GitCatalog(tmp_path, main_ref=ref)


def test_a_symlink_in_a_skill_is_refused(tmp_path):
    repo, _ = git_catalog(tmp_path)
    os.symlink("/etc/passwd", repo / "skills/incident-triage/passwd.md")
    with pytest.raises(CatalogError, match="symlink"):
        GitCatalog(repo).skill("incident-triage", commit_all(repo))


def test_a_submodule_in_a_skill_is_refused(tmp_path):
    repo, head = git_catalog(tmp_path)
    run_git(repo)("update-index", "--add", "--cacheinfo", f"160000,{head},skills/incident-triage/sub")
    run_git(repo)("-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "sub")
    run_git(repo)("update-ref", "refs/remotes/origin/main", "HEAD")
    with pytest.raises(CatalogError, match="not a regular file"):
        GitCatalog(repo).skill("incident-triage", run_git(repo)("rev-parse", "HEAD"))


def test_a_file_that_is_not_utf8_is_refused(tmp_path):
    repo, _ = git_catalog(tmp_path)
    (repo / "skills/incident-triage/bad.md").write_bytes(b"\xff\xfe")
    with pytest.raises(CatalogError, match="UTF-8"):
        GitCatalog(repo).skill("incident-triage", commit_all(repo))


def test_a_git_skill_yaml_naming_another_skill_is_refused(tmp_path):
    repo, _ = git_catalog(tmp_path)
    path = repo / "skills/incident-triage/skill.yaml"
    path.write_text(path.read_text().replace("name: incident-triage", "name: release-notes"))
    with pytest.raises(CatalogError, match="skill.yaml"):
        GitCatalog(repo).skill("incident-triage", commit_all(repo))


def test_the_default_is_the_mock_unless_a_checkout_is_named(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert default_catalog().name == "mock-skills-catalog"
    repo, _ = git_catalog(tmp_path)
    monkeypatch.setenv(ENV, str(repo))
    assert default_catalog().name == "skills-catalog"
    monkeypatch.setenv(ENV, str(tmp_path / "missing"))
    with pytest.raises(CatalogError, match=ENV):
        default_catalog()

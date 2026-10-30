import json

import pytest
import yaml
from golden_road.cli import main
from test_deploy_dir import binding_v2, env_dir  # the helpers from test_deploy_dir.py


def test_compose_then_verify_all(tmp_path, app, capsys):
    env_dir(tmp_path, app, binding=binding_v2()); env_dir(tmp_path, app, environment="prod")
    assert main(["compose", "--root", str(tmp_path), "apps/acme/notes/test"]) == 0
    assert json.loads(capsys.readouterr().out) == {"written": ["apps/acme/notes/test/composed.yaml", "apps/acme/notes/test/kustomization.yaml",
                                                               "clusters/home/flux/acme-notes-test.yaml", "clusters/office/argo/acme-notes-test.yaml"]}
    assert main(["deploy-verify", "--root", str(tmp_path)]) == 1  # prod is not composed yet
    report = json.loads(capsys.readouterr().out)
    assert report["environments"] == 2 and all("apps/acme/notes/prod" in p for p in report["problems"]) and report["problems"]
    assert main(["compose", "--root", str(tmp_path), "apps/acme/notes/prod"]) == 0
    capsys.readouterr()
    assert main(["deploy-verify", "--root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {"environments": 2, "problems": []}


def test_compose_takes_several_environments(tmp_path, app, capsys):
    env_dir(tmp_path, app); env_dir(tmp_path, app, environment="prod")
    assert main(["compose", "--root", str(tmp_path), "apps/acme/notes/test", "apps/acme/notes/prod"]) == 0
    assert len(json.loads(capsys.readouterr().out)["written"]) == 8  # two generated files and two adapters each
    assert main(["deploy-verify", "--root", str(tmp_path)]) == 0


def _other_app(tmp_path, app, namespace="notes-test"):
    other = env_dir(tmp_path / "x", app); target = tmp_path / "apps/acme/other/test"
    target.parent.mkdir(parents=True); other.rename(target)
    release = yaml.safe_load((target / "release.yaml").read_text())
    (target / "release.yaml").write_text(yaml.safe_dump({**release, "repo": "acme/other", "namespace": namespace}))
    return target


def test_deploy_verify_refuses_two_environments_sharing_a_namespace(tmp_path, app, capsys):
    env_dir(tmp_path, app)  # acme/notes test, namespace notes-test
    _other_app(tmp_path, app)  # acme/other test, also notes-test
    for d in ("apps/acme/notes/test", "apps/acme/other/test"):
        assert main(["compose", "--root", str(tmp_path), d]) == 0
    capsys.readouterr()
    assert main(["deploy-verify", "--root", str(tmp_path)]) == 1
    problems = json.loads(capsys.readouterr().out)["problems"]
    assert len(problems) == 1 and "namespace notes-test" in problems[0]
    assert "apps/acme/notes/test" in problems[0] and "apps/acme/other/test" in problems[0]


def test_every_environment_has_its_own_namespace(tmp_path, app, capsys):
    env_dir(tmp_path, app); env_dir(tmp_path, app, environment="prod")  # notes-test and notes-prod
    _other_app(tmp_path, app, namespace="other-test")
    assert main(["compose", "--root", str(tmp_path), "apps/acme/notes/test", "apps/acme/notes/prod", "apps/acme/other/test"]) == 0
    capsys.readouterr()
    assert main(["deploy-verify", "--root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {"environments": 3, "problems": []}


@pytest.mark.parametrize("stray", ["apps/acme/notes/staging/release.yaml", "apps/acme/notes/README.md", "apps/acme/x.yaml",
                                   "apps/x.yaml", "apps/acme/notes/test/extra/x.yaml"])
def test_deploy_verify_reports_paths_that_are_not_owner_repo_env(tmp_path, app, capsys, stray):
    env_dir(tmp_path, app)
    main(["compose", "--root", str(tmp_path), "apps/acme/notes/test"])
    (tmp_path / stray).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / stray).write_text("x: 1\n")
    capsys.readouterr()
    assert main(["deploy-verify", "--root", str(tmp_path)]) == 1
    problems = json.loads(capsys.readouterr().out)["problems"]
    assert problems and all(stray.rsplit("/", 1)[0] in p or stray in p for p in problems)


def test_deploy_verify_reports_a_symlink_under_apps(tmp_path, app, capsys):
    env_dir(tmp_path, app)
    main(["compose", "--root", str(tmp_path), "apps/acme/notes/test"])
    (tmp_path / "apps/evil").symlink_to(tmp_path / "apps/acme")
    capsys.readouterr()
    assert main(["deploy-verify", "--root", str(tmp_path)]) == 1
    assert any("apps/evil" in p and "symlink" in p for p in json.loads(capsys.readouterr().out)["problems"])


def test_deploy_verify_reports_one_app_under_two_cases(tmp_path, app, capsys):
    env_dir(tmp_path, app)
    main(["compose", "--root", str(tmp_path), "apps/acme/notes/test"])
    (tmp_path / "apps/Acme/notes/test").mkdir(parents=True)
    capsys.readouterr()
    assert main(["deploy-verify", "--root", str(tmp_path)]) == 1
    assert any("case" in p for p in json.loads(capsys.readouterr().out)["problems"])


def test_an_empty_deploy_repo_verifies(tmp_path, capsys):
    (tmp_path / "apps").mkdir(); (tmp_path / "apps/.gitkeep").write_text("")
    assert main(["deploy-verify", "--root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == {"environments": 0, "problems": []}


def test_no_apps_directory_is_an_input_error(tmp_path, capsys):
    assert main(["deploy-verify", "--root", str(tmp_path)]) == 2
    assert "apps" in json.loads(capsys.readouterr().err)["error"]


@pytest.mark.parametrize("path", ["apps/acme/notes", "apps/acme/notes/staging", "../apps/acme/notes/test", "/apps/acme/notes/test",
                                  "apps/acme/../acme/notes/test", "other/acme/notes/test"])
def test_compose_takes_only_an_environment_path(tmp_path, app, capsys, path):
    env_dir(tmp_path, app)
    assert main(["compose", "--root", str(tmp_path), path]) == 2
    assert not (tmp_path / "apps/acme/notes/test/composed.yaml").exists()


def test_compose_refuses_a_bad_directory_and_writes_nothing(tmp_path, app, capsys):
    d = env_dir(tmp_path, app)
    (d / "base/manifests.yaml").write_text("changed\n")
    assert main(["compose", "--root", str(tmp_path), "apps/acme/notes/test"]) == 2
    assert "manifests_sha256" in json.loads(capsys.readouterr().err)["error"]
    assert not (d / "composed.yaml").exists()


def test_compose_writes_nothing_unless_every_environment_composes(tmp_path, app, capsys):
    good = env_dir(tmp_path, app)
    bad = env_dir(tmp_path, app, environment="prod")
    (bad / "base/manifests.yaml").write_text("changed\n")
    assert main(["compose", "--root", str(tmp_path), "apps/acme/notes/test", "apps/acme/notes/prod"]) == 2
    assert not (good / "composed.yaml").exists() and not (good / "kustomization.yaml").exists()



def test_deploy_verify_and_compose_pin_the_flux_source_url(tmp_path, app, capsys):
    from golden_road.deploy import InputError, check_repo_url, deploy_repo_url
    env_dir(tmp_path, app)
    url = deploy_repo_url(tmp_path)
    other = "https://github.com/other/golden-road-deploy"
    assert main(["compose", "--root", str(tmp_path), "--expect-url", other, "apps/acme/notes/test"]) == 2
    assert "is not this repository" in capsys.readouterr().err
    assert not (tmp_path / "apps/acme/notes/test/composed.yaml").exists(), "a refused url writes nothing"
    assert main(["compose", "--root", str(tmp_path), "--expect-url", url, "apps/acme/notes/test"]) == 0
    assert main(["deploy-verify", "--root", str(tmp_path), "--expect-url", url]) == 0
    capsys.readouterr()
    assert main(["deploy-verify", "--root", str(tmp_path), "--expect-url", other]) == 1
    [problem] = json.loads(capsys.readouterr().out)["problems"]
    assert f"url {url} is not this repository, {other}" in problem
    for bad in (other, url + ".git", "http://" + url[len("https://"):], "not a url", ""):
        with pytest.raises(InputError):
            check_repo_url(url, bad)
    check_repo_url(url, url.upper().replace("HTTPS://", "https://"))  # GitHub's host and owner have no case

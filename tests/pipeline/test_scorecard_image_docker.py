"""The scorecard inside the builder image, the way scorecard.yml runs it. Opt-in: pytest -m docker"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from test_scorecard_workflow import CHECKS_DOC, DOC, HARDENING, merge_checkout, step

pytestmark = [pytest.mark.docker, pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")]

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_FACTORY = ROOT / "tests/scorecard/fixtures/factory"


def test_the_builder_image_has_git_and_the_scorecard(builder_image):
    for argv in (["git", "--version"], ["scorecard", "precondition", "--help"]):
        out = subprocess.run(["docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp", builder_image, *argv],
                             capture_output=True, text=True)
        assert out.returncode == 0, out.stderr


def make_merge_repo(path):
    """A repository whose HEAD is a --no-ff merge, like the PR checkout, owned by the user running the tests."""
    def git(*args):
        out = subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()
    path.mkdir()
    git("init", "-q", "-b", "main")
    git("commit", "-q", "--allow-empty", "-m", "base")
    git("checkout", "-q", "-b", "pr")
    git("commit", "-q", "--allow-empty", "-m", "change")
    git("checkout", "-q", "main")
    git("merge", "-q", "--no-ff", "pr", "-m", "merge")
    return git("rev-parse", "HEAD^1"), git("rev-parse", "HEAD^2")


def test_git_reads_a_mounted_checkout_as_the_runner_s_uid_with_no_home(builder_image, tmp_path):
    """The scorecard steps run as the runner's uid, which has no passwd entry in the image and no HOME: git must still read the
    runner's checkout (no "dubious ownership"), and the scorecard's own git module must too."""
    import os

    first, second = make_merge_repo(tmp_path / "repo")
    base = ["docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp", "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{tmp_path / 'repo'}:/src:ro", builder_image]
    out = subprocess.run([*base, "git", "-C", "/src", "rev-parse", "HEAD^1"], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == first, out.stderr
    code = "from scorecard import git; print(git.parents('/src', git.rev('/src', 'HEAD')))"
    out = subprocess.run([*base, "python", "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == str([first, second])


def test_the_copied_git_binary_has_its_libraries_a_version_label_and_runs_every_subcommand_the_code_uses(builder_image, tmp_path):
    """git is one binary copied from a build stage, not a package: dpkg doesn't list it, so every library must resolve, the version is
    labelled, and every subcommand src/scorecard/git.py and golden.py call must work without /usr/lib/git-core helpers."""
    import json
    import os

    first, second = make_merge_repo(tmp_path / "repo")
    inspect = subprocess.run(["docker", "image", "inspect", builder_image, "--format", "{{json .Config.Labels}}"], capture_output=True, text=True, check=True)
    labelled = json.loads(inspect.stdout)["git.version"]
    base = ["docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp", "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{tmp_path / 'repo'}:/src:ro", builder_image]
    version = subprocess.run([*base, "git", "--version"], capture_output=True, text=True)
    assert version.returncode == 0 and version.stdout.strip() == f"git version {labelled}", version.stderr
    ldd = subprocess.run([*base, "ldd", "/usr/bin/git"], capture_output=True, text=True)
    assert ldd.returncode == 0 and "not found" not in ldd.stdout, ldd.stdout
    dpkg = subprocess.run([*base, "dpkg", "-s", "git"], capture_output=True, text=True)
    assert dpkg.returncode != 0, "git is copied, not installed: dpkg must not claim it"
    harden = ["-c", "core.quotepath=off", "-c", "core.fsmonitor=false", "-c", "diff.external=", "-c", "core.attributesFile=/dev/null"]
    diff = ["diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--text"]
    for args in (["rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"], ["rev-list", "--parents", "-n", "1", "HEAD"],
                 ["rev-parse", "--verify", "HEAD^{tree}"], [*diff, "--name-status", "-z", first, "HEAD"], ["cat-file", "-t", "HEAD:"],
                 ["cat-file", "-s", "HEAD:"], [*diff, "-U0", first, "HEAD"], [*diff, "-U3", first, "HEAD", "--"], ["log", "-1", "--format=%H"], ["show", "-s", "HEAD"]):
        out = subprocess.run([*base, "git", "-C", "/src", *harden, *args], capture_output=True, text=True, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"})
        assert out.returncode == 0, (args, out.stderr)
    # golden.py builds repositories with init, add, commit, checkout and a --no-ff merge
    script = ("set -e; cd /tmp; git init -q -b main r; cd r; export GIT_CONFIG_COUNT=3 GIT_CONFIG_KEY_0=user.name GIT_CONFIG_VALUE_0=t "
              "GIT_CONFIG_KEY_1=user.email GIT_CONFIG_VALUE_1=t@example.invalid GIT_CONFIG_KEY_2=commit.gpgsign GIT_CONFIG_VALUE_2=false; "
              "echo a > f; git add f; git commit -q -m a; git checkout -q -b pr; echo b >> f; git commit -q -am b; git checkout -q main; "
              "git merge -q --no-ff pr -m m; git rev-parse HEAD^1 HEAD^2 >/dev/null")
    out = subprocess.run([*base, "sh", "-c", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def _id(flag):
    return subprocess.run(["id", flag], capture_output=True, text=True, check=True).stdout.strip()


def test_the_agent_step_and_scorecard_run_in_the_image(builder_image, tmp_path):
    """The agent step's own script with the real image and docker client, forced to --network none: the gateway is unreachable, so the
    assessment is incomplete and the scorecard is `elevated` with T-14 (AC-10), end to end."""
    merge, base, head = merge_checkout(tmp_path)
    factory = tmp_path / "factory"
    shutil.copytree(FIXTURE_FACTORY, factory)
    config = factory / "config/scorecard.yaml"
    retry = yaml.safe_load(config.read_text())["agent"]["gateway"]["retry_seconds"]
    wire = "acme-sensitive-deploy-7"
    config.write_text(config.read_text().replace(f"retry_seconds: {retry}", "retry_seconds: 0")
                      .replace("https://gateway.example.invalid/v1/chat/completions", "https://gateway.example.invalid/openai/deployments/{model}/chat/completions"))
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"draft": False, "title": "t", "head": {"sha": head, "repo": {"full_name": "acme/notes"}},
                                                  "base": {"repo": {"full_name": "acme/notes"}}}}))
    temp = tmp_path / "runner-temp"
    temp.mkdir()
    (temp / "scorecard-gateway.env").write_text(f"SCORECARD_GATEWAY_TOKEN=sk-scorecard-0123456789abcdef\nSCORECARD_GATEWAY_MODEL={wire}\n")
    # The image bakes platform/required-checks.json into its Gate 1 list, so a configured tree (one that lists the scans) needs them green too.
    baked = [{"name": c["name"], "state": "success"} for c in json.loads((ROOT / "platform/required-checks.json").read_text())["required"]]
    checks = json.dumps({**json.loads(CHECKS_DOC), "required_checks": [{"name": "golden-road / drift", "state": "success"},
                                                                         {"name": "golden-road / preview", "state": "success"}, *baked], "sha": head})
    script = step("assess", "Run the review agent")["run"]
    # No network: the gateway is unreachable on purpose, and the test never leaves the machine.
    assert script.count("docker run --rm ") == 1
    assert f"docker run --rm {HARDENING} --read-only" in script, "the test runs the workflow's own hardening flags, the string the structural test checks"
    script = script.replace("docker run --rm ", "docker run --rm --network none ")
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path), "BUILDER_IMAGE": builder_image, "EVENT_FIELDS": DOC["env"]["EVENT_FIELDS"],
           "GITHUB_EVENT_PATH": str(event), "GITHUB_SHA": merge, "GITHUB_REPOSITORY": "acme/notes", "GITHUB_OUTPUT": str(tmp_path / "output"),
           "RUNNER_TEMP": str(temp), "CHECKS": checks, "GITHUB_TOKEN": "ghs_must_not_reach_the_agent", "ACTIONS_RUNTIME_TOKEN": "rt"}
    out = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stdout + out.stderr
    assessment = json.loads((tmp_path / "out/assessment.json").read_text())
    assert assessment["schema"] == "assessment/v1" and not (temp / "scorecard-gateway.env").exists()
    assert "ghs_must_not_reach_the_agent" not in out.stdout + out.stderr

    run = subprocess.run(["docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp", "--user", f"{_id('-u')}:{_id('-g')}",
                          "-v", f"{tmp_path}/src:/src:ro", "-v", f"{factory}:/factory:ro", "-v", f"{tmp_path}/in:/in:ro", "-v", f"{tmp_path}/out:/out",
                          builder_image, "scorecard", "run", "--repo-root", "/src", "--repo", "acme/notes", "--base", base, "--factory", "/factory",
                          "--checks", "/in/checks.json", "--event", "/in/event.json", "--assessment", "/out/assessment.json", "--out", "/out"],
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    card = json.loads((tmp_path / "out/scorecard.json").read_text())
    assert card["level"] == "elevated" and "T-14" in card["triggers_fired"]
    assert (tmp_path / "out/comment.md").read_text().startswith("<!-- golden-road:scorecard -->\n")
    for text in (out.stdout, out.stderr, run.stdout, run.stderr,
                 *((tmp_path / "out" / name).read_text() for name in ("assessment.json", "scorecard.json", "scorecard.md", "comment.md", "github.json", "re-review-event.json"))):
        assert wire not in text


def test_the_image_s_scorecard_sends_its_own_user_agent(builder_image):
    from builder import VERSION

    code = "from scorecard.agent.gateway import USER_AGENT; print(USER_AGENT)"
    out = subprocess.run(["docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp", builder_image, "python", "-c", code],
                         capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == f"golden-road-scorecard/{VERSION}", out.stderr

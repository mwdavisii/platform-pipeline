"""golden-road-deploy's bump: its step scripts run with stand-in docker, gh and cosign (tests/pipeline/deploy_fakes.py). The docker
stand-in runs the real golden-road deploy-pins, deploy-pin and compose; gh answers release list, release download and the git data
and pulls API from a small model, and records every call."""

import base64
import importlib.util
import json
import re
import shutil

import pytest
import yaml

from deploy_fakes import (DEPLOY_BOOTSTRAP, IMAGE, PROD_TAG, REPO, ROOT, TEST_TAG, base_env, calls, commit_all, deploy_repo, gate_record, git,
                          manifests, promotion_record, publish, run_step, step, write_pin)
from helpers import unconfigured_bump_text

WORKFLOW = DEPLOY_BOOTSTRAP / ".github/workflows/bump.yml"
pytestmark = pytest.mark.skipif(shutil.which("jq") is None or shutil.which("git") is None, reason="jq and git are needed")
STEPS = ("Check the repo input", "Read the pins", "Find the releases to pin", "Mint the deploy App token", "Open or update the pin pull requests")
TOKEN = "ghs_" + "T" * 40
NEWER_TAG = "test-20261029160000-42fa829"
MINT_IF = "steps.plan.outputs.needed == 'true' && env.BUMP_TOKEN == 'app'"


def configure_module():
    spec = importlib.util.spec_from_file_location("configure_for_bump", ROOT / "scripts/configure.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def text(mode="app"):
    """bump.yml as committed (app mode, whatever the live tree was configured to), or as configure.py --bump-workflow-token writes it."""
    default = unconfigured_bump_text(WORKFLOW.read_text())
    return default if mode == "app" else configure_module().bump_workflow_token_text(default)


def doc(mode="app"):
    d = yaml.safe_load(text(mode))
    d["on"] = d.pop(True, d.get("on"))
    return d


def run(tmp_path, mode="app", **env):
    d = doc(mode)
    environ = base_env(tmp_path, d, **{"GITHUB_REF": "refs/heads/main", "APP_ID": "1234", "APP_KEY": "-----BEGIN KEY-----", "REPO_INPUT": "", **env})
    shutil.rmtree(tmp_path / "runner-temp/deploy-token", ignore_errors=True)  # each job has its own runner temp
    (tmp_path / "github-output").unlink(missing_ok=True)
    seen = []
    for name in STEPS:
        if name == "Mint the deploy App token" and ("needed=true" not in (tmp_path / "github-output").read_text() or environ["BUMP_TOKEN"] != "app"):
            continue  # its `if:` (MINT_IF): steps.plan.outputs.needed == 'true' && env.BUMP_TOKEN == 'app'
        result = run_step(tmp_path, step(d, name)["run"], environ)
        seen.append(result.stdout)
        if result.returncode != 0:
            break
    result.stdout_all = "".join(seen)
    return name, result


def api(tmp_path, method=None):
    return [c for c in calls(tmp_path, "gh") if c[1] == "api" and (method is None or c[2] == method)]


def writes(tmp_path):
    return [c for c in api(tmp_path) if c[2] in ("POST", "PATCH", "PUT", "DELETE")]


def tree_paths(tmp_path):
    [tree] = [c[4] for c in api(tmp_path, "POST") if c[3].endswith("/git/trees")]
    return sorted(e["path"] for e in tree["tree"]), tree


def blob_for(tmp_path, path):
    _, tree = tree_paths(tmp_path)
    sha = next(e["sha"] for e in tree["tree"] if e["path"] == path)
    blob = json.loads((tmp_path / "gh-state.json").read_text())["blobs"][sha]
    assert blob["encoding"] == "base64"
    return base64.b64decode(blob["content"]).decode("utf-8")


@pytest.fixture
def released(tmp_path):
    """A published test release of acme/notes, and the deploy repo on main with no app in it yet."""
    text = manifests(tmp_path)
    record = gate_record(manifests_text=text)
    publish(tmp_path, TEST_TAG, text, record)
    deploy = deploy_repo(tmp_path)
    return {"text": text, "record": record, "deploy": deploy}


# ---- the shape ----

@pytest.mark.parametrize("mode", ["app", "workflow"])
def test_the_shape(mode):
    d = doc(mode)
    assert d["on"]["schedule"] == [{"cron": "*/15 * * * *"}]
    assert d["on"]["workflow_dispatch"]["inputs"]["repo"]["required"] is False
    assert d["permissions"] == {} and d["concurrency"] == {"group": "bump", "cancel-in-progress": False}
    [job] = d["jobs"].values()
    assert job["environment"] == "bump" and d["env"]["BUMP_TOKEN"] == mode
    for s in job["steps"]:
        assert "${{" not in s.get("run", ""), s.get("name")
        if "uses" in s:
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", s["uses"]), s["uses"]
    mint = step(d, "Mint the deploy App token")
    assert mint["env"] == {"APP_ID": "${{ vars.DEPLOY_APP_ID }}", "APP_KEY": "${{ secrets.DEPLOY_APP_KEY }}"}
    assert mint["if"] == MINT_IF, "in workflow mode the step is skipped, so the App id and key are never read"
    others = [s for s in job["steps"] if s is not mint]
    assert not any("DEPLOY_APP" in json.dumps(s) for s in others), "the App id and key reach only the step that mints the token"
    body = text(mode)
    assert "pull_request_target" not in body and "yq" not in body
    assert "/merge" not in body and "gh pr merge" not in body, "bump never merges"
    assert "closed without merging is opened again on the next run" in " ".join(body.split())


def test_the_default_is_the_app_token_with_a_read_only_job():
    """Spec 2026-10-30, B1: the committed text is app mode; configure.py --bump-workflow-token gives exactly the write permissions."""
    assert next(iter(doc()["jobs"].values()))["permissions"] == {"contents": "read", "packages": "read"}
    assert next(iter(doc("workflow")["jobs"].values()))["permissions"] == {"contents": "write", "pull-requests": "write", "actions": "write",
                                                                            "checks": "read", "packages": "read"}
    assert re.findall(r"(?m)^\s*BUMP_TOKEN:.*$", text()) == ["  BUMP_TOKEN: app"] and re.search(r"BUMP_TOKEN:\s*[\"']?workflow", text()) is None
    live = WORKFLOW.read_text()
    assert live in (text(), text("workflow")), "the live file is one of the two modes, nothing in between"


def test_configure_s_rewrite_refuses_a_text_whose_lines_are_not_there_exactly_once():
    rewrite = configure_module().bump_workflow_token_text
    assert rewrite(text("workflow")) == text("workflow"), "already workflow mode: nothing changes"
    for broken in (text().replace("BUMP_TOKEN: app", "BUMP_TOKEN: maybe"), text().replace("      contents: read\n", "", 1),
                   text() + "\n  BUMP_TOKEN: app\n", text().replace("jobs:\n", "jobs:\n    BUMP_TOKEN: workflow\n", 1)):
        with pytest.raises(ValueError, match="BUMP_TOKEN"):
            rewrite(broken)


@pytest.mark.parametrize("mode", ["app", "workflow", "other"])
def test_an_unknown_token_mode_fails_before_any_call(tmp_path, mode):
    result = run_step(tmp_path, step(doc(), "Check the repo input")["run"], base_env(tmp_path, doc(), GITHUB_REF="refs/heads/main", REPO_INPUT="",
                                                                                     BUMP_TOKEN=mode))
    assert result.returncode == (1 if mode == "other" else 0) and calls(tmp_path) == []
    if mode == "other":
        assert "::error::BUMP_TOKEN must be app or workflow" in result.stdout


def test_the_placeholder_builder_image_is_refused_first(tmp_path):
    first = next(iter(doc()["jobs"].values()))["steps"][0]
    assert first["name"] == "Refuse the placeholder builder image"
    zero = "ghcr.io/acme/golden-road-builder@sha256:" + "0" * 64
    assert run_step(tmp_path, first["run"], base_env(tmp_path, doc(), BUILDER_IMAGE=zero)).returncode == 1


# ---- the token ----

def test_the_token_is_minted_with_exactly_contents_and_pull_requests_write_for_this_repo(tmp_path, released):
    name, result = run(tmp_path, REPO_INPUT=REPO)
    assert result.returncode == 0, (name, result.stdout, result.stderr)
    minted = json.loads((tmp_path / "calls.jsonl.token").read_text())
    argv = minted["argv"]
    assert argv[:2] == ["golden-road", "app-token"] and argv[argv.index("--repo") + 1] == "acme/golden-road-deploy"
    perms = [argv[i + 1] for i, a in enumerate(argv) if a == "--permission"]
    assert perms == ["contents=write", "pull_requests=write"] and argv[argv.index("--app-id") + 1] == "1234"
    assert minted["env"]["GOLDEN_ROAD_APP_KEY"] == "-----BEGIN KEY-----"
    from golden_road.github_app import parse_permissions
    assert parse_permissions(perms, "acme/golden-road-deploy") == {"contents": "write", "pull_requests": "write"}  # the CLI's own allowlist accepts exactly this
    assert f"::add-mask::{TOKEN}" in result.stdout_all
    assert not (tmp_path / "runner-temp/deploy-token/token.json").exists()
    assert (tmp_path / "runner-temp/deploy-token/token").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("env,needle", [({"APP_ID": ""}, "DEPLOY_APP_ID"), ({"APP_ID": "12x"}, "DEPLOY_APP_ID"), ({"APP_KEY": ""}, "DEPLOY_APP_KEY")])
def test_a_missing_app_id_or_key_fails_before_any_write(tmp_path, released, env, needle):
    name, result = run(tmp_path, REPO_INPUT=REPO, **env)
    assert name == "Mint the deploy App token" and result.returncode == 1 and needle in result.stdout
    assert [c[1:3] for c in calls(tmp_path, "gh")] == [["release", "list"]], "only the read-only release list ran"


def test_writes_use_the_app_token_and_reads_the_workflow_token(tmp_path, released):
    name, result = run(tmp_path, REPO_INPUT=REPO)
    assert result.returncode == 0, result.stdout
    for c in calls(tmp_path, "gh"):
        want = TOKEN if c[1] == "api" else "github-token"
        assert c[-1]["GH_TOKEN"] == want, c


# ---- the pull requests ----

def test_a_new_test_release_opens_one_pr_with_exactly_its_files(tmp_path, released):
    name, result = run(tmp_path, REPO_INPUT=REPO)
    assert result.returncode == 0, (name, result.stdout, result.stderr)
    paths, tree = tree_paths(tmp_path)
    assert [p for p in paths if p.startswith("apps/")] == ["apps/acme/notes/test/base/manifests.yaml", "apps/acme/notes/test/composed.yaml",
                                                           "apps/acme/notes/test/kustomization.yaml", "apps/acme/notes/test/release.yaml"]
    assert [p for p in paths if not p.startswith("apps/")] == ["clusters/home/flux/acme-notes-test.yaml", "clusters/office/argo/acme-notes-test.yaml"]
    assert tree["base_tree"] == git(released["deploy"], "rev-parse", "HEAD^{tree}").strip() and all(e["mode"] == "100644" and e["type"] == "blob" for e in tree["tree"])
    pin = yaml.safe_load(blob_for(tmp_path, "apps/acme/notes/test/release.yaml"))
    assert pin["release"] == TEST_TAG and pin["image"] == IMAGE and pin["namespace"] == "notes-test"
    assert blob_for(tmp_path, "apps/acme/notes/test/base/manifests.yaml") == released["text"]
    [commit] = [c[4] for c in api(tmp_path, "POST") if c[3].endswith("/git/commits")]
    assert commit["message"] == f"Pin acme/notes test to {TEST_TAG}" and len(commit["parents"]) == 1
    [ref] = [c[4] for c in api(tmp_path, "POST") if c[3].endswith("/git/refs")]
    assert ref["ref"] == "refs/heads/bump/acme-notes-test" and re.fullmatch(r"c[0-9]{39}", ref["sha"])  # the commit just made
    [pull] = [c[4] for c in api(tmp_path, "POST") if c[3].endswith("/pulls")]
    assert pull["title"] == f"Pin acme/notes test to {TEST_TAG}" and pull["head"] == "bump/acme-notes-test" and pull["base"] == "main"
    assert all(c[3].startswith("repos/acme/golden-road-deploy/") for c in api(tmp_path)), "writes go to the deploy repo only"


def test_the_pr_files_verify_when_applied_to_main(tmp_path, released):
    """The commit's files, written over main, are a tree deploy-verify passes (deploy-check's drift check)."""
    name, result = run(tmp_path, REPO_INPUT=REPO)
    assert result.returncode == 0
    paths, _ = tree_paths(tmp_path)
    for path in paths:
        target = released["deploy"] / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(blob_for(tmp_path, path))
    from golden_road.deploy import verify_tree
    assert verify_tree(released["deploy"]) == {"environments": 1, "problems": []}


def test_an_already_pinned_release_makes_no_api_write(tmp_path, released):
    write_pin(released["deploy"], "test", TEST_TAG, released["text"], released["record"])
    commit_all(released["deploy"])
    name, result = run(tmp_path)  # the schedule: every app under apps/
    assert result.returncode == 0, result.stdout
    assert "apps/acme/notes/test: already pins " + TEST_TAG in result.stdout_all
    assert writes(tmp_path) == [] and [c[1:3] for c in calls(tmp_path, "gh")] == [["release", "list"]]
    assert not (tmp_path / "calls.jsonl.token").exists() and not [c for c in calls(tmp_path, "docker") if "app-token" in c], "nothing to pin: no token"


def test_the_schedule_bumps_a_pinned_app_to_its_newest_test_release(tmp_path, released):
    write_pin(released["deploy"], "test", TEST_TAG, released["text"], released["record"])
    commit_all(released["deploy"])
    newer = gate_record(release=NEWER_TAG, manifests_text=released["text"])
    publish(tmp_path, NEWER_TAG, released["text"], newer, published="2026-10-29T16:05:00Z")
    name, result = run(tmp_path)
    assert result.returncode == 0, result.stdout
    paths, _ = tree_paths(tmp_path)
    assert paths == ["apps/acme/notes/test/release.yaml"], "only the pin changed: the same manifests compose to the same files"
    assert yaml.safe_load(blob_for(tmp_path, paths[0]))["release"] == NEWER_TAG


def test_a_prod_release_opens_the_prod_pr(tmp_path, released):
    write_pin(released["deploy"], "test", TEST_TAG, released["text"], released["record"])
    commit_all(released["deploy"])
    publish(tmp_path, PROD_TAG, released["text"], released["record"], prerelease=False, published="2026-10-29T15:05:00Z")
    name, result = run(tmp_path)
    assert result.returncode == 0, result.stdout
    paths, _ = tree_paths(tmp_path)
    assert [p for p in paths if p.startswith("apps/")] == ["apps/acme/notes/prod/base/manifests.yaml", "apps/acme/notes/prod/composed.yaml",
                                                           "apps/acme/notes/prod/kustomization.yaml", "apps/acme/notes/prod/release.yaml"]
    pin = yaml.safe_load(blob_for(tmp_path, "apps/acme/notes/prod/release.yaml"))
    assert pin["release"] == PROD_TAG and pin["namespace"] == "notes-prod"
    [pull] = [c[4] for c in api(tmp_path, "POST") if c[3].endswith("/pulls")]
    assert pull["title"] == f"Pin acme/notes prod to {PROD_TAG}" and pull["head"] == "bump/acme-notes-prod"


def test_a_release_already_proposed_is_not_proposed_again(tmp_path, released):
    assert run(tmp_path, REPO_INPUT=REPO)[1].returncode == 0
    before = len(writes(tmp_path))
    name, result = run(tmp_path, REPO_INPUT=REPO)
    assert result.returncode == 0 and f"{TEST_TAG} is already proposed" in result.stdout
    assert len(writes(tmp_path)) == before


def test_an_open_pr_for_an_older_release_is_moved_to_the_newer_one(tmp_path, released):
    assert run(tmp_path, REPO_INPUT=REPO)[1].returncode == 0
    newer = gate_record(release=NEWER_TAG, manifests_text=released["text"])
    publish(tmp_path, NEWER_TAG, released["text"], newer, published="2026-10-29T16:05:00Z")
    name, result = run(tmp_path, REPO_INPUT=REPO)
    assert result.returncode == 0, result.stdout
    patches = [c for c in api(tmp_path, "PATCH")]
    assert [c[3] for c in patches] == ["repos/acme/golden-road-deploy/git/refs/heads/bump/acme-notes-test", "repos/acme/golden-road-deploy/pulls/1"]
    assert patches[0][4]["force"] is True and patches[1][4]["title"] == f"Pin acme/notes test to {NEWER_TAG}"
    assert len([c for c in api(tmp_path, "POST") if c[3].endswith("/pulls")]) == 1


@pytest.mark.parametrize("bad", ["acme", "acme/notes/x", "acme/..", "../notes", "acme/no tes", "acme/notes;id", "acme/notes\n::warning::x"])
def test_a_repo_input_that_fails_its_regex_fails_before_any_call(tmp_path, released, bad):
    name, result = run(tmp_path, REPO_INPUT=bad)
    assert name == "Check the repo input" and result.returncode == 1 and "::error::the repo input must be owner/name" in result.stdout
    assert calls(tmp_path) == []


def test_bump_runs_on_main_only(tmp_path, released):
    name, result = run(tmp_path, REPO_INPUT=REPO, GITHUB_REF="refs/heads/feature")
    assert name == "Check the repo input" and result.returncode == 1 and calls(tmp_path) == []


def test_manifests_that_do_not_match_the_gate_record_fail_and_write_nothing(tmp_path, released):
    (tmp_path / "releases" / REPO / TEST_TAG / "manifests.yaml").write_text(released["text"] + "# swapped\n")
    name, result = run(tmp_path, REPO_INPUT=REPO)
    assert name == STEPS[-1] and result.returncode == 1
    assert "manifests_sha256" in result.stderr and "deploy-pin refused " + TEST_TAG in result.stdout
    assert writes(tmp_path) == []


def test_a_refused_app_fails_the_run_but_the_other_environments_are_still_opened(tmp_path, released):
    write_pin(released["deploy"], "test", TEST_TAG, released["text"], released["record"])
    commit_all(released["deploy"])
    bad = gate_record(release=NEWER_TAG, manifests_text="other")  # its manifests_sha256 is not the asset's
    publish(tmp_path, NEWER_TAG, released["text"], bad, published="2026-10-29T16:05:00Z")
    publish(tmp_path, PROD_TAG, released["text"], released["record"], prerelease=False, published="2026-10-29T15:05:00Z")
    name, result = run(tmp_path)
    assert result.returncode == 1 and "at least one app or environment could not be bumped" in result.stdout
    [pull] = [c[4] for c in api(tmp_path, "POST") if c[3].endswith("/pulls")]
    assert pull["head"] == "bump/acme-notes-prod"


def test_a_repo_name_with_no_namespace_says_to_set_it_by_hand(tmp_path, released):
    long = "acme/" + "n" * 70
    text = released["text"]
    publish(tmp_path, TEST_TAG, text, gate_record(manifests_text=text, repo=long), repo=long)
    name, result = run(tmp_path, REPO_INPUT=long)
    assert result.returncode == 1 and "set by hand" in result.stdout and "by hand" in result.stderr
    assert writes(tmp_path) == []


def test_drafts_and_other_tags_are_never_candidates(tmp_path, released):
    rel = tmp_path / "releases" / REPO
    for tag, meta in (("test-20261029230000-42fa829", {"isDraft": True}), ("skill-v1", {}), ("test-latest", {})):
        shutil.copytree(rel / TEST_TAG, rel / tag)
        (rel / tag / "meta.json").write_text(json.dumps({"isPrerelease": True, "isDraft": False, "publishedAt": "2026-10-29T23:00:00Z", **meta}))
    name, result = run(tmp_path, REPO_INPUT=REPO)
    assert result.returncode == 0, result.stdout
    assert yaml.safe_load(blob_for(tmp_path, "apps/acme/notes/test/release.yaml"))["release"] == TEST_TAG


# ---- one app's failure is reported and the others still go ----

def test_an_app_whose_releases_cannot_be_listed_is_reported_and_the_next_app_still_bumped(tmp_path, released):
    # acme/aaa sorts first; its release list fails (a 404 repo). acme/notes still gets its PR.
    write_pin(released["deploy"], "test", TEST_TAG, released["text"], released["record"], repo="acme/aaa")
    write_pin(released["deploy"], "test", "test-20261029000000-42fa829", released["text"], released["record"])  # an older pin
    commit_all(released["deploy"])
    name, result = run(tmp_path, FAKE_LIST_FAIL="acme/aaa")
    assert result.returncode == 1
    assert "::error::apps/acme/aaa: cannot list the releases of acme/aaa" in result.stdout_all
    [pull] = [c[4] for c in api(tmp_path, "POST") if c[3].endswith("/pulls")]
    assert pull["head"] == "bump/acme-notes-test"


def test_a_missing_asset_fails_only_its_environment(tmp_path, released):
    publish(tmp_path, PROD_TAG, released["text"], released["record"], prerelease=False, published="2026-10-29T15:05:00Z")
    (tmp_path / "releases" / REPO / PROD_TAG / "gate-record.json").unlink()
    name, result = run(tmp_path, REPO_INPUT=REPO)
    assert result.returncode == 1 and f"::error::apps/acme/notes/prod: release {PROD_TAG} has no gate-record.json" in result.stdout
    [pull] = [c[4] for c in api(tmp_path, "POST") if c[3].endswith("/pulls")]
    assert pull["head"] == "bump/acme-notes-test"


def test_a_download_error_fails_only_its_environment(tmp_path, released):
    publish(tmp_path, PROD_TAG, released["text"], released["record"], prerelease=False, published="2026-10-29T15:05:00Z")
    name, result = run(tmp_path, REPO_INPUT=REPO, FAKE_DOWNLOAD_FAIL=PROD_TAG)
    assert result.returncode == 1 and f"::error::apps/acme/notes/prod: cannot download the assets of release {PROD_TAG}" in result.stdout
    assert [c[4]["head"] for c in api(tmp_path, "POST") if c[3].endswith("/pulls")] == ["bump/acme-notes-test"]


def test_an_app_of_another_owner_is_refused_before_any_call_for_it(tmp_path, released):
    name, result = run(tmp_path, REPO_INPUT="other/notes")
    assert result.returncode == 1 and "::error::apps/other/notes: other/notes is not in acme" in result.stdout_all
    assert not (tmp_path / "calls.jsonl.token").exists(), "nothing to pin: no token"
    assert [c for c in calls(tmp_path, "gh") if "other/notes" in json.dumps(c)] == []


# ---- an open pull request on an older main is rebuilt ----

def test_an_open_pr_on_an_older_main_is_rebuilt_on_the_current_one(tmp_path, released):
    assert run(tmp_path, REPO_INPUT=REPO)[1].returncode == 0
    (released["deploy"] / "README.md").write_text("main moved on\n")
    commit_all(released["deploy"], "main moved on")
    new_base = git(released["deploy"], "rev-parse", "HEAD").strip()
    name, result = run(tmp_path, REPO_INPUT=REPO)
    assert result.returncode == 0, result.stdout
    assert "not on the current main; rebuilding it" in result.stdout
    commits = [c[4] for c in api(tmp_path, "POST") if c[3].endswith("/git/commits")]
    assert len(commits) == 2 and commits[1]["parents"] == [new_base]
    assert [c[3] for c in api(tmp_path, "PATCH")] == ["repos/acme/golden-road-deploy/git/refs/heads/bump/acme-notes-test",
                                                       "repos/acme/golden-road-deploy/pulls/1"]
    assert len([c for c in api(tmp_path, "POST") if c[3].endswith("/pulls")]) == 1
    # and on the current main it is left alone
    before = len(writes(tmp_path))
    name, result = run(tmp_path, REPO_INPUT=REPO)
    assert "already proposed on the current main" in result.stdout and len(writes(tmp_path)) == before


# ---- a self-approved promotion is named in the prod pull request ----

@pytest.mark.parametrize("fields,named", [({"self_approved": True, "gate2_single_approver": True}, True), ({}, False)])
def test_a_self_approved_promotion_is_named_in_the_prod_pr(tmp_path, released, fields, named):
    d = publish(tmp_path, PROD_TAG, released["text"], released["record"], prerelease=False, published="2026-10-29T15:05:00Z")
    (d / "promotion-record.json").write_text(json.dumps(promotion_record(released["text"], **fields)))
    name, result = run(tmp_path, REPO_INPUT=REPO)
    assert result.returncode == 0, result.stdout
    pulls = {c[4]["head"]: c[4] for c in api(tmp_path, "POST") if c[3].endswith("/pulls")}
    assert ("**Single-approver promotion:**" in pulls["bump/acme-notes-prod"]["body"]) is named
    assert "Single-approver" not in pulls["bump/acme-notes-test"]["body"]


def test_the_flux_source_must_be_this_repository(tmp_path, released):
    """compose --expect-url: an adapter would reconcile whatever repo gitrepository.yaml names, so bump refuses another one."""
    path = released["deploy"] / "clusters/home/flux/gitrepository.yaml"
    path.write_text(re.sub(r"url: \S+", "url: https://github.com/evil/golden-road-deploy", path.read_text()))
    commit_all(released["deploy"])
    name, result = run(tmp_path, REPO_INPUT=REPO)
    assert result.returncode == 1 and "compose refused" in result.stdout and "is not this repository" in result.stderr
    assert writes(tmp_path) == []


# ---- workflow mode (configure.py --bump-workflow-token, the personal PoC): this workflow's own token writes, and deploy-check is
# dispatched on each branch it opens or updates (spec 2026-10-30, B1-B3) ----

def dispatches(tmp_path):
    return [c for c in calls(tmp_path, "gh") if c[1:3] == ["workflow", "run"]]


def writes_and_dispatches(tmp_path):
    """The ref and pull request writes and the dispatches, in order, as (verb, path or ref)."""
    seq = []
    for c in calls(tmp_path, "gh"):
        if c[1:3] == ["workflow", "run"]:
            seq.append(("dispatch", c[-2]))
        elif c[1] == "api" and c[2] in ("POST", "PATCH") and re.search(r"/(git/refs.*|pulls(/[0-9]+)?)$", c[3]):
            seq.append((c[2], c[3].removeprefix("repos/acme/golden-road-deploy/")))
    return seq


def test_workflow_mode_writes_with_the_workflow_token_and_never_mints_or_reads_the_app(tmp_path, released):
    name, result = run(tmp_path, "workflow", REPO_INPUT=REPO, APP_ID="", APP_KEY="")
    assert result.returncode == 0, (name, result.stdout, result.stderr)
    assert not (tmp_path / "calls.jsonl.token").exists() and not [c for c in calls(tmp_path, "docker") if "app-token" in c]
    assert not (tmp_path / "runner-temp/deploy-token").exists()
    assert writes(tmp_path) and all(c[-1]["GH_TOKEN"] == "github-token" for c in calls(tmp_path, "gh"))


def test_workflow_mode_dispatches_deploy_check_right_after_the_branch_moves_before_the_pr_call(tmp_path, released):
    publish(tmp_path, PROD_TAG, released["text"], released["record"], prerelease=False, published="2026-10-29T15:05:00Z")
    name, result = run(tmp_path, "workflow", REPO_INPUT=REPO)
    assert result.returncode == 0, result.stdout
    assert writes_and_dispatches(tmp_path) == [("POST", "git/refs"), ("dispatch", "bump/acme-notes-test"), ("POST", "pulls"),
                                               ("POST", "git/refs"), ("dispatch", "bump/acme-notes-prod"), ("POST", "pulls")]
    [d] = [c for c in dispatches(tmp_path) if c[-2] == "bump/acme-notes-test"]
    assert d[1:-1] == ["workflow", "run", "deploy-check.yml", "--repo", "acme/golden-road-deploy", "--ref", "bump/acme-notes-test"]
    assert "apps/acme/notes/test: started deploy-check on bump/acme-notes-test" in result.stdout


def test_a_pr_call_that_fails_after_the_branch_moved_still_leaves_a_check_started(tmp_path, released):
    name, result = run(tmp_path, "workflow", REPO_INPUT=REPO, FAKE_PULL_CREATE_FAIL="1")
    assert result.returncode == 1 and "cannot open the pull request" in result.stdout
    assert writes_and_dispatches(tmp_path) == [("POST", "git/refs"), ("dispatch", "bump/acme-notes-test"), ("POST", "pulls")]  # refused


def test_a_force_update_dispatches_again(tmp_path, released):
    assert run(tmp_path, "workflow", REPO_INPUT=REPO)[1].returncode == 0
    (released["deploy"] / "README.md").write_text("main moved on\n")
    commit_all(released["deploy"], "main moved on")
    name, result = run(tmp_path, "workflow", REPO_INPUT=REPO)
    assert result.returncode == 0, result.stdout
    assert writes_and_dispatches(tmp_path)[-3:] == [("PATCH", "git/refs/heads/bump/acme-notes-test"), ("dispatch", "bump/acme-notes-test"),
                                                    ("PATCH", "pulls/1")]


def test_a_pr_already_current_is_dispatched_again_only_while_its_head_has_no_completed_deploy_check(tmp_path, released):
    assert run(tmp_path, "workflow", REPO_INPUT=REPO)[1].returncode == 0
    assert len(dispatches(tmp_path)) == 1
    name, result = run(tmp_path, "workflow", REPO_INPUT=REPO, FAKE_DEPLOY_CHECK_DONE="1")
    assert result.returncode == 0 and "already proposed on the current main" in result.stdout and len(dispatches(tmp_path)) == 1
    [read] = [c for c in calls(tmp_path, "gh") if c[1] == "api" and c[3].endswith("check-runs?check_name=deploy-check&filter=all")]
    assert read[3].startswith("repos/acme/golden-road-deploy/commits/c") and read[-1]["GH_TOKEN"] == "github-token"
    name, result = run(tmp_path, "workflow", REPO_INPUT=REPO)  # the run is still in progress, or the first dispatch never ran
    assert result.returncode == 0 and len(dispatches(tmp_path)) == 2 and dispatches(tmp_path)[-1][-2] == "bump/acme-notes-test"
    assert "no completed deploy-check" in result.stdout


def test_a_check_run_read_that_fails_fails_the_run_and_dispatches_nothing(tmp_path, released):
    assert run(tmp_path, "workflow", REPO_INPUT=REPO)[1].returncode == 0
    name, result = run(tmp_path, "workflow", REPO_INPUT=REPO, FAKE_CHECK_RUNS_FAIL="1")
    assert result.returncode == 1 and "::error::apps/acme/notes/test: cannot read deploy-check's runs on bump/acme-notes-test" in result.stdout
    assert len(dispatches(tmp_path)) == 1


def test_app_mode_never_reads_check_runs_for_a_current_pr(tmp_path, released):
    assert run(tmp_path, REPO_INPUT=REPO)[1].returncode == 0
    assert run(tmp_path, REPO_INPUT=REPO)[1].returncode == 0
    assert not [c for c in calls(tmp_path, "gh") if c[1] == "api" and "check-runs" in c[3]]


def test_a_dispatch_that_fails_fails_the_run_and_says_how_to_start_it_but_the_others_still_open(tmp_path, released):
    publish(tmp_path, PROD_TAG, released["text"], released["record"], prerelease=False, published="2026-10-29T15:05:00Z")
    name, result = run(tmp_path, "workflow", REPO_INPUT=REPO, FAKE_DISPATCH_FAIL="bump/acme-notes-test")
    assert result.returncode == 1 and "at least one app or environment could not be bumped" in result.stdout
    assert ("::error::apps/acme/notes/test: cannot start deploy-check on bump/acme-notes-test; start it by hand "
            "(gh workflow run deploy-check.yml --ref bump/acme-notes-test)") in result.stdout
    assert [c[4]["head"] for c in api(tmp_path, "POST") if c[3].endswith("/pulls")] == ["bump/acme-notes-test", "bump/acme-notes-prod"]
    assert [c[-2] for c in dispatches(tmp_path)] == ["bump/acme-notes-test", "bump/acme-notes-prod"]


def test_app_mode_never_dispatches(tmp_path, released):
    assert run(tmp_path, REPO_INPUT=REPO)[1].returncode == 0
    assert dispatches(tmp_path) == [] and [c for c in api(tmp_path, "POST") if c[3].endswith("/pulls")]

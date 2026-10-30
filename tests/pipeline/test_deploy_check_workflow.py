"""golden-road-deploy's deploy-check: its step scripts run with stand-in docker, gh and cosign (tests/pipeline/deploy_fakes.py). The
docker stand-in runs the real golden-road deploy-verify and deploy-pins, so drift and the off-by-default check are the real code."""

import json
import re
import shutil

import pytest
import yaml

from deploy_fakes import (DEPLOY_BOOTSTRAP, IMAGE, PROD_TAG, REPO, ROOT, TEST_TAG, attest, base_env, calls, commit_all, deploy_repo,
                          gate_record, load, manifests, promotion_record, publish, run_step, step, unregister, write_pin)

WORKFLOW = DEPLOY_BOOTSTRAP / ".github/workflows/deploy-check.yml"
pytestmark = pytest.mark.skipif(shutil.which("jq") is None or shutil.which("git") is None, reason="jq and git are needed")
STEPS = ("Changed paths", "Compose and verify every environment", "Every changed environment's app has an intake record",
         "Verify every changed release is the platform's signed release")


def doc():
    return load(WORKFLOW)


def run(tmp_path, upto=len(STEPS), **env):
    """Run the scripted steps in order, stopping at the first failure; returns (the step that ran last, its result)."""
    environ = base_env(tmp_path, doc(), **env)
    for name in STEPS[:upto]:
        result = run_step(tmp_path, step(doc(), name)["run"], environ)
        if result.returncode != 0:
            return name, result
    return name, result


def errors(result):
    return [line for line in result.stdout.splitlines() if line.startswith("::error::")]


@pytest.fixture
def release(tmp_path):
    """A published test release of acme/notes and its gate attestation, and a deploy repo whose PR pins it in test."""
    text = manifests(tmp_path)
    record = gate_record(manifests_text=text)
    publish(tmp_path, TEST_TAG, text, record)
    attest(tmp_path, "gate", record)
    deploy = deploy_repo(tmp_path)
    write_pin(deploy, "test", TEST_TAG, text, record)
    commit_all(deploy)
    return {"text": text, "record": record, "deploy": deploy}


def prod(tmp_path, pinned, **promotion):
    """The same image promoted: a prod release whose gate record is the attested one, and its promotion attestation."""
    publish(tmp_path, PROD_TAG, pinned["text"], pinned["record"], prerelease=False)
    attest(tmp_path, "promotion", promotion_record(pinned["text"], **promotion))
    write_pin(pinned["deploy"], "prod", PROD_TAG, pinned["text"], pinned["record"])
    commit_all(pinned["deploy"], "prod")


# ---- the shape ----

def test_the_shape():
    d = doc()
    assert d["on"] == {"pull_request": {"branches": ["main"]}} and d["permissions"] == {}
    [(name, job)] = d["jobs"].items()
    assert job["name"] == "deploy-check" and job["permissions"] == {"contents": "read", "packages": "read"}
    assert "pull_request_target" not in WORKFLOW.read_text()
    for s in job["steps"]:
        assert "${{" not in s.get("run", ""), s.get("name")
        if "uses" in s:
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", s["uses"]), s["uses"]
    envs = {k: v for s in job["steps"] for k, v in (s.get("env") or {}).items()}
    assert envs == {"GH_TOKEN": "${{ github.token }}"}  # the only expression a script sees, through env
    assert [s["name"] for s in job["steps"] if "GH_TOKEN" in (s.get("env") or {})] == [STEPS[2], STEPS[3]]
    checkout = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"]["persist-credentials"] is False


def test_the_check_name_is_the_one_the_ruleset_requires():
    ruleset = json.loads((DEPLOY_BOOTSTRAP / ".github/rulesets/main.json").read_text())
    contexts = [c["context"] for r in ruleset["rules"] if r["type"] == "required_status_checks" for c in r["parameters"]["required_status_checks"]]
    assert contexts == [next(iter(doc()["jobs"].values()))["name"]]


def test_the_placeholder_builder_image_is_refused_first(tmp_path):
    first = next(iter(doc()["jobs"].values()))["steps"][0]
    assert first["name"] == "Refuse the placeholder builder image"
    zero = "ghcr.io/acme/golden-road-builder@sha256:" + "0" * 64
    assert run_step(tmp_path, first["run"], base_env(tmp_path, doc(), BUILDER_IMAGE=zero)).returncode == 1
    assert run_step(tmp_path, first["run"], base_env(tmp_path, doc())).returncode == 0


def test_the_builder_image_is_pulled_then_the_registry_logged_out_before_the_pr_tree_is_read():
    steps = next(iter(doc()["jobs"].values()))["steps"]
    pull = next(i for i, s in enumerate(steps) if "docker pull" in s.get("run", ""))
    assert "docker logout ghcr.io" in steps[pull]["run"]
    assert pull < next(i for i, s in enumerate(steps) if "deploy-verify" in s.get("run", ""))


def test_the_single_approver_setting_is_refuse_unless_configured_for_the_poc():
    platform = json.loads((ROOT / "platform/platform.json").read_text())
    want = "accept" if platform.get("gate2_single_approver") is True else "refuse"
    assert doc()["env"]["SINGLE_APPROVER_PROMOTIONS"] == want


def test_the_cosign_identities_are_the_release_and_promote_workflows_fully_pinned():
    run_ = step(doc(), STEPS[3])["run"]
    for flag in ('--certificate-identity "${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY_OWNER}/platform-pipeline/.github/workflows/golden-road.yml@refs/heads/main"',
                 '--certificate-identity "${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY_OWNER}/platform-pipeline/.github/workflows/golden-road-promote.yml@refs/heads/main"',
                 '--certificate-github-workflow-repository "$repo"', "--certificate-github-workflow-ref refs/heads/main",
                 "--certificate-github-workflow-trigger push", "--certificate-github-workflow-trigger workflow_dispatch"):
        assert flag in run_, flag
    assert run_.count("--certificate-oidc-issuer https://token.actions.githubusercontent.com") == 2
    assert "yq" not in WORKFLOW.read_text()


# ---- a clean PR ----

def test_a_clean_test_pin_passes(tmp_path, release):
    name, result = run(tmp_path)
    assert result.returncode == 0, (name, result.stdout, result.stderr)
    assert "apps/acme/notes/test: test-20261029132351-42fa829 is the platform's signed release of acme/notes" in result.stdout
    cosign = calls(tmp_path, "cosign")
    assert [c[1] for c in cosign] == ["verify", "verify-attestation"]
    for c in cosign:
        assert c[c.index("--certificate-github-workflow-repository") + 1] == REPO and c[-1] == IMAGE
        assert c[c.index("--certificate-identity") + 1] == "https://github.com/acme/platform-pipeline/.github/workflows/golden-road.yml@refs/heads/main"
    intake, download = calls(tmp_path, "gh")
    assert intake[1:4] == ["api", "GET", "repos/acme/scorecard-factory/contents/intake/acme/notes.yaml?ref=main"]
    assert intake[-1]["GH_TOKEN"] == "github-token"
    assert download[1:4] == ["release", "download", TEST_TAG] and download[download.index("--repo") + 1] == REPO


def test_a_clean_prod_pin_passes(tmp_path, release):
    prod(tmp_path, release)
    name, result = run(tmp_path)
    assert result.returncode == 0, (name, result.stdout, result.stderr)
    promo = [c for c in calls(tmp_path, "cosign") if "workflow_dispatch" in c]
    assert len(promo) == 1 and "https://golden-road.internal/promotion/v1" in promo[0]


def test_a_pr_that_changes_only_bindings_verifies_no_signature(tmp_path, release):
    commit_all(release["deploy"], "again")  # an empty PR: nothing changed
    name, result = run(tmp_path)
    assert result.returncode == 0 and calls(tmp_path, "cosign") == [] and "0 changed release pins verified" in result.stdout


# ---- each failure, with its own message ----

def test_a_path_outside_the_layout_fails(tmp_path, release):
    (release["deploy"] / "apps/acme/notes/test/notes.txt").write_text("x\n")
    (release["deploy"] / "scripts").mkdir()
    (release["deploy"] / "scripts/run.sh").write_text("x\n")
    commit_all(release["deploy"])
    name, result = run(tmp_path)
    assert name == "Changed paths" and result.returncode == 1
    assert errors(result) == ["::error::the pull request changes paths outside the deploy layout (apps/<owner>/<repo>/<test|prod>/..., clusters/, policy/, external-secrets/, .github/)"]
    assert '"apps/acme/notes/test/notes.txt"' in result.stdout and '"scripts/run.sh"' in result.stdout


@pytest.mark.parametrize("path", ["apps/acme/notes/staging/release.yaml", "clusters/home/flux/Evil.yaml", "clusters/prod/flux/x.yaml",
                                  "apps/acme/notes/test/patches/egress.yaml", "apps/acme/notes/test/base/other.yaml",
                                  "external-secrets/extra.yaml", "external-secrets/sub/README.md", "external-secrets/README.md.bak"])
def test_each_path_outside_the_layout_fails(tmp_path, release, path):
    target = release["deploy"] / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x: 1\n")
    commit_all(release["deploy"])
    name, result = run(tmp_path, upto=1)
    assert result.returncode == 1 and json.dumps(path) in result.stdout


def test_a_pr_that_changes_only_the_external_secrets_readme_passes(tmp_path, release):
    readme = release["deploy"] / "external-secrets/README.md"
    readme.write_text(readme.read_text() + "\nA note.\n")
    commit_all(release["deploy"])
    name, layout = run(tmp_path, upto=1)
    assert layout.returncode == 0 and "1 changed paths, all inside the deploy layout" in layout.stdout, layout.stdout
    name, result = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_path_with_a_newline_fails_and_never_reaches_a_workflow_command(tmp_path, release):
    (release["deploy"] / "apps/acme/notes/test/x\n::warning::injected").write_text("x\n")
    commit_all(release["deploy"])
    name, result = run(tmp_path, upto=1)
    assert result.returncode == 1
    assert not any(line.startswith("::warning::") for line in result.stdout.splitlines())


def test_a_manifests_hash_mismatch_fails(tmp_path, release):
    (tmp_path / "releases" / REPO / TEST_TAG / "manifests.yaml").write_text(release["text"] + "# swapped\n")
    name, result = run(tmp_path)
    assert name == STEPS[3] and errors(result) == ["::error::apps/acme/notes/test/release.yaml: the release's manifests.yaml does not match manifests_sha256"]
    assert calls(tmp_path, "cosign") == []


def test_a_gate_record_hash_mismatch_fails(tmp_path, release):
    path = tmp_path / "releases" / REPO / TEST_TAG / "gate-record.json"
    path.write_text(path.read_text() + "\n")
    name, result = run(tmp_path)
    assert errors(result) == ["::error::apps/acme/notes/test/release.yaml: the release's gate-record.json does not match gate_record_sha256"]


def test_a_base_that_is_not_the_releases_fails(tmp_path, release):
    # The release's manifests are swapped after the pin: the pinned hash still names the base, but the release no longer serves it.
    # deploy-verify passes (the base matches release.yaml); the signature step compares the release's own file with the base.
    rel = tmp_path / "releases" / REPO / TEST_TAG
    other = release["text"].replace("notes-api", "notes-apx", 1)
    import hashlib
    (rel / "manifests.yaml").write_text(other)
    d = release["deploy"] / "apps/acme/notes/test"
    pin = yaml.safe_load((d / "release.yaml").read_text())
    pin["manifests_sha256"] = hashlib.sha256(other.encode()).hexdigest()
    (d / "release.yaml").write_text(yaml.safe_dump(pin, sort_keys=False))
    # deploy-verify would refuse this tree (the base's hash), so run only the signature step on pins that say otherwise
    environ = base_env(tmp_path, doc())
    assert run_step(tmp_path, step(doc(), STEPS[0])["run"], environ).returncode == 0
    (tmp_path / "out/pins.json").write_text(json.dumps({"apps/acme/notes/test": pin}))
    result = run_step(tmp_path, step(doc(), STEPS[3])["run"], environ)
    assert errors(result) == ["::error::apps/acme/notes/test/release.yaml: base/manifests.yaml is not the release's manifests.yaml"]


def test_drift_fails_in_deploy_verify(tmp_path, release):
    d = release["deploy"] / "apps/acme/notes/test"
    (d / "composed.yaml").write_text((d / "composed.yaml").read_text() + "# hand edit\n")
    commit_all(release["deploy"])
    name, result = run(tmp_path)
    assert name == STEPS[1] and result.returncode == 1
    assert errors(result)[0].startswith("::error::deploy-verify found 1 problems")
    assert '"apps/acme/notes/test/composed.yaml differs from what golden-road compose writes"' in result.stdout


def test_a_base_edited_in_the_pr_fails_in_deploy_verify(tmp_path, release):
    d = release["deploy"] / "apps/acme/notes/test"
    (d / "base/manifests.yaml").write_text(release["text"] + "# extra\n")
    commit_all(release["deploy"])
    name, result = run(tmp_path)
    assert name == STEPS[1] and "base/manifests.yaml is not the release's manifests" in result.stdout


@pytest.mark.parametrize("edit,needle", [
    ("suspend", "suspend: true"), ("argo", "automated"), ("policy", "warn"), ("stray", "clusters/"),
], ids=["unsuspended-kustomization", "automated-argo-sync", "enforcing-policy", "stray-clusters-file"])
def test_anything_not_off_by_default_fails_in_deploy_verify(tmp_path, release, edit, needle):
    deploy = release["deploy"]
    if edit == "suspend":
        path = deploy / "clusters/home/flux/acme-notes-test.yaml"
        path.write_text(path.read_text().replace("suspend: true", "suspend: false"))
    elif edit == "argo":
        path = deploy / "clusters/office/argo/acme-notes-test.yaml"
        data = yaml.safe_load(path.read_text())
        data["spec"]["syncPolicy"] = {"automated": {"prune": True}}
        path.write_text(yaml.safe_dump(data))
    elif edit == "policy":
        path = deploy / "policy/cluster-image-policy.yaml"
        path.write_text(path.read_text().replace("mode: warn", "mode: enforce"))
    else:
        (deploy / "clusters/home/extra.yaml").write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: x}\n")
    commit_all(deploy)
    name, result = run(tmp_path)
    assert result.returncode == 1
    if edit == "stray":  # outside the layout, so the changed-paths step stops it first; deploy-verify refuses it on its own too
        assert name == "Changed paths"
        result = run_step(tmp_path, step(doc(), STEPS[1])["run"], base_env(tmp_path, doc()))
        assert result.returncode == 1
    else:
        assert name == STEPS[1]
    assert needle in result.stdout, result.stdout


def test_cosign_verify_failing_fails(tmp_path, release):
    name, result = run(tmp_path, FAKE_COSIGN_VERIFY_RC=1)
    assert errors(result) == ["::error::apps/acme/notes/test/release.yaml: ghcr.io/acme/notes-api@sha256:" + "b" * 64
                              + " has no signature from the platform's release workflow for acme/notes"]


def test_verify_attestation_failing_fails(tmp_path, release):
    name, result = run(tmp_path, FAKE_COSIGN_GATE_RC=1)
    assert errors(result) == ["::error::apps/acme/notes/test/release.yaml: ghcr.io/acme/notes-api@sha256:" + "b" * 64
                              + " has no gate-record attestation from the platform's release workflow"]


@pytest.mark.parametrize("predicates,message", [
    ("none", "no single passing gate attestation for " + TEST_TAG),
    ("failed", "no single passing gate attestation for " + TEST_TAG),
    ("other-release", "no single passing gate attestation for " + TEST_TAG),
    ("other-image", "the release's gate-record.json is not the attested gate record"),
])
def test_the_attested_gate_record_must_be_the_releases(tmp_path, release, predicates, message):
    record = release["record"]
    chosen = {"failed": [{**record, "status": "fail"}], "other-release": [{**record, "release": "test-20261029000000-42fa829"}],
              "other-image": [{**record, "image": "ghcr.io/acme/notes-api@sha256:" + "c" * 64}], "none": []}[predicates]
    attest(tmp_path, "gate", *chosen)
    name, result = run(tmp_path)
    assert errors(result) == [f"::error::apps/acme/notes/test/release.yaml: {message}"]


def test_another_orgs_app_is_refused_before_any_call(tmp_path, release):
    name, result = run(tmp_path, GITHUB_REPOSITORY_OWNER="other")
    assert errors(result) == ["::error::apps/acme/notes/test: acme/notes is not in other"] and calls(tmp_path, "gh") == []
    # the signature step refuses it on its own too
    result = run_step(tmp_path, step(doc(), STEPS[3])["run"], base_env(tmp_path, doc(), GITHUB_REPOSITORY_OWNER="other"))
    assert errors(result) == ["::error::apps/acme/notes/test/release.yaml: acme/notes is not in other"]


def test_an_app_without_an_intake_record_fails(tmp_path, release):
    unregister(tmp_path)
    name, result = run(tmp_path)
    assert name == STEPS[2] and errors(result) == [
        "::error::apps/acme/notes/test: acme/notes has no intake record (intake/acme/notes.yaml on main in acme/scorecard-factory); register the app before pinning it"]
    assert calls(tmp_path, "cosign") == []


def test_an_intake_api_error_fails_closed(tmp_path, release):
    name, result = run(tmp_path, FAKE_INTAKE_ERROR="1")
    assert name == STEPS[2] and errors(result) == ["::error::apps/acme/notes/test: cannot read acme/notes's intake record in acme/scorecard-factory (gh api exit 1)"]


def test_a_bindings_only_change_still_needs_the_intake_record(tmp_path, release):
    d = release["deploy"] / "apps/acme/notes/test"
    commit_all(release["deploy"], "again")
    unregister(tmp_path)
    (tmp_path / "out").mkdir(exist_ok=True)
    environ = base_env(tmp_path, doc())
    (tmp_path / "out/changed.txt").write_text("apps/acme/notes/test/bindings.yaml\n")
    assert run_step(tmp_path, step(doc(), STEPS[1])["run"], environ).returncode == 0
    result = run_step(tmp_path, step(doc(), STEPS[2])["run"], environ)
    assert result.returncode == 1 and "has no intake record" in result.stdout and d.is_dir()


def test_a_prod_pin_without_a_promotion_fails(tmp_path, release):
    prod(tmp_path, release)
    name, result = run(tmp_path, FAKE_COSIGN_PROMOTION_RC=1)
    assert errors(result) == ["::error::apps/acme/notes/prod/release.yaml: a prod pin needs a promoted release: ghcr.io/acme/notes-api@sha256:"
                              + "b" * 64 + " has no promotion attestation"]


@pytest.mark.parametrize("promotion", [{"release": "test-20261029000000-42fa829"}, {"image": "ghcr.io/acme/notes-api@sha256:" + "c" * 64},
                                       {"manifests_sha256": "f" * 64}], ids=["other-release", "other-image", "other-manifests"])
def test_a_promotion_of_something_else_fails(tmp_path, release, promotion):
    prod(tmp_path, release, **promotion)
    name, result = run(tmp_path)
    [error] = errors(result)
    assert error.startswith("::error::apps/acme/notes/prod/release.yaml: a prod pin needs a promoted release: no promotion of " + TEST_TAG)


@pytest.mark.parametrize("fields", [{"self_approved": True}, {"gate2_single_approver": True}, {"self_approved": "false"}],
                         ids=["self-approved", "single-approver", "not-a-boolean"])
def test_a_single_approver_promotion_is_refused_by_default(tmp_path, release, fields):
    prod(tmp_path, release, **fields)
    name, result = run(tmp_path, SINGLE_APPROVER_PROMOTIONS="refuse")
    [error] = errors(result)
    assert "approved by someone other than the person who triggered it" in error


def test_the_poc_setting_accepts_a_single_approver_promotion_but_never_a_malformed_one(tmp_path, release):
    prod(tmp_path, release, self_approved=True, gate2_single_approver=True)
    name, result = run(tmp_path, SINGLE_APPROVER_PROMOTIONS="accept")
    assert result.returncode == 0, result.stdout
    warning = "apps/acme/notes/prod/release.yaml: accepted a single-approver (self-approved) promotion; PoC only, never valid for production"
    assert result.stdout.splitlines().count("::warning::" + warning) == 1
    assert (tmp_path / "summary.md").read_text() == f"- {warning}\n"
    attest(tmp_path, "promotion", {k: v for k, v in promotion_record(release["text"]).items() if k != "self_approved"})
    name, result = run(tmp_path, SINGLE_APPROVER_PROMOTIONS="accept")
    assert result.returncode == 1


def test_a_two_person_promotion_raises_no_warning_in_the_poc_setting(tmp_path, release):
    prod(tmp_path, release)
    name, result = run(tmp_path, SINGLE_APPROVER_PROMOTIONS="accept")
    assert result.returncode == 0 and "::warning::" not in result.stdout and not (tmp_path / "summary.md").exists()


def test_an_unknown_single_approver_setting_fails(tmp_path, release):
    name, result = run(tmp_path, SINGLE_APPROVER_PROMOTIONS="yes")
    assert errors(result) == ["::error::SINGLE_APPROVER_PROMOTIONS must be refuse or accept"]


def test_a_prod_pin_of_another_commits_release_fails(tmp_path, release):
    prod(tmp_path, release)
    d = release["deploy"] / "apps/acme/notes/prod"
    pin = yaml.safe_load((d / "release.yaml").read_text())
    other = "prod-20261029150000-1234567"
    rel = tmp_path / "releases" / REPO
    shutil.copytree(rel / PROD_TAG, rel / other)
    (d / "release.yaml").write_text(yaml.safe_dump({**pin, "release": other}, sort_keys=False))
    commit_all(release["deploy"])
    name, result = run(tmp_path)
    assert errors(result) == ["::error::apps/acme/notes/prod/release.yaml: the gate record does not name a test release of " + other + "'s commit"]


def test_a_removed_environment_needs_no_signature(tmp_path, release):
    deploy = release["deploy"]
    shutil.rmtree(deploy / "apps/acme")
    for adapter in ("clusters/home/flux/acme-notes-test.yaml", "clusters/office/argo/acme-notes-test.yaml"):
        (deploy / adapter).unlink()
    commit_all(deploy)
    name, result = run(tmp_path)
    assert result.returncode == 0, result.stdout
    assert "apps/acme/notes/test: removed" in result.stdout
    assert calls(tmp_path, "cosign") == []


@pytest.mark.parametrize("url", ["https://github.com/evil/golden-road-deploy", "https://ghe.example.com/acme/golden-road-deploy"])
def test_a_flux_source_that_is_not_this_repository_fails_in_deploy_verify(tmp_path, release, url):
    """Every adapter reconciles the repo gitrepository.yaml names, so it must be $GITHUB_SERVER_URL/$GITHUB_REPOSITORY. Re-composing
    the adapters from the other url keeps them consistent with the file, so only the pin catches it."""
    from golden_road.cli import main
    path = release["deploy"] / "clusters/home/flux/gitrepository.yaml"
    path.write_text(re.sub(r"url: \S+", f"url: {url}", path.read_text()))
    assert main(["compose", "--root", str(release["deploy"]), "apps/acme/notes/test"]) == 0
    commit_all(release["deploy"])
    name, result = run(tmp_path)
    assert name == STEPS[1] and result.returncode == 1
    assert f"url {url} is not this repository, https://github.com/acme/golden-road-deploy" in result.stdout


def test_the_flux_source_url_compares_without_case(tmp_path, release):
    name, result = run(tmp_path, GITHUB_REPOSITORY="ACME/golden-road-deploy")
    assert result.returncode == 0, result.stdout

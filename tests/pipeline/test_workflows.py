import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from helpers import scorecard_integration_id

ROOT = Path(__file__).resolve().parents[2]
SCANS = ROOT / "reference/security-scans"
DIRS = [ROOT / ".github/workflows", ROOT / "bootstrap/hello-app/.github/workflows", ROOT / "bootstrap/golden-road-portal/.github/workflows",
        ROOT / "bootstrap/golden-road-agent-template/.github/workflows",
        ROOT / "bootstrap/scorecard-factory/.github/workflows",
        ROOT / "bootstrap/golden-road-agents/.github/workflows", ROOT / "bootstrap/golden-road-deploy/.github/workflows", SCANS / ".github/workflows"]
WORKFLOWS = sorted(p for d in DIRS if d.is_dir() for p in d.glob("*.yml"))
ACTIONLINT = "rhysd/actionlint@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667"
SHA_PIN = re.compile(r"^[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}$")
REUSABLE = re.compile(r"^[A-Za-z0-9_-]+/(platform-pipeline/\.github/workflows/golden-road(-promote)?\.yml|security-scans/\.github/workflows/scan\.yml"
                      r"|scorecard-factory/\.github/workflows/scorecard\.yml|scorecard-factory/\.github/actions/(intake|app-token))@main$")


def load(path):
    doc = yaml.safe_load(path.read_text())
    doc["on"] = doc.pop(True, doc.get("on"))
    return doc


def uses(doc):
    for job in doc.get("jobs", {}).values():
        if "uses" in job:
            yield job["uses"]
        for step in job.get("steps", []):
            if "uses" in step:
                yield step["uses"]


def test_expected_workflows_exist():
    expected = {"golden-road.yml", "golden-road-promote.yml", "release-builder.yml", "release-agent-runtime.yml", "test.yml", "ci.yml", "promote.yml", "security-scans.yml", "factory-check.yml", "scorecard.yml", "release-skill.yml", "scorecard-poll.yml", "scorecard-assess.yml", "deploy-check.yml", "bump.yml"}
    assert expected | ({"scan.yml"} if SCANS.is_dir() else set()) <= {p.name for p in WORKFLOWS}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_policy(path):
    text = path.read_text()
    doc = load(path)
    assert "pull_request_target" not in text
    assert "permissions" in doc
    for value in uses(doc):
        assert SHA_PIN.match(value) or REUSABLE.match(value) or value.startswith("ORG_PLACEHOLDER/"), value
    for match in re.finditer(r"_IMAGE:\s*(\S+)", text):
        assert re.search(r"@sha256:[0-9a-f]{64}$", match.group(1)), match.group(1)


def test_reusable_release_shape():
    doc = load(ROOT / ".github/workflows/golden-road.yml")
    assert list(doc["on"]) == ["workflow_call"] and doc["permissions"] == {}
    drift, release = doc["jobs"]["drift"], doc["jobs"]["release"]
    assert "id-token" not in drift["permissions"] and drift["permissions"].get("packages", "read") == "read"
    assert drift["permissions"].get("contents") == "read"
    assert release["needs"] == "drift"
    assert "github.event_name == 'push'" in release["if"] and "refs/heads/main" in release["if"]
    assert release["permissions"]["id-token"] == "write"
    steps = " ".join(s.get("run", "") for s in release["steps"])
    order = [steps.index(k) for k in ("checks wait", "buildx build", "authz-probe", "golden-road render", "golden-road record", "cosign sign", "gh release create")]
    assert order == sorted(order)


def test_promote_requires_environment():
    doc = load(ROOT / ".github/workflows/golden-road-promote.yml")
    [job] = doc["jobs"].values()
    assert job["environment"] == "prod"
    text = (ROOT / ".github/workflows/golden-road-promote.yml").read_text()
    assert text.index("cosign verify-attestation") < text.index("promotion-record") < text.index("cosign attest")


def test_only_release_and_promote_sign():
    for path in WORKFLOWS:
        if path.name not in {"golden-road.yml", "golden-road-promote.yml", "release-builder.yml", "release-agent-runtime.yml"}:
            assert "cosign sign" not in path.read_text() and "cosign attest" not in path.read_text(), path


@pytest.mark.docker
@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_actionlint():
    files = [str(p.relative_to(ROOT)) for p in WORKFLOWS]
    # actionlint 1.7.12 predates the `job.workflow_repository` and `job.workflow_sha` context properties, which scan.yml uses to
    # check out its own scripts at its own commit (GitHub documents both). Only that one message is ignored.
    ignore = r'property "workflow_(repository|sha)" is not defined in object type \{check_run_id'
    out = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{ROOT}:/repo:ro", "-w", "/repo", ACTIONLINT, "-ignore", ignore, *files],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr


ZERO_DIGEST = "0" * 64


def all_steps(doc):
    for name, job in doc.get("jobs", {}).items():
        for step in job.get("steps", []):
            yield name, step


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_expression_in_run_scripts(path):
    for job, step in all_steps(load(path)):
        assert "${{" not in step.get("run", ""), f"{path.name}:{job}: pass expressions through env:\n{step['run']}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_scripts_fail_closed(path):
    for job, step in all_steps(load(path)):
        run = step.get("run", "")
        if "\n" in run.strip():
            assert run.lstrip().startswith("set -euo pipefail"), f"{path.name}:{job}: {step.get('name')}"
        for line in run.splitlines():
            assert "|| true" not in line or line.lstrip().startswith("trap "), f"{path.name}:{job}: {line}"
        if step.get("continue-on-error") and job in {"release", "promote"}:
            raise AssertionError(f"{path.name}:{job}: gates must not continue on error")


def test_drift_job_is_read_only_and_cannot_sign():
    doc = load(ROOT / ".github/workflows/golden-road.yml")
    perms = doc["jobs"]["drift"]["permissions"]
    assert perms == {"contents": "read", "packages": "read", "pull-requests": "write"}
    assert "cosign" not in " ".join(s.get("run", "") for s in doc["jobs"]["drift"]["steps"])


def test_release_signs_only_after_every_gate():
    doc = load(ROOT / ".github/workflows/golden-road.yml")
    names = [s.get("name", "") for s in doc["jobs"]["release"]["steps"]]
    sign = next(i for i, s in enumerate(doc["jobs"]["release"]["steps"]) if "cosign sign" in s.get("run", ""))
    for gate in ("Gate 1", "Container authz probe", "Render manifests and the gate record"):
        assert next(i for i, n in enumerate(names) if n.startswith(gate)) < sign
    assert [s for s in doc["jobs"]["release"]["steps"] if "if" in s] == []


@pytest.mark.parametrize("name,job", [("golden-road.yml", "drift"), ("golden-road.yml", "release"), ("golden-road.yml", "preview"),
                                      ("golden-road.yml", "preview-comment"), ("golden-road-promote.yml", "promote")])
def test_placeholder_builder_image_is_refused_first(name, job):
    doc = load(ROOT / ".github/workflows" / name)
    first = doc["jobs"][job]["steps"][0]
    assert "BUILDER_IMAGE" in first["run"] and ZERO_DIGEST in first["run"] and "exit 1" in first["run"]
    assert "uses" not in first and "if" not in first


def test_promote_refuses_non_test_tags():
    text = (ROOT / ".github/workflows/golden-road-promote.yml").read_text()
    assert "^test-[0-9]{14}-[0-9a-f]{7}$" in text


def test_release_builder_refuses_unconfigured_platform():
    doc = load(ROOT / ".github/workflows/release-builder.yml")
    steps = doc["jobs"]["release"]["steps"]
    refuse = next(i for i, s in enumerate(steps) if "replace-me" in s.get("run", ""))
    assert "ORG_PLACEHOLDER" in steps[refuse]["run"]
    assert refuse < next(i for i, s in enumerate(steps) if "buildx build" in s.get("run", ""))
    run = steps[refuse]["run"]
    assert 'rc=0' in run and '|| rc=$?' in run and '[ "$rc" -eq 1 ]' in run, "a grep error must fail closed"
    checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"]["fetch-depth"] == 0
    ancestor = next(i for i, s in enumerate(steps) if "merge-base --is-ancestor" in s.get("run", ""))
    assert 'is-ancestor "$GITHUB_SHA" origin/main' in steps[ancestor]["run"]
    assert ancestor < next(i for i, s in enumerate(steps) if "buildx build" in s.get("run", ""))


def promote_step(name):
    doc = load(ROOT / ".github/workflows/golden-road-promote.yml")
    return next(s for s in doc["jobs"]["promote"]["steps"] if s.get("name", "").startswith(name))


def test_promote_cosign_identity_is_fully_pinned():
    run = promote_step("Fetch and verify")["run"]
    for flag in (
        '--certificate-identity "${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY_OWNER}/platform-pipeline/.github/workflows/golden-road.yml@refs/heads/main"',
        "--certificate-oidc-issuer https://token.actions.githubusercontent.com",
        '--certificate-github-workflow-repository "$GITHUB_REPOSITORY"',
        "--certificate-github-workflow-ref refs/heads/main",
        "--certificate-github-workflow-trigger push",
    ):
        assert flag in run, flag
    assert 'cosign verify "${id[@]}"' in run and 'cosign verify-attestation "${id[@]}"' in run
    assert "https://github.com/" not in run


ALLOWED_EXPR = [
    r"inputs\.[A-Za-z_][\w-]*", r"github\.token", r"github\.actor", r"github\.triggering_actor", r"github\.run_id",
    r"github\.run_attempt", r"github\.server_url", r"github\.repository", r"github\.event\.pull_request\.number",
    r"steps\.[\w-]+\.outputs\.[\w-]+", r"github\.run_id", r"github\.server_url", r"job\.workflow_repository", r"job\.workflow_sha", r"needs\.(semgrep|trivy|preview)\.result", r"github\.sha",
    # the scorecard: job results, the factory's own job outputs, and the gateway key secret
    r"needs\.(precondition|assess|score|report|level)\.result", r"needs\.precondition\.outputs\.(checks|event|merge_sha)",
    r"needs\.assess\.outputs\.(assessment_sha256|factory_sha256)", r"needs\.score\.outputs\.(level|comment_sha256|github_sha256|summary_sha256|findings_conclusion|findings_title)",
    r"secrets\.SCORECARD_GATEWAY_(KEY|MODEL|ACCESS_ID|ACCESS_SECRET)", r"github\.event\.pull_request\.base\.ref",  # the base ref reaches only an env var, never a script
    # the builder release's kit job: the image digest from the release job, and the App that may publish in golden-road-agents
    r"needs\.release\.outputs\.image", r"vars\.GOLDEN_ROAD_AGENTS_APP_CLIENT_ID", r"secrets\.GOLDEN_ROAD_AGENTS_APP_KEY",
    # central review: the scorecard App's id (a repository variable) and key (an environment secret, only in the two central workflows)
    r"vars\.SCORECARD_APP_ID", r"secrets\.SCORECARD_APP_KEY",
    # golden-road-deploy's bump: the deploy App's id (a repository variable) and key (an environment secret, only in bump.yml)
    r"vars\.DEPLOY_APP_ID", r"secrets\.DEPLOY_APP_KEY", r"steps\.(apply|verify)\.outcome", r"needs\.(precondition|score)\.outputs\.retry",
]


def expressions(doc):
    def walk(value):
        if isinstance(value, str):
            yield from re.findall(r"\$\{\{\s*(.*?)\s*\}\}", value)
        elif isinstance(value, dict):
            for item in value.values():
                yield from walk(item)
    yield from walk(doc.get("env", {}))
    yield from walk(doc.get("concurrency", {}))
    for job in doc.get("jobs", {}).values():
        for key in ("env", "with", "concurrency", "if", "name", "runs-on", "environment"):
            yield from walk(job.get(key, {}))
        for step in job.get("steps", []):
            yield from walk(step.get("env", {}))
            yield from walk(step.get("with", {}))


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_only_allowlisted_contexts_reach_env_and_with(path):
    for expr in expressions(load(path)):
        assert any(re.fullmatch(a, expr) for a in ALLOWED_EXPR), f"{path.name}: {expr}"


def test_allowlist_rejects_attacker_controlled_contexts():
    for bad in ("github.event.pull_request.title", "github.head_ref", "github.event.issue.body", "github.ref_name",
                "github.event.pull_request.number || github.head_ref", "github.event.head_commit.message", "github.event.pull_request.head.sha", "github.event.pull_request.head.ref"):
        assert not any(re.fullmatch(a, bad) for a in ALLOWED_EXPR), bad


def extract_jq():
    run = promote_step("Fetch and verify")["run"]
    match = re.search(r"<<'JQ'\n(.*?)\nJQ\n", run, re.S)
    assert match
    return match.group(1)


def statement(**predicate):
    import base64
    import json
    payload = base64.b64encode(json.dumps({"_type": "https://in-toto.io/Statement/v1", "predicate": predicate}).encode()).decode()
    return json.dumps({"payloadType": "application/vnd.in-toto+json", "payload": payload, "signatures": []})


REL = "test-20261020153000-ccccccc"
OTHER = "test-20261020160000-ddddddd"


def run_select(tmp_path, lines, release=REL):
    (tmp_path / "select.jq").write_text(extract_jq())
    (tmp_path / "in.jsonl").write_text("".join(line + "\n" for line in lines))
    return subprocess.run(["jq", "-s", "-e", "--arg", "release", release, "-f", str(tmp_path / "select.jq"), str(tmp_path / "in.jsonl")],
                          capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
class TestSelectGateAttestation:
    def test_one_valid_pass(self, tmp_path):
        import json
        out = run_select(tmp_path, [statement(release=REL, status="pass", image="i", manifests_sha256="a" * 64)])
        assert out.returncode == 0 and json.loads(out.stdout)["image"] == "i"

    @pytest.mark.parametrize("lines", [
        [],
        ['{"unexpected": true}'],
        ['{"payload": "!!!not base64"}'],
        ["null"],
        [statement(release=REL, status="fail")],
        [statement(release=REL, status="pass"), statement(release=REL, status="fail")],
        [statement(release=REL, status="pass"), statement(release=REL, status="pass", image="other")],
        [statement(release=OTHER, status="pass")],
        [statement(status="pass")],
    ], ids=["empty", "wrong-shape", "bad-base64", "null", "fail", "pass-and-fail", "two-passes", "other-release", "no-release"])
    def test_everything_else_is_refused(self, tmp_path, lines):
        out = run_select(tmp_path, lines)
        assert out.returncode != 0 and out.stdout.strip() == ""

    def test_other_release_alongside_ours_is_ignored(self, tmp_path):
        out = run_select(tmp_path, [statement(release=OTHER, status="pass"), statement(release=REL, status="pass")])
        assert out.returncode == 0


def test_swapped_manifests_fail_the_hash_comparison(tmp_path):
    import hashlib
    run = promote_step("Fetch and verify")["run"]
    lines = [line.strip() for line in run.splitlines() if line.strip().startswith(("have=$(sha256sum out/manifests.yaml", '[ "$have" = "$want" ]'))]
    assert len(lines) == 2, "the step must compare sha256sum of manifests.yaml with the attested digest"
    legit = b"apiVersion: apps/v1\nkind: Deployment\n"
    swapped = (ROOT / "tests/pipeline/fixtures/swapped-manifests.yaml").read_bytes()
    (tmp_path / "out").mkdir()
    script = "set -euo pipefail\nwant=" + hashlib.sha256(legit).hexdigest() + "\n" + "\n".join(lines) + "\n"
    (tmp_path / "out/manifests.yaml").write_bytes(legit)
    assert subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True).returncode == 0
    (tmp_path / "out/manifests.yaml").write_bytes(swapped)
    assert subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True).returncode != 0


def test_promote_trusts_only_the_attested_predicate():
    run = promote_step("Fetch and verify")["run"]
    assert "grep -E" not in run
    assert 'cp out/predicate.json out/gate-record.json' in run
    assert '"$(jq -r .image out/predicate.json)" = "$ref"' in run
    assert "commit=$(jq -r .commit out/predicate.json)" in run
    doc = load(ROOT / ".github/workflows/golden-road-promote.yml")
    assert doc["jobs"]["promote"]["concurrency"] == {"group": "promote-${{ github.repository }}-${{ inputs.release }}", "cancel-in-progress": False}
    final = promote_step("Attest")["run"]
    assert final.index("already promotes this image") < final.index("cosign attest")
    assert "imagetools inspect" in final


def test_release_binds_record_to_tag_and_manifests_and_checks_tag_digest():
    doc = load(ROOT / ".github/workflows/golden-road.yml")
    steps = doc["jobs"]["release"]["steps"]
    render = next(s for s in steps if s.get("id") == "render")["run"]
    assert render.index('tag="test-') < render.index("golden-road record")
    assert '--release "$tag" --manifests /out/manifests.yaml' in render
    sign = next(s for s in steps if "cosign sign" in s.get("run", ""))
    assert sign["env"]["TAG"] == "${{ steps.render.outputs.tag }}" and "imagetools inspect" in sign["run"]
    assert not any(s.get("uses", "").startswith("actions/download-artifact") for s in steps)
    names = [s.get("name", "") for s in steps]
    assert any("Check app name" in n for n in names)
    rerun = next(i for i, s in enumerate(steps) if "builder verify" in s.get("run", ""))
    assert rerun < next(i for i, s in enumerate(steps) if "checks wait" in s.get("run", ""))


def test_release_job_is_serialized_per_repo_without_cancel():
    release = load(ROOT / ".github/workflows/golden-road.yml")["jobs"]["release"]
    assert release["concurrency"] == {"group": "release-${{ github.repository }}", "cancel-in-progress": False}


def test_release_notes_include_commit_date():
    release = load(ROOT / ".github/workflows/golden-road.yml")["jobs"]["release"]
    run = " ".join(s.get("run", "") for s in release["steps"])
    assert "Commit date: %s" in run and 'git -C src show -s --format=%cI "$GITHUB_SHA"' in run
    assert run.index("git -C src show") < run.index("cosign sign")
    assert "Commit date: %s" in promote_step("Attest, tag")["run"]


def test_release_tag_pattern_is_checked_before_signing():
    release = load(ROOT / ".github/workflows/golden-road.yml")["jobs"]["release"]
    run = next(s["run"] for s in release["steps"] if s.get("name", "").startswith("Sign, attest"))
    assert run.index("^test-[0-9]{14}-[0-9a-f]{7}$") < run.index("cosign sign") < run.index("cosign attest")


def test_required_checks_are_validated_in_the_builder_image_not_the_workflow():
    steps = load(ROOT / ".github/workflows/release-builder.yml")["jobs"]["release"]["steps"]
    assert not any("load_required" in s.get("run", "") for s in steps), "the runner has no jsonschema; validate in the image build"
    lines = (ROOT / "images/builder/Dockerfile").read_text().splitlines()
    copy = max(i for i, l in enumerate(lines) if l.startswith("COPY platform/required-checks.json"))
    validate = next(i for i, l in enumerate(lines) if l.startswith("RUN") and "load_required('/etc/golden-road/required-checks.json')" in l)
    assert validate > copy


FORK_GUARD = "github.event.pull_request.head.repo.full_name == github.repository"


def test_drift_comment_is_skipped_on_fork_prs_and_never_fails_the_check():
    """A public repo gets fork PRs, whose token cannot write. The comment is skipped there (an `if:` comparison,
    which is evaluated by the runner and never reaches a shell), and a failed comment never fails the job."""
    doc = load(ROOT / ".github/workflows/golden-road.yml")
    steps = doc["jobs"]["drift"]["steps"]
    comment = next(s for s in steps if s.get("name") == "Comment on the PR")
    assert comment["if"] == f"github.event_name == 'pull_request' && {FORK_GUARD}"
    assert comment.get("continue-on-error") is True
    # the summary, annotations, and the gate run regardless of the comment
    names = [s.get("name", "") for s in steps]
    assert names.index("Drift check") < names.index("Comment on the PR") < names.index("Enforce the drift gate")
    drift = next(s for s in steps if s.get("name") == "Drift check")["run"]
    assert "golden-road annotate" in drift and "GITHUB_STEP_SUMMARY" in drift


def test_head_repo_context_is_only_used_in_if_conditions():
    for path in WORKFLOWS:
        doc = load(path)
        assert "head.repo" not in " ".join(ALLOWED_EXPR)
        for job, step in all_steps(doc):
            for key in ("run", "env", "with"):
                assert "head.repo" not in str(step.get(key, "")), f"{path.name}:{job}: head repo context outside an if:"
        for job in doc.get("jobs", {}).values():
            for key in ("env", "with", "name", "runs-on", "environment", "concurrency"):
                assert "head.repo" not in str(job.get(key, ""))


# ---- security-scans: the reference workflow and the app repo's caller ----

needs_scans = pytest.mark.skipif(not SCANS.is_dir(), reason="the platform export does not carry reference/security-scans")
CALLER = ROOT / "bootstrap/hello-app/.github/workflows/security-scans.yml"


@needs_scans
def test_only_the_platform_workflows_sign_and_the_scan_workflow_has_no_signing_or_id_token():
    text = (SCANS / ".github/workflows/scan.yml").read_text()
    assert "id-token" not in text and "cosign" not in text and "secrets." not in text


def test_security_scans_caller_shape():
    doc = load(CALLER)
    assert doc["on"]["pull_request"] is None and doc["on"]["push"] == {"branches": ["main"]} and set(doc["on"]) == {"pull_request", "push"}
    assert doc["permissions"] == {}
    [(name, job)] = doc["jobs"].items()
    assert name == "security-scans" and "name" not in job, "the checks are named <job id> / <called job id>"
    assert re.fullmatch(r"[A-Za-z0-9_-]+/security-scans/\.github/workflows/scan\.yml@main|ORG_PLACEHOLDER/security-scans/\.github/workflows/scan\.yml@main", job["uses"])
    assert job["permissions"] == {"contents": "read", "pull-requests": "write"}
    assert "secrets" not in job, "no secrets and no `secrets: inherit`"
    assert "with" not in job, "the caller passes nothing: tuning lives in the security-scans repo"


@needs_scans
def test_the_called_workflow_never_asks_for_more_than_the_caller_grants():
    caller = load(CALLER)["jobs"]["security-scans"]["permissions"]
    called = load(SCANS / ".github/workflows/scan.yml")
    for job in called["jobs"].values():
        for scope, level in job["permissions"].items():
            assert caller.get(scope) == level, scope


@needs_scans
def test_check_names_match_the_ruleset_and_the_documented_required_checks():
    called = load(SCANS / ".github/workflows/scan.yml")
    names = {f"security-scans / {job}" for job in called["jobs"] if job != "comment"}  # `comment` only posts PR comments and is not required
    ruleset = json.loads((ROOT / "bootstrap/hello-app/.github/rulesets/main.json").read_text())
    required = [r for r in ruleset["rules"] if r["type"] == "required_status_checks"][0]["parameters"]["required_status_checks"]
    assert {c["context"] for c in required if c["context"].startswith("security-scans / ")} == names
    # the Actions app posts every check but `scorecard / assessed` when the platform reviews centrally: that one is the scorecard App's
    assert all(c["integration_id"] == (scorecard_integration_id() if c["context"] == "scorecard / assessed" else 15368) for c in required)
    assert "security-scans / comment" not in {c["context"] for c in required}
    doc = (ROOT / "docs/setup/personal-poc.md").read_text()
    for name in names:
        assert f'name: "{name}", app_id: 15368' in doc, name


# ---- PR preview: build the image and run the container authz probe on the pull request ----

PREVIEW_ENV = {"BUILDER_IMAGE"}


def gr_doc():
    return load(ROOT / ".github/workflows/golden-road.yml")


def preview_job():
    return gr_doc()["jobs"]["preview"]


def preview_text():
    return yaml.safe_dump(preview_job())


def step_named(job, prefix):
    return next(s for s in job["steps"] if s.get("name", "").startswith(prefix))


def build_command(job_steps):
    runs = re.sub(r"\\\n\s*", " ", "\n".join(s.get("run", "") for s in job_steps))
    match = re.search(r"docker buildx build [^\n]*", runs)
    assert match, "no docker buildx build"
    return match.group(0).strip()


def test_preview_runs_only_on_pull_requests_and_needs_nothing():
    job = preview_job()
    assert job["if"] == "github.event_name == 'pull_request'"
    assert "needs" not in job, "runs in parallel with drift: a failing drift check is exactly when the build result is still useful"
    assert "push" not in job["if"]
    assert gr_doc()["jobs"]["release"]["needs"] == "drift", "the authoritative build and probe stay in the release job"


def test_preview_can_read_and_nothing_else():
    perms = preview_job()["permissions"]
    # packages: read only pulls the digest-pinned builder image (private or internal on GHE); it is never write.
    assert perms == {"contents": "read", "packages": "read"}
    for forbidden in ("id-token", "pull-requests", "packages: write", "contents: write"):
        assert forbidden not in preview_text(), forbidden


def test_preview_checks_out_the_same_tree_as_drift_without_credentials():
    """No `ref:`: the default merge ref, which is what drift and the scans check out, so all of them judge the same tree."""
    checkouts = [s for s in preview_job()["steps"] if s.get("uses", "").startswith("actions/checkout")]
    assert len(checkouts) == 1
    assert checkouts[0]["with"] == {"persist-credentials": False, "path": "src"}
    drift = next(s for s in gr_doc()["jobs"]["drift"]["steps"] if s.get("uses", "").startswith("actions/checkout"))
    assert drift["with"] == checkouts[0]["with"] and drift["uses"] == checkouts[0]["uses"]


def test_preview_never_pushes_signs_attests_or_publishes():
    text = preview_text()
    for banned in ("cosign", "attest", "docker push", "push=true", "push-by-digest", "--push", "type=image", "imagetools",
                   "gh release", "gh pr", "GH_TOKEN", "GITHUB_TOKEN", "--sbom=true", "--provenance=mode", "--metadata-file", "upload-sarif"):
        assert banned not in text, banned
    # the only use of the token is the read-only builder pull, and the job logs out before any PR code runs
    steps = preview_job()["steps"]
    logins = [s for s in steps if s.get("uses", "").startswith("docker/login-action")]
    assert len(logins) == 1 and logins[0]["with"]["registry"] == "ghcr.io"
    assert str(preview_job()).count("github.token") == 1
    login = steps.index(logins[0])
    logout = next(i for i, s in enumerate(steps) if "docker logout" in s.get("run", ""))
    pull = next(i for i, s in enumerate(steps) if 'docker pull -q "$BUILDER_IMAGE"' in s.get("run", ""))
    build = next(i for i, s in enumerate(steps) if "buildx build" in s.get("run", ""))
    assert login < pull == logout < build
    assert steps[pull]["run"].index("docker pull") < steps[pull]["run"].index("docker logout")


RELEASE_EXTRAS = ["--sbom=true", "--provenance=mode=max", "--output", "type=image,name=${image},push-by-digest=true,push=true",
                  "--metadata-file", "out/build.json"]
PREVIEW_EXTRAS = ["--sbom=false", "--provenance=false", "--load", "--tag", "golden-road-preview:pr"]


def build_tokens(job):
    import shlex
    return shlex.split(build_command(job["steps"]))


def test_preview_builds_exactly_like_release_but_loads_locally():
    release, preview = build_tokens(gr_doc()["jobs"]["release"]), build_tokens(preview_job())
    shared = ["docker", "buildx", "build", "-f", "src/Dockerfile"]
    # Exact lists, not subsets: any extra flag (--allow, --build-arg, --target, --secret, --ssh, --network ...) fails here.
    assert release[:5] == preview[:5] == shared and release[-1] == preview[-1] == "src"
    assert release[5:-1] == RELEASE_EXTRAS
    assert preview[5:-1] == PREVIEW_EXTRAS
    pins = [{s["uses"] for s in job["steps"] if s.get("uses", "").startswith("docker/setup-buildx-action")} for job in (gr_doc()["jobs"]["release"], preview_job())]
    assert pins[0] == pins[1] and len(pins[0]) == 1


def joined(run):
    return re.sub(r"[ \t]*\\\n\s*", " ", run)


def probe_commands(job):
    run = joined(step_named(job, "Container authz probe")["run"])
    start = re.search(r"docker run -d [^\n)]*", run).group(0)
    probe = re.search(r"docker run --rm --network \"container:\$cid\"[^\n]*?--output /out/authz\.json", run).group(0)
    return start, probe


def test_preview_probe_runs_exactly_like_release_with_no_network():
    (rel_start, rel_probe), (pre_start, pre_probe) = probe_commands(gr_doc()["jobs"]["release"]), probe_commands(preview_job())
    assert rel_start.endswith(' "$REF"') and pre_start.endswith(" golden-road-preview:pr")
    expected = 'docker run -d --network none --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges "${probe_env[@]}"'
    assert rel_start.removesuffix(' "$REF"') == pre_start.removesuffix(" golden-road-preview:pr") == expected
    assert rel_probe == pre_probe
    assert rel_probe == ('docker run --rm --network "container:$cid" --read-only --tmpfs /tmp --user "$(id -u):$(id -g)" '
                         '-v "$PWD/src:/src:ro" -v "$PWD/out:/out" "$BUILDER_IMAGE" '
                         'golden-road authz-probe --repo-root /src --base-url "http://127.0.0.1:$port" --output /out/authz.json')
    run = step_named(preview_job(), "Container authz probe")["run"]
    assert "docker pull" not in run and "ghcr.io" not in run, "the image is the local build, never a pulled one"
    assert "docker.sock" not in run and "--privileged" not in run


def test_preview_pr_derived_output_runs_between_stop_commands_with_a_random_token():
    steps = preview_job()["steps"]
    for prefix, anchor in (("Build the image", "buildx build"), ("Container authz probe", "tail -n")):
        run = step_named(preview_job(), prefix)["run"]
        assert "/dev/urandom" in run and 'echo "::stop-commands::$guard"' in run and 'echo "::$guard::"' in run, prefix
        # the end token is emitted from an EXIT trap, so it also fires when the build or probe fails
        assert run.index("::stop-commands::") < run.index(anchor), prefix
        assert "trap '" in run and run.index("trap '") < run.index('"::$guard::"') and run.count("::$guard::") == 1, prefix
    assert steps and "stop-commands" not in step_named(preview_job(), "Report")["run"], "trusted, escaped report output must stay live"


def test_preview_reports_uploads_and_fails_the_job():
    steps = preview_job()["steps"]
    names = [s.get("name", "") for s in steps]
    assert names.index("Container authz probe") < names.index("Report (annotations and summary)")
    report = step_named(preview_job(), "Report")
    assert report["if"] == "always()"
    assert "preview-report" in report["run"] and "--annotate" in report["run"] and "GITHUB_STEP_SUMMARY" in report["run"]
    upload = next(s for s in steps if s.get("uses", "").startswith("actions/upload-artifact"))
    assert upload["if"] == "always()" and upload["with"]["name"] == "golden-road-preview" and upload["with"]["path"].strip() == "out/authz.json"
    assert upload["with"]["if-no-files-found"] == "error", "there is always a result file, so a missing one is loud"
    assert upload["uses"] == next(s for s in gr_doc()["jobs"]["drift"]["steps"] if s.get("uses", "").startswith("actions/upload-artifact"))["uses"]
    assert [s for s in steps if s.get("continue-on-error")] == [], "a failed build or probe must fail the job"
    assert names.index("Build the image (not pushed)") < names.index("Container authz probe")


def comment_job():
    return gr_doc()["jobs"]["preview-comment"]


def test_preview_comment_is_the_only_preview_job_that_writes_and_never_touches_app_code():
    job = comment_job()
    assert job["needs"] == "preview"
    assert job["permissions"] == {"contents": "read", "packages": "read", "pull-requests": "write"}
    assert job["if"] == f"always() && github.event_name == 'pull_request' && {FORK_GUARD}"
    text = yaml.safe_dump(job)
    assert not any(s.get("uses", "").startswith("actions/checkout") for s in job["steps"])
    for banned in ("src/", "buildx", "docker build", "cosign", "id-token", "persist-credentials", "$PWD/src"):
        assert banned not in text, banned
    # the artifact is parsed only by the pinned builder image, read-only and without a network
    run = step_named(job, "Comment on the PR")["run"]
    assert '--network none --read-only' in run and ':/in:ro' in run and "preview-report" in run
    assert step_named(job, "Comment on the PR")["env"]["RESULT"] == "${{ needs.preview.result }}"
    assert '--job-result "$RESULT"' in run


def test_preview_comment_downloads_the_artifact_with_the_pin_the_scans_use():
    job = comment_job()
    download = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/download-artifact"))
    assert download["uses"] == "actions/download-artifact@9000827ccba6bdab643e8b6fd33ac0654aef8333"
    if SCANS.is_dir():
        assert download["uses"] in (SCANS / ".github/workflows/scan.yml").read_text()
    assert download["with"]["name"] == "golden-road-preview" and download.get("continue-on-error") is True


def test_the_comments_are_found_by_marker_and_author_so_one_never_overwrites_another():
    for job, marker in ((comment_job(), "golden-road:preview"), (gr_doc()["jobs"]["drift"], "golden-road:drift")):
        step = step_named(job, "Comment on the PR")
        assert step.get("continue-on-error") is True
        assert "--edit-last" not in step["run"] and marker in step["run"] and "github-actions[bot]" in step["run"]


def test_the_preview_check_is_required_for_merge_from_the_actions_app():
    ruleset = json.loads((ROOT / "bootstrap/hello-app/.github/rulesets/main.json").read_text())
    required = [r for r in ruleset["rules"] if r["type"] == "required_status_checks"][0]["parameters"]["required_status_checks"]
    assert {"context": "golden-road / preview", "integration_id": 15368} in required
    assert "golden-road / preview-comment" not in {c["context"] for c in required}
    # the check name is <caller job id> / <called job id>, and the called job has no `name:` that would change it
    assert "name" not in preview_job()


def test_preview_comment_is_serialized_per_pr_and_names_the_tested_commit():
    job = comment_job()
    assert job["concurrency"] == {"group": "preview-comment-${{ github.event.pull_request.number }}", "cancel-in-progress": True}
    step = step_named(job, "Comment on the PR")
    assert step["env"]["SHA"] == "${{ github.sha }}" and '--commit "$SHA"' in step["run"]
    drift = step_named(gr_doc()["jobs"]["drift"], "Comment on the PR")
    assert drift["env"]["SHA"] == "${{ github.sha }}" and '$SHA' in drift["run"]


def test_preview_comment_runs_the_report_as_the_runner_user_so_out_needs_no_chmod():
    assert "chmod" not in yaml.safe_dump(gr_doc())
    run = step_named(comment_job(), "Comment on the PR")["run"]
    assert '--user "$(id -u):$(id -g)"' in run


@pytest.mark.parametrize("job", ["preview", "release"])
def test_dockerfile_must_be_a_regular_file_before_it_is_built(job):
    steps = gr_doc()["jobs"][job]["steps"]
    step = next(s for s in steps if "buildx build" in s.get("run", ""))
    guard = "{ [ -f src/Dockerfile ] && [ ! -L src/Dockerfile ]; } || { echo \"::error::src/Dockerfile must be a regular file, not a symlink\"; exit 1; }"
    assert guard in step["run"] and step["run"].index(guard) < step["run"].index("buildx build")


@pytest.mark.parametrize("kind,ok", [("file", True), ("symlink", False), ("dir", False), ("missing", False)])
def test_the_dockerfile_guard_refuses_anything_but_a_regular_file(tmp_path, kind, ok):
    run = next(s for s in preview_job()["steps"] if "buildx build" in s.get("run", ""))["run"]
    line = next(l.strip() for l in run.splitlines() if "[ -f src/Dockerfile ]" in l)
    src = tmp_path / "src"
    src.mkdir()
    if kind == "file":
        (src / "Dockerfile").write_text("FROM scratch\n")
    elif kind == "symlink":
        (tmp_path / "real").write_text("FROM scratch\n")
        (src / "Dockerfile").symlink_to(tmp_path / "real")
    elif kind == "dir":
        (src / "Dockerfile").mkdir()
    assert (subprocess.run(["bash", "-c", line], cwd=tmp_path, capture_output=True).returncode == 0) is ok


# The probe step must always leave authz.json, even if the app exits before the probe can join its network namespace.
FAKE_DOCKER = r'''#!/usr/bin/env bash
case "$1 $2" in
  "run -d") echo cid123 ;;
  "inspect -f")
    if [ "$3" = "{{.State.Running}}" ]; then
      n=$(cat "$FAKE_STATE" 2>/dev/null || echo 0); echo $((n + 1)) > "$FAKE_STATE"
      set -- $RUNNING_SEQ; shift "$n" 2>/dev/null || true; echo "${1:-false}"
    else echo "$EXIT_CODE"; fi ;;
  "logs cid123") echo "Traceback: ModuleNotFoundError: No module named 'requests'" ;;
  "rm -f") ;;
  "run --rm")
    if [ -n "${PROBE_JSON:-}" ]; then printf '%s' "$PROBE_JSON" > out/authz.json; exit 1; fi
    exit "$JOIN_RC" ;;
  *) echo "unexpected docker $*" >&2; exit 99 ;;
esac
'''


def run_probe_step(tmp_path, **env):
    shim = tmp_path / "bin"
    shim.mkdir()
    (shim / "docker").write_text(FAKE_DOCKER)
    (shim / "docker").chmod(0o755)
    (tmp_path / "src").mkdir()
    (tmp_path / "src/builder-spec.json").write_text('{"port": 8080}')
    (tmp_path / "out").mkdir()
    script = step_named(preview_job(), "Container authz probe")["run"]
    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, env={
        "PATH": f"{shim}:/usr/bin:/bin", "BUILDER_IMAGE": "builder@sha256:" + "a" * 64, "FAKE_STATE": str(tmp_path / "state"),
        "EXIT_CODE": "1", "JOIN_RC": "125", "RUNNING_SEQ": "true", **env})
    return result, tmp_path / "out/authz.json"


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
class TestProbeStepAlwaysLeavesAResult:
    def test_app_exits_before_the_probe_joins(self, tmp_path):
        result, authz = run_probe_step(tmp_path, RUNNING_SEQ="false", EXIT_CODE="1")
        assert result.returncode != 0
        assert json.loads(authz.read_text()) == {"status": "fail", "reason": "app exited before the probe could run", "exit_code": 1}
        assert result.stdout.index("::stop-commands::") < result.stdout.index("ModuleNotFoundError") < result.stdout.rindex("::stop-commands::") + 10_000
        assert "::error title=golden-road preview::" in result.stdout

    def test_app_exits_while_the_probe_is_joining(self, tmp_path):
        result, authz = run_probe_step(tmp_path, RUNNING_SEQ="true false", EXIT_CODE="3", JOIN_RC="125")
        assert result.returncode != 0
        assert json.loads(authz.read_text()) == {"status": "fail", "reason": "app exited before the probe could run", "exit_code": 3}

    def test_probe_cannot_join_but_the_app_is_still_running(self, tmp_path):
        result, authz = run_probe_step(tmp_path, RUNNING_SEQ="true true", JOIN_RC="125")
        assert result.returncode != 0
        assert json.loads(authz.read_text())["status"] == "fail"

    def test_app_exits_after_the_probe_joined_and_the_probe_times_out(self, tmp_path):
        probe = '{"status": "fail", "reason": "the app did not start", "cases": 1, "denied": 0, "failures": [], "failures_total": 0}'
        result, authz = run_probe_step(tmp_path, RUNNING_SEQ="true", PROBE_JSON=probe)
        assert result.returncode != 0 and json.loads(authz.read_text())["reason"] == "the app did not start"

    def test_the_app_log_is_always_inside_stop_commands(self, tmp_path):
        result, _ = run_probe_step(tmp_path, RUNNING_SEQ="false")
        lines = result.stdout.splitlines()
        start = next(i for i, l in enumerate(lines) if l.startswith("::stop-commands::"))
        token = lines[start].split("::")[-1]
        end = lines.index(f"::{token}::")
        assert start < next(i for i, l in enumerate(lines) if "ModuleNotFoundError" in l) < end


def test_promote_says_plainly_when_the_approval_was_a_self_approval():
    run = promote_step("Attest, tag")["run"]
    assert run.count("Single-approver PoC mode: approved by the person who triggered it. Not valid for production use.") == 1
    assert "self_approved=$(jq -r .self_approved out/promotion-record.json)" in run
    assert "single_mode=$(jq -r .gate2_single_approver out/promotion-record.json)" in run
    assert 'case "$v" in true|false) ;; *) echo "::error::' in run and "exit 1;; esac" in run
    assert run.index("esac") < run.index("self_note=")
    assert '[ "$self_approved" = "true" ] || [ "$single_mode" = "true" ]' in run
    # the note goes into the release notes (before the release is created) and into the job summary
    assert run.index('notes+=$\'\\n\\n\'"$self_note"') < run.index("gh release create")
    assert 'echo "- **$self_note**" >> "$GITHUB_STEP_SUMMARY"' in run
    # the mode is never an input of the workflow or a value passed to promotion-record
    record = promote_step("Record the approval")["run"]
    assert "single" not in record.lower() and "self_approved" not in record
    docker_run = next(l for l in record.splitlines() if "docker run" in l)
    assert not re.search(r"(^|\s)(-e|--env|--env-file)(\s|=)", docker_run)
    assert len(re.findall(r"(^|\s)(-v|--volume)(\s|=)", docker_run)) == 1
    assert "single" not in " ".join(load(ROOT / ".github/workflows/golden-road-promote.yml")["on"]["workflow_call"]["inputs"])


# ---- golden-road-agents' release-skill.yml ----

RELEASE_SKILL = ROOT / "bootstrap/golden-road-agents/.github/workflows/release-skill.yml"
ATTEST = "actions/attest-build-provenance@4d101475d8b20a2381f78447822ac1eab6504dd8"  # v4.2.2


def step_index(steps, predicate):
    return next(i for i, s in enumerate(steps) if predicate(s))


def test_release_skill_runs_only_on_skill_tags_with_only_what_it_needs():
    doc = load(RELEASE_SKILL)
    assert doc["on"] == {"push": {"tags": ["skill-v*"]}} and doc["permissions"] == {}
    assert doc["concurrency"] == {"group": "release-skill", "cancel-in-progress": False}
    assert list(doc["jobs"]) == ["release"]
    assert doc["jobs"]["release"]["permissions"] == {"contents": "write", "id-token": "write", "attestations": "write"}


def test_release_skill_checks_the_tag_then_builds_attests_and_publishes():
    steps = load(RELEASE_SKILL)["jobs"]["release"]["steps"]
    assert steps[0]["uses"].startswith("actions/checkout@") and steps[0]["with"] == {"persist-credentials": False, "fetch-depth": 0}
    check = step_index(steps, lambda s: "merge-base --is-ancestor" in s.get("run", ""))
    build = step_index(steps, lambda s: "scripts/build_release.py" in s.get("run", ""))
    attest = step_index(steps, lambda s: s.get("uses") == ATTEST)
    publish = step_index(steps, lambda s: "gh release create" in s.get("run", ""))
    assert check < build < attest < publish == len(steps) - 1
    assert '"$GITHUB_SHA" origin/main' in steps[check]["run"] and "^skill-v" in steps[check]["run"]
    assert '"${GITHUB_REF_NAME#skill-v}" dist' in steps[build]["run"]
    assert steps[attest]["id"] == "attest" and steps[attest]["with"] == {"subject-path": "dist/*"}
    run = steps[publish]["run"]
    command = run.split("--notes")[0]  # the flags, not the release notes text that follows
    for needle in ('gh release create "$GITHUB_REF_NAME" dist/*', '--repo "$GITHUB_REPOSITORY"', "--verify-tag --latest "):
        assert needle in command, needle
    assert ".sigstore.json" in run
    assert steps[publish]["env"] == {"GH_TOKEN": "${{ github.token }}", "ATTESTATION": "${{ steps.attest.outputs.bundle-path }}"}


def test_the_stub_trusts_exactly_this_workflow():
    text = (ROOT / "bootstrap/golden-road-agents/stub/golden-road/fetch_skill.py").read_text()
    assert 'SIGNER = f"{REPO}/.github/workflows/release-skill.yml"' in text and RELEASE_SKILL.name == "release-skill.yml"


# ---- release-builder.yml: the builder kit ----

RELEASE_BUILDER = ROOT / ".github/workflows/release-builder.yml"
APP_TOKEN = "actions/create-github-app-token@64ab729a6294000f5aa4a4bc0b73d1f32bc45510"  # v3.3.0


def test_release_builder_checks_the_tag_against_builder_version_before_the_image_is_built():
    steps = load(RELEASE_BUILDER)["jobs"]["release"]["steps"]
    tag = step_index(steps, lambda s: '"builder-v$version"' in s.get("run", ""))
    assert "src/builder/__init__.py" in steps[tag]["run"] and "exit 1" in steps[tag]["run"]
    assert tag < step_index(steps, lambda s: "buildx build" in s.get("run", ""))


def test_release_builder_hands_the_pushed_image_digest_to_the_kit_job():
    release = load(RELEASE_BUILDER)["jobs"]["release"]
    assert release["permissions"] == {"contents": "read", "packages": "write", "id-token": "write"}
    assert release["outputs"] == {"image": "${{ steps.image.outputs.image }}"}
    push = next(s for s in release["steps"] if "buildx build" in s.get("run", ""))
    assert push["id"] == "image" and 'echo "image=$image@$digest" >> "$GITHUB_OUTPUT"' in push["run"]
    assert push["run"].index("cosign sign") < push["run"].index("GITHUB_OUTPUT")


def test_the_kit_job_builds_attests_then_publishes_a_draft_with_a_token_only_it_sees():
    doc = load(RELEASE_BUILDER)
    assert set(doc["jobs"]) == {"release", "kit"}
    kit = doc["jobs"]["kit"]
    assert kit["needs"] == "release" and kit["runs-on"] == "ubuntu-24.04"
    assert kit["permissions"] == {"contents": "read", "id-token": "write", "attestations": "write"}
    steps = kit["steps"]
    assert steps[0]["uses"].startswith("actions/checkout@") and steps[0]["with"] == {"persist-credentials": False}
    build = step_index(steps, lambda s: "scripts/build_builder_kit.py" in s.get("run", ""))
    attest = step_index(steps, lambda s: s.get("uses") == ATTEST)
    token = step_index(steps, lambda s: s.get("uses") == APP_TOKEN)
    publish = step_index(steps, lambda s: "gh release create" in s.get("run", ""))
    assert build < attest < token < publish == len(steps) - 1
    run = steps[build]["run"]
    for needle in ("--require-hashes", "--only-binary=:all:", "images/builder/requirements.txt", '--tag "$GITHUB_REF_NAME"', '--image "$IMAGE"',
                   "--source-date-epoch", "--self-test"):
        assert needle in run, needle
    assert steps[build]["env"] == {"IMAGE": "${{ needs.release.outputs.image }}"}
    assert steps[attest]["id"] == "attest"
    assert steps[attest]["with"]["subject-path"].split() == ["dist/golden_road-*.whl", "dist/golden_road-*-requirements.txt", "dist/platform-pin.json"]
    assert steps[token]["id"] == "agents-token" and steps[token]["with"] == {
        "client-id": "${{ vars.GOLDEN_ROAD_AGENTS_APP_CLIENT_ID }}", "private-key": "${{ secrets.GOLDEN_ROAD_AGENTS_APP_KEY }}",
        "repositories": "golden-road-agents", "permission-contents": "write"}
    publish_step = steps[publish]
    assert publish_step["env"] == {"GH_TOKEN": "${{ steps.agents-token.outputs.token }}", "ATTESTATION": "${{ steps.attest.outputs.bundle-path }}"}
    run = publish_step["run"]
    command = run.split("--notes")[0]  # the flags, not the release notes text that follows (it mentions both)
    for needle in ('gh release create "$GITHUB_REF_NAME" dist/*', '--repo "${GITHUB_REPOSITORY_OWNER}/golden-road-agents"', "--draft --latest=false "):
        assert needle in command, needle
    assert '"dist/${GITHUB_REF_NAME}.sigstore.json"' in run
    # a stale draft with this tag name is a failure, not a second draft (gh release create would make one)
    check = run.index("gh release list")
    assert check < run.index("gh release create") and "delete the stale draft" in run[check:run.index("gh release create")]
    assert '--repo "${GITHUB_REPOSITORY_OWNER}/golden-road-agents"' in run[check:run.index("gh release create")]
    assert '[ "$existing" = "0" ] \\\n' in run and "exit 1" in run[check:run.index("gh release create")]
    assert [i for i, s in enumerate(steps) if "agents-token.outputs" in json.dumps(s)] == [publish]
    assert [i for i, s in enumerate(steps) if "secrets." in json.dumps(s)] == [token]


def test_no_other_workflow_uses_the_app_secret():
    for path in WORKFLOWS:
        if path != RELEASE_BUILDER:
            assert "GOLDEN_ROAD_AGENTS_APP" not in path.read_text(), path


def test_only_bump_uses_the_deploy_app_key():
    assert sorted(p.name for p in WORKFLOWS if "DEPLOY_APP_KEY" in p.read_text()) == ["bump.yml"]


def test_no_other_workflow_uses_the_scorecard_app_key():
    """The scorecard App's key lives only in the factory's `scorecard` environment, read by the poller and the assess run."""
    holders = sorted(p.name for p in WORKFLOWS if "SCORECARD_APP_KEY" in p.read_text())
    assert holders == ["scorecard-assess.yml", "scorecard-poll.yml"]


# ---- the signed release is the app and nothing else: bindings are per environment in golden-road-deploy (builder 0.4.0) ----

# A 0.3.16 test release may still carry a binding.yaml asset; promote must neither download nor attach it.
OLD_BINDING_TEXT = "schema: binding/v1\nrepo: acme/notes\n"


def release_steps():
    return gr_doc()["jobs"]["release"]["steps"]


def _step_text(step):
    return json.dumps(step)


@pytest.mark.parametrize("workflow", ["golden-road.yml", "golden-road-promote.yml"])
def test_no_step_of_the_release_or_promote_mentions_a_binding_or_the_factory(workflow):
    doc = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            text = _step_text(step).lower()
            assert "binding" not in text and "scorecard-factory" not in text and "factory-unreadable" not in text, step.get("name")


def test_render_and_record_take_only_the_app():
    render = step_named(gr_doc()["jobs"]["release"], "Render manifests")["run"]
    assert "gr golden-road render --repo-root /src --image \"$REF\" --output /out/manifests.yaml" in render
    assert "--binding" not in render


# Stand-ins for the publish step's tools: each logs its arguments; docker answers the tag's digest, git the commit date.
PUBLISH_FAKES = {
    "cosign": '#!/usr/bin/env bash\necho "cosign $*" >> "$TOOL_LOG"\n',
    "git": '#!/usr/bin/env bash\necho 2026-10-20T15:30:00Z\n',
    "docker": '#!/usr/bin/env bash\necho "docker $*" >> "$TOOL_LOG"\n'
              'case "$*" in *"imagetools inspect"*) printf \'{"digest": "sha256:%s"}\' "$DIGEST" ;; esac\n',
    "gh": '#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "$GH_LOG"\n',
}


def run_publish(tmp_path):
    shim = tmp_path / "bin"
    shim.mkdir()
    for name, text in PUBLISH_FAKES.items():
        (shim / name).write_text(text)
        (shim / name).chmod(0o755)
    (tmp_path / "out").mkdir()
    (tmp_path / "out/manifests.yaml").write_text("kind: Deployment\n")
    (tmp_path / "out/gate-record.json").write_text(json.dumps({"binding": None}))
    (tmp_path / "out/binding.yaml").write_text(OLD_BINDING_TEXT)  # a stray file is never attached
    script = step_named(gr_doc()["jobs"]["release"], "Sign, attest")["run"]
    result = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, env={
        "PATH": f"{shim}:/usr/bin:/bin", "GITHUB_REPOSITORY": "acme/notes", "GITHUB_REPOSITORY_OWNER": "acme", "GITHUB_SHA": "c" * 40,
        "TAG": "test-20261020153000-ccccccc", "IMAGE": "ghcr.io/acme/notes", "REF": "ghcr.io/acme/notes@sha256:" + "d" * 64, "DIGEST": "d" * 64,
        "GATE_PREDICATE": "https://example.test/gate", "GH_TOKEN": "t", "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "TOOL_LOG": str(tmp_path / "tools.log"), "GH_LOG": str(tmp_path / "gh.log")})
    gh = (tmp_path / "gh.log").read_text().splitlines() if (tmp_path / "gh.log").exists() else None
    return result, gh


def release_assets(gh):
    return gh[gh.index("create") + 2:gh.index("--repo")]


def release_notes(gh):
    return gh[gh.index("--notes") + 1:]


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
def test_the_test_release_attaches_exactly_the_manifests_and_the_gate_record(tmp_path):
    result, gh = run_publish(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert release_assets(gh) == ["out/manifests.yaml", "out/gate-record.json"]
    assert "Binding" not in "\n".join(release_notes(gh)) and "Binding" not in (tmp_path / "summary.md").read_text()


# ---- promote: the prod release carries the manifests and the records, and no binding ----

PROMO_REF = "ghcr.io/acme/notes@sha256:" + "d" * 64
PROMO_COMMIT = "c" * 40
PROMO_MANIFESTS = "kind: Deployment\n"
# Stand-ins for the promote steps' tools. gh downloads the assets in $ASSETS that match each --pattern (a missing one fails as gh
# does), answers the commit date and an empty release list, and logs a release create; cosign answers the attestation in $ATTEST.
PROMOTE_FAKES = {
    "cosign": '#!/usr/bin/env bash\necho "cosign $*" >> "$TOOL_LOG"\n'
              'case "$1" in verify-attestation) cat "$ATTEST" ;; esac\n',
    "docker": '#!/usr/bin/env bash\necho "docker $*" >> "$TOOL_LOG"\n'
              'case "$*" in *"imagetools inspect"*) printf \'{"digest": "sha256:%s"}\' "$DIGEST" ;; esac\n',
    "gh": r"""#!/usr/bin/env bash
echo "gh $*" >> "$TOOL_LOG"
case "$1 $2" in
  "release download")
    dir=""; pats=()
    while [ $# -gt 0 ]; do case "$1" in --dir) dir="$2"; shift 2 ;; --pattern) pats+=("$2"); shift 2 ;; *) shift ;; esac; done
    for p in "${pats[@]}"; do [ -e "$ASSETS/$p" ] || { echo "no assets match the file pattern" >&2; exit 1; }; cp "$ASSETS/$p" "$dir/"; done ;;
  "release list") ;;
  "api repos/"*) echo 2026-10-20T15:30:00Z ;;
  "release create") printf '%s\n' "$@" > "$GH_LOG" ;;
  *) echo "unexpected gh $*" >&2; exit 99 ;;
esac
""",
}


def promote_env(tmp_path, **extra):
    shim = tmp_path / "bin"
    if not shim.exists():
        shim.mkdir()
        for name, text in PROMOTE_FAKES.items():
            (shim / name).write_text(text)
            (shim / name).chmod(0o755)
    return {"PATH": f"{shim}:/usr/bin:/bin", "GITHUB_REPOSITORY": "acme/notes", "GITHUB_REPOSITORY_OWNER": "acme",
            "GITHUB_SERVER_URL": "https://github.com", "GITHUB_OUTPUT": str(tmp_path / "gh-out"), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
            "RELEASE": REL, "GH_TOKEN": "t", "GATE_PREDICATE": "https://example.test/gate", "PROMOTION_PREDICATE": "https://example.test/promotion",
            "TOOL_LOG": str(tmp_path / "tools.log"), "GH_LOG": str(tmp_path / "gh.log"), **extra}


def tool_log(tmp_path):
    return (tmp_path / "tools.log").read_text() if (tmp_path / "tools.log").exists() else ""


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
@pytest.mark.parametrize("record_binding", [None, {"path": "bindings/acme/notes.yaml", "sha256": "e" * 64}], ids=["0.4.0", "0.3.16"])
def test_promote_verifies_without_downloading_a_binding(tmp_path, record_binding):
    import hashlib
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "manifests.yaml").write_text(PROMO_MANIFESTS)
    (assets / "gate-record.json").write_text(json.dumps({"image": PROMO_REF}))
    (assets / "binding.yaml").write_text(OLD_BINDING_TEXT)
    predicate = {"release": REL, "status": "pass", "image": PROMO_REF, "repo": "acme/notes", "commit": PROMO_COMMIT,
                 "manifests_sha256": hashlib.sha256(PROMO_MANIFESTS.encode()).hexdigest(), "binding": record_binding}
    (tmp_path / "attest.jsonl").write_text(statement(**predicate) + "\n")
    result = subprocess.run(["bash", "-c", promote_step("Fetch and verify")["run"]], cwd=tmp_path, capture_output=True, text=True,
                            env=promote_env(tmp_path, ASSETS=str(assets), ATTEST=str(tmp_path / "attest.jsonl")))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "binding.yaml" not in tool_log(tmp_path) and not (tmp_path / "out/binding.yaml").exists()


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")
def test_the_prod_release_attaches_exactly_the_manifests_and_the_records(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out/manifests.yaml").write_text(PROMO_MANIFESTS)
    (tmp_path / "out/gate-record.json").write_text(json.dumps({"image": PROMO_REF, "binding": None}))
    (tmp_path / "out/promotion-record.json").write_text(json.dumps({"approvals": [{"login": "boss"}], "self_approved": False,
                                                                     "gate2_single_approver": False}))
    (tmp_path / "out/binding.yaml").write_text(OLD_BINDING_TEXT)  # a stray file is never attached
    result = subprocess.run(["bash", "-c", promote_step("Attest, tag")["run"]], cwd=tmp_path, capture_output=True, text=True,
                            env=promote_env(tmp_path, REF=PROMO_REF, COMMIT=PROMO_COMMIT, DIGEST="d" * 64))
    assert result.returncode == 0, result.stdout + result.stderr
    gh = (tmp_path / "gh.log").read_text().splitlines()
    assert release_assets(gh) == ["out/manifests.yaml", "out/gate-record.json", "out/promotion-record.json"]
    assert "Binding" not in "\n".join(release_notes(gh)) and "Binding" not in (tmp_path / "summary.md").read_text()


def test_the_agent_runtime_release_is_tag_driven_pinned_and_signed():
    path = ROOT / ".github/workflows/release-agent-runtime.yml"
    text, doc = path.read_text(), load(path)
    assert doc["on"] == {"push": {"tags": ["agent-runtime-v*"]}} and doc["permissions"] == {}
    assert list(doc["jobs"]) == ["release"] and doc["jobs"]["release"]["permissions"] == {"contents": "read", "packages": "write", "id-token": "write"}
    assert 'agent-runtime-v$version' in text and "merge-base --is-ancestor" in text
    assert "golden-road-agent-runtime" in text and 'cosign sign --yes "$image@$digest"' in text
    assert "-f images/agent-runtime/Dockerfile" in text and "--metadata-file build.json images/agent-runtime" in text
    order = [text.index(k) for k in ("merge-base --is-ancestor", "agent-runtime-v$version", "buildx build", "cosign sign")]
    assert order == sorted(order)


def test_the_agent_runtime_release_uses_the_builder_release_s_actions():
    """The same pinned actions as release-builder.yml (decided while planning: item 3): one set of pins to review and bump."""
    builder = set(uses(load(ROOT / ".github/workflows/release-builder.yml")))
    assert set(uses(load(ROOT / ".github/workflows/release-agent-runtime.yml"))) <= builder


def test_the_builder_release_refuses_an_unpinned_agent_runtime():
    """(decided while planning: item 4) The builder image bakes platform/platform.json, so the runtime's digest must be in it first."""
    text = (ROOT / ".github/workflows/release-builder.yml").read_text()
    step = text[text.index("- name: Refuse an unpinned agent runtime"):text.index("- uses: docker/setup-buildx-action")]
    assert "jq -r '.images.agent.runtime // \"\"' platform/platform.json" in step and "@sha256:0000000000000000000000000000000000000000000000000000000000000000" in step
    assert "${{" not in step, "no expression is interpolated into the script"
    assert text.index("Refuse unconfigured platform values") < text.index("Refuse an unpinned agent runtime")


def _steps(workflow, name, job=None):
    """The steps named `name` in the workflow (in one job, when given)."""
    jobs = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())["jobs"]
    return [s for key, j in jobs.items() if job in (None, key) for s in j.get("steps", []) if s.get("name") == name]


def _step_run(workflow, name):
    """The run script of the one step named `name` in the workflow."""
    [step] = _steps(workflow, name)
    return step["run"]


@pytest.mark.parametrize("runtime, ok", [
    ("ghcr.io/example-org/golden-road-agent-runtime@sha256:" + "2" * 64, True),
    ("ghcr.io/example/golden-road-agent-runtime@sha256:" + "0" * 64, False),
    ("ghcr.io/example-org/golden-road-agent-runtime:latest", False),
    ("", False),
    (None, False),
])
def test_the_runtime_guard_runs(tmp_path, runtime, ok):
    if shutil.which("jq") is None:
        pytest.skip("jq not installed")
    (tmp_path / "platform").mkdir()
    doc = {} if runtime is None else {"images": {"agent": {"runtime": runtime}}}
    (tmp_path / "platform/platform.json").write_text(json.dumps(doc))
    out = subprocess.run(["bash", "-c", _step_run("release-builder.yml", "Refuse an unpinned agent runtime")], cwd=tmp_path,
                         capture_output=True, text=True)
    assert (out.returncode == 0) is ok, out.stdout + out.stderr
    if not ok:
        assert "images.agent.runtime is not pinned" in out.stdout


BUILD_STEP = ("- name: Build and push by digest", "- name: Container authz probe")


def test_an_agent_s_image_is_named_after_its_repository():
    """(decided while planning: item 38) Every agent repo keeps the template's app_name; two repos can't push one GHCR package."""
    text = (ROOT / ".github/workflows/golden-road.yml").read_text()
    start = text.index(BUILD_STEP[0])
    step = text[start:text.index(BUILD_STEP[1], start)]
    assert 'if [ "$(jq -r .app_type src/builder-spec.json)" = "agent" ]; then' in step
    assert 'name="${GITHUB_REPOSITORY#*/}"' in step and 'image="ghcr.io/${owner}/${name}"' in step
    assert "${name,,}" not in step, "the name is checked as GitHub spells it: an upper-case letter is refused, never lower-cased"
    assert "${{" not in _step_run("golden-road.yml", "Build and push by digest"), "the repository and the app name reach the script as environment variables only"


NAME_CASES = [
    ("python", "Example-Org/notes", "ghcr.io/example-org/hello-app"),
    ("node", "example-org/Agent-Calendar", "ghcr.io/example-org/hello-app"),
    ("python", "example-org/golden-road-notes", "ghcr.io/example-org/hello-app"),
    ("agent", "Example-Org/agent-calendar", "ghcr.io/example-org/agent-calendar"),
    ("agent", "example-org/Agent-Calendar", "lower-case letters, digits and -"),
    ("agent", "example-org/agent-calendaR", "lower-case letters, digits and -"),
    ("agent", "example-org/calendar2", "ghcr.io/example-org/calendar2"),
    ("agent", "example-org/golden-road-agent-template", "ghcr.io/example-org/golden-road-agent-template"),
    ("agent", "example-org/agent_calendar", "lower-case letters, digits and -"),
    ("agent", "example-org/agent.calendar", "lower-case letters, digits and -"),
    ("agent", "example-org/-agent", "lower-case letters, digits and -"),
    ("agent", "example-org/agent$(id)", "lower-case letters, digits and -"),
    ("agent", "example-org/golden-road-builder", "golden-road-"),
    ("agent", "example-org/Golden-Road-Portal", "lower-case letters, digits and -"),
]
# The agent repo name rule, held in both places: the drift job's app-name check (the first PR) and the release's build (the backstop).
NAME_RULE = [
    'if [ "$(jq -r .app_type src/builder-spec.json)" = "agent" ]; then',
    'name="${GITHUB_REPOSITORY#*/}"',
    '[[ "$name" =~ ^[a-z0-9]([a-z0-9-]{0,98}[a-z0-9])?$ ]] || { echo "::error::an agent repo\'s name must be lower-case letters, digits and - (it names the image)"; exit 1; }',
    '[[ "$name" != golden-road-* || "$name" == golden-road-agent-template ]] || { echo "::error::an agent repo\'s name must not start with golden-road- (the platform\'s own packages are named so)"; exit 1; }',
    'fi',
]


def _name_rule_env(tmp_path, app_type, repository):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/builder-spec.json").write_text(json.dumps({"app_name": "hello-app", "app_type": app_type}))
    return {"PATH": os.environ["PATH"], "APP": "hello-app", "GITHUB_REPOSITORY": repository, "GITHUB_REPOSITORY_OWNER": repository.split("/")[0]}


def _assert_named(out, expect, image=None):
    if expect.startswith("ghcr.io/"):
        assert out.returncode == 0, out.stdout + out.stderr
        if image is not None:
            assert out.stdout.strip() == expect
    else:
        assert out.returncode != 0 and expect in out.stdout and "::error::" in out.stdout, out.stdout + out.stderr


@pytest.mark.parametrize("app_type, repository, expect", NAME_CASES)
def test_the_image_name_rule_runs(tmp_path, app_type, repository, expect):
    """The release's naming lines, run with bash up to the build: a non-agent keeps app_name, an agent takes its repo's name or stops."""
    if shutil.which("jq") is None:
        pytest.skip("jq not installed")
    script = _step_run("golden-road.yml", "Build and push by digest")
    head = script[:script.index('image="ghcr.io/${owner}/${name}"') + len('image="ghcr.io/${owner}/${name}"')] + '\necho "$image"\n'
    out = subprocess.run(["bash", "-c", head], cwd=tmp_path, capture_output=True, text=True, env=_name_rule_env(tmp_path, app_type, repository))
    _assert_named(out, expect, image=True)


@pytest.mark.parametrize("app_type, repository, expect", NAME_CASES)
def test_the_first_pr_already_checks_an_agent_repo_s_name(tmp_path, app_type, repository, expect):
    """The drift job's app-name check refuses the names the release's image rule refuses, so the first pull request fails, not the
    release after the merge (and its Gate 1 wait). The release keeps its own copy as the backstop."""
    if shutil.which("jq") is None:
        pytest.skip("jq not installed")
    [step] = _steps("golden-road.yml", "Check app name", job="drift")
    assert "${{" not in step["run"]
    out = subprocess.run(["bash", "-c", step["run"]], cwd=tmp_path, capture_output=True, text=True, env=_name_rule_env(tmp_path, app_type, repository))
    _assert_named(out, expect)


def test_the_two_name_rules_are_the_same_lines():
    [drift] = _steps("golden-road.yml", "Check app name", job="drift")
    for script in (drift["run"], _step_run("golden-road.yml", "Build and push by digest")):
        lines = [l.split("  # ")[0].strip() for l in script.splitlines()]
        first = lines.index(NAME_RULE[0])
        assert lines[first:first + len(NAME_RULE)] == NAME_RULE


def test_the_builder_release_verifies_the_runtime_s_signature_before_building():
    """A1: the runtime is signed and pinned. The release checks that the pinned digest's signature is release-agent-runtime.yml's, from an
    agent-runtime-v tag of this repository, before it builds the builder that bakes the pin."""
    steps = yaml.safe_load((ROOT / ".github/workflows/release-builder.yml").read_text())["jobs"]["release"]["steps"]
    names = [s.get("name") or s.get("uses") for s in steps]
    verify = names.index("Verify the agent runtime's signature")
    cosign = next(i for i, s in enumerate(steps) if s.get("uses", "").startswith("sigstore/cosign-installer@"))
    assert names.index("Refuse an unpinned agent runtime") < cosign < verify < names.index("Build, push, and sign the builder image")
    assert SHA_PIN.match(steps[cosign]["uses"]) and steps[cosign]["with"]["cosign-release"] == "v3.1.3"
    run = steps[verify]["run"]
    assert "${{" not in run and "jq -r '.images.agent.runtime' platform/platform.json" in run and 'cosign verify' in run and '"$runtime"' in run
    assert ('--certificate-identity-regexp "^${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/\\.github/workflows/release-agent-runtime\\.yml'
            '@refs/tags/agent-runtime-v[0-9]+\\.[0-9]+\\.[0-9]+$"') in run
    assert "--certificate-oidc-issuer https://token.actions.githubusercontent.com" in run
    agent = (ROOT / ".github/workflows/release-agent-runtime.yml").read_text()
    assert 'tags: ["agent-runtime-v*"]' in agent and 'cosign sign --yes "$image@$digest"' in agent



# An agent's runtime stops at start (exit 2) without its gateway settings, so both probe steps give an agent placeholder ones; the
# container has no network, so nothing is ever sent to them.
PROBE_ENV = [
    "probe_env=()",
    'if [ "$(jq -r .app_type src/builder-spec.json)" = "agent" ]; then probe_env=(-e AGENT_GATEWAY_URL=http://127.0.0.1:9/v1 -e AGENT_GATEWAY_KEY=probe); fi',
]


def probe_env_lines(job):
    run = step_named(job, "Container authz probe")["run"]
    lines = [l.split("  # ")[0].strip() for l in run.splitlines()]
    start = lines.index(PROBE_ENV[0])
    return run, lines[start:start + len(PROBE_ENV)]


@pytest.mark.parametrize("job", ["preview", "release"])
def test_both_probe_steps_start_an_agent_with_placeholder_gateway_settings(job):
    run, lines = probe_env_lines(preview_job() if job == "preview" else gr_doc()["jobs"]["release"])
    assert lines == PROBE_ENV
    assert "${{" not in run, "the step reads the spec itself: no expression in run"
    assert run.index("probe_env=()") < run.index("docker run -d")


@pytest.mark.parametrize("app_type, expect", [("agent", "-e|AGENT_GATEWAY_URL=http://127.0.0.1:9/v1|-e|AGENT_GATEWAY_KEY=probe"),
                                              ("python", ""), (None, "")])
def test_the_probe_env_lines_run(tmp_path, app_type, expect):
    if shutil.which("jq") is None:
        pytest.skip("jq not installed")
    (tmp_path / "src").mkdir()
    spec = {"port": 8080} if app_type is None else {"port": 8080, "app_type": app_type}
    (tmp_path / "src/builder-spec.json").write_text(json.dumps(spec))
    script = "set -euo pipefail\n" + "\n".join(PROBE_ENV) + '\n(IFS="|"; echo "${probe_env[*]}")\n'
    out = subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == expect, out.stdout + out.stderr

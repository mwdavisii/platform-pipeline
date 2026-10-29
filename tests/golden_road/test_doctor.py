"""golden-road doctor against a generated app, a platform pin, and a fake `gh` on PATH that answers from a rules file
and logs every call. Nothing here reaches GitHub."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from builder import VERSION
from builder.config import load_platform, platform_sha256
from builder.generate import generate
from contracts.schema import validate
from golden_road import doctor
from golden_road.cli import main

ROOT = Path(__file__).resolve().parents[2]
ORG = "example-org"
REPO = "example-org/notes"



def repo_platform():
    """The tree's own platform/platform.json: per-app as committed, central when configure.py was run with --scorecard-mode central."""
    return json.loads((ROOT / "platform/platform.json").read_text())


FAKE_GH = f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as log:
    log.write(json.dumps({{"args": args, "host": os.environ.get("GH_HOST"), "cwd": os.getcwd(), "gh_repo": os.environ.get("GH_REPO"), "debug": [os.environ.get("GH_DEBUG"), os.environ.get("DEBUG")]}}) + "\\n")
for rule in json.load(open(os.environ["FAKE_GH_RULES"])):
    if args[:len(rule["args"])] == rule["args"]:
        sys.stdout.write(rule.get("stdout", ""))
        sys.stderr.write(rule.get("stderr", ""))
        sys.exit(rule.get("code", 0))
sys.stderr.write("fake gh: no rule for " + " ".join(args) + "\\n")
sys.exit(99)
"""


def good_rules():
    contexts = [{"context": c, "integration_id": 15368} for c in (*doctor.REQUIRED_CONTEXTS, doctor.FINDINGS_CONTEXT)]
    return [
        {"args": ["auth", "status"], "stderr": "Logged in\n"},
        {"args": ["repo", "view"], "stdout": json.dumps({"nameWithOwner": REPO, "visibility": "PRIVATE"})},
        {"args": ["api", f"repos/{REPO}/rules/branches/main?per_page=100"],
         "stdout": json.dumps([{"type": "pull_request", "parameters": {}}, {"type": "required_status_checks", "parameters": {"required_status_checks": contexts}}])},
        {"args": ["pr", "checks"], "stdout": json.dumps([{"name": c, "bucket": "pass"} for c in doctor.REQUIRED_CONTEXTS])},
        {"args": ["api", f"repos/{ORG}/scorecard-factory/contents/intake/{REPO}.yaml"], "stdout": "abc123\n"},
        {"args": ["api", f"repos/{ORG}/scorecard-factory", "--jq", ".full_name"], "stdout": f"{ORG}/scorecard-factory\n"},
    ]


class FakeGh:
    def __init__(self, tmp_path):
        self.rules_path, self.log = tmp_path / "gh-rules.json", tmp_path / "gh-calls.log"
        self.rules = good_rules()
        self._save()

    def answer(self, args, stdout="", code=0, stderr=""):
        """Answer commands starting with args this way, ahead of the good answers."""
        self.rules.insert(0, {"args": args, "stdout": stdout, "code": code, "stderr": stderr})
        self._save()

    def _save(self):
        self.rules_path.write_text(json.dumps(self.rules))

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []


def caller(uses, name="ci.yml", extra=""):
    """A caller file with the platform's triggers for that file name (JSON is valid YAML flow)."""
    return f"name: caller\non: {json.dumps(doctor.CALLER_TRIGGERS[name])}\njobs:\n  call:\n{extra}    uses: {uses}\n"


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def app(tmp_path, monkeypatch, notes_spec, python_app):
    """A generated app with the four caller workflows, under a platform config that names ORG."""
    config = tmp_path / "platform.json"
    config.write_text(json.dumps({**load_platform(), "org": ORG}))
    monkeypatch.setenv("BUILDER_PLATFORM_CONFIG", str(config))
    generate(python_app, notes_spec)
    workflows = python_app / ".github/workflows"
    workflows.mkdir(parents=True)
    for name, template in doctor.CALLERS.items():
        (workflows / name).write_text(caller(template.format(org=ORG), name))
    git(python_app, "init", "-q")
    git(python_app, "remote", "add", "origin", f"https://github.com/{REPO}.git")
    return python_app


def make_pin(**changes):
    return {"builder_version": VERSION, "wheel": f"golden_road-{VERSION}-py3-none-any.whl", "wheel_sha256": "a" * 64,
            "requirements": "requirements.txt", "requirements_sha256": "b" * 64,
            "image": f"ghcr.io/{ORG}/golden-road-builder@sha256:" + "c" * 64, "platform_config_sha256": platform_sha256(), **changes}


@pytest.fixture
def pin(tmp_path, app):
    path = tmp_path / "platform-pin.json"
    path.write_text(json.dumps(make_pin()))
    return path


@pytest.fixture
def gh(tmp_path, monkeypatch):
    fake = FakeGh(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH)
    (bin_dir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_GH_RULES", str(fake.rules_path))
    monkeypatch.setenv("FAKE_GH_LOG", str(fake.log))
    monkeypatch.delenv("GH_HOST", raising=False)
    return fake


def ctx(app, pin, repo=REPO, org=ORG):
    return doctor.Context(root=app, pin=pin, host="github.com", org=org, repo=repo)


# ---- the schemas ----

def test_pin_schema_accepts_a_pin_and_refuses_bad_ones():
    assert validate("platform-pin.v1", make_pin()) == []
    for bad in (make_pin(builder_version="latest"), make_pin(wheel_sha256="x"), make_pin(image="ghcr.io/o/b:latest"), {**make_pin(), "extra": 1}):
        assert validate("platform-pin.v1", bad)


# ---- builder-pin ----

def test_builder_pin_passes(app, pin):
    assert doctor.check_builder_pin(ctx(app, pin))["status"] == "pass"


def test_builder_pin_missing_or_invalid_pin_fails(app, pin):
    missing = doctor.check_builder_pin(ctx(app, pin.parent / "nope.json"))
    assert missing["status"] == "fail" and "golden-road skill" in missing["fix"]
    pin.write_text(json.dumps({"builder_version": VERSION}))
    assert doctor.check_builder_pin(ctx(app, pin))["status"] == "fail"


def test_builder_pin_other_version_fails(app, pin):
    pin.write_text(json.dumps(make_pin(builder_version="9.9.9", wheel="golden_road-9.9.9-py3-none-any.whl")))
    result = doctor.check_builder_pin(ctx(app, pin))
    assert result["status"] == "fail" and "9.9.9" in result["message"] and "builder generate ." in result["fix"]


def test_builder_pin_other_platform_config_fails(app, pin):
    pin.write_text(json.dumps(make_pin(platform_config_sha256="d" * 64)))
    assert "platform config" in doctor.check_builder_pin(ctx(app, pin))["message"]


def test_builder_pin_old_lock_fails(app, pin):
    lock = json.loads((app / ".builder.lock").read_text())
    lock["builder_version"] = "0.1.0"
    (app / ".builder.lock").write_text(json.dumps(lock))
    result = doctor.check_builder_pin(ctx(app, pin))
    assert result["status"] == "fail" and "0.1.0" in result["message"]
    (app / ".builder.lock").unlink()
    assert "missing" in doctor.check_builder_pin(ctx(app, pin))["message"]


# ---- verify ----

def test_verify_passes_and_fails(app, pin):
    assert doctor.check_verify(ctx(app, pin))["status"] == "pass"
    path = app / "AGENTS.md"
    path.write_text(path.read_text().replace("Never make", "Do make"))
    result = doctor.check_verify(ctx(app, pin))
    assert result["status"] == "fail" and "builder.managed-block-modified" in result["message"]


# ---- callers ----

def test_callers_pass(app, pin):
    assert doctor.check_callers(ctx(app, pin))["status"] == "pass"


@pytest.mark.parametrize("break_it, needle", [
    (lambda w: (w / "scorecard.yml").unlink(), "scorecard.yml is missing"),
    (lambda w: (w / "ci.yml").write_text(caller("other-org/platform-pipeline/.github/workflows/golden-road.yml@main", "ci.yml")), "ci.yml doesn't call"),
    (lambda w: (w / "promote.yml").write_text(caller(f"{ORG}/platform-pipeline/.github/workflows/golden-road-promote.yml@v1", "promote.yml")), "promote.yml doesn't call"),
    (lambda w: (w / "security-scans.yml").write_text("jobs: [unclosed\n"), "not valid YAML"),
    (lambda w: (w / "ci.yml").write_text("- just a list\n"), "ci.yml doesn't call"),
])
def test_callers_fail(app, pin, break_it, needle):
    break_it(app / ".github/workflows")
    result = doctor.check_callers(ctx(app, pin))
    assert result["status"] == "fail" and needle in result["message"]


def test_symlinked_caller_fails(app, pin, tmp_path):
    workflows = app / ".github/workflows"
    real = tmp_path / "real-ci.yml"
    real.write_text((workflows / "ci.yml").read_text())
    (workflows / "ci.yml").unlink()
    (workflows / "ci.yml").symlink_to(real)
    assert "ci.yml can't be read" in doctor.check_callers(ctx(app, pin))["message"]


def test_callers_without_an_org_fail(app, pin):
    result = doctor.check_callers(ctx(app, pin, org=None))
    assert result["status"] == "fail" and "no org" in result["message"]


def test_ruleset_warns_without_the_findings_check(app, pin, gh):
    contexts = [{"context": c, "integration_id": 15368} for c in doctor.REQUIRED_CONTEXTS]
    gh.answer(["api", f"repos/{REPO}/rules/branches/main?per_page=100"],
              stdout=json.dumps([{"type": "required_status_checks", "parameters": {"required_status_checks": contexts}}]))
    result = doctor.check_ruleset(ctx(app, pin))
    assert result["status"] == "warn" and "scorecard / findings" in result["message"]
    assert "scorecard-findings" in result["fix"]


def test_doctor_matches_hello_app(tmp_path, monkeypatch):
    """The caller templates and the required contexts are hello-app's, and doctor accepts hello-app's real caller files."""
    hello = ROOT / "bootstrap/hello-app"
    ruleset = json.loads((hello / ".github/rulesets/main.json").read_text())
    [rule] = [r for r in ruleset["rules"] if r["type"] == "required_status_checks"]
    assert tuple(c["context"] for c in rule["parameters"]["required_status_checks"]) == doctor.REQUIRED_CONTEXTS
    findings = json.loads((hello / ".github/rulesets/scorecard-findings.json").read_text())
    [rule] = findings["rules"]
    assert [c["context"] for c in rule["parameters"]["required_status_checks"]] == [doctor.FINDINGS_CONTEXT]
    root = tmp_path / "hello"
    shutil.copytree(hello / ".github/workflows", root / ".github/workflows")
    for path in (root / ".github/workflows").iterdir():
        path.write_text(re.sub(r"uses: [A-Za-z0-9_-]+/", f"uses: {ORG}/", path.read_text()))
    platform = repo_platform()
    assert set(doctor.callers_for(platform)) == {p.name for p in (root / ".github/workflows").iterdir()}  # central: no scorecard.yml
    ctx = doctor.Context(root=root, pin=tmp_path / "pin.json", host="github.com", org=ORG, platform=platform)
    assert doctor.check_callers(ctx)["status"] == "pass"


# ---- the run, the report and the summary ----

def test_a_crashing_check_fails_and_the_others_still_run(app, pin, gh, monkeypatch):
    def boom(root):
        raise RuntimeError("boom")

    monkeypatch.setattr(doctor, "verify", boom)
    checks = {c["id"]: c for c in doctor.run_checks(app, pin, "github.com")}
    assert list(checks) == list(doctor.CHECK_IDS)
    assert checks["verify"]["status"] == "fail" and "could not run" in checks["verify"]["message"]
    assert checks["callers"]["status"] == "pass"


def test_report_and_summary(app, pin, gh):
    doc = doctor.report(doctor.run_checks(app, pin, "github.com"))
    assert validate("doctor-result.v1", doc) == [] and doc["status"] == "pass"
    assert doctor.summary(doc).splitlines()[0] == "golden-road doctor: ready"
    doc["checks"][3] = {**doc["checks"][3], "status": "fail", "message": "broken", "fix": "fix it"}
    doc["checks"][7] = {**doc["checks"][7], "status": "warn", "message": "later", "fix": "wait"}
    text = doctor.summary({**doc, "status": "fail"})
    assert text.splitlines()[0] == "golden-road doctor: 1 failed (verify); 1 warning (intake)"
    assert "fix: fix it" in text and "fix: wait" in text


def test_messages_have_no_control_characters():
    result = doctor._result("pr-checks", "warn", "a\x1b[31mred\x07\nline" + "x" * 500, "f\x1bix")
    assert not re.search(r"[\x00-\x1f\x7f]", result["message"] + result["fix"]) and len(result["message"]) == doctor.MAX_TEXT


# ---- access and visibility: never degrade ----

def test_access_passes_and_asks_for_the_platform_host(app, pin, gh):
    assert doctor.check_access(ctx(app, pin))["status"] == "pass"
    assert gh.calls()[0]["args"] == ["auth", "status", "--hostname", "github.com"] and gh.calls()[0]["host"] == "github.com"
    assert Path(gh.calls()[0]["cwd"]).resolve() == app.resolve()


@pytest.mark.parametrize("stderr", ["You are not logged into any GitHub hosts.\n", "gh: Forbidden (HTTP 403)\n", "gh: Not Found (HTTP 404)\n", ""])
def test_access_fails_and_never_warns(app, pin, gh, stderr):
    gh.answer(["auth", "status"], code=1, stderr=stderr)
    result = doctor.check_access(ctx(app, pin))
    assert result["status"] == "fail" and "gh auth login --hostname github.com" in result["fix"]


@pytest.mark.parametrize("visibility", ["PRIVATE", "INTERNAL"])
def test_visibility_passes_for_private_and_internal(app, pin, gh, visibility):
    gh.answer(["repo", "view"], stdout=json.dumps({"nameWithOwner": REPO, "visibility": visibility}))
    context = ctx(app, pin, repo=None)
    assert doctor.check_visibility(context)["status"] == "pass" and context.repo == REPO


def test_public_repo_fails(app, pin, gh):
    gh.answer(["repo", "view"], stdout=json.dumps({"nameWithOwner": REPO, "visibility": "PUBLIC"}))
    result = doctor.check_visibility(ctx(app, pin, repo=None))
    assert result["status"] == "fail" and "public" in result["message"] and "Never make" in result["fix"]


@pytest.mark.parametrize("answer", [
    {"code": 1, "stderr": "gh: Not Found (HTTP 404)\n"},
    {"code": 1, "stderr": "gh: Forbidden (HTTP 403)\n"},
    {"code": 1, "stderr": "no git remotes found\n"},
    {"stdout": "not json"},
    {"stdout": json.dumps({"nameWithOwner": "../etc", "visibility": "PRIVATE"})},
    {"stdout": json.dumps({"nameWithOwner": "o/..", "visibility": "PRIVATE"})},
    {"stdout": json.dumps({"nameWithOwner": REPO})},
    {"stdout": "[]"},
])
def test_visibility_that_cant_be_read_fails(app, pin, gh, answer):
    gh.answer(["repo", "view"], **answer)
    context = ctx(app, pin, repo=None)
    assert doctor.check_visibility(context)["status"] == "fail" and context.repo is None


def test_no_gh_fails_access_and_visibility(app, pin, gh, tmp_path, monkeypatch):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    checks = {c["id"]: c for c in doctor.run_checks(app, pin, "github.com")}
    assert checks["access"]["status"] == checks["visibility"]["status"] == "fail"
    assert "not installed" in checks["access"]["message"]
    for check_id in ("ruleset", "pr-checks", "intake"):
        assert checks[check_id]["status"] == "warn" and "not checked" in checks[check_id]["message"]


# ---- ruleset ----

def test_ruleset_passes(app, pin, gh):
    assert doctor.check_ruleset(ctx(app, pin))["status"] == "pass"


def test_ruleset_missing_contexts_warn(app, pin, gh):
    gh.answer(["api", f"repos/{REPO}/rules/branches/main?per_page=100"],
              stdout=json.dumps([{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "golden-road / drift"}]}}]))
    result = doctor.check_ruleset(ctx(app, pin))
    assert result["status"] == "warn" and "scorecard / assessed" in result["message"] and "golden-road / drift" not in result["message"]
    gh.answer(["api", f"repos/{REPO}/rules/branches/main?per_page=100"], stdout="[]")
    assert doctor.check_ruleset(ctx(app, pin))["status"] == "warn"


@pytest.mark.parametrize("answer, status", [
    ({"code": 1, "stderr": "gh: Not Found (HTTP 404)\n"}, "warn"),
    ({"code": 1, "stderr": "gh: Forbidden (HTTP 403)\n"}, "warn"),
    ({"code": 1, "stderr": "gh: Server Error (HTTP 500)\n"}, "fail"),
    ({"code": 1, "stderr": "error connecting to github.com\n"}, "fail"),
    ({"stdout": "{}"}, "fail"),
])
def test_ruleset_unreadable(app, pin, gh, answer, status):
    gh.answer(["api", f"repos/{REPO}/rules/branches/main?per_page=100"], **answer)
    result = doctor.check_ruleset(ctx(app, pin))
    assert result["status"] == status
    if status == "warn":
        assert result["message"].startswith("could not check")


# ---- pr-checks ----

def pr(*buckets):
    return json.dumps([{"name": f"check {i}", "bucket": bucket} for i, bucket in enumerate(buckets)])


@pytest.mark.parametrize("answer, status, needle", [
    ({"stdout": pr("pass", "skipping")}, "pass", "required checks pass"),
    ({"stdout": pr("pass", "fail"), "code": 1}, "fail", "check 1"),
    ({"stdout": pr("cancel")}, "fail", "check 0"),
    ({"stdout": pr("pass", "pending"), "code": 8}, "warn", "still running"),
    ({"stdout": pr("pass", "weird")}, "warn", "still running"),
    ({"stdout": "[]"}, "warn", "no required checks"),
    ({"code": 1, "stderr": "no pull requests found for branch \"main\"\n"}, "warn", "no open pull request"),
    ({"code": 1, "stderr": "no required checks reported on the 'x' branch\n"}, "warn", "no required checks"),
    ({"code": 1, "stderr": "gh: Not Found (HTTP 404)\n"}, "warn", "could not check"),
    ({"code": 1, "stderr": "error connecting to github.com\n"}, "fail", "failed"),
])
def test_pr_checks(app, pin, gh, answer, status, needle):
    gh.answer(["pr", "checks"], **answer)
    result = doctor.check_pr_checks(ctx(app, pin))
    assert result["status"] == status and needle in result["message"]
    assert gh.calls()[-1]["args"] == ["pr", "checks", git(app, "symbolic-ref", "--short", "HEAD"), "--repo", REPO, "--required", "--json", "name,bucket"]


# ---- intake ----

INTAKE = ["api", f"repos/{ORG}/scorecard-factory/contents/intake/{REPO}.yaml"]


def test_intake_record_passes(app, pin, gh):
    assert doctor.check_intake(ctx(app, pin))["status"] == "pass"


def test_intake_without_an_org_fails(app, pin, gh):
    assert doctor.check_intake(ctx(app, pin, org=None))["status"] == "fail"


# ---- the command ----

def run(capsys, *argv):
    code = main(["doctor", *argv])
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert validate("doctor-result.v1", doc) == [], doc
    return code, {c["id"]: c for c in doc["checks"]}, captured.err


def test_ready_repo_exits_0(app, pin, gh, capsys):
    code, checks, err = run(capsys, str(app), "--pin", str(pin))
    assert code == 0 and list(checks) == list(doctor.CHECK_IDS)
    assert {c["status"] for c in checks.values()} == {"pass"}
    assert err.startswith("golden-road doctor: ready")


def test_warnings_alone_exit_0(app, pin, gh, capsys):
    gh.answer(["pr", "checks"], code=1, stderr="no pull requests found for branch \"x\"\n")
    code, checks, err = run(capsys, str(app), "--pin", str(pin))
    assert code == 0 and checks["pr-checks"]["status"] == "warn" and "1 warning (pr-checks)" in err


def test_a_failing_check_exits_1_and_is_named(app, pin, gh, capsys):
    gh.answer(["repo", "view"], stdout=json.dumps({"nameWithOwner": REPO, "visibility": "PUBLIC"}))
    code, checks, err = run(capsys, str(app), "--pin", str(pin))
    assert code == 1 and checks["visibility"]["status"] == "fail"
    # A repository that isn't private is a stop: nothing else is read about it.
    assert err.splitlines()[0] == "golden-road doctor: 1 failed (visibility); 3 warnings (ruleset, pr-checks, intake)"


def test_doctor_never_changes_anything_and_only_reads_github(app, pin, gh, capsys):
    def tree():
        return {p.relative_to(app).as_posix(): p.read_bytes() for p in sorted(app.rglob("*")) if p.is_file()}

    before, pin_before = tree(), pin.read_bytes()
    gh.answer(INTAKE, code=1, stderr="gh: Not Found (HTTP 404)\n")
    run(capsys, str(app), "--pin", str(pin))
    assert tree() == before and pin.read_bytes() == pin_before
    read_only = (["auth", "status"], ["repo", "view"], ["api"], ["pr", "checks"])
    calls = [call["args"] for call in gh.calls()]
    assert len(calls) == 6
    for args in calls:
        assert any(args[:len(prefix)] == prefix for prefix in read_only), args
        if args[0] == "api":
            assert not {"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"} & set(args), args


def test_host_comes_from_the_flag_then_gh_host(app, pin, gh, capsys, monkeypatch):
    run(capsys, str(app), "--pin", str(pin), "--host", "ghe.example.org")
    assert gh.calls()[0]["args"] == ["auth", "status", "--hostname", "ghe.example.org"]
    assert {call["host"] for call in gh.calls()} == {"ghe.example.org"}
    monkeypatch.setenv("GH_HOST", "other.example.org")
    run(capsys, str(app), "--pin", str(pin))
    auth = [call for call in gh.calls() if call["args"][:2] == ["auth", "status"]]
    assert auth[-1]["args"] == ["auth", "status", "--hostname", "other.example.org"] and auth[-1]["host"] == "other.example.org"


def test_the_platform_config_s_host_comes_after_the_flag_and_before_gh_host(app, pin, gh, capsys, monkeypatch):
    config = Path(os.environ["BUILDER_PLATFORM_CONFIG"])
    config.write_text(json.dumps({**json.loads(config.read_text()), "github_host": "acme.ghe.com"}))
    monkeypatch.setenv("GH_HOST", "other.example.org")
    run(capsys, str(app), "--pin", str(pin))
    auth = [call for call in gh.calls() if call["args"][:2] == ["auth", "status"]]
    assert auth[-1]["args"] == ["auth", "status", "--hostname", "acme.ghe.com"] and auth[-1]["host"] == "acme.ghe.com"
    run(capsys, str(app), "--pin", str(pin), "--host", "flag.example.org")
    auth = [call for call in gh.calls() if call["args"][:2] == ["auth", "status"]]
    assert auth[-1]["args"][-1] == "flag.example.org"


def test_a_bad_host_in_the_platform_config_names_the_platform_config(app, pin, gh, capsys):
    config = Path(os.environ["BUILDER_PLATFORM_CONFIG"])
    config.write_text(json.dumps({**json.loads(config.read_text()), "github_host": "not a host"}))
    assert main(["doctor", str(app), "--pin", str(pin)]) != 0
    assert "platform config" in capsys.readouterr().err


def test_default_pin_is_the_skill_cache(app, gh, capsys, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".cache/golden-road").mkdir(parents=True)
    (home / ".cache/golden-road/platform-pin.json").write_text(json.dumps(make_pin()))
    monkeypatch.setenv("HOME", str(home))
    assert doctor.default_pin() == home / ".cache/golden-road/platform-pin.json"
    _, checks, _ = run(capsys, str(app))
    assert checks["builder-pin"]["status"] == "pass"


def test_bad_arguments_exit_2(app, pin, gh, capsys, tmp_path):
    assert main(["doctor", str(tmp_path / "missing"), "--pin", str(pin)]) == 2
    assert "error" in json.loads(capsys.readouterr().err)
    assert main(["doctor", str(app), "--pin", str(pin), "--host", "bad host/x"]) == 2
    assert "error" in json.loads(capsys.readouterr().err)
    assert gh.calls() == []



# ---- review fixes: the gh environment, every remote, the callers' shape, hostile text ----

FORK = "example-org/notes-fork"
OTHER = "other-org/notes"


def view(gh, repo, visibility):
    gh.answer(["repo", "view", repo], stdout=json.dumps({"nameWithOwner": repo, "visibility": visibility}))


def branch(app):
    return git(app, "symbolic-ref", "--short", "HEAD")


def test_gh_never_sees_gh_repo_or_debug_variables(app, pin, gh, capsys, monkeypatch):
    for name, value in (("GH_REPO", "evil-org/other"), ("GH_DEBUG", "api"), ("DEBUG", "1")):
        monkeypatch.setenv(name, value)
    run(capsys, str(app), "--pin", str(pin))
    assert len(gh.calls()) == 5
    assert all(call["gh_repo"] is None and call["debug"] == [None, None] for call in gh.calls())


@pytest.mark.parametrize("visibility", ["", "UNKNOWN", "PUBLIC ", "public", "private", "Internal", " PRIVATE"])
def test_visibility_must_be_exactly_private_or_internal(app, pin, gh, visibility):
    view(gh, REPO, visibility)
    context = ctx(app, pin, repo=None)
    assert doctor.check_visibility(context)["status"] == "fail" and context.repo is None


def test_visibility_json_with_a_failing_exit_code_fails(app, pin, gh):
    gh.answer(["repo", "view"], stdout=json.dumps({"nameWithOwner": REPO, "visibility": "PRIVATE"}), code=1, stderr="boom\n")
    assert doctor.check_visibility(ctx(app, pin, repo=None))["status"] == "fail"


def test_a_public_origin_beside_an_internal_upstream_fails(app, pin, gh):
    git(app, "remote", "add", "upstream", f"https://github.com/{OTHER}.git")
    view(gh, REPO, "PUBLIC")
    view(gh, OTHER, "INTERNAL")
    context = ctx(app, pin, repo=None)
    result = doctor.check_visibility(context)
    assert result["status"] == "fail" and REPO in result["message"] and "Never make" in result["fix"] and context.repo is None
    asked = [call["args"][2] for call in gh.calls() if call["args"][:2] == ["repo", "view"]]
    assert sorted(asked) == sorted([REPO, OTHER])


def test_every_host_remote_is_read_and_all_must_pass(app, pin, gh):
    git(app, "remote", "add", "upstream", f"git@github.com:{OTHER}.git")
    view(gh, REPO, "PRIVATE")
    view(gh, OTHER, "INTERNAL")
    context = ctx(app, pin, repo=None)
    assert doctor.check_visibility(context)["status"] == "pass" and context.repo == REPO
    view(gh, OTHER, "PUBLIC")
    assert doctor.check_visibility(ctx(app, pin, repo=None))["status"] == "fail"
    gh.answer(["repo", "view", OTHER], code=1, stderr="gh: Not Found (HTTP 404)\n")
    assert doctor.check_visibility(ctx(app, pin, repo=None))["status"] == "fail"


@pytest.mark.parametrize("name, url", [
    ("gitlab", "git@gitlab.com:someone/notes.git"),
    ("lookalike", f"https://github.com.evil.example/{OTHER}.git"),
    ("tokened", "https://user:SECRET-TOKEN@gitlab.com/o/r.git"),
    ("garbage", "not-a-url:"),
    ("nested", "https://github.com/o/r/extra"),
])
def test_a_remote_off_the_host_or_unparseable_fails_and_is_named_without_its_url(app, pin, gh, name, url):
    git(app, "remote", "add", name, url)
    result = doctor.check_visibility(ctx(app, pin, repo=None))
    assert result["status"] == "fail" and name in result["message"]
    assert "SECRET" not in result["message"] + result["fix"] and "someone/notes" not in result["message"]
    assert "remove or rename" in result["fix"].lower() and "github.com" in result["fix"]
    assert gh.calls() == []


def test_a_github_remote_fails_when_the_platform_host_is_ghe(app, pin, gh):
    git(app, "remote", "add", "ghe", f"https://ghe.example.org/{REPO}.git")
    result = doctor.check_visibility(doctor.Context(root=app, pin=pin, host="ghe.example.org", org=ORG))
    assert result["status"] == "fail" and "origin" in result["message"] and "ghe.example.org" in result["fix"]


@pytest.mark.parametrize("url", ["file:///srv/git/r.git", "/srv/git/r.git", "../sibling.git", "~/code/r.git", "relative/dir/r"])
def test_local_remotes_are_ignored(app, pin, gh, url):
    git(app, "remote", "add", "local", url)
    context = ctx(app, pin, repo=None)
    assert doctor.check_visibility(context)["status"] == "pass" and context.repo == REPO
    assert [call["args"][2] for call in gh.calls() if call["args"][:2] == ["repo", "view"]] == [REPO]


def test_no_remote_on_the_platform_host_fails(app, pin, gh):
    git(app, "remote", "remove", "origin")
    result = doctor.check_visibility(ctx(app, pin, repo=None))
    assert result["status"] == "fail" and "no git remote" in result["message"] and "github.com" in result["message"]
    git(app, "remote", "add", "local", "/srv/git/r.git")
    assert "no git remote" in doctor.check_visibility(ctx(app, pin, repo=None))["message"]
    assert gh.calls() == []


def test_a_remote_with_several_push_urls_checks_them_all(app, pin, gh):
    """git pushes to every push URL, so every one must be a private or internal repo, and the push remote must name one repo."""
    git(app, "remote", "set-url", "--push", "origin", "https://github.com/me/public.git")
    git(app, "remote", "set-url", "--add", "--push", "origin", f"https://github.com/{REPO}.git")
    context = ctx(app, pin, repo=None)
    result = doctor.check_visibility(context)
    assert result["status"] == "fail" and "push remote origin pushes to more than one repo" in result["message"] and context.repo is None


def test_every_push_url_repo_is_read(app, pin, gh):
    git(app, "remote", "add", "mirror", "https://github.com/me/public.git")
    git(app, "remote", "set-url", "--add", "--push", "mirror", f"https://github.com/{REPO}.git")
    view(gh, REPO, "PRIVATE")
    view(gh, "me/public", "PUBLIC")
    result = doctor.check_visibility(ctx(app, pin, repo=None))
    assert result["status"] == "fail" and "me/public" in result["message"]
    asked = sorted(call["args"][2] for call in gh.calls() if call["args"][:2] == ["repo", "view"])
    assert asked == sorted(["me/public", REPO])


@pytest.mark.parametrize("config, expected", [
    ({}, REPO),
    ({"remote.pushDefault": "fork"}, FORK),
    ({"branch.PUSH.pushRemote": "fork"}, FORK),
    ({"remote.pushDefault": "upstream", "branch.PUSH.pushRemote": "fork"}, FORK),
    ({"remote.pushDefault": "upstream"}, OTHER),
    ({"branch.PUSH.remote": "fork"}, FORK),
    ({"branch.PUSH.remote": "fork", "remote.pushDefault": "upstream"}, OTHER),
    ({"branch.PUSH.remote": "fork", "branch.PUSH.pushRemote": "upstream"}, OTHER),
])
def test_the_push_remote_names_the_repo(app, pin, gh, config, expected):
    git(app, "remote", "add", "fork", f"https://github.com/{FORK}")
    git(app, "remote", "add", "upstream", f"ssh://git@github.com:22/{OTHER}.git")
    for key, value in config.items():
        git(app, "config", key.replace("PUSH", branch(app)), value)
    context = ctx(app, pin, repo=None)
    assert doctor.check_visibility(context)["status"] == "pass" and context.repo == expected


def test_a_push_remote_off_the_host_fails(app, pin, gh):
    git(app, "remote", "add", "local", "/srv/git/r.git")
    git(app, "config", f"branch.{branch(app)}.pushRemote", "local")
    result = doctor.check_visibility(ctx(app, pin, repo=None))
    assert result["status"] == "fail" and "push remote local is not on github.com" in result["message"]


@pytest.mark.parametrize("url, repo", [
    ("https://github.com/o/r.git", "o/r"), ("https://github.com/o/r", "o/r"), ("https://github.com/o/r/", "o/r"),
    ("https://user:tok@github.com/o/r.git", "o/r"), ("HTTPS://GitHub.com/o/r.git", "o/r"),
    ("ssh://git@github.com/o/r.git", "o/r"), ("ssh://git@github.com:22/o/r.git", "o/r"), ("git@github.com:o/r.git", "o/r"),
    ("github.com:o/r", "o/r"), ("git@github.com:/o/r.git", "o/r"), ("github.com:/o/r", "o/r"), ("git://github.com/o/r.git", "o/r"),
    ("https://gitlab.com/o/r.git", None), ("https://github.com.evil.example/o/r.git", None), ("https://evilgithub.com/o/r", None),
    ("git@gitlab.com:o/r.git", None), ("file:///srv/o/r.git", None), ("/srv/git/r.git", None), ("https://github.com/o", None),
    ("https://github.com/../etc", None), ("https://github.com/o/r/extra", None), ("", None),
])
def test_remote_urls(url, repo):
    assert doctor.remote_repo(url, "github.com") == repo


def test_remote_url_with_a_port_host(app):
    assert doctor.remote_repo("ssh://git@ghe.example.org:2222/o/r.git", "ghe.example.org") == "o/r"
    assert doctor.remote_repo("https://ghe.example.org:8443/o/r.git", "ghe.example.org:8443") == "o/r"


def test_pr_checks_names_the_branch_and_repo_and_needs_a_branch(app, pin, gh):
    assert doctor.check_pr_checks(ctx(app, pin))["status"] == "pass"
    assert gh.calls()[-1]["args"][2:5] == [branch(app), "--repo", REPO]
    (app / ".git/HEAD").write_text("0" * 40 + "\n")  # detached
    result = doctor.check_pr_checks(ctx(app, pin))
    assert result["status"] == "warn" and "branch" in result["message"]


# ---- callers: the job and the triggers ----

@pytest.mark.parametrize("name", list(doctor.CALLERS))
def test_caller_triggers_are_hello_apps(name):
    if name not in doctor.callers_for(repo_platform()):
        pytest.skip(f"{name} is not an app file when the platform reviews centrally; the factory's own triggers are tested there")
    parsed = doctor.yaml.safe_load((ROOT / "bootstrap/hello-app/.github/workflows" / name).read_text())
    assert (parsed[True] if True in parsed else parsed["on"]) == doctor.CALLER_TRIGGERS[name]


def put(app, name, text):
    (app / ".github/workflows" / name).write_text(text)


def test_a_quoted_on_key_is_still_the_triggers(app, pin):
    want = doctor.CALLERS["ci.yml"].format(org=ORG)
    put(app, "ci.yml", f'name: x\n"on": {json.dumps(doctor.CALLER_TRIGGERS["ci.yml"])}\njobs:\n  call:\n    uses: {want}\n')
    assert doctor.check_callers(ctx(app, pin))["status"] == "pass"


def test_the_expected_uses_in_a_comment_does_not_count(app, pin):
    want = doctor.CALLERS["ci.yml"].format(org=ORG)
    put(app, "ci.yml", f"name: x\non: {json.dumps(doctor.CALLER_TRIGGERS['ci.yml'])}\n# uses: {want}\njobs:\n  call:\n    uses: other-org/x/.github/workflows/y.yml@main\n")
    result = doctor.check_callers(ctx(app, pin))
    assert result["status"] == "fail" and "ci.yml doesn't call" in result["message"]


def test_a_caller_job_with_an_if_fails(app, pin):
    put(app, "ci.yml", caller(doctor.CALLERS["ci.yml"].format(org=ORG), "ci.yml", extra="    if: false\n"))
    result = doctor.check_callers(ctx(app, pin))
    assert result["status"] == "fail" and "ci.yml" in result["message"] and "if:" in result["message"]


@pytest.mark.parametrize("triggers", [{"workflow_dispatch": None}, {"pull_request": None}, {"push": {"branches": ["main"]}},
                                      {"pull_request": None, "push": {"branches": ["dev"]}}, None])
def test_caller_triggers_must_match(app, pin, triggers):
    want = doctor.CALLERS["ci.yml"].format(org=ORG)
    put(app, "ci.yml", f"name: x\non: {json.dumps(triggers)}\njobs:\n  call:\n    uses: {want}\n")
    result = doctor.check_callers(ctx(app, pin))
    assert result["status"] == "fail" and "ci.yml has different triggers" in result["message"]


# ---- the pin file ----

@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")
def test_a_pin_that_is_a_fifo_is_refused_without_blocking(app, tmp_path):
    fifo = tmp_path / "pin-fifo"
    os.mkfifo(fifo)
    assert doctor.check_builder_pin(ctx(app, fifo))["status"] == "fail"


def test_an_oversize_pin_is_refused(app, pin, monkeypatch):
    monkeypatch.setattr("golden_road.io.MAX_INPUT_BYTES", 20)
    assert doctor.check_builder_pin(ctx(app, pin))["status"] == "fail"


def test_a_newer_pinned_builder_than_the_installed_one_fails(app, pin):
    major, minor, patch = (int(part) for part in VERSION.split("."))
    newer = f"{major}.{minor}.{patch + 1}"  # derived: a version bump must not break this test
    pin.write_text(json.dumps(make_pin(builder_version=newer, wheel=f"golden_road-{newer}-py3-none-any.whl")))
    result = doctor.check_builder_pin(ctx(app, pin))
    assert result["status"] == "fail" and f"installed builder is {VERSION}" in result["message"] and f"pins {newer}" in result["message"]


# ---- hostile text ----

def test_messages_drop_c1_and_bidi_controls():
    nasty = "a\x85b\x9fc‮d‪e⁦f⁩g‎h‏i"
    result = doctor._result("pr-checks", "warn", nasty, nasty)
    assert not re.search("[\x00-\x1f\x7f-\x9f‎‏‪-‮⁦-⁩]", result["message"] + result["fix"])
    assert result["message"].replace(" ", "") == "abcdefghi"


# ---- intake ----

def test_intake_record_read_forbidden_warns(app, pin, gh):
    gh.answer(INTAKE, code=1, stderr="gh: Forbidden (HTTP 403)\n")
    result = doctor.check_intake(ctx(app, pin))
    assert result["status"] == "warn" and result["message"].startswith("could not check")


# ---- the help ----

def test_help_says_stdout_is_a_result_only_for_exit_0_and_1(capsys):
    with pytest.raises(SystemExit):
        main(["doctor", "--help"])
    text = " ".join(capsys.readouterr().out.split()).lower()
    assert "exit 2" in text and "stderr" in text


def test_callers_fix_says_why_and_whom_to_ask(app, pin):
    (app / ".github/workflows/ci.yml").unlink()
    fix = doctor.check_callers(ctx(app, pin))["fix"]
    assert "required checks must run on every PR and on main" in fix and "ask the platform team" in fix


# ---- git remote -v lines ----

def fake_remotes(monkeypatch, output):
    """Answer `git remote -v` with this output; every other git command runs for real."""
    real = doctor.run_git

    def run_git(root, args):
        return doctor.GhResult(0, output, "") if args == ["remote", "-v"] else real(root, args)

    monkeypatch.setattr(doctor, "run_git", run_git)


def test_a_partial_clone_remote_counts(app, pin, gh, monkeypatch):
    fake_remotes(monkeypatch, f"origin\thttps://github.com/{REPO}.git (fetch) [blob:none]\norigin\thttps://github.com/{REPO}.git (push)\n")
    context = ctx(app, pin, repo=None)
    assert doctor.check_visibility(context)["status"] == "pass" and context.repo == REPO


def test_a_real_partial_clone_config_passes(app, pin, gh):
    """Whatever this git version prints for a promisor remote, doctor reads it."""
    git(app, "config", "remote.origin.promisor", "true")
    git(app, "config", "remote.origin.partialclonefilter", "blob:none")
    context = ctx(app, pin, repo=None)
    assert doctor.check_visibility(context)["status"] == "pass" and context.repo == REPO


def remote_result(app, pin, monkeypatch, line):
    fake_remotes(monkeypatch, f"origin\thttps://github.com/{REPO}.git (fetch)\n{line}\norigin\thttps://github.com/{REPO}.git (push)\n")
    return doctor.check_visibility(ctx(app, pin, repo=None))


@pytest.mark.parametrize("line", [
    "odd\thttps://user:SECRET@github.com/o/r.git (fetch) trailing",
    "odd\thttps://user:SECRET@github.com/o/r.git (fetch) [blob:none",
    "odd\thttps://user:SECRET@github.com/o/r.git (mirror)",
    "odd\thttps://user:SECRET@github.com/o/my repo.git (fetch)",
    "odd\t (fetch)",
    "odd\t",
], ids=["trailing-text", "unclosed-filter", "unknown-kind", "spaced-token-url", "empty-url", "empty-url-as-git-prints-it"])
def test_a_remote_line_doctor_does_not_understand_fails_naming_the_remote_not_the_line(app, pin, gh, monkeypatch, line):
    result = remote_result(app, pin, monkeypatch, line)
    assert result["status"] == "fail" and "odd" in result["message"]
    assert "SECRET" not in result["message"] + result["fix"]
    assert gh.calls() == []


def test_a_remote_line_with_no_name_fails_as_not_understood(app, pin, gh, monkeypatch):
    result = remote_result(app, pin, monkeypatch, "just-a-name-SECRET")
    assert result["status"] == "fail" and "not understood" in result["message"]
    assert "SECRET" not in result["message"] + result["fix"]
    assert gh.calls() == []


def test_a_local_remote_path_with_a_space_is_ignored(app, pin, gh, monkeypatch):
    path = "/Users/Jane Doe/backup.git"
    result = remote_result(app, pin, monkeypatch, f"backup\t{path} (fetch)\nbackup\t{path} (push)")
    assert result["status"] == "pass", result


def test_no_intake_record_points_at_the_portal(app, pin, gh, monkeypatch):
    monkeypatch.setattr(doctor, "load_platform", lambda: {"org": ORG, "portal_url": "https://portal.example.org"})
    gh.answer(INTAKE, code=1, stderr="gh: Not Found (HTTP 404)\n")
    result = doctor.check_intake(ctx(app, pin))
    assert result["status"] == "warn" and result["message"] == "no intake record yet"
    assert f"golden-road register --repo {REPO}" in result["fix"] and "an admin approves it" in result["fix"]
    assert not [c for c in gh.calls() if c["args"][:2] == ["issue", "list"]]


def test_without_a_portal_the_fix_says_who_adds_the_record(app, pin, gh, monkeypatch):
    monkeypatch.setattr(doctor, "load_platform", lambda: {"org": ORG})
    gh.answer(INTAKE, code=1, stderr="gh: Not Found (HTTP 404)\n")
    assert "security adds the intake record" in doctor.check_intake(ctx(app, pin))["fix"]


@pytest.mark.parametrize("record, status", [({"code": 1, "stderr": "gh: Forbidden (HTTP 403)\n"}, "warn"),
                                            ({"code": 1, "stderr": "gh: Server Error (HTTP 502)\n"}, "fail")])
def test_intake_unreadable(app, pin, gh, record, status):
    gh.answer(INTAKE, **record)
    assert doctor.check_intake(ctx(app, pin))["status"] == status


def test_doctor_has_no_intake_issue_vocabulary_left():
    assert not hasattr(doctor, "INTAKE_TITLE_PREFIX") and not hasattr(doctor, "INTAKE_LABEL")


FACTORY = ["api", f"repos/{ORG}/scorecard-factory"]


def test_no_record_in_a_factory_that_can_be_read_is_no_record_yet_after_one_call_to_the_factory(app, pin, gh):
    gh.answer(INTAKE, code=1, stderr="gh: Not Found (HTTP 404)\n")
    result = doctor.check_intake(ctx(app, pin))
    assert result["message"] == "no intake record yet"
    assert len([c for c in gh.calls() if c["args"][:2] == FACTORY]) == 1


@pytest.mark.parametrize("answer, status", [({"code": 1, "stderr": "gh: Not Found (HTTP 404)\n"}, "warn"),
                                            ({"code": 1, "stderr": "gh: Forbidden (HTTP 403)\n"}, "warn"),
                                            ({"code": 1, "stderr": "gh: Server Error (HTTP 502)\n"}, "fail")])
def test_a_factory_that_cannot_be_read_is_not_reported_as_no_record(app, pin, gh, answer, status):
    """A 404 on the record is also what a person without read access to the factory sees, so doctor asks the factory itself."""
    gh.answer(INTAKE, code=1, stderr="gh: Not Found (HTTP 404)\n")
    gh.answer(FACTORY, **answer)
    result = doctor.check_intake(ctx(app, pin))
    assert result["status"] == status and result["message"] != "no intake record yet" and "register" not in result["fix"]

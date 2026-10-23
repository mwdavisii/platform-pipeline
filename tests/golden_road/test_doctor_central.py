"""golden-road doctor in central review mode: three callers are enough, a left-over scorecard caller is a warning, and the
ruleset's `scorecard / assessed` must be pinned to the scorecard App. Per-app mode is unchanged."""
import json
import os
from pathlib import Path

import pytest

from golden_road import doctor
from test_doctor import FakeGh, ORG, REPO, app, caller, ctx, gh, pin  # noqa: F401 - fixtures

CENTRAL = {"scorecard_mode": "central", "scorecard_app_id": 99}
FACTORY_CALLER = f"{ORG}/scorecard-factory/.github/workflows/scorecard.yml@main"


def central_ctx(app, pin, **platform):
    return doctor.Context(root=app, pin=pin, host="github.com", org=ORG, repo=REPO, platform={**CENTRAL, **platform})


def test_callers_for_drops_only_the_scorecard_caller_in_central_mode():
    assert set(doctor.callers_for({})) == set(doctor.CALLERS) == {"ci.yml", "promote.yml", "scorecard.yml", "security-scans.yml"}
    assert set(doctor.callers_for(CENTRAL)) == {"ci.yml", "promote.yml", "security-scans.yml"}
    assert doctor.callers_for({"scorecard_mode": "per-app"}) == doctor.CALLERS
    assert doctor.callers_for({"scorecard_mode": "nonsense"}) == doctor.CALLERS, "an invalid mode is the builder-pin check's to report; callers stay per-app"


def test_central_mode_needs_only_three_callers(app, pin):
    (app / ".github/workflows/scorecard.yml").unlink()
    result = doctor.check_callers(central_ctx(app, pin))
    assert result["status"] == "pass" and "caller workflows" in result["message"]
    assert doctor.check_callers(ctx(app, pin))["status"] == "fail", "per-app mode still wants the fourth"


def test_central_mode_still_checks_the_three(app, pin):
    (app / ".github/workflows/scorecard.yml").unlink()
    (app / ".github/workflows/promote.yml").unlink()
    result = doctor.check_callers(central_ctx(app, pin))
    assert result["status"] == "fail" and "promote.yml is missing" in result["message"] and "scorecard.yml" not in result["message"]


def test_central_mode_warns_about_a_left_over_platform_scorecard_caller(app, pin):
    result = doctor.check_callers(central_ctx(app, pin))  # the fixture still has the four
    assert result["status"] == "warn" and "scorecard.yml" in result["message"]
    assert "remove" in result["fix"].lower() and ".github/workflows/scorecard.yml" in result["fix"] and "pull request" in result["fix"]


def test_central_mode_ignores_a_scorecard_yml_that_is_the_apps_own(app, pin):
    (app / ".github/workflows/scorecard.yml").write_text("name: mine\non: workflow_dispatch\njobs:\n  x:\n    runs-on: ubuntu-latest\n    steps: []\n")
    assert doctor.check_callers(central_ctx(app, pin))["status"] == "pass"


def test_a_failing_caller_outranks_the_left_over_warning(app, pin):
    (app / ".github/workflows/ci.yml").unlink()
    assert doctor.check_callers(central_ctx(app, pin))["status"] == "fail"


def scorecard_rules(gh, integration_id):
    contexts = [{"context": c, "integration_id": 15368 if c != "scorecard / assessed" else integration_id} for c in doctor.REQUIRED_CONTEXTS]
    gh.answer(["api", f"repos/{REPO}/rules/branches/main?per_page=100"],
              stdout=json.dumps([{"type": "required_status_checks", "parameters": {"required_status_checks": contexts}}]))


def test_central_mode_warns_when_assessed_is_not_pinned_to_the_app(app, pin, gh):
    scorecard_rules(gh, 15368)
    result = doctor.check_ruleset(central_ctx(app, pin))
    assert result["status"] == "warn" and "not pinned to the scorecard App (id 99)" in result["message"]
    assert "re-pin" in result["fix"].lower() or "pin" in result["fix"].lower()


def test_central_mode_passes_when_assessed_is_pinned_to_the_app(app, pin, gh):
    scorecard_rules(gh, 99)
    assert doctor.check_ruleset(central_ctx(app, pin))["status"] == "pass"


def test_central_mode_with_another_apps_id_warns(app, pin, gh):
    scorecard_rules(gh, 100)
    assert doctor.check_ruleset(central_ctx(app, pin))["status"] == "warn"


def test_per_app_mode_is_unchanged(app, pin, gh):
    assert doctor.check_ruleset(ctx(app, pin))["status"] == "pass"
    scorecard_rules(gh, 15368)
    assert doctor.check_ruleset(ctx(app, pin))["status"] == "pass"
    assert doctor.check_callers(ctx(app, pin))["status"] == "pass"


def test_run_checks_reads_the_mode_from_the_platform_config(app, pin, gh, monkeypatch, tmp_path):
    config = tmp_path / "platform.json"
    config.write_text(json.dumps({**json.loads(Path(os.environ["BUILDER_PLATFORM_CONFIG"]).read_text()), **CENTRAL}))
    monkeypatch.setenv("BUILDER_PLATFORM_CONFIG", str(config))
    (app / ".github/workflows/scorecard.yml").unlink()
    results = {c["id"]: c for c in doctor.run_checks(app, pin, "github.com")}
    assert results["callers"]["status"] == "pass"
    assert results["ruleset"]["status"] == "warn" and "not pinned" in results["ruleset"]["message"]


def test_an_unusable_mode_means_per_app(app, pin, gh, monkeypatch, tmp_path):
    config = tmp_path / "bad.json"
    config.write_text(json.dumps({"org": ORG, "scorecard_mode": "nonsense"}))
    monkeypatch.setenv("BUILDER_PLATFORM_CONFIG", str(config))
    results = {c["id"]: c for c in doctor.run_checks(app, pin, "github.com")}
    assert results["callers"]["status"] == "pass", "four callers present: per-app rules apply"
    assert results["ruleset"]["status"] == "pass"


def test_central_without_an_app_id_has_nothing_to_pin_against(app, pin, gh, monkeypatch, tmp_path):
    config = tmp_path / "bad.json"
    config.write_text(json.dumps({"org": ORG, "scorecard_mode": "central"}))
    monkeypatch.setenv("BUILDER_PLATFORM_CONFIG", str(config))
    results = {c["id"]: c for c in doctor.run_checks(app, pin, "github.com")}
    assert results["callers"]["status"] == "warn" and results["ruleset"]["status"] == "pass"

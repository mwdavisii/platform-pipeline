"""The bundle's templates: the callers are hello-app's (with the app name left open), and there is no intake issue template
any more (apps register in the portal)."""

import json
import re
from pathlib import Path

from golden_road import doctor

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "bootstrap/golden-road-agents/skill"
HELLO = ROOT / "bootstrap/hello-app/.github/workflows"
FIXTURE = Path(__file__).resolve().parent / "fixtures/scorecard.yml"


def repo_platform():
    """The tree's own platform/platform.json: per-app as committed, central when configure.py was run with --scorecard-mode central."""
    return json.loads((ROOT / "platform/platform.json").read_text())


def test_the_caller_templates_are_hello_app_s_with_the_app_name_left_open():
    templates = SKILL / "templates/workflows"
    callers = doctor.callers_for(repo_platform())  # central: no scorecard.yml in the bundle or in hello-app
    assert sorted(p.name for p in templates.iterdir()) == sorted(callers)
    assert sorted(p.name for p in HELLO.iterdir()) == sorted(callers)
    for name in callers:
        assert (templates / name).read_text().replace("__APP_NAME__", "hello-app") == (HELLO / name).read_text(), name
    assert "app_name: __APP_NAME__" in (templates / "ci.yml").read_text()


def test_the_bundle_carries_the_callers_the_platform_mode_needs():
    """Per app (the committed mode) the bundle has all four; configure.py --scorecard-mode central removes scorecard.yml from it,
    and doctor's list for that mode is the three that remain."""
    assert sorted(p.name for p in (SKILL / "templates/workflows").iterdir()) == sorted(doctor.callers_for(repo_platform()))
    assert sorted(doctor.callers_for({"scorecard_mode": "central", "scorecard_app_id": 5})) == sorted(set(doctor.CALLERS) - {"scorecard.yml"})


def test_the_write_callers_tests_scorecard_fixture_is_the_committed_caller():
    """Whenever the tree has the per-app scorecard caller (hello-app's file and the bundle's template), the kit tests' copy is it."""
    for path in (HELLO / "scorecard.yml", SKILL / "templates/workflows/scorecard.yml"):
        if path.exists():
            assert re.sub(r"[A-Za-z0-9_-]+/scorecard-factory/", "ORG_PLACEHOLDER/scorecard-factory/", path.read_text()) == FIXTURE.read_text()


def test_the_bundle_has_no_intake_issue_template():
    assert not (SKILL / "templates/intake-issue.md").exists()


def test_write_callers_uses_the_spec_schema_s_app_name_rule():
    pattern = json.loads((ROOT / "src/contracts/schemas/builder-spec.v1.json").read_text())["properties"]["app_name"]["pattern"]
    assert f're.compile(r"{pattern[1:-1]}")' in (SKILL / "write_callers.py").read_text()

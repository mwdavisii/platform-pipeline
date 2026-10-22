"""The bundle's templates: the callers are hello-app's (with the app name left open), and the intake request template has
the factory form's fields in the form's order."""

import json
import re
from pathlib import Path

import yaml

from golden_road import doctor

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "bootstrap/golden-road-agents/skill"
HELLO = ROOT / "bootstrap/hello-app/.github/workflows"


def test_the_caller_templates_are_hello_app_s_with_the_app_name_left_open():
    templates = SKILL / "templates/workflows"
    assert sorted(p.name for p in templates.iterdir()) == sorted(doctor.CALLERS)
    for name in doctor.CALLERS:
        assert (templates / name).read_text().replace("__APP_NAME__", "hello-app") == (HELLO / name).read_text(), name
    assert "app_name: __APP_NAME__" in (templates / "ci.yml").read_text()


def test_the_intake_template_has_the_form_s_fields_in_order():
    form = yaml.safe_load((ROOT / "bootstrap/scorecard-factory/.github/ISSUE_TEMPLATE/intake-request.yml").read_text())
    labels = [f["attributes"]["label"] for f in form["body"] if "id" in f]
    assert re.findall(r"(?m)^### (.+)$", (SKILL / "templates/intake-issue.md").read_text()) == labels


def test_write_callers_uses_the_spec_schema_s_app_name_rule():
    pattern = json.loads((ROOT / "src/contracts/schemas/builder-spec.v1.json").read_text())["properties"]["app_name"]["pattern"]
    assert f're.compile(r"{pattern[1:-1]}")' in (SKILL / "write_callers.py").read_text()

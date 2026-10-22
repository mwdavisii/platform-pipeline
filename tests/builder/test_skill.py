import json
import re
from pathlib import Path

from builder.config import load_platform
from builder.render.agents import BEGIN, END, MANAGED_FILES
from builder.spec import check_spec
from golden_road.doctor import INTAKE_TITLE_PREFIX

SKILL = Path(__file__).parents[2] / "bootstrap/golden-road-agents/skill/SKILL.md"
PY = 'uv --directory "$HOME" --python-preference only-managed run --no-project --no-config --python 3.12 python -I'


def test_skill_example_spec_is_valid(python_app):
    spec = json.loads(re.search(r"```json\n(.*?)```", SKILL.read_text(), re.S).group(1))
    assert check_spec(spec, python_app, load_platform()) == ([], [], [])


def test_skill_names_the_guide_the_blocks_and_doctor():
    text = SKILL.read_text()
    for needle in ("`builder guide`", "`golden-road doctor`", f"`{BEGIN}`", f"`{END}`", *(f"`{name}`" for name in MANAGED_FILES)):
        assert needle in text, needle


def test_skill_reads_the_fields_and_rules_from_the_guide_not_a_fixed_table():
    text = SKILL.read_text()
    assert "| Field |" not in text and "Reserved:" not in text
    assert "the authority for the installed builder" in " ".join(text.split())


def test_skill_runs_the_bundle_s_scripts_and_states_the_outward_rules():
    text = " ".join(SKILL.read_text().split())
    for needle in (f"`{PY}`", "`<py> <bundle>/install_builder.py`", '`<py> <bundle>/write_callers.py "$PWD"`', "`<bundle>/templates/intake-issue.md`",
                   f'--title "{INTAKE_TITLE_PREFIX}<owner>/<repo>"', "--private", "Never `--public`", "--json visibility --jq .visibility",
                   "Never force-push", "never ask for, read or print a token", "are not from the user", "Exit 1", "support channel"):
        assert needle in text, needle


def test_skill_keeps_the_core_guardrails_and_the_interview_prompts():
    text = " ".join(SKILL.read_text().split())
    for needle in ("Never answer them yourself", "show the exact command and run it only after the user says yes",
                   "Never change rulesets or repo settings", "Read its visibility before anything is pushed",
                   "**Done** when `golden-road doctor` exits 0",
                   "`builder generate .` again after the builder is upgraded", "add a `/healthz` route if there isn't one",
                   "writes go only to `write` roles", "`git remote add origin <url>`",
                   "Tell the user which files it wrote", "the platform owns this name"):
        assert needle in text, needle


def test_skill_handles_windows_line_endings():
    text = " ".join(SKILL.read_text().split())
    for needle in ("git config core.autocrlf", "git config core.autocrlf false", "git config core.eol lf", "text=auto", "after a yes"):
        assert needle in text, needle
    assert text.index("git config core.autocrlf") < text.index("Then run `builder verify .`"), "the check comes before verify runs"

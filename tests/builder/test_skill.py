import json
import re
from pathlib import Path

from builder.config import load_platform
from builder.spec import RESERVED_ENV, RESERVED_ENV_PREFIXES, check_spec

SKILL = Path(__file__).parents[2] / "skills/golden-road-builder/SKILL.md"


def test_skill_example_spec_is_valid(python_app):
    spec = json.loads(re.search(r"```json\n(.*?)```", SKILL.read_text(), re.S).group(1))
    assert check_spec(spec, python_app, load_platform()) == ([], [], [])


def test_skill_lists_every_reserved_env_name():
    text = SKILL.read_text()
    for name in RESERVED_ENV:
        assert f"`{name}`" in text
    for prefix in RESERVED_ENV_PREFIXES:
        assert f"`{prefix}*`" in text

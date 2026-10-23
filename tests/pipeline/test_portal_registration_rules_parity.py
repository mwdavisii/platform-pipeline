"""The scorecard (check-factory) and the portal's collector must accept the same config/registration.yaml files."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bootstrap/golden-road-portal"))

from golden_road.io import InputError  # noqa: E402

from portal.collectors.rules import RulesProblem, parse_rules  # noqa: E402
from scorecard.config import load_registration_rules  # noqa: E402

FACTORY_RULES = (ROOT / "bootstrap/scorecard-factory/config/registration.yaml").read_text()


@pytest.mark.parametrize("text", [
    FACTORY_RULES, FACTORY_RULES.replace("phi: high", "phi: low"), FACTORY_RULES + "extra: 1\n",
    FACTORY_RULES + "no_spec: {tier: low, data_classification: public}\n", "schema: registration-rules/v1\n",
    "", "- a\n", FACTORY_RULES.replace("many_dependencies_over: 50", "many_dependencies_over: 2026-10-23"),
    FACTORY_RULES.replace("internal: low", "internal: moderate"), FACTORY_RULES + "#" * 70000 + "\n", FACTORY_RULES + "#" * (65536 - len(FACTORY_RULES) - 1) + "\n",
])
def test_the_scorecard_and_the_portal_accept_the_same_rules(tmp_path, text):
    factory = tmp_path / "factory"
    (factory / "config").mkdir(parents=True)
    (factory / "config/registration.yaml").write_text(text)
    try:
        load_registration_rules(factory)
        scorecard_ok = True
    except InputError:
        scorecard_ok = False
    try:
        parse_rules(text.encode())
        portal_ok = True
    except RulesProblem:
        portal_ok = False
    assert scorecard_ok == portal_ok


def test_the_factory_s_committed_rules_are_valid_for_the_portal():
    assert parse_rules((ROOT / "bootstrap/scorecard-factory/config/registration.yaml").read_bytes())

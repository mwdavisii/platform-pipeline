"""The floor for the three levels (spec 2026-10-30 C3), from the bootstrap factory's config/registration.yaml, computed the same way by
the scorecard (T-21) and the portal's collector."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bootstrap/golden-road-portal"))

from portal.floor import compute_floor as portal_floor  # noqa: E402
from scorecard.config import load_registration_rules  # noqa: E402
from scorecard.signals.registration import compute_floor as scorecard_floor  # noqa: E402

RULES = load_registration_rules(ROOT / "bootstrap/scorecard-factory")


def evidence(classification, spec=True, signals=()):
    return {"spec_present": spec, "data_classification": classification, "signals": [{"rule": r, "line": r} for r in signals]}


@pytest.mark.parametrize("level, floor", [("public", ("low", "public")), ("confidential", ("high", "confidential")),
                                          ("protected", ("high", "protected"))])
def test_each_level_s_minimum_tier(level, floor):
    assert scorecard_floor(evidence(level), RULES) == floor == portal_floor(evidence(level), RULES)


def test_no_spec_is_high_and_confidential():
    assert RULES["no_spec"] == {"tier": "high", "data_classification": "confidential"}
    assert scorecard_floor(evidence(None, spec=False), RULES) == ("high", "confidential") == portal_floor(evidence(None, spec=False), RULES)


def test_a_signal_still_raises_a_public_app():
    assert scorecard_floor(evidence("public", signals=["secret_env"]), RULES) == ("moderate", "public")

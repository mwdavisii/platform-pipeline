"""Scrutiny levels, data classifications, and the intake-driven floor and ASVS level."""

from golden_road.io import InputError

LEVELS = ("standard", "elevated", "security_review")
CLASSIFICATIONS = ("public", "internal", "restricted", "phi")


def rank(level: str) -> int:
    return LEVELS.index(level)


def highest(levels) -> str:
    """The highest of the given levels; `standard` when there are none."""
    return max(levels, key=rank, default="standard")


def effective_classification(*values) -> str | None:
    """The most sensitive of the given classifications, ignoring missing ones."""
    present = [v for v in values if v is not None]
    return max(present, key=CLASSIFICATIONS.index, default=None)


def asvs_level(config: dict, intake: dict | None, spec_classification: str | None) -> int:
    """The highest of the tier's level and the levels for the intake and builder-spec classifications.

    With no intake the level is the configured `missing_intake_asvs_level`: not knowing never lowers it.
    """
    if intake is None:
        candidates = [config["missing_intake_asvs_level"]]
    else:
        candidates = [config["tier_asvs_level"][intake["tier"]], config["classification_asvs_level"][intake["data_classification"]]]
    if spec_classification is not None:
        candidates.append(config["classification_asvs_level"][spec_classification])
    return max(candidates)


def floor(config: dict, intake: dict | None) -> str:
    """The level from intake alone. A missing intake record is trigger T-8, not a floor."""
    if intake is None:
        return "standard"
    if intake["tier"] not in config["tier_floor"]:
        raise InputError(f"intake tier {intake['tier']!r} has no floor in the config")
    return config["tier_floor"][intake["tier"]]

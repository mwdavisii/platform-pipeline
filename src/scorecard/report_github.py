"""What the report job does on the PR, as data. These are conveniences: the authoritative level is the factory run's `level` check."""

from .levels import LEVELS

LABEL_PREFIX = "scrutiny:"


def github_actions(level: str, config: dict) -> dict:
    """The label to set, the stale labels to remove, and the teams to request review from."""
    return {
        "label": LABEL_PREFIX + level,
        "stale_labels": [LABEL_PREFIX + other for other in LEVELS if other != level],
        "review_teams": list(config["reviewers"].get(level, [])),
    }

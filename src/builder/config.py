"""Platform config (editable per cluster) and digest-pinned base images."""

import fnmatch
import hashlib
import json
import os
from importlib import resources
from pathlib import Path


def _data(name: str) -> str:
    return resources.files("builder").joinpath("data", name).read_text(encoding="utf-8")


def platform_config_text() -> str:
    override = os.environ.get("BUILDER_PLATFORM_CONFIG")
    return Path(override).read_text(encoding="utf-8") if override else _data("platform.json")


def load_platform() -> dict:
    return json.loads(platform_config_text())


def gate2_single_approver(platform: dict) -> bool:
    """The PoC-only switch that lets the person who triggered a promotion approve it. Missing means false; anything but a boolean is an error."""
    value = platform.get("gate2_single_approver", False)
    if type(value) is not bool:
        raise ValueError("gate2_single_approver in the platform config must be true or false")
    return value


SCORECARD_MODES = ("per-app", "central")


def scorecard_mode(platform: dict) -> str:
    """per-app: each app calls the factory's reusable workflow; central: the factory's poller assesses (central review). Missing means per-app."""
    value = platform.get("scorecard_mode", "per-app")
    if value not in SCORECARD_MODES:
        raise ValueError("scorecard_mode in the platform config must be per-app or central")
    return value


def scorecard_app_id(platform: dict) -> int | None:
    """The scorecard GitHub App's id in central mode (the ruleset pins `scorecard / assessed` to it); None in per-app mode."""
    if scorecard_mode(platform) != "central":
        return None
    value = platform.get("scorecard_app_id")
    if type(value) is not int or not 0 < value < 2**31:
        raise ValueError("scorecard_app_id in the platform config must be the scorecard App's id in central mode")
    return value


def platform_sha256() -> str:
    return hashlib.sha256(platform_config_text().encode("utf-8")).hexdigest()


def load_base_images() -> dict:
    return json.loads(_data("base_images.json"))


def matches_any(host: str, patterns: list[str]) -> bool:
    """Case-sensitive glob match; spec hosts are schema-constrained to lowercase."""
    return any(fnmatch.fnmatchcase(host, pattern) for pattern in patterns)


def is_model_provider(host: str, platform: dict) -> bool:
    """True for a model provider host other than the platform gateway."""
    if host in platform["model_gateway_hosts"]:
        return False
    return matches_any(host, platform["model_provider_hosts"])


def is_denied_egress(host: str, platform: dict) -> bool:
    """True for hosts that must never be reachable, such as cloud metadata endpoints."""
    return matches_any(host, platform["denied_egress_hosts"])

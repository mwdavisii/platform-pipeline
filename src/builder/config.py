"""Platform config (editable per cluster) and digest-pinned base images."""

import fnmatch
import hashlib
import json
import os
import re
from importlib import resources
from pathlib import Path

from contracts.classification import POLICY_PLACEHOLDER, policy_link_ok


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


def classification_policy_url(platform: dict) -> str:
    """The data classification policy's link from the platform config (configure.py --classification-policy-url). Missing means the
    placeholder; anything but a valid link (contracts.classification.policy_link_ok) is an error."""
    value = platform.get("classification_policy_url", POLICY_PLACEHOLDER)
    if not policy_link_ok(value):
        raise ValueError("classification_policy_url in the platform config must be an https link (see contracts.classification)")
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


GATEWAY = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+")
SKILLS_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")


def mcp_gateways(platform: dict) -> dict:
    """{"test": host, "prod": host}: the two shared Cloudflare MCP gateways (configure.py --mcp-gateway-test/--mcp-gateway-prod), or {}
    when the platform names none (then no binding may name MCP servers). Each environment has its own gateway, so the two differ."""
    value = platform.get("mcp_gateways", {})
    if (not isinstance(value, dict) or set(value) - {"test", "prod"}
            or not all(isinstance(h, str) and len(h) <= 253 and GATEWAY.fullmatch(h) for h in value.values())):
        raise ValueError("mcp_gateways in the platform config must map test and prod to host names")
    if "test" in value and value.get("test") == value.get("prod"):
        raise ValueError("mcp_gateways in the platform config must name a different host for test and prod")
    return dict(value)


def skills_repo(platform: dict) -> str | None:
    """OWNER/NAME of the platform's reviewed skills catalog (configure.py --skills-repo), or None when the platform names none."""
    value = platform.get("skills_repo")
    if value is not None and not (isinstance(value, str) and SKILLS_REPO.fullmatch(value)):
        raise ValueError("skills_repo in the platform config must be OWNER/NAME")
    return value


IMAGE_REF = re.compile(r"[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}")  # golden_road.render.IMAGE_REF
AGENT_UID = 10001  # the agent runtime image's user (images/agent-runtime/Dockerfile; a test holds them equal)
MODEL_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")  # agent/v1's model
MAX_AGENT_MODELS = 20


def agent_runtime_image(platform: dict) -> str | None:
    """The agent runtime's image (images.agent.runtime: a registry reference pinned by digest, configure.py --agent-runtime-image), or
    None when the platform names none (then no agent app can be built). images may name nothing else."""
    images = platform.get("images", {})
    if not isinstance(images, dict) or set(images) - {"agent"}:
        raise ValueError("images in the platform config may name only agent")
    agent = images.get("agent")
    if agent is None:
        return None
    if (not isinstance(agent, dict) or set(agent) != {"runtime"} or not isinstance(agent["runtime"], str)
            or not IMAGE_REF.fullmatch(agent["runtime"])):
        raise ValueError("images.agent.runtime in the platform config must be a registry reference pinned by sha256 digest")
    return agent["runtime"]


def agent_models(platform: dict) -> list[dict]:
    """[{id, label}]: the models an agent may name (agent.yaml's model). Missing means none."""
    value = platform.get("agent_models", [])
    ok = isinstance(value, list) and len(value) <= MAX_AGENT_MODELS and all(
        isinstance(m, dict) and set(m) == {"id", "label"} and isinstance(m["id"], str) and MODEL_ID.fullmatch(m["id"])
        and isinstance(m["label"], str) and 1 <= len(m["label"]) <= 80 and m["label"].isprintable() for m in value)
    if not ok or len({m["id"] for m in value}) != len(value):
        raise ValueError(f"agent_models in the platform config must list at most {MAX_AGENT_MODELS} {{id, label}} models with distinct ids")
    return [dict(m) for m in value]


def agent_gateway_url(platform: dict) -> str:
    """AGENT_GATEWAY_URL: the platform gateway's OpenAI-compatible base, https on the first model gateway host (decided item 9)."""
    hosts = platform.get("model_gateway_hosts")
    if not (isinstance(hosts, list) and hosts and isinstance(hosts[0], str) and len(hosts[0]) <= 253 and GATEWAY.fullmatch(hosts[0])):
        raise ValueError("model_gateway_hosts in the platform config must name the model gateway's host first (an agent's model calls go there)")
    return f"https://{hosts[0]}/v1"


def images_for(platform: dict) -> dict:
    """The digest-pinned base images, and the agent runtime when the platform names one (its user is the runtime image's)."""
    images = load_base_images()
    runtime = agent_runtime_image(platform)
    if runtime is not None:
        images["agent"] = {"runtime": runtime, "uid": AGENT_UID}
    return images


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

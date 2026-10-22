"""Read the factory's trusted inputs: config/scorecard.yaml, intake/, and na/.

They come from the platform-owned factory repo, never from the app repo. Anything malformed is an
InputError: the scorecard fails closed rather than scoring with a guessed config.
"""

import os
import re
from pathlib import Path

import yaml

from contracts.schema import validate
from golden_road.checks import load_required, validate_required
from golden_road.io import InputError

from .agent.prompt import PROMPTS

MAX_YAML_BYTES = 1_000_000
# Runner credentials that must never reach the review agent's process (SEC-2), so they can't name the gateway credential either.
RUNNER_TOKENS = ("GITHUB_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "ACTIONS_RUNTIME_TOKEN",
                 "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_CACHE_URL", "ACTIONS_RESULTS_URL")
RUNNER_PREFIXES = ("GITHUB_", "ACTIONS_", "RUNNER_")
# Prefixes of GitHub-issued tokens (installation, personal, OAuth, user-to-server, refresh, fine-grained): never a gateway credential.
GITHUB_TOKEN_PREFIXES = ("ghs_", "ghp_", "gho_", "ghu_", "ghr_", "github_pat_")


def runner_env(environ) -> list[str]:
    """Names of the runner's variables in `environ`: the exact names in RUNNER_TOKENS and anything starting with GITHUB_, ACTIONS_ or RUNNER_."""
    return sorted(name for name in environ if name in RUNNER_TOKENS or name.startswith(RUNNER_PREFIXES))
# Headers the gateway client sets itself; the credential header can't replace them.
RESERVED_HEADERS = {"host", "content-type", "content-length", "transfer-encoding", "connection", "accept",
                    "cf-access-client-id", "cf-access-client-secret"}  # the Access service-token headers are set from their own variables
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _strings(key)
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


class _Loader(yaml.SafeLoader):
    """SafeLoader without implicit timestamps, so `2026-10-01` stays the string the schemas expect."""


def _construct_mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)
        try:
            duplicate = key in seen
            seen.add(key)
        except TypeError:
            continue  # an unhashable key is rejected by the standard constructor below
        if duplicate:
            raise InputError(f"duplicate key {key!r} on line {key_node.start_mark.line + 1}")
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_Loader.construct_mapping = _construct_mapping
_Loader.yaml_implicit_resolvers = {
    key: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def no_symlink(path) -> None:
    """Refuse a symlink: factory records are regular files and directories, so a link can't point outside the factory."""
    if Path(path).is_symlink():
        raise InputError(f"{path} is a symlink")


def load_yaml(path):
    path = Path(path)
    try:
        no_symlink(path)
        if path.stat().st_size > MAX_YAML_BYTES:
            raise InputError(f"{path} is larger than {MAX_YAML_BYTES} bytes")
        return yaml.load(path.read_text(encoding="utf-8"), Loader=_Loader)  # noqa: S506 - _Loader is a SafeLoader
    except InputError:
        raise
    except (OSError, UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:
        raise InputError(f"can't read {path}: {type(exc).__name__}") from exc


def _checked(path, schema: str):
    doc = load_yaml(path)
    errors = validate(schema, doc)
    if errors:
        raise InputError(f"{path} is not a valid {schema}: {errors[0]}")
    return doc


def _is_file(path: Path) -> bool:
    """True when the record exists. A dangling or unreadable path is an error, not a missing record."""
    try:
        return path.is_symlink() or path.is_file()
    except OSError as exc:
        raise InputError(f"can't read {path}: {type(exc).__name__}") from exc


def repo_parts(repo: str) -> tuple[str, str]:
    if not isinstance(repo, str) or not _REPO.match(repo):
        raise InputError("repo must be owner/name")
    owner, name = repo.split("/")
    if {owner, name} & {".", ".."}:
        raise InputError("repo must be owner/name")
    return owner, name


def load_config(factory) -> dict:
    path = Path(factory) / "config" / "scorecard.yaml"
    config = _checked(path, "scorecard-config.v1")
    if config["drift_check"] not in {c.get("name") for c in config["gate1_checks"]}:
        raise InputError(f"{path} drift_check {config['drift_check']!r} is not one of the gate1_checks names")
    validate_required(config["gate1_checks"], f"{path} gate1_checks")
    for key, patterns in config["patterns"].items():
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise InputError(f"{path} patterns.{key} has an invalid regex {pattern!r}: {exc}") from exc
    agent = config["agent"]
    if agent["prompt_version"] not in PROMPTS:
        raise InputError(f"{path} agent.prompt_version {agent['prompt_version']!r} is not a released prompt")
    for text in _strings(agent):
        if "\n" in text or "\r" in text:  # the schema's "$" accepts a trailing newline, so refuse line breaks outright
            raise InputError(f"{path} agent values can't contain line breaks")
    credential = agent["gateway"]["credential_env"]
    if credential in RUNNER_TOKENS or credential.startswith(RUNNER_PREFIXES):
        raise InputError(f"{path} agent.gateway.credential_env can't be a runner token")
    if agent["gateway"]["auth"].get("header", "").lower() in RESERVED_HEADERS:
        raise InputError(f"{path} agent.gateway.auth.header can't replace a header the client sets")
    return config


PREVIEW_CHECK = "golden-road / preview"


def _source(entry: dict) -> tuple:
    """(what the check is, who must report it): the same name from another app or creator is a different check that can't be met."""
    if "context" in entry:
        return ("status", entry["context"]), f"creator {entry['creator']}"
    return ("run", entry["name"]), f"app {entry['app_id']}"


def gate1_list(factory, config: dict | None = None) -> list[dict]:
    """The factory's Gate 1 list, plus every entry of the platform's required-checks list baked into the builder image
    (GOLDEN_ROAD_REQUIRED_CHECKS), so a scan added there is never skipped. The factory's order first, then the baked entries it
    lacks. A list without the preview check is refused (Gate 1 is not Gate 1 without it), and so is a check name that appears
    with two different sources: one of them can never pass, so every PR would wait out Gate 1 and come out "not assessed"."""
    config = config or load_config(factory)
    checks = [(entry, "the factory config") for entry in config["gate1_checks"]]
    if PREVIEW_CHECK not in {c.get("name") for c, _ in checks}:
        raise InputError(f"gate1_checks must include {PREVIEW_CHECK!r}")
    baked = os.environ.get("GOLDEN_ROAD_REQUIRED_CHECKS")
    if baked:
        checks += [(entry, "the builder image's required checks") for entry in load_required(baked) if entry not in config["gate1_checks"]]
    seen: dict[tuple, tuple[str, str]] = {}
    for entry, origin in checks:
        key, source = _source(entry)
        if key in seen and seen[key][0] != source:
            raise InputError(f"gate 1 check {key[1]!r} is listed with two sources: {seen[key][0]} in {seen[key][1]} and {source} in {origin}; "
                             "only one can report it, so Gate 1 could never pass")
        seen.setdefault(key, (source, origin))
    return validate_required([entry for entry, _ in checks], "gate1_checks")


def load_intake(factory, repo: str) -> dict | None:
    """The intake record, or None when the repo has none (trigger T-8)."""
    owner, name = repo_parts(repo)
    path = Path(factory) / "intake" / owner / f"{name}.yaml"
    if not _is_file(path):
        return None
    intake = _checked(path, "intake.v1")
    if intake["repo"] != repo:
        raise InputError(f"{path} is for {intake['repo']}, not {repo}")
    return intake


def load_confirmed_na(factory, repo: str) -> list[dict]:
    owner, name = repo_parts(repo)
    path = Path(factory) / "na" / owner / f"{name}.yaml"
    if not _is_file(path):
        return []
    doc = _checked(path, "na.v1")
    if doc["repo"] != repo:
        raise InputError(f"{path} is for {doc['repo']}, not {repo}")
    return doc["entries"]

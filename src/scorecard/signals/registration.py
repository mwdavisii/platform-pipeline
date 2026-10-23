"""T-21's input: the registration floor computed from the checked-out merge tree.

This is the same computation as the portal's collector (bootstrap/golden-road-portal/portal/collectors/evidence.py and floor.py),
from the repository's stored blobs instead of the GitHub API: the same files, the same size caps, the same signal rules, the same no-spec /
unreadable / invalid-spec behavior, and the factory's rules (config/registration.yaml). The two trees can't share code (the portal
must not import the scorecard), so tests/pipeline/test_portal_scorecard_floor_parity.py runs both on the same fixture trees and
requires the same signal rules and floor. Change one, change the other.

Nothing here reads the PR text, and nothing reads the working tree: a pull request's .gitattributes can re-encode a checked-out file, so
every file is read as the blob stored at the commit (symlinks and submodules are unreadable, as in the portal).
"""

import json
import re

from contracts.schema import validate
from golden_road.io import InputError

from .. import git
from ..config import CLASSIFICATION_ORDER, TIER_ORDER

# File caps in bytes, as in the portal's collector.
CAPS = {"builder-spec.json": 65536, ".builder.lock": 65536, "requirements.txt": 262144, "package-lock.json": 10485760}
MAX_ITEMS = 100
MAX_NAME = 200
LOCKFILES = {"python": ("requirements.txt",), "node": ("package-lock.json",), "static": ()}
PINNED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?==\S+")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
SHOWN = 10
MAX_LINE = 300
RAISES = ("write_roles", "secret_env", "model_gateway_egress", "other_egress", "many_dependencies")

UNREADABLE = object()


def _read(root, commit, name, cap):
    """The file's stored bytes at the commit (not the working tree), None when it isn't there, UNREADABLE when it is there but can't be
    used (a symlink or submodule, not a plain file, over the cap). A git failure is an InputError for the caller (T-14)."""
    try:
        return git.stored_file(root, commit, name, cap)
    except git.Unreadable:
        return UNREADABLE


def _list(values) -> str:
    values = list(values)
    return ", ".join(values[:SHOWN]) + (f" and {len(values) - SHOWN} more" if len(values) > SHOWN else "")


def _signal(rule, line) -> dict:
    return {"rule": rule, "line": line[:MAX_LINE]}


def _unreadable(name) -> dict:
    return _signal(f"unreadable:{name}", f"{name} could not be read, or is larger than {CAPS[name]} bytes.")


def _json(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return UNREADABLE


def _spec(root, commit, signals):
    raw = _read(root, commit, "builder-spec.json", CAPS["builder-spec.json"])
    if raw is None:
        signals.append(_signal("no_spec", "No builder-spec.json in the repository at this commit."))
        return None
    if raw is UNREADABLE:
        signals.append(_unreadable("builder-spec.json"))
        return None
    doc = _json(raw)
    if doc is UNREADABLE or validate("builder-spec.v1", doc):
        signals.append(_signal("no_spec", "builder-spec.json is not valid builder-spec/v1."))
        return None
    authz = doc.get("authz") or {}
    lists = [doc.get("egress", []), doc.get("env", []), authz.get("roles", []), authz.get("rules", [])]
    if any(len(items) > MAX_ITEMS for items in lists):
        signals.append(_unreadable("builder-spec.json"))
        return None
    copied = [e["name"] for e in doc["env"] if e.get("secret") is True] + [r["name"] for r in authz.get("roles", []) if r["access"] == "write"]
    if any(len(name) > MAX_NAME for name in copied):
        signals.append(_signal("no_spec", "builder-spec.json is not valid builder-spec/v1."))
        return None
    return doc


def _count(name, raw) -> int | None:
    if name == "requirements.txt":
        return sum(1 for line in raw.decode("utf-8", "replace").splitlines() if PINNED.match(line))
    doc = _json(raw)
    if not isinstance(doc, dict):
        return None
    if isinstance(doc.get("packages"), dict):
        return sum(1 for key in doc["packages"] if key != "")
    if isinstance(doc.get("dependencies"), dict):
        return len(doc["dependencies"])
    return None


def _dependencies(root, commit, spec, signals):
    names = LOCKFILES[spec["app_type"]] if spec else ("requirements.txt", "package-lock.json")
    for name in names:
        raw = _read(root, commit, name, CAPS[name])
        if raw is None:
            continue
        count = None if raw is UNREADABLE else _count(name, raw)
        if count is None:
            signals.append(_unreadable(name))
            return {"count": 0, "lockfile_present": True, "lockfile": name}
        return {"count": count, "lockfile_present": True, "lockfile": name}
    return {"count": 0, "lockfile_present": False, "lockfile": None}


def _builder_version(root, commit, signals):
    raw = _read(root, commit, ".builder.lock", CAPS[".builder.lock"])
    if raw is None:
        return None
    doc = _json(raw) if raw is not UNREADABLE else UNREADABLE
    if not isinstance(doc, dict):
        signals.append(_unreadable(".builder.lock"))
        return None
    version = doc.get("builder_version")
    return version if isinstance(version, str) and VERSION.fullmatch(version) else None


def compute_evidence(root, *, repo: str, commit: str, gateway_hosts, many_dependencies_over: int) -> dict:
    """registration-evidence/v1 for the tree of `commit` in the repository at `root`. Raises InputError when the result isn't a valid document."""
    signals: list[dict] = []
    spec = _spec(root, commit, signals)
    version = _builder_version(root, commit, signals)
    dependencies = _dependencies(root, commit, spec, signals)
    doc = {"schema": "registration-evidence/v1", "repo": repo, "commit": commit, "spec_present": spec is not None,
           "data_classification": None, "app_type": None, "resources_tier": None, "egress": [], "secret_env": [], "write_roles": [],
           "write_routes": 0, "builder_version": version, "dependencies": dependencies, "signals": signals}
    if spec is not None:
        authz = spec.get("authz") or {}
        doc.update(data_classification=spec["data_classification"], app_type=spec["app_type"], resources_tier=spec["resources_tier"],
                   egress=[{"host": e["host"], "port": e["port"], "model_gateway": e["host"] in gateway_hosts} for e in spec["egress"]],
                   secret_env=[e["name"] for e in spec["env"] if e.get("secret") is True],
                   write_roles=[r["name"] for r in authz.get("roles", []) if r["access"] == "write"],
                   write_routes=sum(1 for rule in authz.get("rules", []) if any(m != "GET" for m in rule["methods"])))
        fired = []
        if doc["write_roles"]:
            fired.append(_signal("write_roles", f"Write roles: {_list(doc['write_roles'])}."))
        if doc["secret_env"]:
            fired.append(_signal("secret_env", f"Secret settings: {_list(doc['secret_env'])}."))
        gateway = [f"{e['host']}:{e['port']}" for e in doc["egress"] if e["model_gateway"]]
        other = [f"{e['host']}:{e['port']}" for e in doc["egress"] if not e["model_gateway"]]
        if gateway:
            fired.append(_signal("model_gateway_egress", f"Calls the model gateway: {_list(gateway)}."))
        if other:
            fired.append(_signal("other_egress", f"Other egress: {_list(other)}."))
        signals.extend(fired)
    if dependencies["count"] > many_dependencies_over:
        signals.append(_signal("many_dependencies", f"{dependencies['count']} pinned dependencies (more than {many_dependencies_over})."))
    problems = validate("registration-evidence.v1", doc)
    if problems:
        raise InputError(f"the computed registration evidence is not valid: {problems[0]}")
    return doc


def _rank(order, value) -> int:
    return order.index(value) if value in order else -1


def compute_floor(evidence: dict, rules: dict) -> tuple[str, str]:
    """(floor tier, floor classification), as the portal's floor.compute_floor."""
    fired = {s["rule"] for s in evidence["signals"]}
    spec = evidence["spec_present"]
    no_spec = not spec or any(r.startswith("unreadable:") or r == "no_spec" for r in fired)
    if spec:
        cls = evidence["data_classification"] if evidence["data_classification"] in CLASSIFICATION_ORDER else CLASSIFICATION_ORDER[-1]
        tiers = [rules["classification_tier"][cls]]
    else:
        cls, tiers = rules["no_spec"]["data_classification"], []
    tiers += [rules["raise_to"][r] for r in RAISES if r in fired]
    if no_spec:
        cls = max(cls, rules["no_spec"]["data_classification"], key=lambda c: _rank(CLASSIFICATION_ORDER, c))
        tiers += [rules["no_spec"]["tier"], rules["classification_tier"][cls]]
    return max(tiers, key=lambda t: _rank(TIER_ORDER, t)), cls


def below_floor(tier: str, classification: str, floor_tier: str, floor_classification: str) -> bool:
    """True when the registered tier or classification is below the floor. A value the orderings don't know is below (unknown is lowest)."""
    return (_rank(TIER_ORDER, tier) < _rank(TIER_ORDER, floor_tier)
            or _rank(CLASSIFICATION_ORDER, classification) < _rank(CLASSIFICATION_ORDER, floor_classification))


def registration_floor(root, rules: dict, *, repo: str, commit: str, gateway_hosts, problems: list) -> dict | None:
    """{tier, data_classification, rules: [fired rule names]} for the tree, or None when it can't be computed (an input problem is added)."""
    try:
        evidence = compute_evidence(root, repo=repo, commit=commit, gateway_hosts=gateway_hosts, many_dependencies_over=rules["many_dependencies_over"])
    except InputError as exc:
        problems.append({"source": "registration floor", "detail": str(exc)[:300]})
        return None
    tier, classification = compute_floor(evidence, rules)
    return {"tier": tier, "data_classification": classification, "rules": [s["rule"] for s in evidence["signals"]]}

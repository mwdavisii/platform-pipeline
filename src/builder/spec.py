"""Validate a builder spec against the schema and the platform rules.

Returns (errors, questions, warnings). Errors mean the spec breaks a rule
(exit 2). Questions mean a human has to decide something (exit 3).
"""

import re
from pathlib import Path

from contracts.schema import validate as schema_validate

from .config import is_denied_egress, is_model_provider
from .detect import JS_SUFFIXES, MUTATING, PY_SUFFIXES, find_routes, node_reads_port
from .source import Tree

MODULE_ATTR = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")
STATIC_PORT = 8080  # the unprivileged nginx port


def check_spec(spec: dict, root: Path, platform: dict) -> tuple[list[str], list[str], list[str]]:
    errors = schema_validate("builder-spec.v1", spec)
    if errors:
        return errors, [], []
    tree = Tree(root)
    # Beyond-schema checks. A new check here needs a rule in guide.FIELDS (tests/builder/test_guide.py lists the fields).
    errors = _entrypoint_errors(spec, tree) + _authz_errors(spec) + _egress_errors(spec, platform) + _env_errors(spec)
    if spec["app_type"] == "static" and spec["port"] != STATIC_PORT:
        errors.append(f"static sites must use port {STATIC_PORT} (the unprivileged nginx port)")
    if errors:
        return errors, [], []

    app_type = spec["app_type"]
    questions = _lockfile_questions(spec, tree)
    if app_type in ("python", "node") and not spec.get("authz"):
        writes = [r for r in find_routes(tree, app_type) if r["method"] in MUTATING]
        if writes:
            listed = ", ".join(f"{r['method']} {r['path']}" for r in writes[:5])
            questions.append(f"This app has write endpoints ({listed}). Who may call them? Add an authz block with roles and rules.")
    if app_type in ("python", "node"):
        # A file we couldn't read could hide routes, so it needs a human, not a pass.
        questions += tree.scan_problems(PY_SUFFIXES if app_type == "python" else JS_SUFFIXES)
    if app_type == "node" and not node_reads_port(tree):
        questions.append("The server must listen on process.env.PORT. Update it to use process.env.PORT.")

    warnings = []
    if app_type != "static" and "health_path" not in spec:
        warnings.append("No health_path set; using a TCP probe. Add a /healthz route and set health_path for a real health check.")
    return [], questions, warnings


def _entrypoint_errors(spec: dict, tree: Tree) -> list[str]:
    root = tree.root
    app_type, entry = spec["app_type"], spec["entrypoint"]
    errors = []
    if app_type != "python" and "server" in spec:
        errors.append("server only applies to python apps")
    is_module = app_type == "python" and MODULE_ATTR.match(entry)
    target = root / entry.split(":")[0].replace(".", "/") if is_module else root / entry
    # A module:attr entrypoint loads <target>.py or <target>/__init__.py, and importing it runs
    # every parent package's __init__.py, so all of those must really live inside the root.
    to_check = [target]
    if is_module:
        parts = entry.split(":")[0].split(".")
        to_check += [target.with_suffix(".py"), target / "__init__.py"]
        to_check += [root.joinpath(*parts[:i], "__init__.py") for i in range(1, len(parts))]
    if not all(tree.contains(p) for p in to_check):
        return errors + [f"entrypoint {entry} is outside the app directory"]

    if app_type == "python":
        if is_module:
            if "server" not in spec:
                errors.append("a module:attr entrypoint needs server: uvicorn or gunicorn")
            if not (target.with_suffix(".py").is_file() or (target / "__init__.py").is_file()):
                errors.append(f"entrypoint module {entry.split(':')[0]} not found")
        elif entry.endswith(".py"):
            if "server" in spec:
                errors.append("server only applies to a module:attr entrypoint")
            if spec.get("authz"):
                errors.append("authz needs a module:attr entrypoint so the platform can wrap the app")
            if not target.is_file():
                errors.append(f"entrypoint file {entry} not found")
        else:
            errors.append("python entrypoint must be module:attr or a .py file")
    elif app_type == "node":
        if not target.is_file():
            errors.append(f"entrypoint file {entry} not found")
    else:
        if spec.get("authz"):
            errors.append("authz does not apply to static sites; protect them at the edge")
        pkg = tree.package_json()
        builds = pkg is not None and "build" in pkg.get("scripts", {})
        if not builds and not target.is_dir():
            errors.append(f"static directory {entry} not found")
    return errors


def _authz_errors(spec: dict) -> list[str]:
    authz = spec.get("authz")
    if not authz:
        return []
    errors = []
    names = [r["name"] for r in authz["roles"]]
    if len(names) != len(set(names)):
        errors.append("authz role names must be unique")
    access = {r["name"]: r["access"] for r in authz["roles"]}
    for i, rule in enumerate(authz["rules"]):
        writes = sorted(MUTATING.intersection(rule["methods"]))
        for role in rule["roles"]:
            if role not in access:
                errors.append(f"authz.rules[{i}] names undeclared role '{role}'")
            elif access[role] == "read" and writes and not rule.get("justification"):
                errors.append(f"authz.rules[{i}] grants {', '.join(writes)} to read-only role '{role}'. Grant writes to a write role, or add a justification.")
    return errors


RESERVED_ENV = {
    "PORT": "PORT is set by the platform from the spec's port field",
    "NODE_ENV": "NODE_ENV is set by the platform image",
    "NODE_OPTIONS": "NODE_OPTIONS changes how code loads and can't be set from config",
    "PATH": "PATH decides which programs run and can't be set from config",
    "HOME": "HOME is fixed by the platform image because the container runs as a non-root user with a read-only filesystem",
}
RESERVED_ENV_PREFIXES = {
    "LD_": "LD_* variables change how libraries load and can't be set from config",
    "PYTHON": "PYTHON* variables change how the interpreter loads code and can't be set from config",
    "GUNICORN_": "GUNICORN_* variables change how the server starts and can't be set from config; the platform sets the server command",
    "UVICORN_": "UVICORN_* variables change how the server starts and can't be set from config; the platform sets the server command",
    "NODE_": "NODE_* variables change how Node loads code and can't be set from config",
    "NPM_": "NPM_* variables change how npm runs and can't be set from config",
}


def _env_errors(spec: dict) -> list[str]:
    errors, seen = [], set()
    for entry in spec["env"]:
        name = entry["name"]
        if name in seen:
            errors.append(f"env name {name} is listed more than once. Each variable can be declared only once.")
        seen.add(name)
        reason = RESERVED_ENV.get(name) or next((why for prefix, why in RESERVED_ENV_PREFIXES.items() if name.startswith(prefix)), None)
        if reason:
            errors.append(f"env name {name} is reserved: {reason}.")
    return errors


def _egress_errors(spec: dict, platform: dict) -> list[str]:
    gateways = ", ".join(platform["model_gateway_hosts"])
    errors = []
    for e in spec["egress"]:
        if is_denied_egress(e["host"], platform):
            errors.append(f"egress to {e['host']} is not allowed: cloud metadata endpoints expose node credentials")
        elif is_model_provider(e["host"], platform):
            errors.append(f"egress to model provider {e['host']} is not allowed. Call models through the platform gateway: {gateways}")
    return errors


def _lockfile_questions(spec: dict, tree: Tree) -> list[str]:
    app_type, root = spec["app_type"], tree.root
    if app_type == "python":
        req = root / "requirements.txt"
        text = tree.read_text(req)
        if text is None:
            if req.exists() or req.is_symlink():
                return ["requirements.txt can't be read (it is too large, binary, or outside the app directory). Replace it with a regular hashed requirements file."]
            if (root / "uv.lock").is_file():
                return ["No requirements.txt. Run: uv export --format requirements-txt --output-file requirements.txt"]
            if (root / "poetry.lock").is_file():
                return ["No requirements.txt. Run: poetry export --format requirements.txt --output requirements.txt (hashes are included by default)"]
            return ["No locked dependencies. Create requirements.txt with hashes, for example: uv pip compile --generate-hashes requirements.in -o requirements.txt"]
        questions = []
        if not re.search(r"^[^#\n]*--hash=", text, re.M):
            questions.append("requirements.txt has no hashes. Regenerate it with: uv pip compile --generate-hashes requirements.in -o requirements.txt")
        server = spec.get("server")
        if server and not re.search(rf"^{server}(\[[^\]]*\])?==", text, re.M | re.I):
            questions.append(f"{server} is not pinned in requirements.txt. Add it to requirements.in and re-lock.")
        old = re.search(r"^gunicorn(?:\[[^\]]*\])?==(\d+)", text, re.M | re.I)
        if server == "gunicorn" and old and int(old.group(1)) < 22:
            questions.append("gunicorn is pinned below 22, which can't refuse ambiguous identity headers (--header-map). Upgrade to gunicorn 22 or newer and re-lock.")
        return questions
    has_package = (root / "package.json").exists()
    if app_type == "node" or has_package:
        if tree.package_json() is None:
            return ["package.json is missing or is not a valid JSON object. Node apps need package.json and package-lock.json (run: npm install --package-lock-only)."]
        lock = tree.json_object("package-lock.json")
        if lock is None or "lockfileVersion" not in lock:
            return ["package-lock.json is missing or invalid. Run: npm install --package-lock-only (pnpm and yarn lockfiles are not supported in v0)."]
    return []

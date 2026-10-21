"""Inspect a source tree and draft a builder spec. Never writes files."""

import bisect
import re
from pathlib import Path

from .config import is_model_provider, load_platform
from .source import Tree, as_tree, package_json, source_files  # noqa: F401  (re-exported for later tasks)

PY_SUFFIXES = {".py"}
JS_SUFFIXES = {".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx", ".mts", ".cts"}
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
HEALTH_PATHS = ("/healthz", "/health")
FLASK_WINDOW = 500

_PY_ROUTE = re.compile(r"@\w+\.(get|post|put|patch|delete)\(\s*[\"']([^\"']+)[\"']")
_FLASK_ROUTE = re.compile(r"@\w+\.route\(\s*[\"']([^\"']+)[\"']")
_FLASK_DECORATOR_END = re.compile(r"\n[ \t]*(?:@|async\s+def\b|def\b|class\b)")
_FLASK_METHODS = re.compile(r"methods\s*=\s*[\[(]([^\])]*)[\])]")
_JS_ROUTE = re.compile(r"\b\w+\.(get|post|put|patch|delete)\(\s*[\"'`](/[^\"'`]*)[\"'`]")
_URL = re.compile(r"https?://([A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,})")
_PY_ENV = re.compile(r"os\.(?:environ\.get|getenv)\(\s*[\"']([A-Z_][A-Z0-9_]*)[\"']|os\.environ\[\s*[\"']([A-Z_][A-Z0-9_]*)[\"']\s*\]")
_JS_ENV = re.compile(r"process\.env\.([A-Z_][A-Z0-9_]*)")
_FASTAPI = re.compile(r"^(\w+)\s*=\s*FastAPI\(", re.M)
_FLASK = re.compile(r"^(\w+)\s*=\s*Flask\(", re.M)
_NODE_START = re.compile(r"\bnode\s+([\w./-]+\.(?:js|mjs|cjs))")
_SECRET_HINT = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|DSN|URL|CONN")
_NODE_SERVERS = {"express", "fastify", "koa", "@hapi/hapi", "hono", "@nestjs/core"}
_PLATFORM_ENV = {"PORT", "NODE_ENV"}


def _newline_offsets(text: str) -> list[int]:
    return [m.start() for m in re.finditer("\n", text)]


def _flask_methods(text: str, end: int) -> list[str]:
    window = text[end:end + FLASK_WINDOW]
    stop = _FLASK_DECORATOR_END.search(window)
    if stop:
        window = window[:stop.start()]
    listed = _FLASK_METHODS.search(window)
    return re.findall(r"[\"'](\w+)[\"']", listed.group(1)) if listed else ["GET"]


def find_routes(root, app_type: str | None) -> list[dict]:
    tree = as_tree(root)
    routes = []

    def scan(suffixes, patterns):
        for file in tree.files(suffixes):
            text = tree.read_text(file)
            offsets = _newline_offsets(text)
            rel = file.relative_to(tree.root).as_posix()
            for pattern, emit in patterns:
                for m in pattern.finditer(text):
                    line = bisect.bisect_left(offsets, m.start()) + 1
                    for method, path in emit(m, text):
                        routes.append({"method": method.upper(), "path": path, "file": rel, "line": line})

    def method_path(m, text):
        yield m.group(1), m.group(2)

    def flask(m, text):
        for method in _flask_methods(text, m.end()):
            yield method, m.group(1)

    if app_type == "python":
        scan(PY_SUFFIXES, [(_PY_ROUTE, method_path), (_FLASK_ROUTE, flask)])
    elif app_type == "node":
        scan(JS_SUFFIXES, [(_JS_ROUTE, method_path)])
    return sorted(routes, key=lambda r: (r["file"], r["line"], r["method"]))


def node_reads_port(root) -> bool:
    tree = as_tree(root)
    return any("PORT" in _JS_ENV.findall(tree.read_text(f)) for f in tree.files(JS_SUFFIXES))


def detect_app_type(root) -> tuple[str | None, list[str]]:
    tree = as_tree(root)
    root = tree.root
    has_py = bool(tree.files(PY_SUFFIXES)) or (root / "requirements.txt").is_file() or (root / "pyproject.toml").is_file()
    pkg = tree.package_json()
    if pkg is None:
        if has_py:
            return "python", []
        if (root / "index.html").is_file():
            return "static", []
        return None, ["Couldn't tell the app type. Is this a python, node, or static app?"]
    if has_py:
        return None, ["Found both Python and Node files. Which is the app: python or node?"]
    scripts = pkg.get("scripts", {})
    if _NODE_SERVERS.intersection(pkg.get("dependencies", {})) or "start" in scripts or pkg.get("main"):
        return "node", []
    if "build" in scripts or (root / "index.html").is_file():
        return "static", []
    return None, ["Couldn't tell the app type. Is this a node server or a static site?"]


def detect_entrypoint(root, app_type: str | None) -> tuple[dict, list[str], list[str]]:
    """Return (spec fields, warnings, questions)."""
    tree = as_tree(root)
    root = tree.root
    if app_type == "python":
        for file in tree.files(PY_SUFFIXES):
            text = tree.read_text(file)
            module = file.relative_to(root).with_suffix("").as_posix().replace("/", ".")
            if m := _FASTAPI.search(text):
                return {"entrypoint": f"{module}:{m.group(1)}", "server": "uvicorn"}, [], []
            if m := _FLASK.search(text):
                return {"entrypoint": f"{module}:{m.group(1)}", "server": "gunicorn"}, [], []
        for name in ("main.py", "app.py"):
            if (root / name).is_file():
                return {"entrypoint": name}, [], []
        return {}, [], ["Which file starts the app? Set entrypoint to module:attr or a .py file."]
    if app_type == "node":
        pkg = tree.package_json() or {}

        def usable(name):
            return (root / name).is_file() and tree.contains(root / name)

        main = pkg.get("main")
        if main and usable(main):
            return {"entrypoint": main}, [], []
        start = pkg.get("scripts", {}).get("start")
        m = _NODE_START.search(start) if isinstance(start, str) else None
        if m and usable(m.group(1)):
            return {"entrypoint": m.group(1)}, [], []
        for name in ("server.js", "index.js", "app.js"):
            if usable(name):
                return {"entrypoint": name}, [], []
        return {}, [], ["Which file starts the server? Set entrypoint to that file."]
    if app_type == "static":
        pkg = tree.package_json()
        if pkg is not None and "build" in pkg.get("scripts", {}):
            return {"entrypoint": "dist"}, ["Assumed the build writes to dist/. Confirm the build output directory."], []
        return {"entrypoint": "."}, [], []
    return {}, [], []


def _app_name(root: Path) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", root.name.lower()).strip("-")[:42].strip("-")
    if not name or not name[0].isalpha():
        name = f"app-{name}".strip("-")[:42].strip("-")
    return name


def detect(root: Path) -> dict:
    platform = load_platform()
    tree = Tree(root)
    root = tree.root
    app_type, questions = detect_app_type(tree)
    warnings = []
    draft = {"app_name": _app_name(root), "app_type": app_type, "port": 8080, "egress": [], "env": [], "resources_tier": "default"}
    fields, entry_warnings, entry_questions = detect_entrypoint(tree, app_type)
    draft.update(fields)
    warnings += entry_warnings
    questions += entry_questions

    routes = find_routes(tree, app_type)
    health = [r["path"] for r in routes if r["method"] == "GET" and r["path"] in HEALTH_PATHS]
    if health:
        draft["health_path"] = health[0]

    if app_type in ("python", "node"):
        texts = [tree.read_text(f) for f in tree.files(PY_SUFFIXES | JS_SUFFIXES)]
        hosts = sorted({h.lower() for t in texts for h in _URL.findall(t)})
        py_env = {name for t in texts for pair in _PY_ENV.findall(t) for name in pair if name}
        js_env = {name for t in texts for name in _JS_ENV.findall(t)}
        env_names = sorted((py_env | js_env) - _PLATFORM_ENV)
    else:
        hosts, env_names = [], []
    draft["env"] = [{"name": n, "secret": bool(_SECRET_HINT.search(n))} for n in env_names]

    gateways = ", ".join(platform["model_gateway_hosts"])
    for host in hosts:
        if is_model_provider(host, platform):
            questions.append(f"The app calls {host} directly. Model calls must go through the platform gateway ({gateways}). Point the client at the gateway and add the gateway to egress.")
        else:
            questions.append(f"The app calls {host}. Why? Add it to egress with a reason, or remove the call.")
    writes = [r for r in routes if r["method"] in MUTATING]
    if writes:
        listed = ", ".join(f"{r['method']} {r['path']}" for r in writes[:5])
        questions.append(f"Found write endpoints ({listed}). Which roles exist, and which may write? This becomes the authz block.")
    if app_type == "node" and not node_reads_port(tree):
        questions.append("The server must listen on process.env.PORT.")
    questions.append("What data does this app handle: public, internal, restricted, or phi?")

    return {
        "status": "ok",
        "draft_spec": draft,
        "needs": {"egress_hosts": hosts, "env": env_names, "routes": routes},
        "warnings": warnings + tree.warnings,
        "questions": questions,
    }

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from builder.config import load_platform
from builder.render.authz import render_authz

PLATFORM = load_platform()
TABLE = json.loads((Path(__file__).parent / "authz_cases.json").read_text(encoding="utf-8"))
CASES = [c[:4] for c in TABLE["cases"] if "python" not in (c[4] if len(c) > 4 else [])]


def table_spec(spec):
    spec["authz"] = TABLE["authz"]
    return spec


@pytest.fixture
def authz(tmp_path, notes_spec):
    for rel, text in render_authz(table_spec(notes_spec), PLATFORM).items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("generated_authz", tmp_path / "_platform" / "authz.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_asgi(authz, scope, inner=None):
    sent = []

    async def default_inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "websocket.connect"}

    asyncio.run(authz.AuthzASGI(inner or default_inner)(scope, receive, send))
    return sent


def test_rendered_files(notes_spec):
    files = render_authz(notes_spec, PLATFORM)
    assert sorted(files) == ["_platform/__init__.py", "_platform/authz-policy.json", "_platform/authz.py", "_platform/entry.py"]
    policy = json.loads(files["_platform/authz-policy.json"])
    assert policy["default"] == "deny"
    assert policy["exempt_paths"] == ["/healthz"]
    assert policy["identity_header"] == "X-Platform-Roles"
    assert "from _platform.authz import AuthzASGI" in files["_platform/entry.py"]
    assert "from app import app as _inner" in files["_platform/entry.py"]


def test_gunicorn_uses_wsgi_wrapper(notes_spec):
    notes_spec["server"] = "gunicorn"
    assert "AuthzWSGI" in render_authz(notes_spec, PLATFORM)["_platform/entry.py"]


def test_static_sites_have_no_authz(notes_spec):
    notes_spec["app_type"] = "static"
    with pytest.raises(ValueError):
        render_authz(notes_spec, PLATFORM)


@pytest.mark.parametrize("method, path, roles, expected", CASES)
def test_asgi(authz, method, path, roles, expected):
    headers = [] if roles is None else [(b"x-platform-roles", roles.encode())]
    sent = run_asgi(authz, {"type": "http", "method": method, "path": path, "headers": headers})
    assert sent[0]["status"] == expected
    marked = (b"x-platform-authz", b"deny") in sent[0]["headers"]
    assert marked == (expected == 403)


@pytest.mark.parametrize("method, path, roles, expected", CASES)
def test_wsgi(authz, method, path, roles, expected):
    def inner(environ, start_response):
        start_response("200 OK", [])
        return [b"ok"]

    statuses, header_lists = [], []
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path}
    if roles is not None:
        environ["HTTP_X_PLATFORM_ROLES"] = roles
    authz.AuthzWSGI(inner)(environ, lambda status, headers: (statuses.append(status), header_lists.append(headers)))
    assert int(statuses[0].split()[0]) == expected
    assert (("X-Platform-Authz", "deny") in header_lists[0]) == (expected == 403)


@pytest.mark.parametrize("method, path, roles, expected", CASES)
def test_allowed_matches_table(authz, method, path, roles, expected):
    # allowed() sees the path without a query string, as the servers pass it
    assert authz.allowed(method, path.split("?")[0], authz.roles_from(roles)) == (expected == 200)


def test_control_characters_are_denied(authz):
    for char in ("\x00", "\n", "\r", "\x1f", "\x7f"):
        assert not authz.allowed("GET", f"/api/notes{char}", frozenset({"reader"}))


def test_duplicate_identity_headers_mean_no_roles(authz):
    headers = [(b"x-platform-roles", b"reader"), (b"X-Platform-Roles", b"writer")]
    assert run_asgi(authz, {"type": "http", "method": "POST", "path": "/api/notes", "headers": headers})[0]["status"] == 403
    same = [(b"x-platform-roles", b"writer"), (b"x-platform-roles", b"writer")]
    assert run_asgi(authz, {"type": "http", "method": "POST", "path": "/api/notes", "headers": same})[0]["status"] == 403
    assert run_asgi(authz, {"type": "http", "method": "GET", "path": "/healthz", "headers": same})[0]["status"] == 200


def test_denied_websocket_is_closed_with_1008(authz):
    for path in ("/api/notes", "/healthz"):
        sent = run_asgi(authz, {"type": "websocket", "path": path, "headers": []})
        assert sent == [{"type": "websocket.close", "code": 1008}]


def test_allowed_websocket_reaches_app(authz):
    called = []

    async def inner(scope, receive, send):
        called.append(scope["type"])

    headers = [(b"x-platform-roles", b"reader")]
    run_asgi(authz, {"type": "websocket", "path": "/api/notes", "headers": headers}, inner)
    assert called == ["websocket"]


def test_lifespan_passes_through(authz):
    called = []

    async def inner(scope, receive, send):
        called.append(scope["type"])

    asyncio.run(authz.AuthzASGI(inner)({"type": "lifespan"}, None, None))
    assert called == ["lifespan"]

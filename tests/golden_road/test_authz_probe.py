import json
import threading
from wsgiref.simple_server import WSGIRequestHandler, make_server

import pytest

from builder.config import load_platform
from builder.render.authz import policy_json
from golden_road.authz_probe import build_cases, evaluate, load_matcher, probe, run_cases

PLATFORM = load_platform()


class Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def spec_app(python_app, notes_spec):
    (python_app / "builder-spec.json").write_text(json.dumps(notes_spec))
    return python_app, notes_spec


def test_cases_cover_every_route_and_role_combination(spec_app):
    root, spec = spec_app
    cases = build_cases(spec, root, PLATFORM)
    keys = {(c["method"], c["path"], c["roles"]) for c in cases}
    for roles in (None, "reader", "writer", "reader,writer"):
        assert ("POST", "/api/notes", roles) in keys
        assert ("GET", "/api/notes", roles) in keys
    by_key = {(c["method"], c["path"], c["roles"]): c["expected"] for c in cases}
    assert by_key[("POST", "/api/notes", "reader")] == "deny"
    assert by_key[("POST", "/api/notes", "writer")] == "allow"
    assert by_key[("GET", "/api/notes", None)] == "deny"
    assert by_key[("GET", "/healthz", None)] == "allow"


def test_path_parameters_are_filled(make_app, notes_spec):
    root = make_app({"app.py": 'from fastapi import FastAPI\napp = FastAPI()\n@app.delete("/api/notes/{note_id}")\ndef d(note_id: int): ...\n'})
    paths = {c["path"] for c in build_cases(notes_spec, root, PLATFORM)}
    assert "/api/notes/1" in paths


def test_expected_uses_packaged_template_not_repo_copy(spec_app):
    root, spec = spec_app
    (root / "_platform").mkdir()
    (root / "_platform" / "authz.py").write_text("def allowed(*a): return True\n")
    permissive = {"identity_header": PLATFORM["identity_header"], "exempt_paths": [],
                  "rules": [{"methods": ["GET", "POST", "PUT", "PATCH", "DELETE"], "path": "/**", "roles": ["reader", "writer"]}]}
    (root / "_platform" / "authz-policy.json").write_text(json.dumps(permissive))
    cases = build_cases(spec, root, PLATFORM)
    assert any(c["expected"] == "deny" for c in cases)


def serve(app):
    server = make_server("127.0.0.1", 0, app, handler_class=Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def ok_app(environ, start_response):
    start_response("200 OK", [("Content-Type", "application/json")])
    return [b"{}"]


def test_guarded_server_passes(spec_app):
    root, spec = spec_app
    allowed, roles_from, module = load_matcher(policy_json(spec, PLATFORM))
    server, base = serve(module.AuthzWSGI(ok_app))
    try:
        result = evaluate(run_cases(base, PLATFORM["identity_header"], build_cases(spec, root, PLATFORM)))
    finally:
        server.shutdown()
    assert result["status"] == "pass" and result["denied"] > 0 and result["failures"] == []


def test_unguarded_server_fails(spec_app):
    root, spec = spec_app
    server, base = serve(ok_app)
    try:
        result = evaluate(run_cases(base, PLATFORM["identity_header"], build_cases(spec, root, PLATFORM)))
    finally:
        server.shutdown()
    assert result["status"] == "fail"
    assert {"method": "POST", "path": "/api/notes", "roles": "reader"} in [
        {k: f[k] for k in ("method", "path", "roles")} for f in result["failures"]]


def test_app_level_403_without_guard_body_fails(spec_app):
    root, spec = spec_app

    def forbid_all(environ, start_response):
        start_response("403 Forbidden", [("Content-Type", "text/plain")])
        return [b"nope"]

    server, base = serve(forbid_all)
    try:
        result = evaluate(run_cases(base, PLATFORM["identity_header"], build_cases(spec, root, PLATFORM)))
    finally:
        server.shutdown()
    assert result["status"] == "fail"


def test_unreachable_server_fails(spec_app):
    root, spec = spec_app
    result = evaluate(run_cases("http://127.0.0.1:9", PLATFORM["identity_header"], build_cases(spec, root, PLATFORM), timeout=0.5))
    assert result["status"] == "fail"


def test_not_declared_without_authz(python_app, notes_spec):
    del notes_spec["authz"]
    (python_app / "builder-spec.json").write_text(json.dumps(notes_spec))
    assert probe(python_app, "http://127.0.0.1:9", platform=PLATFORM)["status"] == "not-declared"


def test_authz_with_no_deny_cases_fails(make_app, notes_spec):
    root = make_app({"app.py": "from fastapi import FastAPI\napp = FastAPI()\n"})
    notes_spec.pop("health_path")
    (root / "builder-spec.json").write_text(json.dumps(notes_spec))
    result = probe(root, "http://127.0.0.1:9", platform=PLATFORM, ready_timeout=0.1)
    assert result["status"] == "fail" and "did not start" in result["reason"]


def _deny_results(spec_app, respond):
    """Run every case against a server whose responses come from `respond`; return the evaluation."""
    root, spec = spec_app
    server, base = serve(respond)
    try:
        return evaluate(run_cases(base, PLATFORM["identity_header"], build_cases(spec, root, PLATFORM), timeout=1))
    finally:
        server.shutdown()


def _responder(status, body, headers=()):
    def app(environ, start_response):
        start_response(status, [("Content-Type", "application/json"), *headers])
        return [body]
    return app


@pytest.mark.parametrize("status,body,headers", [
    ("401 Unauthorized", b'{"error":"forbidden"}', ()),
    ("404 Not Found", b'{"error":"forbidden"}', ()),
    ("403 Forbidden", b'{"error":"forbidden","extra":1}', ()),
    ("403 Forbidden", b'{"error":"nope"}', ()),
    ("403 Forbidden", b'{"error":"x","error":"forbidden"}', ()),
    ("403 Forbidden", b'[{"error":"forbidden"}]', ()),
    ("403 Forbidden", b'{"error": "forbidden"}', ()),
    ("403 Forbidden", b'{"error":"forbidden"}\n', ()),
    ("302 Found", b"", (("Location", "/login"),)),
    ("500 Server Error", b'{"error":"forbidden"}', ()),
])
def test_only_the_guards_exact_403_counts_as_denied(spec_app, status, body, headers):
    assert _deny_results(spec_app, _responder(status, body, headers))["status"] == "fail"


MARK = (("X-Platform-Authz", "deny"),)


def test_guard_403_everywhere_fails_because_health_is_allowed(spec_app):
    result = _deny_results(spec_app, _responder("403 Forbidden", b'{"error":"forbidden"}', MARK))
    assert result["status"] == "fail" and "policy and image disagree" in result["reason"]
    assert any(f["path"] == "/healthz" for f in result["failures"])


def test_app_403_with_guard_body_but_no_marker_fails(spec_app):
    result = _deny_results(spec_app, _responder("403 Forbidden", b'{"error":"forbidden"}'))
    assert result["status"] == "fail"
    assert {"method": "POST", "path": "/api/notes", "roles": "reader", "status": 403} in [
        {k: f[k] for k in ("method", "path", "roles", "status")} for f in result["failures"]]


def test_marker_name_is_case_insensitive(spec_app):
    root, spec = spec_app
    allowed, roles_from, module = load_matcher(policy_json(spec, PLATFORM))

    def lower(environ, start_response):
        def sr(status, headers, *a):
            return start_response(status, [(k.lower(), v) for k, v in headers], *a)
        return module.AuthzWSGI(ok_app)(environ, sr)

    assert _deny_results(spec_app, lower)["status"] == "pass"


@pytest.mark.parametrize("status", ["404 Not Found", "422 Unprocessable Entity", "200 OK", "500 Server Error"])
def test_allowed_cases_with_non_guard_errors_do_not_gate(spec_app, status):
    root, spec = spec_app
    allowed, roles_from, module = load_matcher(policy_json(spec, PLATFORM))

    def inner(environ, start_response):
        start_response(status, [("Content-Type", "application/json")])
        return [b"{}"]

    assert _deny_results(spec_app, module.AuthzWSGI(inner))["status"] == "pass"


def test_canary_cases_always_present_and_denied(spec_app):
    import re

    root, spec = spec_app
    cases = build_cases(spec, root, PLATFORM)
    canary = [c for c in cases if re.fullmatch(r"/__golden_road_probe_[0-9a-f]{16}", c["path"])]
    assert {(c["method"], c["roles"]) for c in canary} == {(m, r) for m in ("GET", "POST") for r in (None, "reader", "writer")}
    assert all(c["expected"] == "deny" for c in canary)
    assert len({c["path"] for c in canary}) == 1
    other = {c["path"] for c in build_cases(spec, root, PLATFORM) if c["path"].startswith("/__golden")}
    assert other != {canary[0]["path"]}  # a fresh random path each build


def test_unguarded_app_fails_on_the_canary(spec_app):
    root, spec = spec_app
    server, base = serve(ok_app)
    try:
        out = evaluate(run_cases(base, PLATFORM["identity_header"], build_cases(spec, root, PLATFORM)))
    finally:
        server.shutdown()
    assert any(f["path"].startswith("/__golden_road_probe_") for f in out["failures"])


def test_cases_come_from_policy_rules_not_just_routes(make_app, notes_spec):
    root = make_app({"app.py": 'from fastapi import FastAPI, APIRouter\napp = FastAPI()\nrouter = APIRouter(prefix="/api")\n'
                               '@router.post("/notes")\ndef create(): ...\n@app.get(\n    "/api/notes")\ndef lst(): ...\n'
                               'app.include_router(router)\n'})
    cases = build_cases(notes_spec, root, PLATFORM)
    by_key = {(c["method"], c["path"], c["roles"]): c["expected"] for c in cases}
    assert by_key[("POST", "/api/notes", "reader")] == "deny"
    assert by_key[("POST", "/api/probe/x", "reader")] == "deny"
    assert by_key[("POST", "/api/probe/x", "writer")] == "allow"
    assert by_key[("GET", "/api/probe/x", "reader")] == "allow"


def test_rule_samples_methods_and_role_variants(make_app, notes_spec):
    notes_spec["authz"]["rules"].append({"methods": ["GET"], "path": "/items/{id}/sub/*", "roles": ["reader"]})
    root = make_app({"app.py": "from fastapi import FastAPI\napp = FastAPI()\n"})
    cases = build_cases(notes_spec, root, PLATFORM)
    keys = {(c["method"], c["path"], c["roles"]) for c in cases}
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "PROPFIND"):
        for roles in (None, "reader", "writer", "reader,writer"):
            assert (method, "/items/1/sub/probe", roles) in keys
            assert (method, "/api/probe/x", roles) in keys
    for variant in ("WRITER", " writer ", "writer,", "writer,writer"):
        assert ("POST", "/api/probe/x", variant) in keys
    assert len(keys) == len(cases)
    assert cases == sorted(cases, key=lambda c: (c["method"], c["path"], c["roles"] is not None, c["roles"] or ""))
    assert {c["expected"] for c in cases if c["roles"] == "WRITER"} <= {"allow", "deny"}


def test_many_failures_are_capped_but_counted():
    results = [{"method": "POST", "path": "/" + "x" * 500 + str(i), "roles": "reader", "expected": "deny", "status": 200, "guard_denied": False}
               for i in range(250)]
    out = evaluate(results)
    assert out["status"] == "fail" and len(out["failures"]) == 200 and out["failures_total"] == 250
    assert all(len(f["path"]) <= 300 for f in out["failures"])


def test_gate_record_stays_writable_with_250_failures():
    from golden_road.record import build_gate_record

    results = [{"method": "POST", "path": f"/x{i}", "roles": "reader", "expected": "deny", "status": 200, "guard_denied": False}
               for i in range(250)]
    authz = evaluate(results)
    zero = {s: 0 for s in ("critical", "high", "medium", "low", "info")}
    record = build_gate_record(repo="o/r", commit="c" * 40, image="ghcr.io/o/a@sha256:" + "d" * 64,
                               builder_image="ghcr.io/o/b@sha256:" + "e" * 64,
                               drift={"status": "pass", "counts": {"by_severity": zero, "by_layer": {}}, "blocking": [], "overrides_applied": []},
                               checks={"passed": True, "required_checks": []}, authz=authz, created="2026-10-20T15:30:00Z",
                               release="test-20261020153000-ccccccc", manifests_sha256="a" * 64)
    assert record["status"] == "fail"
    assert len(record["authz_probe"]["failures"]) == 200 and record["authz_probe"]["failures_total"] == 250


def test_unreachable_stops_after_consecutive_failures(spec_app):
    root, spec = spec_app
    cases = build_cases(spec, root, PLATFORM)
    results = run_cases("http://127.0.0.1:9", PLATFORM["identity_header"], cases, timeout=0.5)
    out = evaluate(results)
    assert out["status"] == "fail" and "consecutive" in out["reason"] and out["cases"] == len(cases)


def test_overall_deadline_fails_with_reason(spec_app):
    import time

    def slow(environ, start_response):
        time.sleep(0.2)
        start_response("200 OK", [])
        return [b""]

    root, spec = spec_app
    server, base = serve(slow)
    try:
        out = evaluate(run_cases(base, PLATFORM["identity_header"], build_cases(spec, root, PLATFORM), timeout=2, deadline=0.5))
    finally:
        server.shutdown()
    assert out["status"] == "fail" and "deadline" in out["reason"]


def test_connection_reset_and_garbage_fail_without_raising(spec_app):
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    stop = threading.Event()

    def reset_loop():
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except OSError:
                continue
            conn.recv(1024)
            conn.send(b"garbage not http\r\n\r\n" if stop.is_set() is False else b"")
            conn.close()

    threading.Thread(target=reset_loop, daemon=True).start()
    root, spec = spec_app
    try:
        result = evaluate(run_cases(f"http://127.0.0.1:{sock.getsockname()[1]}", PLATFORM["identity_header"],
                                    build_cases(spec, root, PLATFORM), timeout=1))
    finally:
        stop.set()
        sock.close()
    assert result["status"] == "fail"


def test_slow_server_fails(spec_app):
    import time

    def slow(environ, start_response):
        time.sleep(1.5)
        start_response("403 Forbidden", [("Content-Type", "application/json")])
        return [b'{"error":"forbidden"}']

    root, spec = spec_app
    cases = [c for c in build_cases(spec, root, PLATFORM) if c["expected"] == "deny"][:1]
    server, base = serve(slow)
    try:
        assert evaluate(run_cases(base, PLATFORM["identity_header"], cases, timeout=0.2))["status"] == "fail"
    finally:
        server.shutdown()


def test_evaluate_with_nothing_to_deny_fails():
    assert evaluate([{"method": "GET", "path": "/", "roles": None, "expected": "allow", "status": 200, "guard_denied": False}])["status"] == "fail"


def test_health_path_is_probed_and_allowed(spec_app):
    root, spec = spec_app
    health = [c for c in build_cases(spec, root, PLATFORM) if c["path"] == "/healthz"]
    assert health and all(c["expected"] == "allow" for c in health if c["method"] == "GET")


def test_flask_and_express_parameter_paths(make_app, node_spec):
    py_spec = {**node_spec, "app_type": "python", "entrypoint": "app:app"}
    flask = make_app({"app.py": 'from flask import Flask\napp = Flask(__name__)\n@app.route("/api/notes/<int:note_id>", methods=["DELETE"])\ndef d(note_id): ...\n'})
    assert "/api/notes/1" in {c["path"] for c in build_cases(py_spec, flask, PLATFORM)}
    express = make_app({"server.js": "const app = require('express')();\napp.delete('/api/notes/:id', (req, res) => res.end());\n"}, name="ex")
    assert "/api/notes/1" in {c["path"] for c in build_cases(node_spec, express, PLATFORM)}
    assert all(":" not in c["path"] and "<" not in c["path"] for c in build_cases(node_spec, express, PLATFORM))


def test_every_detected_route_is_a_case(spec_app):
    from builder.detect import find_routes

    root, spec = spec_app
    cases = {(c["method"], c["path"]) for c in build_cases(spec, root, PLATFORM)}
    for route in find_routes(root, spec["app_type"]):
        assert (route["method"], route["path"]) in cases

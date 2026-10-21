import pytest

from builder.config import load_platform
from builder.spec import check_spec

PLATFORM = load_platform()


def check(spec, root):
    return check_spec(spec, root, PLATFORM)


def test_valid_spec_passes(notes_spec, python_app):
    assert check(notes_spec, python_app) == ([], [], [])


def test_schema_errors_short_circuit(notes_spec, python_app):
    notes_spec["port"] = 80
    errors, questions, _ = check(notes_spec, python_app)
    assert any("port" in e for e in errors)
    assert questions == []


def test_reader_cannot_be_granted_writes(notes_spec, python_app):
    notes_spec["authz"]["rules"][1]["roles"] = ["reader", "writer"]
    errors, _, _ = check(notes_spec, python_app)
    assert any("read-only role 'reader'" in e for e in errors)


def test_justified_reader_write_passes(notes_spec, python_app):
    notes_spec["authz"]["rules"][1]["roles"] = ["reader", "writer"]
    notes_spec["authz"]["rules"][1]["justification"] = "Readers may add comments by design."
    assert check(notes_spec, python_app)[0] == []


def test_undeclared_role(notes_spec, python_app):
    notes_spec["authz"]["rules"][0]["roles"] = ["admin"]
    assert any("undeclared role 'admin'" in e for e in check(notes_spec, python_app)[0])


def test_duplicate_role(notes_spec, python_app):
    notes_spec["authz"]["roles"].append({"name": "reader", "access": "write"})
    assert any("unique" in e for e in check(notes_spec, python_app)[0])


def test_direct_model_provider_egress_rejected(notes_spec, python_app):
    notes_spec["egress"] = [{"host": "myorg.openai.azure.com", "port": 443, "reason": "model calls"}]
    errors = check(notes_spec, python_app)[0]
    assert any("myorg.openai.azure.com" in e and "litellm.internal" in e for e in errors)


def test_cloud_metadata_egress_rejected(notes_spec, python_app):
    notes_spec["egress"] = [{"host": "metadata.google.internal", "port": 80, "reason": "instance metadata"}]
    assert any("cloud metadata" in e for e in check(notes_spec, python_app)[0])


def test_missing_module(notes_spec, python_app):
    notes_spec["entrypoint"] = "nope:app"
    assert any("module nope not found" in e for e in check(notes_spec, python_app)[0])


def test_dotdot_entrypoint_rejected_by_schema(notes_spec, python_app):
    notes_spec["entrypoint"] = "../evil.py"
    del notes_spec["server"]
    del notes_spec["authz"]
    assert any("entrypoint" in e for e in check(notes_spec, python_app)[0])


def test_symlinked_entrypoint_outside_root(notes_spec, python_app, tmp_path):
    outside = tmp_path / "evil.py"
    outside.write_text("print('outside')\n")
    (python_app / "link.py").symlink_to(outside)
    notes_spec["entrypoint"] = "link.py"
    del notes_spec["server"]
    del notes_spec["authz"]
    assert any("outside the app directory" in e for e in check(notes_spec, python_app)[0])


def test_symlinked_module_file_outside_root(notes_spec, python_app, tmp_path):
    outside = tmp_path / "evil.py"
    outside.write_text("app = None\n")
    (python_app / "linked.py").symlink_to(outside)
    notes_spec["entrypoint"] = "linked:app"
    del notes_spec["authz"]
    assert any("outside the app directory" in e for e in check(notes_spec, python_app)[0])


def test_module_attr_requires_server(notes_spec, python_app):
    del notes_spec["server"]
    assert any("needs server" in e for e in check(notes_spec, python_app)[0])


def test_file_entrypoint_with_authz_rejected(notes_spec, make_app):
    root = make_app({"main.py": "print('hi')\n", "requirements.txt": "x==1 --hash=sha256:" + "c" * 64 + "\n"})
    notes_spec["entrypoint"] = "main.py"
    del notes_spec["server"]
    assert any("module:attr entrypoint so the platform can wrap" in e for e in check(notes_spec, root)[0])


def test_static_rules(notes_spec, make_app):
    root = make_app({"index.html": "<h1>hi</h1>"})
    notes_spec.update(app_type="static", entrypoint=".", port=8000)
    del notes_spec["server"]
    errors = check(notes_spec, root)[0]
    assert any("port 8080" in e for e in errors)
    assert any("does not apply to static" in e for e in errors)


def test_uv_lock_without_requirements_asks(notes_spec, python_app):
    (python_app / "requirements.txt").unlink()
    (python_app / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    _, questions, _ = check(notes_spec, python_app)
    assert any("uv export --format requirements-txt" in q for q in questions)


def test_requirements_without_hashes_asks(notes_spec, python_app):
    (python_app / "requirements.txt").write_text("fastapi==0.115.0\nuvicorn==0.30.0\n", encoding="utf-8")
    assert any("no hashes" in q for q in check(notes_spec, python_app)[1])


def test_server_not_pinned_asks(notes_spec, python_app):
    (python_app / "requirements.txt").write_text("fastapi==0.115.0 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8")
    assert any("uvicorn is not pinned" in q for q in check(notes_spec, python_app)[1])


def gunicorn_requirements(version):
    return f"fastapi==0.115.0 --hash=sha256:{'a' * 64}\ngunicorn=={version} --hash=sha256:{'b' * 64}\n"


@pytest.mark.parametrize("version, asks", [("21.2.0", True), ("20.1.0", True), ("22.0.0", False), ("23.0.0", False)])
def test_old_gunicorn_asks(notes_spec, python_app, version, asks):
    notes_spec["server"] = "gunicorn"
    (python_app / "requirements.txt").write_text(gunicorn_requirements(version), encoding="utf-8")
    questions = check(notes_spec, python_app)[1]
    assert any("gunicorn" in q and "22" in q for q in questions) == asks


def test_write_routes_without_authz_ask(notes_spec, python_app):
    del notes_spec["authz"]
    _, questions, _ = check(notes_spec, python_app)
    assert any("write endpoints (POST /api/notes)" in q for q in questions)


def test_node_needs_lockfile_and_port(node_spec, make_app):
    root = make_app({"server.js": "require('express')().listen(3000);\n", "package.json": "{}"})
    _, questions, _ = check(node_spec, root)
    assert any("package-lock.json" in q for q in questions)
    assert any("process.env.PORT" in q for q in questions)


def test_missing_health_path_warns(notes_spec, python_app):
    del notes_spec["health_path"]
    assert any("TCP probe" in w for w in check(notes_spec, python_app)[2])


def test_gateway_allowed_but_provider_in_same_spec_rejected(notes_spec, python_app):
    notes_spec["egress"] = [
        {"host": "litellm.internal", "port": 443, "reason": "model calls through the platform gateway"},
        {"host": "api.anthropic.com", "port": 443, "reason": "model calls"},
    ]
    errors = check(notes_spec, python_app)[0]
    assert len(errors) == 1
    assert "api.anthropic.com" in errors[0]


def test_glob_denied_metadata_host_rejected(notes_spec, python_app):
    notes_spec["egress"] = [{"host": "instance-data.us-west-2.compute.internal", "port": 80, "reason": "instance metadata"}]
    assert any("cloud metadata" in e for e in check(notes_spec, python_app)[0])
    notes_spec["egress"] = [{"host": "litellm.internal", "port": 443, "reason": "model calls through the platform gateway"}]
    assert check(notes_spec, python_app)[0] == []


def test_hash_only_in_comment_does_not_count(notes_spec, python_app):
    (python_app / "requirements.txt").write_text(
        "# --hash=sha256:" + "a" * 64 + "\nuvicorn==0.30.0\n", encoding="utf-8")
    assert any("no hashes" in q for q in check(notes_spec, python_app)[1])


def test_node_without_package_json_asks(node_spec, make_app):
    root = make_app({"server.js": "require('http').createServer().listen(process.env.PORT);\n"})
    assert any("package.json" in q for q in check(node_spec, root)[1])


@pytest.mark.parametrize("lock", ["{}", "[]", "not json", '{"lockfileVersion": 3}'])
def test_package_lock_must_be_valid(node_spec, make_app, lock):
    root = make_app({
        "server.js": "require('http').createServer().listen(process.env.PORT);\n",
        "package.json": "{}",
        "package-lock.json": lock,
    })
    asked = any("package-lock.json" in q for q in check(node_spec, root)[1])
    assert asked == (lock != '{"lockfileVersion": 3}')


def test_symlinked_package_init_outside_root(notes_spec, make_app, tmp_path):
    outside = tmp_path / "evil_init.py"
    outside.write_text("")
    root = make_app({"pkg/sub.py": "app = None\n", "requirements.txt": "uvicorn==1 --hash=sha256:" + "a" * 64 + "\n"})
    (root / "pkg" / "__init__.py").symlink_to(outside)
    notes_spec["entrypoint"] = "pkg.sub:app"
    del notes_spec["authz"]
    assert any("outside the app directory" in e for e in check(notes_spec, root)[0])


def test_unreadable_requirements_asks_instead_of_raising(notes_spec, python_app, tmp_path):
    outside = tmp_path / "req.txt"
    outside.write_text("uvicorn==1 --hash=sha256:" + "a" * 64 + "\n")
    (python_app / "requirements.txt").unlink()
    (python_app / "requirements.txt").symlink_to(outside)
    assert any("requirements.txt" in q for q in check(notes_spec, python_app)[1])


def test_oversize_source_is_a_question_in_check_spec(notes_spec, python_app):
    (python_app / "big.py").write_text("#" * 1_000_001)
    errors, questions, _ = check(notes_spec, python_app)
    assert errors == []
    assert any("Couldn't scan big.py (larger than 1 MB)" in q for q in questions)


@pytest.mark.parametrize("name,why", [
    ("PORT", "set by the platform"), ("NODE_ENV", "NODE_ENV"), ("NODE_OPTIONS", "changes how code loads"),
    ("PATH", "PATH"), ("HOME", "HOME"), ("LD_PRELOAD", "libraries"), ("LD_LIBRARY_PATH", "libraries"),
    ("PYTHONPATH", "interpreter"), ("PYTHONSTARTUP", "interpreter"), ("GUNICORN_CMD_ARGS", "server"),
    ("UVICORN_WORKERS", "server"), ("NODE_PATH", "Node loads code"), ("NPM_CONFIG_REGISTRY", "npm"),
])
def test_reserved_env_names_are_rejected(notes_spec, python_app, name, why):
    notes_spec["env"].append({"name": name, "secret": False})
    errors = check(notes_spec, python_app)[0]
    assert any(name in e and why in e for e in errors), errors


def test_duplicate_env_names_are_rejected(notes_spec, python_app):
    notes_spec["env"].append({"name": "NOTES_DB_URL", "secret": False})
    assert any("NOTES_DB_URL is listed more than once" in e for e in check(notes_spec, python_app)[0])


@pytest.mark.parametrize("name", ["PORTAL_URL", "HOMEPAGE", "MY_NODE_ID", "LDAP_HOST", "PATHS"])
def test_similar_but_unreserved_env_names_pass(notes_spec, python_app, name):
    notes_spec["env"].append({"name": name, "secret": False})
    assert check(notes_spec, python_app)[0] == []

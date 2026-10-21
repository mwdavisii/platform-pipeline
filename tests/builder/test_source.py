import os
import time

import pytest

from builder import source
from builder.config import load_platform
from builder.detect import detect, find_routes, package_json
from builder.source import MAX_SOURCE_BYTES, read_text, source_files
from builder.spec import check_spec


def rels(root, files):
    return [f.relative_to(root).as_posix() for f in files]


def test_skips_vcs_and_builder_owned_but_not_build_dirs(make_app):
    root = make_app({
        "a.py": "", "build/b.py": "", "dist/c.py": "", "deploy/d.py": "",
        "deploy/base/e.py": "", "_platform/f.py": "", "node_modules/g.py": "",
        ".git/h.py": "", "venv/i.py": "", ".venv/j.py": "", "x/__pycache__/k.py": "",
    })
    assert rels(root, source_files(root, {".py"})) == ["a.py", "build/b.py", "deploy/d.py", "dist/c.py"]


def test_symlinked_file_and_dir_outside_tree_skipped(make_app, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "evil.py").write_text("x = 1\n")
    root = make_app({"ok.py": ""})
    (root / "link.py").symlink_to(outside / "evil.py")
    (root / "linkdir").symlink_to(outside)
    assert rels(root, source_files(root, {".py"})) == ["ok.py"]
    assert read_text(root / "link.py", root) is None


def test_oversize_and_nul_files_skipped(make_app):
    root = make_app({"ok.py": "x = 1\n", "big.py": "#" * (MAX_SOURCE_BYTES + 1), "bin.py": "a\0b"})
    warnings = []
    assert rels(root, source_files(root, {".py"}, warnings)) == ["ok.py"]
    assert any("big.py" in w for w in warnings)
    assert read_text(root / "big.py", root) is None
    assert read_text(root / "bin.py", root) is None
    assert read_text(root / "ok.py", root) == "x = 1\n"


def test_unreadable_and_non_regular_files_do_not_raise(make_app):
    root = make_app({"ok.py": ""})
    os.mkfifo(root / "pipe.py")
    (root / "locked.py").write_text("x")
    (root / "locked.py").chmod(0)
    try:
        assert "ok.py" in rels(root, source_files(root, {".py"}))
    finally:
        (root / "locked.py").chmod(0o644)
    assert read_text(root / "missing.py", root) is None


def test_oversize_warning_surfaces_in_detect(make_app):
    root = make_app({"app.py": "#" * (MAX_SOURCE_BYTES + 1)})
    assert any("app.py" in w and "larger than 1 MB" in w for w in detect(root)["warnings"])


def test_symlinked_source_ignored_by_detect(make_app, tmp_path):
    outside = tmp_path / "evil.py"
    outside.write_text('@app.post("/evil")\nX = "https://evil.example.org/x"\n')
    root = make_app({"main.py": "x = 1\n"})
    (root / "link.py").symlink_to(outside)
    out = detect(root)
    assert out["needs"]["routes"] == []
    assert out["needs"]["egress_hosts"] == []


def test_routes_in_build_and_deploy_dirs_count(make_app):
    for d in ("build", "deploy"):
        root = make_app({"app.py": "x = 1\n", f"{d}/r.py": '@app.post("/x")\ndef f(): pass\n'}, name=d)
        assert [r["method"] for r in find_routes(root, "python")] == ["POST"]


@pytest.mark.parametrize("d", ["build", "deploy"])
def test_write_endpoint_question_for_routes_in_build_dirs(notes_spec, make_app, d):
    root = make_app({
        "app.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        f"{d}/r.py": '@app.post("/x")\ndef f(): pass\n',
        "requirements.txt": "uvicorn==0.30.0 --hash=sha256:" + "a" * 64 + "\n",
    }, name=f"w{d}")
    del notes_spec["authz"]
    _, questions, _ = check_spec(notes_spec, root, load_platform())
    assert any("write endpoints (POST /x)" in q for q in questions)


def test_node_entrypoint_in_dist_is_scanned(node_spec, make_app):
    root = make_app({
        "dist/server.js": "require('express')().listen(Number(process.env.PORT));\n",
        "package.json": '{"main": "dist/server.js"}',
        "package-lock.json": '{"lockfileVersion": 3}',
    })
    node_spec["entrypoint"] = "dist/server.js"
    assert check_spec(node_spec, root, load_platform()) == ([], [], [])
    assert not any("process.env.PORT" in q for q in detect(root)["questions"])


def test_line_numbers_are_correct(make_app):
    root = make_app({"a.py": 'x = 1\n\n@app.get("/a")\ndef a(): pass\n\n\n@app.post("/b")\ndef b(): pass\n'})
    assert [(r["path"], r["line"]) for r in find_routes(root, "python")] == [("/a", 3), ("/b", 7)]


def test_unclosed_route_lines_are_not_quadratic(make_app):
    root = make_app({"a.py": '@x.route("/a"\n' * 15000, "b.py": '@x.route("/a"' * 15000})
    assert (root / "a.py").stat().st_size > 200_000
    start = time.monotonic()
    find_routes(root, "python")
    assert time.monotonic() - start < 2


def test_flask_methods_after_nested_call_and_tuple(make_app):
    root = make_app({"a.py": (
        '@app.route("/a", strict_slashes=f(1, 2), methods=["POST"])\ndef a(): pass\n\n'
        '@app.route("/b", methods=("GET", "DELETE"))\ndef b(): pass\n\n'
        '@app.route("/c")\n@app.route("/d", methods=["PUT"])\ndef c(): pass\n'
    )})
    got = sorted((r["path"], r["method"]) for r in find_routes(root, "python"))
    assert got == [("/a", "POST"), ("/b", "DELETE"), ("/b", "GET"), ("/c", "GET"), ("/d", "PUT")]


def test_jsx_tsx_mts_cts_scanned(make_app):
    root = make_app({f"s{i}{s}": "app.post('/x', h)\n" for i, s in enumerate((".jsx", ".tsx", ".mts", ".cts"))})
    assert len(find_routes(root, "node")) == 4


HOSTILE = [
    b"[]", b'"str"', b"1", b'{"scripts": {"start": 5}}', b'{"main": 5}',
    b'{"scripts": "rebuild"}', b'{"dependencies": ["express"]}', b"\xff\xfe\x00bad", b"[" * 100000,
]


@pytest.mark.parametrize("content", HOSTILE)
def test_hostile_package_json_does_not_raise(content, node_spec, make_app):
    root = make_app({"server.js": "require('express')().listen(process.env.PORT);\n"})
    (root / "package.json").write_bytes(content)
    out = detect(root)
    assert out["status"] == "ok"
    result = check_spec(node_spec, root, load_platform())
    assert len(result) == 3
    pkg = package_json(root)
    assert pkg is None or (isinstance(pkg.get("scripts", {}), dict) and isinstance(pkg.get("dependencies", {}), dict))
    assert pkg is None or isinstance(pkg.get("main", ""), str)


def test_package_json_symlink_outside_ignored(make_app, tmp_path):
    outside = tmp_path / "package.json"
    outside.write_text('{"main": "x.js"}')
    root = make_app({"app.py": ""})
    (root / "package.json").symlink_to(outside)
    assert package_json(root) is None


def test_source_module_is_the_reader():
    import builder.detect as d
    assert d.source_files is source.source_files


def _node_root(make_app, routes_text, **extra):
    files = {
        "server.js": "require('./routes');\nrequire('express')().listen(process.env.PORT);\n",
        "routes.js": routes_text,
        "package.json": '{"main": "server.js"}',
        "package-lock.json": '{"lockfileVersion": 3}',
    }
    files.update(extra)
    return make_app(files)


def test_nul_in_js_comment_hides_routes_so_check_spec_asks(node_spec, make_app):
    root = _node_root(make_app, "// \0\napp.post('/x', h)\n")
    del node_spec["authz"]
    errors, questions, _ = check_spec(node_spec, root, load_platform())
    assert errors == []
    assert any("routes.js" in q and "NUL" in q for q in questions)
    assert any("routes.js" in w and "NUL" in w for w in detect(root)["warnings"])


def test_oversize_py_makes_check_spec_ask(notes_spec, python_app):
    (python_app / "routes.py").write_text('@app.post("/x")\n' + "#" * MAX_SOURCE_BYTES)
    _, questions, _ = check_spec(notes_spec, python_app, load_platform())
    assert any("routes.py" in q and "larger than 1 MB" in q for q in questions)
    assert any("routes.py" in w for w in detect(python_app)["warnings"])


def test_oversize_python_entrypoint_makes_check_spec_ask(notes_spec, python_app):
    (python_app / "app.py").write_text(" " * (MAX_SOURCE_BYTES + 1))
    _, questions, _ = check_spec(notes_spec, python_app, load_platform())
    assert any("app.py" in q for q in questions)


def test_oversize_node_entrypoint_makes_check_spec_ask(node_spec, make_app):
    root = _node_root(make_app, "")
    (root / "server.js").write_text(" " * (MAX_SOURCE_BYTES + 1))
    _, questions, _ = check_spec(node_spec, root, load_platform())
    assert any("server.js" in q for q in questions)


def test_symlink_out_of_tree_makes_check_spec_ask(notes_spec, python_app, tmp_path):
    outside = tmp_path / "evil.py"
    outside.write_text('@app.post("/evil")\n')
    (python_app / "routes.py").symlink_to(outside)
    _, questions, _ = check_spec(notes_spec, python_app, load_platform())
    assert any("routes.py links outside the app directory" in q for q in questions)


def test_symlinked_dir_out_of_tree_makes_check_spec_ask(notes_spec, python_app, tmp_path):
    (tmp_path / "ext").mkdir()
    (python_app / "pkg").symlink_to(tmp_path / "ext")
    _, questions, _ = check_spec(notes_spec, python_app, load_platform())
    assert any("pkg links outside the app directory" in q for q in questions)


def test_unscanned_suffixes_do_not_ask(notes_spec, python_app):
    (python_app / "bundle.min.js").write_text("#" * (MAX_SOURCE_BYTES + 1))
    assert check_spec(notes_spec, python_app, load_platform()) == ([], [], [])


def test_tree_byte_budget_asks_and_warns(notes_spec, python_app, monkeypatch):
    (python_app / "more.py").write_text("x = 1\n" * 100)
    monkeypatch.setattr(source, "MAX_TREE_BYTES", 700)
    _, questions, _ = check_spec(notes_spec, python_app, load_platform())
    assert any("scan budget" in q for q in questions)
    assert any("scan budget" in w for w in detect(python_app)["warnings"])


def test_skip_order_is_deterministic(make_app):
    root = make_app({f"d{i}/f.py": "x = 1\n" for i in range(5)})
    first = rels(root, source_files(root, {".py"}))
    assert first == sorted(first)

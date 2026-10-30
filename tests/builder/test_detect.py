from builder.detect import detect, find_routes


def test_fastapi_app(python_app):
    out = detect(python_app)
    draft = out["draft_spec"]
    assert draft["app_type"] == "python"
    assert draft["entrypoint"] == "app:app"
    assert draft["server"] == "uvicorn"
    assert draft["health_path"] == "/healthz"
    assert any("write endpoints" in q and "POST /api/notes" in q for q in out["questions"])
    assert any("public, internal, restricted, or phi" in q for q in out["questions"])


def test_flask_app_in_package(make_app):
    root = make_app({
        "pkg/__init__.py": "",
        "pkg/web.py": 'from flask import Flask\napplication = Flask(__name__)\n\n@application.route("/items", methods=["GET", "POST"])\ndef items():\n    return "ok"\n',
        "requirements.txt": "flask==3.0.0\n",
    })
    out = detect(root)
    assert out["draft_spec"]["entrypoint"] == "pkg.web:application"
    assert out["draft_spec"]["server"] == "gunicorn"
    assert [(r["method"], r["path"]) for r in out["needs"]["routes"]] == [("GET", "/items"), ("POST", "/items")]


def test_node_express_app(node_app):
    out = detect(node_app)
    assert out["draft_spec"]["app_type"] == "node"
    assert out["draft_spec"]["entrypoint"] == "server.js"
    assert "PORT" not in out["needs"]["env"]
    assert not any("process.env.PORT" in q for q in out["questions"])


def test_node_without_port_asks(make_app):
    root = make_app({"server.js": "require('express')().listen(3000);\n", "package.json": '{"main": "server.js", "dependencies": {"express": "4"}}'})
    assert any("process.env.PORT" in q for q in detect(root)["questions"])


def test_static_plain_html(make_app):
    out = detect(make_app({"index.html": "<a href='https://cdn.example.org/x.js'>x</a>"}))
    assert out["draft_spec"]["app_type"] == "static"
    assert out["draft_spec"]["entrypoint"] == "."
    assert out["needs"]["egress_hosts"] == []


def test_static_with_build(make_app):
    out = detect(make_app({"package.json": '{"scripts": {"build": "vite build"}, "dependencies": {"react": "18"}}', "index.html": ""}))
    assert out["draft_spec"]["app_type"] == "static"
    assert out["draft_spec"]["entrypoint"] == "dist"
    assert any("dist/" in w for w in out["warnings"])


def test_egress_and_env_detection(make_app):
    root = make_app({
        "main.py": 'import os\nKEY = os.environ["OPENAI_API_KEY"]\nURL = os.getenv("SERVICE_URL")\nMODE = os.environ.get("MODE")\nBASE = "https://api.openai.com/v1"\nOTHER = "https://api.github.com/repos"\nLOCAL = "http://localhost:8000"\n',
    })
    out = detect(root)
    assert out["needs"]["egress_hosts"] == ["api.github.com", "api.openai.com"]
    assert out["draft_spec"]["env"] == [
        {"name": "MODE", "secret": False},
        {"name": "OPENAI_API_KEY", "secret": True},
        {"name": "SERVICE_URL", "secret": True},
    ]
    assert any("api.openai.com directly" in q and "litellm.internal" in q for q in out["questions"])
    assert any("api.github.com. Why?" in q for q in out["questions"])


def test_skips_vendored_dirs(make_app):
    root = make_app({"app.py": "x = 1\n", "node_modules/pkg/index.js": "app.post('/x', h)\n", ".venv/lib/a.py": '@app.post("/y")\n'})
    assert find_routes(root, "python") == []


def test_mixed_python_and_node_asks(make_app):
    out = detect(make_app({"app.py": "", "package.json": "{}"}))
    assert out["draft_spec"]["app_type"] is None
    assert any("python or node" in q for q in out["questions"])


def test_the_draft_leaves_out_names_the_platform_sets(make_app):
    root = make_app({
        "main.py": 'import os\nA = os.environ["AZURE_CLIENT_ID"]\nB = os.getenv("DATABRICKS_HOST")\nM = os.environ["APP_MODE"]\nH = os.environ["HOME"]\n',
    })
    out = detect(root)
    assert out["draft_spec"]["env"] == [{"name": "APP_MODE", "secret": False}]
    assert out["needs"]["env"] == ["APP_MODE", "AZURE_CLIENT_ID", "DATABRICKS_HOST", "HOME"]
    assert out["needs"]["set_by_platform"] == ["AZURE_CLIENT_ID", "DATABRICKS_HOST", "HOME"]
    assert any("AZURE_CLIENT_ID" in q and "DATABRICKS_HOST" in q and "binding" in q for q in out["questions"])


def test_the_draft_leaves_out_secretserver_names(make_app):
    out = detect(make_app({"main.py": 'import os\nT = os.environ["SECRETSERVER_TOKEN"]\nM = os.environ["APP_MODE"]\n'}))
    assert out["draft_spec"]["env"] == [{"name": "APP_MODE", "secret": False}]
    assert out["needs"]["set_by_platform"] == ["SECRETSERVER_TOKEN"]
    assert any("SECRETSERVER_TOKEN" in q and "binding" in q and "Secret Server" in q for q in out["questions"])

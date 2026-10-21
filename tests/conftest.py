import copy

import pytest

NOTES_SPEC = {
    "app_name": "notes-api",
    "app_type": "python",
    "entrypoint": "app:app",
    "server": "uvicorn",
    "port": 8080,
    "health_path": "/healthz",
    "data_classification": "internal",
    "egress": [{"host": "litellm.internal", "port": 443, "reason": "model calls through the platform gateway"}],
    "env": [{"name": "NOTES_DB_URL", "secret": True}],
    "resources_tier": "default",
    "authz": {
        "roles": [{"name": "reader", "access": "read"}, {"name": "writer", "access": "write"}],
        "default": "deny",
        "rules": [
            {"methods": ["GET"], "path": "/api/**", "roles": ["reader", "writer"]},
            {"methods": ["DELETE", "PATCH", "POST", "PUT"], "path": "/api/**", "roles": ["writer"]},
        ],
    },
}

FASTAPI_APP = '''from fastapi import FastAPI

app = FastAPI()
NOTES = []


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/notes")
def list_notes():
    return NOTES


@app.post("/api/notes")
def add_note(note: dict):
    NOTES.append(note.get("text", ""))
    return {"count": len(NOTES)}
'''

HASHED_REQUIREMENTS = (
    "fastapi==0.115.0 \\\n    --hash=sha256:" + "a" * 64 + "\n"
    "uvicorn==0.30.0 \\\n    --hash=sha256:" + "b" * 64 + "\n"
)

EXPRESS_APP = """const express = require('express');
const app = express();
app.use(express.json());
const notes = [];
app.get('/healthz', (req, res) => res.json({ ok: true }));
app.get('/api/notes', (req, res) => res.json(notes));
app.post('/api/notes', (req, res) => { notes.push(String(req.body.text || '')); res.json({ count: notes.length }); });
app.listen(Number(process.env.PORT || 8080));
"""

EXPRESS_PACKAGE = '{"name": "notes-node", "version": "1.0.0", "main": "server.js", "dependencies": {"express": "^4.21.2"}}'


@pytest.fixture
def notes_spec():
    return copy.deepcopy(NOTES_SPEC)


@pytest.fixture
def make_app(tmp_path):
    def _make(files, name="app"):
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return root

    return _make


@pytest.fixture
def python_app(make_app):
    return make_app({"app.py": FASTAPI_APP, "requirements.txt": HASHED_REQUIREMENTS})


@pytest.fixture
def node_app(make_app):
    return make_app({"server.js": EXPRESS_APP, "package.json": EXPRESS_PACKAGE, "package-lock.json": "{\"lockfileVersion\": 3}"}, name="node")


@pytest.fixture
def node_spec(notes_spec):
    spec = notes_spec
    spec.update(app_type="node", entrypoint="server.js")
    del spec["server"]
    return spec


@pytest.fixture
def valid_finding():
    from contracts.findings import make_finding

    return make_finding(
        layer="deterministic",
        tool="trivy",
        rule_id="CVE-2024-0001",
        category="sca",
        severity="high",
        location={"kind": "line", "file": "requirements.txt", "start_line": 3, "end_line": 3},
        evidence=[{"file": "requirements.txt", "start_line": 3, "snippet": "jinja2==2.10"}],
        evidence_verified=True,
        title="Vulnerable jinja2",
        rationale="jinja2 2.10 has a sandbox escape.",
        fix={"summary": "Upgrade jinja2.", "steps": ["Pin jinja2>=3.1.4 and re-lock"]},
        standards=[{"framework": "cwe", "id": "CWE-1395"}],
    )

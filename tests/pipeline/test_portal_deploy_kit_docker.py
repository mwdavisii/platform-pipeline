"""The kit's Postgres really starts under its restricted context, the init script creates the four roles, and the portal's migrations
apply as portal_owner and grant each process what it needs. Opt-in: pytest -m docker"""

import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.docker, pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")]
ROOT = Path(__file__).resolve().parents[2]
PORTAL = ROOT / "bootstrap/golden-road-portal"
PW = {"POSTGRES_PASSWORD": "t-super", "PORTAL_OWNER_PASSWORD": "t-owner", "PORTAL_WEB_PASSWORD": "t-web",
      "PORTAL_WRITER_PASSWORD": "t-writer", "PORTAL_COLLECTOR_PASSWORD": "t-collector"}


def test_postgres_starts_restricted_creates_the_roles_and_takes_the_migrations(tmp_path):
    import psycopg
    docs = [d for d in yaml.safe_load_all((ROOT / "deploy-kits/portal-homelab/postgres.yaml").read_text()) if d]
    script = next(d for d in docs if d["kind"] == "ConfigMap")["data"]["10-portal-roles.sh"]
    sts = next(d for d in docs if d["kind"] == "StatefulSet")
    image = sts["spec"]["template"]["spec"]["containers"][0]["image"]
    init = tmp_path / "init"
    init.mkdir()
    (init / "10-portal-roles.sh").write_text(script)
    name = f"portal-kit-pg-{uuid.uuid4().hex[:8]}"
    env = [x for k, v in PW.items() for x in ("-e", f"{k}={v}")] + ["-e", "PGDATA=/var/lib/postgresql/data/pgdata"]
    cmd = ["docker", "run", "-d", "--rm", "--name", name, "--user", "70:70", "--read-only", "--cap-drop", "ALL",
           "--security-opt", "no-new-privileges", "--tmpfs", "/var/run/postgresql:uid=70,gid=70", "--tmpfs", "/tmp",
           "--tmpfs", "/var/lib/postgresql/data:uid=70,gid=70", "-v", f"{init}:/docker-entrypoint-initdb.d:ro", "-p", "127.0.0.1::5432",
           *env, image]
    subprocess.run(cmd, check=True, capture_output=True)
    try:
        mapped = subprocess.run(["docker", "port", name, "5432/tcp"], check=True, capture_output=True, text=True).stdout.split()[0]
        host, port = mapped.rsplit(":", 1)

        def dsn(role, password):
            return f"postgresql://{role}:{password}@{host}:{port}/portal"

        deadline = time.monotonic() + 90
        while True:
            try:  # the init script's server is local-only; this succeeds only on the real one, after the roles exist
                with psycopg.connect(dsn("portal_owner", "t-owner"), connect_timeout=2) as conn:
                    conn.execute("SELECT 1")
                break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    pytest.fail(subprocess.run(["docker", "logs", name], capture_output=True, text=True).stderr[-2000:])
                time.sleep(0.5)
        sys.path.insert(0, str(PORTAL))
        try:
            from portal.migrate import apply
        finally:
            sys.path.remove(str(PORTAL))
        assert apply(dsn("portal_owner", "t-owner")) == ["0001_core", "0002_database_enforces_the_rules", "0003_pin_the_commit_the_pr_opened_with", "0004_registration_evidence",
                                                         "0005_approval_respects_the_floor", "0006_binding_requests", "0007_binding_environment"]
        assert apply(dsn("portal_owner", "t-owner")) == []
        for role, pw, table in [("portal_web", "t-web", "session"), ("portal_writer", "t-writer", "request"), ("portal_collector", "t-collector", "repo_admin")]:
            with psycopg.connect(dsn(role, pw)) as conn:
                conn.execute(f"SELECT count(*) FROM {table}")
        with psycopg.connect(dsn("portal_collector", "t-collector")) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT draft FROM request")
        with psycopg.connect(dsn("portal_web", "t-web")) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE TABLE nope (x int)")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

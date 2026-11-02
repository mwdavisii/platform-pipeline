import json
import re
from pathlib import Path

import yaml

from helpers import repo_platform

ROOT = Path(__file__).resolve().parents[2]
PORTAL = ROOT / "bootstrap/golden-road-portal"


def test_ci_installs_the_portal_lock_with_hashes():
    steps = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]["test"]["steps"]
    run = next(s["run"] for s in steps if s.get("name") == "Run tests")
    assert "--require-hashes -r bootstrap/golden-road-portal/requirements-test.txt" in run
    assert run.index("requirements-test.txt") < run.index(".venv/bin/pytest -q\n")


def test_pytest_collects_the_portal_tests():
    import tomllib
    paths = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]["testpaths"]
    assert "bootstrap/golden-road-portal/tests" in paths


def test_the_portal_lock_pins_every_direct_dependency_with_hashes():
    names = [re.split(r"[\[<>=]", line.strip())[0].lower() for line in (PORTAL / "requirements.in").read_text().splitlines()
             if line.strip() and not line.startswith("#")]
    lock = (PORTAL / "requirements.txt").read_text().lower()
    for name in names:
        assert re.search(rf"(?m)^{re.escape(name)}(\[[^\]]*\])?==\S+ \\$", lock), name
    assert "--hash=sha256:" in lock and "uvicorn==" in lock


def test_the_portal_s_bindings_module_is_the_release_s():
    """The app page shows what the release renders: the same effects(), byte for byte (spec 2026-10-29 app bindings)."""
    assert (PORTAL / "portal/bindings.py").read_bytes() == (ROOT / "src/golden_road/bindings.py").read_bytes()


def test_the_portal_s_compose_is_the_release_s():
    """The writer writes composed.yaml with the same code deploy-check re-runs, so its PR is never stale (spec 2026-10-29 GitOps)."""
    assert (PORTAL / "portal/compose.py").read_bytes() == (ROOT / "src/golden_road/compose.py").read_bytes()


def _pin(path, package):
    found = re.findall(rf"(?mi)^{re.escape(package)}==(\S+)", path.read_text())
    assert len(found) == 1, (path, found)
    return found[0]


def test_the_builder_and_the_portal_pin_the_same_pyyaml():
    """compose.py runs in both images and must dump composed.yaml byte for byte the same, so both pin one PyYAML."""
    builder = _pin(ROOT / "images/builder/requirements.txt", "pyyaml")
    assert _pin(PORTAL / "requirements.txt", "pyyaml") == builder
    assert _pin(PORTAL / "requirements-test.txt", "pyyaml") == builder

def test_the_portal_s_vendored_schemas_are_the_contracts():
    names = ("binding.v1.json", "binding.v2.json", "builder-spec.v1.json", "deploy-release.v1.json", "finding.v2.json", "identity-link.v1.json",
             "intake.v1.json", "intake.v2.json", "re-review-event.v1.json", "registration-draft.v1.json", "registration-evidence.v1.json",
             "registration-rules.v1.json", "scorecard.v1.json", "scorecard.v2.json", "skill.v1.json")
    for name in names:
        assert (PORTAL / "portal/schemas" / name).read_bytes() == (ROOT / "src/contracts/schemas" / name).read_bytes(), name
    assert sorted(p.name for p in (PORTAL / "portal/schemas").glob("*.json")) == sorted(names)


def test_the_start_here_paste_block_is_the_agents_readme_block_word_for_word():
    import jinja2
    platform = json.loads((ROOT / "platform/platform.json").read_text())
    org, host = platform.get("org", "ORG_PLACEHOLDER"), platform.get("github_host", "github.com")
    readme = (ROOT / "bootstrap/golden-road-agents/README.md").read_text()
    block = re.search(r"## Paste block\n.*?```text\n(.*?)```", readme, re.S).group(1)
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(PORTAL / "portal/templates"), undefined=jinja2.StrictUndefined)
    rendered = env.get_template("paste_block.txt").render(org=org, github_host=host)
    assert rendered.strip() == block.strip()


def test_ci_runs_the_portal_s_docker_tests():
    steps = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]["test"]["steps"]
    run = next(s["run"] for s in steps if s.get("name") == "Run tests")
    for path in ("bootstrap/golden-road-portal/tests/test_portal_store_contract.py", "bootstrap/golden-road-portal/tests/test_portal_postgres_docker.py",
                 "tests/pipeline/test_portal_semgrep_docker.py", "bootstrap/golden-road-portal/tests/test_portal_postgres_flow_docker.py",
                 "bootstrap/golden-road-portal/tests/test_portal_postgres_rereview_docker.py",
                 "bootstrap/golden-road-portal/tests/test_portal_postgres_evidence_docker.py", "tests/pipeline/test_portal_deploy_kit_docker.py",
                 "bootstrap/golden-road-portal/tests/test_portal_postgres_cmdb_docker.py"):
        assert path in run.split("-m docker", 1)[1], path


import os  # noqa: E402
import subprocess  # noqa: E402

from helpers import builder_cmd, builder_env  # noqa: E402

TEMPLATES = ROOT / "bootstrap/golden-road-agents/skill/templates/workflows"
HELLO = ROOT / "bootstrap/hello-app"


def test_the_portal_verifies_with_the_platform_config():
    env = {**os.environ, **builder_env(), "BUILDER_PLATFORM_CONFIG": str(ROOT / "platform/platform.json")}
    out = subprocess.run([*builder_cmd(), "verify", str(PORTAL)], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stdout


def test_the_portal_callers_are_the_skill_s_templates():
    from golden_road import doctor
    callers = doctor.callers_for(repo_platform())  # central: no scorecard.yml in the portal or in the bundle
    for name in callers:
        assert (PORTAL / ".github/workflows" / name).read_text() == (TEMPLATES / name).read_text().replace("__APP_NAME__", "golden-road-portal"), name
    assert sorted(p.name for p in (PORTAL / ".github/workflows").iterdir()) == sorted(callers) == sorted(p.name for p in TEMPLATES.iterdir())


def test_the_portal_has_hello_app_s_repo_settings():
    for rel in (".github/CODEOWNERS", ".github/dependabot.yml", ".github/environment-prod.json", ".github/rulesets/main.json",
                ".github/rulesets/code-scanning.json", ".github/rulesets/scorecard-findings.json"):
        assert (PORTAL / rel).read_text() == (HELLO / rel).read_text(), rel


def test_the_portal_s_generated_authz_is_one_role_on_every_route():
    """P14: the portal enforces its own roles; the generated layer only needs the edge's one role."""
    spec = json.loads((PORTAL / "builder-spec.json").read_text())
    assert spec["entrypoint"] == "portal.main:app" and spec["server"] == "uvicorn" and spec["health_path"] == "/healthz"
    assert spec["authz"]["roles"] == [{"access": "write", "name": "portal"}]
    assert spec["authz"]["rules"] == [{"methods": ["DELETE", "GET", "PATCH", "POST", "PUT"], "path": "/**", "roles": ["portal"]}]
    secrets = {e["name"] for e in spec["env"] if e["secret"]}
    assert secrets == {"PORTAL_OIDC_CLIENT_SECRET", "PORTAL_GITHUB_OAUTH_CLIENT_SECRET", "PORTAL_DATABASE_URL"}
    assert not any("WRITE" in e["name"] for e in spec["env"]), "the web process never gets the write App's key (decision D11)"


def test_the_portal_egress_is_the_github_hosts_only():
    platform = json.loads((ROOT / "platform/platform.json").read_text())
    host = platform.get("github_host", "github.com")
    api = "api.github.com" if host == "github.com" else (f"api.{host}" if host.endswith(".ghe.com") else host)
    hosts = sorted({e["host"] for e in json.loads((PORTAL / "builder-spec.json").read_text())["egress"]})
    assert hosts == sorted({host, api})


def test_the_portal_s_own_intake_record_is_tier_high_and_loads():
    from scorecard.config import load_intake
    owner = next(p.name for p in (ROOT / "bootstrap/scorecard-factory/intake").iterdir() if p.is_dir())
    intake = load_intake(ROOT / "bootstrap/scorecard-factory", f"{owner}/golden-road-portal")
    assert intake["tier"] == "high" and intake["schema"] == "intake/v1"
    assert intake["data_classification"] == json.loads((PORTAL / "builder-spec.json").read_text())["data_classification"]


def test_the_portal_readme_records_the_p14_deviation_and_the_processes():
    text = " ".join((PORTAL / "README.md").read_text().split())
    for needle in ("P14", "ignores `X-Platform-Roles`", "python -m portal.writer", "python -m portal.collectors", "python -m portal.migrate",
                   "portal_web", "portal_writer", "portal_collector", "requirements-test.txt"):
        assert needle in text, needle


def test_the_readme_lists_the_writer_s_github_calls_including_the_ones_that_trace_a_check_to_its_pr():
    text = " ".join((PORTAL / "README.md").read_text().split())
    for needle in ("actions/runs?check_suite_id=", "/pulls/{n}/files", "/commits/{sha}/check-runs", "/pulls/{n}/merge", "/git/ref/heads/{branch}",
                   "the merge commit", "merge confirmation pending", "lists only the writer's PR"):
        assert needle in text, needle


def test_the_readme_names_the_actions_read_permission_and_the_merge_confirmation_rule():
    readme = " ".join((PORTAL / "README.md").read_text().split())
    assert "`actions` read" in readme and "never fails the request for want of an answer" in readme


def test_the_portal_declares_its_directory_settings():
    env = {e["name"]: e["secret"] for e in json.loads((PORTAL / "builder-spec.json").read_text())["env"]}
    assert env["PORTAL_REPO_ADMINS_STALE_AFTER"] is False
    assert env["PORTAL_DIRECTORY"] is False and env["PORTAL_IDENTITY_PROCESS_URL"] is False and env["PORTAL_DATA_PROCESS_URL"] is False
    deployment = (PORTAL / "deploy/base/deployment.yaml").read_text()
    assert all(f"key: {name}" in deployment for name in ("PORTAL_DIRECTORY", "PORTAL_IDENTITY_PROCESS_URL", "PORTAL_DATA_PROCESS_URL"))


def test_the_portal_declares_its_secret_server_settings():
    env = {e["name"]: e["secret"] for e in json.loads((PORTAL / "builder-spec.json").read_text())["env"]}
    assert env["PORTAL_SECRET_SERVER"] is False and env["PORTAL_SECRET_PROCESS_URL"] is False
    assert "PORTAL_SECRET_SERVER_URL" not in env  # only the mock exists; the settings refuse it
    deployment = (PORTAL / "deploy/base/deployment.yaml").read_text()
    assert all(f"key: {name}" in deployment for name in ("PORTAL_SECRET_SERVER", "PORTAL_SECRET_PROCESS_URL"))
    readme = (PORTAL / "README.md").read_text()
    assert all(f"`{name}`" in readme for name in ("PORTAL_SECRET_SERVER", "PORTAL_SECRET_PROCESS_URL", "PORTAL_SECRET_SERVER_URL"))


def test_the_portal_s_classification_module_is_the_contracts():
    """The levels, the retired names and the one-liners are one file in two places (spec 2026-10-30 C1)."""
    assert (PORTAL / "portal/classification.py").read_bytes() == (ROOT / "src/contracts/classification.py").read_bytes()


def test_the_portal_declares_its_classification_policy_setting():
    env = {e["name"]: e["secret"] for e in json.loads((PORTAL / "builder-spec.json").read_text())["env"]}
    assert env["PORTAL_CLASSIFICATION_POLICY_URL"] is False
    assert "key: PORTAL_CLASSIFICATION_POLICY_URL" in (PORTAL / "deploy/base/deployment.yaml").read_text()
    assert "`PORTAL_CLASSIFICATION_POLICY_URL`" in (PORTAL / "README.md").read_text()


def test_the_portal_validates_the_event_the_scorecard_writes():
    """The scorecard's own builder makes an event; the portal's validator (its vendored schemas and registry) accepts it, and refuses
    one the scorecard's schema refuses (spec 2026-10-31 R4)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("portal_contracts_under_test", PORTAL / "portal/contracts.py")
    portal_contracts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(portal_contracts)
    from contracts.schema import validate as contract_validate
    event = {"schema": "re-review-event/v1", "repo": "acme/notes", "pr": 7, "head_sha": "a" * 40, "tree_sha": "b" * 40,
             "scorecard_version": "0.5.1", "created": "2026-10-31T12:00:00Z", "level": "security_review", "floor": "standard",
             "complete": True, "signals": [{"id": "T-17", "kind": "trigger", "level": "elevated", "summary": "resources_tier raised", "count": 1},
                                           {"id": "egress-removed", "kind": "recorded", "summary": "egress removed: api.example.test:443"}]}
    assert contract_validate("re-review-event.v1", event) == [] and portal_contracts.validate("re-review-event.v1", event) == []
    for bad in ({**event, "head_sha": "x"}, {**event, "level": "high"}, {**event, "created": "yesterday"},
                {**event, "signals": [{"id": "T-1x", "kind": "trigger", "level": "elevated", "summary": "s", "count": 1}]}):
        assert contract_validate("re-review-event.v1", bad) and portal_contracts.validate("re-review-event.v1", bad)


def test_the_portal_declares_its_rereview_stale_setting():
    env = {e["name"]: e["secret"] for e in json.loads((PORTAL / "builder-spec.json").read_text())["env"]}
    assert env["PORTAL_REREVIEW_STALE_DAYS"] is False
    assert "key: PORTAL_REREVIEW_STALE_DAYS" in (PORTAL / "deploy/base/deployment.yaml").read_text()


def test_the_portal_readme_describes_changes_and_the_re_review_list():
    text = " ".join((PORTAL / "README.md").read_text().split())
    for needle in ("## Changing a registration", "record_changed", "record_conflict", "record_case_mismatch", "already_registered",
                   "`scorecard: central`", "## The re-review list", "`PORTAL_REVIEW_DUE_SOON_DAYS`", "`PORTAL_REREVIEW_SIGNALS`",
                   "`PORTAL_REREVIEW_STALE_DAYS`", "`PORTAL_ARTIFACT_HOSTS`", "`actions: read`", "untrusted_run",
                   "`GET /repos/{r}/actions/workflows/{workflow}/runs`", "`GET /repos/{r}/actions/runs/{id}`", "`GET /repos/{r}/actions/artifacts/{id}/zip`",
                   "without the token", "re-review-event.json", "16 MiB", "1 MiB", "0010", "anchors or aliases", "artifact_host_refused",
                   "not_a_file", "`git/ref/heads/main`"):
        assert needle in text, needle


def test_the_skills_catalog_fixture_is_the_release_s():
    """The writer's skills ConfigMap and deploy-check's are built from the same bytes (plan 2026-10-31 MCP, skills and CMDB, item 3)."""
    assert (PORTAL / "portal/mock_skills_catalog.json").read_bytes() == (ROOT / "src/golden_road/data/mock_skills_catalog.json").read_bytes()


def test_the_wheel_carries_the_catalog_fixture():
    import tomllib
    doc = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert doc["tool"]["setuptools"]["package-data"]["golden_road"] == ["data/*.json"]


NEW_0_6_0 = ("PORTAL_CLOUDFLARE", "PORTAL_MCP_GATEWAY_TEST", "PORTAL_MCP_GATEWAY_PROD", "PORTAL_MCP_PROCESS_URL", "PORTAL_SKILLS_CATALOG",
             "PORTAL_SERVICENOW", "PORTAL_ORG_NAME", "PORTAL_LIFECYCLE_GRACE_DAYS")


def test_the_portal_declares_its_0_6_0_settings():
    env = {e["name"]: e["secret"] for e in json.loads((PORTAL / "builder-spec.json").read_text())["env"]}
    for name in NEW_0_6_0:
        assert env[name] is False, name
    assert "PORTAL_CLOUDFLARE_ACCOUNT_ID" not in env and "PORTAL_SERVICENOW_URL" not in env  # only the mocks exist; the settings refuse them
    deployment = (PORTAL / "deploy/base/deployment.yaml").read_text()
    assert all(f"key: {name}" in deployment for name in NEW_0_6_0)


def test_the_portal_readme_describes_mcp_servers_skills_and_the_cmdb_record():
    text = " ".join((PORTAL / "README.md").read_text().split())
    for name in (*NEW_0_6_0, "PORTAL_CLOUDFLARE_ACCOUNT_ID", "PORTAL_SERVICENOW_URL"):
        assert f"`{name}`" in text, name
    for needle in ("## MCP servers, skills and the CMDB record (0.6.0, mock)", "cloudflare-access.yaml", "/access/ai-controls/mcp/portals",
                   "/api/now/identifyreconcile", "cmdb_unreachable", "cmdb_owner_unknown", "cmdb_refused", "0011",
                   "per-user policy is the mock's"):
        assert needle in text, needle

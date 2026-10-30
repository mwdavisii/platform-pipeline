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
    names = ("binding.v1.json", "binding.v2.json", "builder-spec.v1.json", "deploy-release.v1.json", "identity-link.v1.json", "intake.v2.json", "registration-draft.v1.json", "registration-evidence.v1.json", "registration-rules.v1.json")
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
                 "tests/pipeline/test_portal_semgrep_docker.py", "bootstrap/golden-road-portal/tests/test_portal_postgres_flow_docker.py"):
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

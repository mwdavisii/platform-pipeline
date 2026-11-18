"""The portal's platform repositories (portal/lifecycle.py PLATFORM_REPOS: never decommissioned, and the lifecycle and agent Apps must be
installed on none) are exactly the repositories the platform creates: each bootstrap/<name> that scripts/push-bootstrap.sh pushes but the
sample app hello-app, this repository (platform-pipeline) and the security-scans repository scripts/export-public-poc.sh exports."""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SAMPLE_APPS = {"hello-app"}  # an app the platform ships to show the road, not a platform repository


def _platform_repos() -> set:
    tree = ast.parse((ROOT / "bootstrap/golden-road-portal/portal/lifecycle.py").read_text())
    found = next(n.value for n in tree.body if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "PLATFORM_REPOS" for t in n.targets))
    return set(ast.literal_eval(found))


def test_platform_repos_are_the_repositories_the_platform_creates():
    pushed = {d.name for d in (ROOT / "bootstrap").iterdir() if d.is_dir()} - SAMPLE_APPS
    assert _platform_repos() == pushed | {"platform-pipeline", "security-scans"}


def test_the_export_script_exports_the_platform_and_security_scans():
    script = ROOT / "scripts/export-public-poc.sh"
    if not script.exists():  # the export leaves the script out, so a published tree has nothing to check here
        pytest.skip("scripts/export-public-poc.sh is not in an exported tree")
    exported = set(re.findall(r"--tree ([a-z-]+)\|([a-z-]+)", script.read_text())[0])
    assert exported == {"platform", "security-scans"}


def test_push_bootstrap_still_pushes_a_bootstrap_directory_by_its_name():
    script = (ROOT / "scripts/push-bootstrap.sh").read_text()
    assert 'src="$root/bootstrap/$name"' in script and 'gh repo create "$org/$name"' in script

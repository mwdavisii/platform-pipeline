"""scripts/bump-builder-version.py: a version change that leaves the tree passing its own tests, unconfigured and configured."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import json
import pytest
import yaml

from helpers import unconfigure_agents, unconfigure_factory, unconfigure_hello_app, unconfigure_platform, unconfigure_portal

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = "scripts/bump-builder-version.py"
# The test files a version change used to break (the doctor, the setup docs, the package and the hello-app lock), the kit, and the
# builder's own tests. test_export needs a git history, which a scratch copy doesn't have, and this file would recurse.
RELEVANT = ["tests/test_smoke.py", "tests/golden_road/test_doctor.py", "tests/kit", "tests/builder",
            "tests/pipeline/test_agents_setup_doc.py", "tests/pipeline/test_bootstrap.py", "tests/pipeline/test_configure.py",
            "tests/pipeline/test_scorecard_factory.py", "tests/pipeline/test_internal_docs.py", "tests/pipeline/test_portal_bootstrap.py", "tests/pipeline/test_workflows.py"]
GOOD = ["--org", "example-org", "--gateway-host", "llm.example.org", "--edge-namespace", "edge-ns", "--edge-label", "app=edge",
        "--identity-header", "X-Roles-Test", "--scorecard-gateway-url", "https://llm-ci.example.org/v1/chat/completions"]


def current_version(root=ROOT):
    return re.search(r'^VERSION = "([^"]+)"$', (root / "src/builder/__init__.py").read_text(), re.M).group(1)


def next_patch(version):
    major, minor, patch = (int(part) for part in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def make_scratch(root, source=ROOT):
    """A scratch copy of `source` in the committed, unconfigured state, whatever `source` has been configured to: the same steps as
    test_configure.py's workspace fixture. Without them configure.py refuses a tree that is already configured (an exported one)."""
    shutil.copytree(source, root, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".venv", ".claude", ".codegraph", "node_modules"))
    live = json.loads((source / "platform/platform.json").read_text())
    unconfigure_platform(root / "platform")
    unconfigure_hello_app(root / "bootstrap/hello-app")
    unconfigure_portal(root / "bootstrap/golden-road-portal")
    unconfigure_factory(root / "bootstrap/scorecard-factory", live.get("org"))
    unconfigure_agents(root / "bootstrap/golden-road-agents", live.get("org"), live.get("github_host"), live.get("actions_oidc_issuer"))
    return root


@pytest.fixture
def scratch(tmp_path):
    return make_scratch(tmp_path / "tree")


def run(root, *argv):
    return subprocess.run([sys.executable, str(root / SCRIPT), *argv], capture_output=True, text=True, cwd=root)


def snapshot(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts}


def run_relevant_tests(root):
    env = {**os.environ, "PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    paths = [p for p in RELEVANT if (root / p).exists()]
    out = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *paths], cwd=root, env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-1000:]


@pytest.mark.parametrize("version", ["0.3", "v1.0.0", "1.0.0-rc1", "", "1.0.x", "0.03.2", "01.0.0", "1.0.00"])
def test_a_malformed_version_is_refused_and_nothing_changes(scratch, version):
    before = snapshot(scratch)
    out = run(scratch, version)
    assert out.returncode != 0 and "X.Y.Z" in out.stderr
    assert snapshot(scratch) == before


def test_a_lower_version_is_refused_and_nothing_changes(scratch):
    old = current_version(scratch)
    assert run(scratch, next_patch(old)).returncode == 0
    before = snapshot(scratch)
    out = run(scratch, old)
    assert out.returncode != 0 and "lower" in out.stderr
    assert snapshot(scratch) == before


def test_the_current_version_again_changes_nothing(scratch):
    before = snapshot(scratch)
    out = run(scratch, current_version(scratch))
    assert out.returncode == 0, out.stderr
    assert snapshot(scratch) == before


def test_a_bump_changes_the_version_and_hello_app_s_lock_and_dockerfile_and_is_repeatable(scratch):
    before = snapshot(scratch)
    new = next_patch(current_version(scratch))
    out = run(scratch, new)
    assert out.returncode == 0, out.stderr
    after = snapshot(scratch)
    changed = {str(p) for p in before if after.get(p) != before[p]} | {str(p) for p in after if p not in before}
    assert changed == {"src/builder/__init__.py", "bootstrap/hello-app/.builder.lock", "bootstrap/hello-app/Dockerfile",
                       "bootstrap/golden-road-portal/.builder.lock", "bootstrap/golden-road-portal/Dockerfile"}
    assert current_version(scratch) == new
    again = run(scratch, new)
    assert again.returncode == 0 and snapshot(scratch) == after


def test_after_a_bump_an_unconfigured_tree_still_passes_its_tests(scratch):
    assert run(scratch, next_patch(current_version(scratch))).returncode == 0
    run_relevant_tests(scratch)


def test_after_a_bump_a_tree_configured_with_the_sample_config_still_passes_its_tests(scratch):
    configure = subprocess.run([sys.executable, str(scratch / "scripts/configure.py"), "--root", str(scratch), *GOOD], capture_output=True, text=True)
    assert configure.returncode == 0, configure.stderr
    configured = snapshot(scratch)
    new = next_patch(current_version(scratch))
    out = run(scratch, new)
    assert out.returncode == 0, out.stderr
    after = snapshot(scratch)
    changed = {str(p) for p in configured if after.get(p) != configured[p]}
    assert changed == {"src/builder/__init__.py", "bootstrap/hello-app/.builder.lock", "bootstrap/hello-app/Dockerfile",
                       "bootstrap/golden-road-portal/.builder.lock", "bootstrap/golden-road-portal/Dockerfile"}
    assert b"example-org" in (scratch / "bootstrap/hello-app/.github/workflows/ci.yml").read_bytes()
    run_relevant_tests(scratch)


def test_a_tree_configured_for_central_review_passes_the_relevant_tests_too(scratch):
    """Central mode removes the scorecard callers and pins `scorecard / assessed` to the App; each test asserts the mode its tree is in."""
    configure = subprocess.run([sys.executable, str(scratch / "scripts/configure.py"), "--root", str(scratch), *GOOD, "--scorecard-mode", "central",
                                "--scorecard-app-id", "1234567"], capture_output=True, text=True)
    assert configure.returncode == 0, configure.stderr
    assert not (scratch / "bootstrap/hello-app/.github/workflows/scorecard.yml").exists()
    assert json.loads((scratch / "platform/platform.json").read_text())["scorecard_app_id"] == 1234567
    run_relevant_tests(scratch)


def test_the_scratch_copy_is_unconfigured_even_when_the_tree_it_copies_is_configured(tmp_path):
    """Regression: an exported tree is configured, and configure.py refuses to run in one. Both variants must start from a copy that is not."""
    pristine = make_scratch(tmp_path / "pristine")
    configured = make_scratch(tmp_path / "configured-source")
    done = subprocess.run([sys.executable, str(configured / "scripts/configure.py"), "--root", str(configured), *GOOD], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert b"example-org" in (configured / "bootstrap/hello-app/.github/workflows/ci.yml").read_bytes()
    again = make_scratch(tmp_path / "again", source=configured)
    for rel in ("platform", "bootstrap/hello-app", "bootstrap/golden-road-portal", "bootstrap/golden-road-agents", "bootstrap/scorecard-factory"):
        left, right = snapshot(pristine / rel), snapshot(again / rel)
        assert left == right, [str(p) for p in set(left) | set(right) if left.get(p) != right.get(p)][:5]
    # and both variants of the bump test work on it
    assert run(again, next_patch(current_version(again))).returncode == 0
    second = subprocess.run([sys.executable, str(again / "scripts/configure.py"), "--root", str(again), *GOOD], capture_output=True, text=True)
    assert second.returncode == 0, second.stderr


def _break_hello_app_spec(root, how):
    spec = root / "bootstrap/hello-app/builder-spec.json"
    if how == "not json":  # raises ValueError, not BuilderError
        spec.write_text("{not json")
    else:  # the builder refuses it
        doc = json.loads(spec.read_text())
        doc["data_classification"] = "not-a-classification"
        spec.write_text(json.dumps(doc))


@pytest.mark.parametrize("how", ["not json", "invalid spec"])
def test_a_failing_regeneration_leaves_the_version_and_everything_else_as_it_was(scratch, how):
    _break_hello_app_spec(scratch, how)
    before = snapshot(scratch)
    out = run(scratch, next_patch(current_version(scratch)))
    assert out.returncode != 0
    assert snapshot(scratch) == before


def test_every_doc_version_line_names_a_tag_the_release_workflow_accepts(scratch):
    """Run each doc's own `version=` line on the tree and the release workflow's own tag check on the tag it makes."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/release-builder.yml").read_text())
    [check] = [step["run"] for job in workflow["jobs"].values() for step in job["steps"] if "Require the tag" in step.get("name", "")]

    def accepts(root, tag):
        return subprocess.run(["bash", "-c", check], cwd=root, env={**os.environ, "GITHUB_REF_NAME": tag}, capture_output=True, text=True).returncode == 0

    path = ROOT / "docs/setup/personal-poc.md"
    lines = [l for l in path.read_text().splitlines() if l.startswith("version=$(")]
    assert len(lines) >= 3, "steps 3 and 13.2 and 14.0 each read the version"
    for line in lines:
        out = subprocess.run(["bash", "-c", f'{line}\nprintf "%s" "$version"'], cwd=ROOT, capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        assert out.stdout == current_version() and accepts(ROOT, f"builder-v{out.stdout}") and not accepts(ROOT, f"builder-v{out.stdout}x")
    # the bump path (steps 11 and 14.5 and the link change): the tag is builder-v<new version>
    new = next_patch(current_version(scratch))
    assert run(scratch, new).returncode == 0 and accepts(scratch, f"builder-v{new}") and not accepts(scratch, f"builder-v{current_version()}")

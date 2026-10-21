"""configure.py hardening, exercised against a synthetic bootstrap (hello-app itself lands in Task 6)."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from helpers import unconfigure_platform

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/builder/fixtures/python-fastapi"


@pytest.fixture
def workspace(tmp_path):
    shutil.copytree(ROOT / "platform", tmp_path / "platform")
    unconfigure_platform(tmp_path / "platform")
    app = tmp_path / "bootstrap/hello-app"
    shutil.copytree(FIXTURE, app)
    (app / ".github/workflows").mkdir(parents=True)
    (app / ".github/workflows/ci.yml").write_text("uses: ORG_PLACEHOLDER/platform-pipeline\n")
    (app / "blob.json").write_bytes(b"\xff\xfe ORG_PLACEHOLDER")
    return tmp_path


def configure(workspace, *args):
    return subprocess.run([sys.executable, str(ROOT / "scripts/configure.py"), "--root", str(workspace), *args],
                          capture_output=True, text=True)


EDGE = ["--edge-namespace", "edge-ns", "--edge-label", "app=edge", "--identity-header", "X-Roles-Test"]
GOOD = ["--org", "example-org", "--gateway-host", "llm.example.org", *EDGE]


def snapshot(workspace):
    return {p.relative_to(workspace).as_posix(): p.read_bytes() for p in sorted(workspace.rglob("*")) if p.is_file()}


def test_binary_text_file_is_refused_and_nothing_is_written(workspace):
    before = snapshot(workspace)
    out = configure(workspace, *GOOD)
    assert out.returncode != 0 and "blob.json" in out.stderr
    assert snapshot(workspace) == before


def test_spec_problem_is_found_before_any_write(workspace):
    (workspace / "bootstrap/hello-app/blob.json").unlink()
    (workspace / "bootstrap/hello-app/app.py").unlink()  # entrypoint missing: check_spec fails
    before = snapshot(workspace)
    assert configure(workspace, *GOOD).returncode != 0
    assert snapshot(workspace) == before


def test_any_file_type_is_rewritten(workspace):
    (workspace / "bootstrap/hello-app/blob.json").unlink()
    app = workspace / "bootstrap/hello-app"
    (app / "CODEOWNERS").write_text("* @ORG_PLACEHOLDER/platform\n")
    (app / "conf.toml").write_text('org = "ORG_PLACEHOLDER"\n')
    assert configure(workspace, *GOOD).returncode == 0
    assert (app / "CODEOWNERS").read_text() == "* @example-org/platform\n"
    assert (app / "conf.toml").read_text() == 'org = "example-org"\n'


def test_crlf_is_preserved(workspace):
    (workspace / "bootstrap/hello-app/blob.json").unlink()
    ci = workspace / "bootstrap/hello-app/.github/workflows/ci.yml"
    ci.write_bytes(b"a: ORG_PLACEHOLDER\r\nb: 1\r\n")
    assert configure(workspace, *GOOD).returncode == 0
    assert ci.read_bytes() == b"a: example-org\r\nb: 1\r\n"


def test_rerun_same_org_changes_gateway(workspace):
    (workspace / "bootstrap/hello-app/blob.json").unlink()
    assert configure(workspace, *GOOD).returncode == 0
    out = configure(workspace, "--org", "example-org", "--gateway-host", "llm2.example.org", *EDGE)
    assert out.returncode == 0, out.stderr
    spec = json.loads((workspace / "bootstrap/hello-app/builder-spec.json").read_text())
    assert spec["egress"][0]["host"] == "llm2.example.org"


def test_rerun_different_org_fails(workspace):
    (workspace / "bootstrap/hello-app/blob.json").unlink()
    assert configure(workspace, *GOOD).returncode == 0
    before = snapshot(workspace)
    out = configure(workspace, "--org", "other-org", "--gateway-host", "llm.example.org", *EDGE)
    assert out.returncode != 0
    assert "bootstrap already configured for org example-org; restore with git checkout -- bootstrap platform to change it" in out.stderr
    assert snapshot(workspace) == before


def test_no_placeholders_and_no_recorded_org_fails(workspace):
    (workspace / "bootstrap/hello-app/blob.json").unlink()
    ci = workspace / "bootstrap/hello-app/.github/workflows/ci.yml"
    ci.write_text("uses: someone/platform-pipeline\n")
    assert configure(workspace, *GOOD).returncode != 0


def test_root_platform_must_be_real_directory(workspace, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere") / "platform"
    shutil.move(workspace / "platform", elsewhere)
    os.symlink(elsewhere, workspace / "platform")
    assert configure(workspace, *GOOD).returncode != 0


def test_fills_and_regenerates(workspace):
    (workspace / "bootstrap/hello-app/blob.json").unlink()
    out = configure(workspace, *GOOD)
    assert out.returncode == 0, out.stderr
    assert json.loads((workspace / "platform/platform.json").read_text())["model_gateway_hosts"] == ["llm.example.org"]
    assert "example-org/platform-pipeline" in (workspace / "bootstrap/hello-app/.github/workflows/ci.yml").read_text()
    assert (workspace / "bootstrap/hello-app/Dockerfile").is_file()


def test_symlinks_are_never_followed(workspace, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "victim.yml").write_text("ORG_PLACEHOLDER\n")
    (outside / "deep").mkdir()
    (outside / "deep/other.yml").write_text("ORG_PLACEHOLDER\n")
    other = workspace / "bootstrap/other"
    other.mkdir()
    (workspace / "bootstrap/hello-app/blob.json").unlink()
    os.symlink(outside / "victim.yml", other / "link.yml")
    os.symlink(outside / "deep", other / "linkdir")
    out = configure(workspace, *GOOD)
    assert out.returncode == 0, out.stderr
    assert (outside / "victim.yml").read_text() == "ORG_PLACEHOLDER\n"
    assert (outside / "deep/other.yml").read_text() == "ORG_PLACEHOLDER\n"


def test_symlinked_platform_file_is_refused(workspace, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "p.json"
    shutil.copy(workspace / "platform/platform.json", outside)
    (workspace / "platform/platform.json").unlink()
    os.symlink(outside, workspace / "platform/platform.json")
    before = outside.read_text()
    assert configure(workspace, *GOOD).returncode != 0
    assert outside.read_text() == before


@pytest.mark.parametrize("args", [
    ["--org", "ok\n", "--gateway-host", "llm.example.org"],
    ["--org", "ok", "--gateway-host", "llm.example.org\n"],
    ["--org", "ok", "--gateway-host", "api.openai.com"],
    ["--org", "ok", "--gateway-host", "my.openai.azure.com"],
    ["--org", "ok", "--gateway-host", "metadata.google.internal"],
    ["--org", "ok", "--gateway-host", "LLM.example.org"],
    ["--org", "ok", "--gateway-host", "llm.example.org", "--edge-namespace", "Bad_NS"],
    ["--org", "ok", "--gateway-host", "llm.example.org", "--edge-label", "a=b\n"],
    ["--org", "-ok", "--gateway-host", "llm.example.org"],
    ["--org", "a--b", "--gateway-host", "llm.example.org"],
    ["--org", "ok-", "--gateway-host", "llm.example.org"],
    ["--org", "a" * 40, "--gateway-host", "llm.example.org"],
    ["--org", "ok", "--gateway-host", "a" * 64 + ".example.org"],
    ["--org", "ok", "--gateway-host", "llm.example.org", "--edge-label", "a=-b"],
    ["--org", "ok", "--gateway-host", "llm.example.org", "--edge-label", "a=b-"],
    ["--org", "ok", "--gateway-host", "llm.example.org", "--edge-label", "Bad Prefix/a=b"],
])
def test_rejects_bad_values_without_touching_files(workspace, args):
    if "--edge-namespace" not in args:
        args = [*args, "--edge-namespace", "edge-ns"]
    if "--edge-label" not in args:
        args = [*args, "--edge-label", "app=edge"]
    args = [*args, "--identity-header", "X-Roles-Test"]
    before = (workspace / "platform/platform.json").read_text()
    assert configure(workspace, *args).returncode != 0
    assert (workspace / "platform/platform.json").read_text() == before
    assert "ORG_PLACEHOLDER" in (workspace / "bootstrap/hello-app/.github/workflows/ci.yml").read_text()

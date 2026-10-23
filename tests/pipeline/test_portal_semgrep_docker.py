"""The portal is held to the bar it reports on (P9): the security team's vendored semgrep rules find nothing in its tree."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCANS = ROOT / "reference/security-scans"
pytestmark = [pytest.mark.docker, pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed"),
              pytest.mark.skipif(not SCANS.is_dir(), reason="the platform export does not carry reference/security-scans")]


def _image():
    return re.search(r"SEMGREP_IMAGE: (\S+)", (SCANS / ".github/workflows/scan.yml").read_text()).group(1)


def test_the_portal_tree_has_no_semgrep_error(tmp_path):
    shutil.copytree(ROOT / "bootstrap/golden-road-portal", tmp_path / "src", ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    env = {**os.environ, "SEMGREP_IMAGE": _image(), "REGISTRY_RULESETS": ""}
    out = subprocess.run(["bash", str(SCANS / "scripts/run_semgrep.sh")], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    report = subprocess.run([sys.executable, str(SCANS / "scripts/sarif_report.py"), "--tool", "semgrep", "--fail-on", "error", "--root",
                             str(tmp_path / "src"), "--markdown", str(tmp_path / "report.md"), str(tmp_path / "out/semgrep.sarif")],
                            capture_output=True, text=True)
    shown = (tmp_path / "report.md").read_text() if (tmp_path / "report.md").exists() else ""
    assert report.returncode == 0, report.stdout + report.stderr + shown

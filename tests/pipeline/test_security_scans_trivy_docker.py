"""Opt-in: run the pinned Trivy (repository scan, then the built image) on bootstrap/hello-app with scan.yml's own settings.

If the platform-pinned base image gets a fixed HIGH or CRITICAL, `security-scans / trivy` fails for every app, and the app
team cannot fix it. This test is the early warning: run it before step 5 of the setup doc, and after any builder bump.

Opt in with GOLDEN_ROAD_SCANS_TRIVY=1 (and `-m docker`). The host network is used ONLY to download the vulnerability
database and checks; the scans themselves run with --network none. Building the image needs the package index: set
GOLDEN_ROAD_DOCKER_BUILD_NETWORK=host where the default bridge does not work.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCANS = ROOT / "reference/security-scans"
APP = ROOT / "bootstrap/hello-app"
pytestmark = [pytest.mark.docker,
              pytest.mark.skipif(shutil.which("docker") is None or os.environ.get("GOLDEN_ROAD_SCANS_TRIVY") != "1",
                                 reason="opt-in: set GOLDEN_ROAD_SCANS_TRIVY=1 (needs docker and network access for the database download)"),
              pytest.mark.skipif(not SCANS.is_dir(), reason="the platform export does not carry reference/security-scans")]


def workflow_env():
    return yaml.safe_load((SCANS / ".github/workflows/scan.yml").read_text())["env"]


def test_hello_app_passes_the_pinned_trivy_with_the_workflows_settings(tmp_path):
    env = workflow_env()
    cache = tmp_path / "cache"
    cache.mkdir(mode=0o777)
    cache.chmod(0o777)
    prime = tmp_path / "prime"
    prime.mkdir()
    (prime / "Dockerfile").write_text("FROM scratch\n")
    (prime / "requirements.txt").write_text("fastapi==0.115.0\n")
    primed = subprocess.run(["docker", "run", "--rm", "--network", "host", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp", "-v", f"{cache}:/cache",
                             "-v", f"{prime}:/src:ro", env["TRIVY_IMAGE"], "fs", "--cache-dir", "/cache", "--scanners", "vuln,misconfig", "--format", "json",
                             "--output", "/dev/null", "/src"], capture_output=True, text=True)
    assert primed.returncode == 0, primed.stderr[-1500:]

    work = tmp_path / "work"
    work.mkdir()
    shutil.copytree(APP, work / "src")
    run_env = {**os.environ, "TRIVY_IMAGE": env["TRIVY_IMAGE"], "IGNORE_UNFIXED": env["IGNORE_UNFIXED"], "SCANS_TRIVY_CACHE": str(cache),
               "SCANS_TRIVY_OFFLINE": "true", "GITHUB_OUTPUT": str(work / "gh_output")}
    if os.environ.get("GOLDEN_ROAD_DOCKER_BUILD_NETWORK"):
        run_env["SCANS_BUILD_NETWORK"] = os.environ["GOLDEN_ROAD_DOCKER_BUILD_NETWORK"]
    for script, *args in (("run_trivy.sh", "fs"), ("build_image.sh",), ("run_trivy.sh", "image")):
        out = subprocess.run(["bash", str(SCANS / "scripts" / script), *args], cwd=work, env=run_env, capture_output=True, text=True)
        assert out.returncode == 0, f"{script}: {out.stdout[-1500:]}{out.stderr[-1500:]}"
    assert "built=true" in (work / "gh_output").read_text()
    report = subprocess.run([sys.executable, str(SCANS / "scripts/sarif_report.py"), "--tool", "trivy", "--root", str(work / "src"), "--fail-on", "error",
                             "--annotate", "--outside-label", "container image", str(work / "out/trivy-fs.sarif"), str(work / "out/trivy-image.sarif")],
                            capture_output=True, text=True)
    assert report.returncode == 0, ("a fixed HIGH or CRITICAL is now in hello-app or its pinned base image; the platform must bump the builder:\n"
                                    + report.stdout[:3000])

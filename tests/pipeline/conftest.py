import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILDER_TAG = "golden-road-builder:test"


@pytest.fixture(scope="session")
def builder_image():
    cmd = ["docker", "build", "-q", "-f", "images/builder/Dockerfile", "-t", BUILDER_TAG]
    if os.environ.get("GOLDEN_ROAD_DOCKER_BUILD_NETWORK"):
        cmd += ["--network", os.environ["GOLDEN_ROAD_DOCKER_BUILD_NETWORK"]]
    subprocess.run([*cmd, "."], cwd=ROOT, check=True, capture_output=True)
    yield BUILDER_TAG
    subprocess.run(["docker", "rmi", "-f", BUILDER_TAG], capture_output=True)


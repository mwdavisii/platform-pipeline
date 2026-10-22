"""Fixtures for the kit's scripts: the configured kit, and the fake gh and uv first on PATH with HOME in a temp folder."""

import os
import shutil

import pytest

from kit_fakes import AGENTS, FAKE_GH, FAKE_UV, ORG, PLACEHOLDER, World


@pytest.fixture
def kit(tmp_path):
    """bootstrap/golden-road-agents as configure.py leaves it for ORG."""
    dest = tmp_path / "golden-road-agents"
    shutil.copytree(AGENTS, dest, ignore=shutil.ignore_patterns("__pycache__"))
    for path in dest.rglob("*"):
        if path.is_file() and PLACEHOLDER.encode() in path.read_bytes():
            path.write_bytes(path.read_bytes().replace(PLACEHOLDER.encode(), ORG.encode()))
    return dest


@pytest.fixture
def world(tmp_path, monkeypatch):
    fake = World(tmp_path)
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    for name, text in (("gh", FAKE_GH), ("uv", FAKE_UV)):
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    fake.home = tmp_path / "home"
    fake.home.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("HOME", str(fake.home))
    monkeypatch.setenv("FAKE_WORLD", str(fake.path))
    monkeypatch.setenv("FAKE_LOG", str(fake.log))
    for name in ("GH_HOST", "GH_REPO", "GH_DEBUG", "DEBUG", "GOLDEN_ROAD_SKILL_VERSION", "BUILDER_PLATFORM_CONFIG", "VIRTUAL_ENV", "PYTHONPATH",
                 *(k for k in os.environ if k.startswith("UV_"))):
        monkeypatch.delenv(name, raising=False)
    return fake

import re
import tomllib
from pathlib import Path

import builder
import contracts

ROOT = Path(__file__).resolve().parents[1]


def test_packages_import():
    assert re.fullmatch(r"\d+\.\d+\.\d+", builder.VERSION)  # the setup docs bump it before a builder re-release
    assert contracts.__doc__


def test_package_version_is_the_builder_version():
    """The image and the wheel are versioned by builder.VERSION, and the builder-v* tag must equal it."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "version" not in project["project"] and project["project"]["dynamic"] == ["version"]
    assert project["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "builder.VERSION"}

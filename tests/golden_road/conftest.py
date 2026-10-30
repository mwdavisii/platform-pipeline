import shutil
from pathlib import Path

import pytest

GOLDEN = Path(__file__).resolve().parents[1] / "builder/golden/python-fastapi"


@pytest.fixture
def app(tmp_path):
    """A copy of the python-fastapi golden app (its image name is notes-api), as tests/golden_road/test_render.py renders it."""
    shutil.copytree(GOLDEN, tmp_path / "golden-app")
    return tmp_path / "golden-app"

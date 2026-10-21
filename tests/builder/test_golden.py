import json
import os
import shutil
from pathlib import Path

import pytest

from builder.generate import generate

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"
GOLDEN = HERE / "golden"
NAMES = ["python-fastapi", "node-express", "static-site"]


@pytest.mark.parametrize("name", NAMES)
def test_generate_matches_golden(name, tmp_path, monkeypatch):
    monkeypatch.delenv("BUILDER_PLATFORM_CONFIG", raising=False)
    root = tmp_path / name
    shutil.copytree(FIXTURES / name, root, ignore=shutil.ignore_patterns("node_modules", "__pycache__"))
    result = generate(root, json.loads((root / "builder-spec.json").read_text(encoding="utf-8")))
    produced = {rel: (root / rel).read_text(encoding="utf-8") for rel in result["files"]}

    golden_dir = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        shutil.rmtree(golden_dir, ignore_errors=True)
        for rel, text in produced.items():
            path = golden_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")

    expected = {p.relative_to(golden_dir).as_posix(): p.read_text(encoding="utf-8") for p in sorted(golden_dir.rglob("*")) if p.is_file()}
    assert produced == expected

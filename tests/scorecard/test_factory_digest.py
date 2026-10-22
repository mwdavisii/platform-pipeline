"""`scorecard factory-digest`: one SHA-256 over the factory inputs a run read, so the assess and score jobs can tell whether they saw the
same ones (each downloads the factory at main on its own, and a merge could land between them)."""

import json
import shutil
import re

import pytest

from golden_road.io import InputError
from scorecard.cli import main
from scorecard.factory_digest import factory_digest


def make(root, files):
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data if isinstance(data, bytes) else data.encode())
    return root


BASE = {"config/scorecard.yaml": "config\n", "intake/o/app.yaml": "intake\n", "na/o/app.yaml": "na\n", "suppressions/o/app/s.json": "{}\n"}


def test_the_digest_is_a_sha256_and_does_not_depend_on_creation_order_or_the_path(tmp_path):
    a = make(tmp_path / "a", BASE)
    b = make(tmp_path / "elsewhere/b", dict(reversed(list(BASE.items()))))
    assert re.fullmatch(r"[0-9a-f]{64}", factory_digest(a)) and factory_digest(a) == factory_digest(b)


@pytest.mark.parametrize("change", [
    lambda r: (r / "config/scorecard.yaml").write_text("config!\n"),
    lambda r: (r / "intake/o/app.yaml").rename(r / "intake/o/other.yaml"),
    lambda r: (r / "na/o/extra.yaml").write_text("x"),
    lambda r: (r / "suppressions/o/app/s.json").unlink(),
    lambda r: (r / "intake/o/app.yaml").rename(r / "na/o/app2.yaml"),
], ids=["changed-byte", "renamed", "added", "removed", "moved-between-kinds"])
def test_any_change_to_a_name_a_byte_or_the_set_of_files_changes_the_digest(tmp_path, change):
    root = make(tmp_path / "f", BASE)
    before = factory_digest(root)
    change(root)
    assert factory_digest(root) != before


def test_names_and_contents_are_length_prefixed_so_they_cannot_run_together(tmp_path):
    one = make(tmp_path / "one", {"config/scorecard.yaml": "x", "intake/o/a": "bc"})
    two = make(tmp_path / "two", {"config/scorecard.yaml": "x", "intake/o/ab": "c"})
    assert factory_digest(one) != factory_digest(two)


def test_missing_directories_and_empty_ones_are_fine(tmp_path):
    root = make(tmp_path / "f", {"config/scorecard.yaml": "c"})
    before = factory_digest(root)
    (root / "intake/o").mkdir(parents=True)
    assert factory_digest(root) == before


@pytest.mark.parametrize("link", ["config/scorecard.yaml", "intake/o/app.yaml", "na/o", "suppressions"])
def test_a_symlink_anywhere_is_refused(tmp_path, link):
    root = make(tmp_path / "f", BASE)
    target = tmp_path / "target"
    target.mkdir()
    (target / "x").write_text("x")
    path = root / link
    if path.is_dir() and not path.is_symlink():
        import shutil
        shutil.rmtree(path)
    else:
        path.unlink()
    path.symlink_to(target if link in ("na/o", "suppressions") else target / "x")
    with pytest.raises(InputError, match="symlink"):
        factory_digest(root)


def test_other_top_level_files_do_not_count(tmp_path):
    root = make(tmp_path / "f", {**BASE, ".github/workflows/x.yml": "a", "README.md": "r"})
    assert factory_digest(root) == factory_digest(make(tmp_path / "g", BASE))


def test_the_cli_prints_the_digest_and_refuses_a_missing_config(tmp_path, capsys):
    root = make(tmp_path / "f", BASE)
    assert main(["factory-digest", "--factory", str(root)]) == 0
    assert json.loads(capsys.readouterr().out) == {"sha256": factory_digest(root)}
    (root / "config/scorecard.yaml").unlink()
    assert main(["factory-digest", "--factory", str(root)]) == 1
    assert "scorecard.yaml" in capsys.readouterr().err


def test_a_file_name_that_is_not_utf8_is_an_input_error_not_a_crash(tmp_path):
    import os

    root = make(tmp_path / "f", BASE)
    with open(os.path.join(os.fsencode(root), b"intake/o/bad-\xff.yaml"), "wb") as handle:
        handle.write(b"x")
    with pytest.raises(InputError, match="UTF-8"):
        factory_digest(root)


def test_check_factory_reports_a_file_name_that_is_not_utf8(tmp_path, capsys):
    import os

    root = tmp_path / "f"
    shutil.copytree(__import__("pathlib").Path(__file__).parent / "fixtures/factory", root)
    with open(os.path.join(os.fsencode(root), b"intake/acme/bad-\xff.yaml"), "wb") as handle:
        handle.write(b"x")
    assert main(["check-factory", "--factory", str(root)]) == 1
    err = json.loads(capsys.readouterr().err)
    assert any("UTF-8" in p for p in err["problems"])

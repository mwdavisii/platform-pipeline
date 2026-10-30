"""check-factory refuses every file under bindings/ (bindings live per environment in golden-road-deploy from 0.4.0), except a README.md."""

import json

import pytest

from scorecard.cli import main

MOVED = "bindings moved to golden-road-deploy (apps/<owner>/<repo>/<env>/bindings.yaml)"


def check(root, capsys):
    code = main(["check-factory", "--factory", str(root)])
    return code, capsys.readouterr()


def test_a_binding_file_is_refused(factory, capsys):
    (factory / "bindings/acme").mkdir(parents=True)
    (factory / "bindings/acme/notes.yaml").write_text("schema: binding/v1\n")
    code, out = check(factory, capsys)
    problems = json.loads(out.err)["problems"]
    assert code != 0 and [p for p in problems if p.startswith("bindings/acme/notes.yaml: " + MOVED)]


@pytest.mark.parametrize("path", ["bindings/notes.yaml", "bindings/acme/README.md", "bindings/acme/.gitkeep", "bindings/acme/deep/x.txt"])
def test_any_file_other_than_the_top_readme_is_refused(factory, capsys, path):
    target = factory / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x\n")
    code, out = check(factory, capsys)
    assert code != 0 and f"{path}: {MOVED}" in out.err


def test_a_readme_directly_under_bindings_alone_passes(factory, capsys):
    (factory / "bindings").mkdir()
    (factory / "bindings/README.md").write_text("# Bindings\n")
    code, out = check(factory, capsys)
    assert code == 0 and "bindings" not in json.loads(out.out.strip().splitlines()[-1])


def test_no_bindings_directory_passes_and_the_counts_drop_bindings(factory, capsys):
    code, out = check(factory, capsys)
    assert code == 0 and "bindings" not in json.loads(out.out.strip().splitlines()[-1])


def test_a_symlinked_binding_is_refused(factory, capsys, tmp_path):
    target = tmp_path / "elsewhere.yaml"
    target.write_text("x: 1\n")
    (factory / "bindings/acme").mkdir(parents=True)
    (factory / "bindings/acme/notes.yaml").symlink_to(target)
    code, out = check(factory, capsys)
    assert code != 0 and "symlink" in out.err and MOVED in out.err


def test_a_bindings_entry_that_is_a_regular_file_is_refused(factory, capsys):
    (factory / "bindings").write_text("schema: binding/v1\n")
    code, out = check(factory, capsys)
    assert code != 0 and f"bindings: {MOVED}" in out.err

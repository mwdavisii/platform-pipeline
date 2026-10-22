import pytest

from golden_road.io import InputError
from scorecard import git


def test_changes_show_and_added_lines(git_repo):
    base = git_repo.commit({"a.py": "one\ntwo\n", "gone.py": "x\n"})
    head = git_repo.commit({"a.py": "one\nTWO\nthree\n", "gone.py": None, "new dir/b.py": "b\n"})
    assert git.rev(git_repo.root, "HEAD") == head
    assert git.changes(git_repo.root, base, head) == [
        {"status": "M", "path": "a.py"}, {"status": "D", "path": "gone.py"}, {"status": "A", "path": "new dir/b.py"},
    ]
    assert git.show(git_repo.root, base, "gone.py") == "x\n"
    assert git.show(git_repo.root, head, "gone.py") is None
    assert git.added_lines(git_repo.root, base, head, "a.py") == [(2, "TWO"), (3, "three")]
    assert len(git.tree(git_repo.root, head)) == 40


def test_refs_that_look_like_options_are_refused(git_repo):
    git_repo.commit({"a": "a\n"})
    with pytest.raises(InputError):
        git.rev(git_repo.root, "--output=/tmp/x")
    with pytest.raises(InputError):
        git.changes(git_repo.root, "HEAD", "HEAD")


def test_gitattributes_cannot_run_a_diff_driver(git_repo, tmp_path):
    marker = tmp_path / "ran"
    git_repo.git("config", "diff.evil.textconv", f"touch {marker}; cat")
    base = git_repo.commit({".gitattributes": "*.py diff=evil\n", "a.py": "1\n"})
    head = git_repo.commit({"a.py": "2\n"})
    assert git.added_lines(git_repo.root, base, head, "a.py") == [(1, "2")]
    assert not marker.exists()


def test_attributes_and_nul_bytes_cannot_hide_added_lines(git_repo):
    base = git_repo.commit({".gitattributes": "*.py -diff\n*.dat binary\n", "a.py": "x = 1\n", "b.dat": "a\n"})
    head = git_repo.commit({"a.py": "x = 1\n@pytest.mark.skip\n", "b.dat": "a\n\0@pytest.mark.skip\n"})
    assert (2, "@pytest.mark.skip") in git.added_lines(git_repo.root, base, head, "a.py")
    assert any("@pytest.mark.skip" in text for _, text in git.added_lines(git_repo.root, base, head, "b.dat"))


def test_show_distinguishes_absent_from_unreadable(git_repo, monkeypatch):
    head = git_repo.commit({"big.txt": "x" * 50, "d/inner": "y\n"})
    assert git.show(git_repo.root, head, "missing.txt") is None
    monkeypatch.setattr(git, "MAX_BLOB_BYTES", 10)
    with pytest.raises(git.Unreadable):
        git.show(git_repo.root, head, "big.txt")
    with pytest.raises(git.Unreadable):  # a directory is present but not a file
        git.show(git_repo.root, head, "d")


def test_inherited_git_environment_is_ignored(git_repo, monkeypatch, tmp_path):
    head = git_repo.commit({"a": "a\n"})
    monkeypatch.setattr(git, "_ENV", {**git._ENV})  # the module snapshot is what counts
    assert not any(k.startswith("GIT_") and k not in {"GIT_LITERAL_PATHSPECS", "GIT_TERMINAL_PROMPT", "GIT_OPTIONAL_LOCKS",
                                                      "GIT_CONFIG_NOSYSTEM"} for k in git._ENV)
    assert git.rev(git_repo.root, "HEAD") == head


def test_non_utf8_paths_round_trip(git_repo):
    import os

    base = git_repo.commit({"a": "a\n"})
    name = os.fsdecode(b"caf\xe9.txt")
    (git_repo.root / name).write_text("hi\n")
    git_repo.git("add", "-A")
    head = git_repo.commit()
    changed = git.changes(git_repo.root, base, head)
    assert [c["path"] for c in changed] == [name]
    assert git.show(git_repo.root, head, name) == "hi\n"


def test_diff_is_the_unified_diff_between_commits(git_repo):
    base = git_repo.commit({"a.py": "x = 1\n", ".gitattributes": "*.py -diff\n"})
    head = git_repo.commit({"a.py": "x = 2\n"})
    text = git.diff(git_repo.root, base, head)
    assert "-x = 1\n+x = 2" in text and "Binary files" not in text


def test_diff_capped_stops_streaming_at_the_cap(git_repo):
    base = git_repo.commit({"a.py": "x = 1\n"})
    head = git_repo.commit({"a.py": "".join(f"line_{n} = {n}\n" for n in range(20000))})
    text, cut = git.diff_capped(git_repo.root, base, head, 5000)
    assert cut is True and len(text.encode()) == 5000
    whole, cut = git.diff_capped(git_repo.root, base, head)
    assert cut is False and len(whole) > 100_000 and git.diff_capped(git_repo.root, base, head, 10**7) == (whole, False)

from golden_road.io import InputError
from scorecard import git
from scorecard.signals.size import change_size


def test_change_size_adds_added_and_removed_lines_and_counts_a_binary_file_as_one_file_and_no_lines(git_repo):
    base = git_repo.commit({"a.py": "one\ntwo\n"})
    head = git_repo.commit({"a.py": "one\nTWO\nthree\n", "blob.bin": "\x00\x01\n"})
    problems = []
    assert change_size(git_repo.root, base, head, problems) == {"lines": 3, "files": 2}
    assert problems == []


def test_a_comparison_that_fails_is_an_input_problem_and_no_size(git_repo, monkeypatch):
    base = git_repo.commit({"a.py": "one\n"})
    head = git_repo.commit({"a.py": "two\n"})

    def boom(*args, **kwargs):
        raise InputError("git diff failed: boom")

    monkeypatch.setattr(git, "numstat", boom)
    problems = []
    assert change_size(git_repo.root, base, head, problems) is None
    assert problems == [{"source": "change size", "detail": "can't count the changed lines: git diff failed: boom"}]

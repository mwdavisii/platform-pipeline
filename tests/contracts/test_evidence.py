from contracts.evidence import anchor_ok, verify_evidence

SOURCE = """from fastapi import FastAPI

def require_role(user):
    if not user.roles:
        raise PermissionError

@app.post("/api/notes")
def add_note():
    pass
"""


def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    (root / "app.py").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "outside.txt").write_text("secret\n", encoding="utf-8")
    return root


def test_anchor_matches_indented_substring(tmp_path):
    assert anchor_ok(repo(tmp_path), {"file": "app.py", "start_line": 4, "snippet": "if not user.roles:"})


def test_anchor_matches_multiline_snippet(tmp_path):
    snippet = '@app.post("/api/notes")\ndef add_note():'
    assert anchor_ok(repo(tmp_path), {"file": "app.py", "start_line": 7, "snippet": snippet})


def test_anchor_wrong_line_fails(tmp_path):
    assert not anchor_ok(repo(tmp_path), {"file": "app.py", "start_line": 2, "snippet": "if not user.roles:"})


def test_anchor_missing_file_fails(tmp_path):
    assert not anchor_ok(repo(tmp_path), {"file": "nope.py", "start_line": 1, "snippet": "x"})


def test_anchor_line_past_end_fails(tmp_path):
    assert not anchor_ok(repo(tmp_path), {"file": "app.py", "start_line": 500, "snippet": "x"})


def test_anchor_path_traversal_fails(tmp_path):
    assert not anchor_ok(repo(tmp_path), {"file": "../outside.txt", "start_line": 1, "snippet": "secret"})


def test_verify_evidence_flags_any_bad_anchor(tmp_path):
    finding = {
        "evidence": [
            {"file": "app.py", "start_line": 4, "snippet": "if not user.roles:"},
            {"file": "app.py", "start_line": 1, "snippet": "import os"},
        ],
        "evidence_verified": True,
    }
    out = verify_evidence(finding, repo(tmp_path))
    assert out["evidence_verified"] is False
    assert finding["evidence_verified"] is True, "input must not be mutated"


def test_verify_evidence_passes_good_anchors(tmp_path):
    finding = {"evidence": [{"file": "app.py", "start_line": 4, "snippet": "if not user.roles:"}], "evidence_verified": False}
    assert verify_evidence(finding, repo(tmp_path))["evidence_verified"] is True


import pytest

import contracts.evidence as evidence_mod


def anchor(root, **kw):
    base = {"file": "app.py", "start_line": 4, "snippet": "if not user.roles:"}
    base.update(kw)
    return anchor_ok(root, base)


def test_blank_snippet_fails(tmp_path):
    assert not anchor(repo(tmp_path), snippet="   ")
    assert not anchor(repo(tmp_path), snippet="")


def test_trivial_substring_fails(tmp_path):
    root = repo(tmp_path)
    (root / "eq.py").write_text("x = 1\n", encoding="utf-8")
    assert not anchor(root, file="eq.py", start_line=1, snippet="=")


def test_short_snippet_passes_when_equal_to_full_line(tmp_path):
    root = repo(tmp_path)
    (root / "paren.py").write_text("call(\n    a,\n)\n", encoding="utf-8")
    assert anchor(root, file="paren.py", start_line=3, snippet=")")


def test_short_multiline_snippet_needs_every_line_equal(tmp_path):
    root = repo(tmp_path)
    (root / "m.py").write_text("a = 1\nb\n", encoding="utf-8")
    assert not anchor(root, file="m.py", start_line=1, snippet="a\nb")
    assert anchor(root, file="m.py", start_line=1, snippet="a = 1\nb")


def test_absolute_path_fails(tmp_path):
    root = repo(tmp_path)
    assert not anchor(root, file=str(tmp_path / "outside.txt"), start_line=1, snippet="secret")


def test_symlink_outside_repo_fails(tmp_path):
    root = repo(tmp_path)
    (root / "link.txt").symlink_to(tmp_path / "outside.txt")
    assert not anchor(root, file="link.txt", start_line=1, snippet="secret")


def test_non_utf8_file_fails(tmp_path):
    root = repo(tmp_path)
    (root / "bin.py").write_bytes(b"\xff\xfe\x00bad\n")
    assert not anchor(root, file="bin.py", start_line=1, snippet="whatever here")


@pytest.mark.parametrize("line", [0, -1, True, "4", 4.0])
def test_bad_start_line_fails(tmp_path, line):
    assert not anchor(repo(tmp_path), start_line=line)


def test_nul_in_path_fails(tmp_path):
    assert not anchor(repo(tmp_path), file="app.py\x00.txt")


@pytest.mark.parametrize("bad", [None, "x", [], 5])
def test_non_dict_anchor_fails(tmp_path, bad):
    assert not anchor_ok(repo(tmp_path), bad)


@pytest.mark.parametrize("missing", ["file", "start_line", "snippet"])
def test_missing_key_fails(tmp_path, missing):
    a = {"file": "app.py", "start_line": 4, "snippet": "if not user.roles:"}
    del a[missing]
    assert not anchor_ok(repo(tmp_path), a)


@pytest.mark.parametrize("key,value", [("file", 3), ("snippet", 3), ("snippet", None)])
def test_non_string_fields_fail(tmp_path, key, value):
    assert not anchor(repo(tmp_path), **{key: value})


def test_unreadable_file_fails(tmp_path, monkeypatch):
    root = repo(tmp_path)

    def boom(self, *a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr(evidence_mod.Path, "read_text", boom)
    assert not anchor(root)


def test_oversized_file_fails_closed(tmp_path, monkeypatch):
    root = repo(tmp_path)
    assert anchor(root)
    monkeypatch.setattr(evidence_mod, "MAX_FILE_BYTES", 10)
    assert not anchor(root)


@pytest.mark.parametrize("bad", [{"a": 1}, "text", None, [None, "x"]])
def test_verify_evidence_bad_shapes_are_unverified(tmp_path, bad):
    out = verify_evidence({"evidence": bad, "evidence_verified": True}, repo(tmp_path))
    assert out["evidence_verified"] is False


def test_repo_root_may_be_a_string(tmp_path):
    root = repo(tmp_path)
    assert anchor(str(root))
    out = verify_evidence({"evidence": [{"file": "app.py", "start_line": 4, "snippet": "if not user.roles:"}]}, str(root))
    assert out["evidence_verified"] is True

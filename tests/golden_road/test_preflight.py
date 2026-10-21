from contracts.schema import validate
from golden_road.preflight import case_collisions


def test_clean_tree_has_no_findings(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / "Dockerfile").write_text("FROM x\n")
    assert case_collisions(tmp_path) == []


def test_case_colliding_files_are_high(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM a\n")
    (tmp_path / "dockerfile").write_text("FROM b\n")
    [f] = case_collisions(tmp_path)
    assert validate("finding.v1", f) == []
    assert f["severity"] == "high" and f["rule_id"] == "pipeline.case-collision"
    assert "Dockerfile" in f["evidence"][0]["snippet"] and "dockerfile" in f["evidence"][0]["snippet"]


def test_case_colliding_directories(tmp_path):
    (tmp_path / "deploy" / "base").mkdir(parents=True)
    (tmp_path / "deploy" / "Base").mkdir()
    assert len(case_collisions(tmp_path)) == 1


def test_git_dir_ignored(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("x")
    (tmp_path / ".git" / "head").write_text("x")
    assert case_collisions(tmp_path) == []


def test_hostile_names_still_produce_valid_findings(tmp_path):
    (tmp_path / "a\nb").write_text("x")
    (tmp_path / "A\nB").write_text("x")
    [f] = case_collisions(tmp_path)
    assert validate("finding.v1", f) == []


def test_non_utf8_names_survive_preflight_gate_and_summary(tmp_path):
    import json
    import os

    from golden_road.cli import main

    src = tmp_path / "src"
    src.mkdir()
    for name in (b"a\xff.txt", b"A\xff.txt"):
        (src / os.fsdecode(name)).write_text("x")
    pf, gr, md = tmp_path / "p.json", tmp_path / "g.json", tmp_path / "s.md"
    assert main(["preflight", str(src), "--output", str(pf)]) == 0
    assert len(json.loads(pf.read_text())) == 1
    assert main(["gate", str(pf), "--repo", "r/x", "--output", str(gr)]) == 1
    assert main(["summary", str(gr), str(pf), "--output", str(md)]) == 0
    assert md.read_text().strip()


def test_unreadable_directory_is_an_error(tmp_path):
    import os

    import pytest

    from golden_road.io import InputError

    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "f").write_text("x")
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):
            pytest.skip("running as a user who can read mode-0 directories")
        with pytest.raises(InputError):
            case_collisions(tmp_path)
    finally:
        locked.chmod(0o755)


def test_unicode_normalization_collisions(tmp_path):
    (tmp_path / "café").write_text("x")
    (tmp_path / "café").write_text("x")
    assert len(case_collisions(tmp_path)) == 1


def test_marker_outside_platform_is_high(tmp_path):
    from golden_road.preflight import forged_guard_markers
    (tmp_path / "app.py").write_text('def deny():\n    return {"headers": {"X-Platform-Authz": "deny"}}\n')
    [f] = forged_guard_markers(tmp_path)
    assert validate("finding.v1", f) == []
    assert f["severity"] == "high" and f["rule_id"] == "pipeline.forged-guard-marker"
    assert f["location"]["file"] == "app.py" and f["location"]["start_line"] == 2


def test_marker_inside_platform_and_git_is_ignored(tmp_path):
    from golden_road.preflight import forged_guard_markers
    (tmp_path / "_platform").mkdir()
    (tmp_path / "_platform" / "authz.py").write_text('MARKER = "X-Platform-Authz"\n')
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "x").write_text("x-platform-authz\n")
    assert forged_guard_markers(tmp_path) == []


def test_marker_check_case_insensitive_streams_and_skips_symlinks(tmp_path, monkeypatch):
    from golden_road import preflight
    (tmp_path / "a.js").write_text("res.set('x-PLATFORM-authz', 'deny')\n")
    assert len(preflight.forged_guard_markers(tmp_path)) == 1
    (tmp_path / "a.js").unlink()
    monkeypatch.setattr(preflight, "CHUNK", 8)
    (tmp_path / "big.bin").write_bytes(b"\x00" * 37 + b"X-Platform-Authz" + b"\x00" * 50)
    [f] = preflight.forged_guard_markers(tmp_path)
    assert f["location"]["file"] == "big.bin"
    (tmp_path / "big.bin").unlink()
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("x-platform-authz\n")
    (tmp_path / "link.txt").symlink_to(outside)
    assert preflight.forged_guard_markers(tmp_path) == []


def test_nested_platform_dir_is_scanned(tmp_path):
    from golden_road.preflight import forged_guard_markers
    (tmp_path / "pkg" / "_platform").mkdir(parents=True)
    (tmp_path / "pkg" / "_platform" / "x.py").write_text('H = "X-Platform-Authz"\n')
    assert len(forged_guard_markers(tmp_path)) == 1


def test_marker_line_number_across_chunks(tmp_path, monkeypatch):
    from golden_road import preflight
    monkeypatch.setattr(preflight, "CHUNK", 7)
    (tmp_path / "app.py").write_text("a\nbb\nccc\nheaders['X-Platform-Authz'] = 'deny'\n")
    [f] = preflight.forged_guard_markers(tmp_path)
    assert f["location"]["start_line"] == 4
    assert "X-Platform-Authz" in f["evidence"][0]["snippet"]


def test_cli_preflight_includes_marker_findings(tmp_path):
    import json
    from golden_road.cli import main
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text('H = "X-Platform-Authz"\n')
    assert main(["preflight", str(tmp_path / "src"), "--output", str(tmp_path / "p.json")]) == 0
    assert [f["rule_id"] for f in json.loads((tmp_path / "p.json").read_text())] == ["pipeline.forged-guard-marker"]

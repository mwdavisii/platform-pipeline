import base64
import json

import pytest

from contracts.schema import validate
from golden_road import register
from golden_road.cli import main
from golden_road.io import InputError


def decode(url):
    fragment = url.split("#", 1)[1]
    return json.loads(base64.urlsafe_b64decode(fragment + "=" * (-len(fragment) % 4)))


def test_the_link_carries_a_valid_draft_in_the_fragment():
    doc = register.draft("acme/notes", "Stores notes; no email.", "internal", "low")
    url = register.link("https://portal.example.org", doc)
    assert url.startswith("https://portal.example.org/register#") and "?" not in url
    assert decode(url) == doc and validate("registration-draft.v1", doc) == []


def test_the_portal_comes_from_the_platform_config_only():
    assert register.portal_url({"portal_url": "https://portal.example.org/"}) == "https://portal.example.org"
    for platform in ({}, {"portal_url": "http://portal.example.org"}, {"portal_url": "https://x.example/evil"}, {"portal_url": 5}):
        with pytest.raises(register.NoPortal):
            register.portal_url(platform)


def test_a_draft_too_long_for_the_page_is_refused():
    with pytest.raises(InputError):
        register.link("https://p.example", {"schema": "registration-draft/v1", "repo": "a/b", "purpose": "中" * 6000})


def test_a_bad_draft_is_refused():
    with pytest.raises(InputError):
        register.draft("notes", "x", None, None)
    with pytest.raises(InputError):
        register.draft("acme/notes", "x" * 4001, None, None)


def test_the_purpose_file_is_read_and_capped(tmp_path):
    (tmp_path / "p.txt").write_text("  Stores notes.\r\nNo email.  \n")
    assert register.read_purpose(tmp_path / "p.txt") == "Stores notes.\nNo email."
    (tmp_path / "long.txt").write_text("x" * 4001)
    with pytest.raises(InputError):
        register.read_purpose(tmp_path / "long.txt")
    assert register.read_purpose(None) is None


def test_the_command_prints_the_link_and_takes_the_classification_from_the_spec(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(register, "load_platform", lambda: {"portal_url": "https://portal.example.org"})
    (tmp_path / "builder-spec.json").write_text(json.dumps({"data_classification": "restricted"}))
    (tmp_path / "purpose.txt").write_text("Stores clinic schedules; no PHI.")
    code = main(["register", "--repo", "acme/notes", "--purpose-file", str(tmp_path / "purpose.txt"), "--tier", "moderate", "--root", str(tmp_path)])
    out, err = capsys.readouterr()
    doc = json.loads(out)
    assert code == 0 and doc["draft"]["data_classification"] == "restricted" and decode(doc["url"]) == doc["draft"]
    assert "sign in, check every field" in err


def test_the_command_sends_nothing(tmp_path, monkeypatch, capsys):
    """It only prints: no network, no subprocess."""
    import socket
    import subprocess
    monkeypatch.setattr(register, "load_platform", lambda: {"portal_url": "https://portal.example.org"})

    def boom(*a, **k):
        raise AssertionError("register must not reach out")
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert main(["register", "--repo", "acme/notes", "--root", str(tmp_path)]) == 0
    capsys.readouterr()


def test_no_portal_is_exit_3_with_a_fix(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(register, "load_platform", lambda: {})
    assert main(["register", "--repo", "acme/notes", "--root", str(tmp_path)]) == 3
    err = json.loads(capsys.readouterr().err)
    assert "portal" in err["error"] and "security adds the intake record" in err["fix"]


def test_bad_arguments_are_exit_2(capsys):
    assert main(["register", "--repo", "notes"]) == 2
    with pytest.raises(SystemExit) as exc:  # argparse refuses the choice itself: a JSON error on stderr, exit 2
        main(["register", "--repo", "acme/notes", "--tier", "none"])
    assert exc.value.code == 2
    capsys.readouterr()

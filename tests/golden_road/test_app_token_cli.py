"""golden-road app-token: the token goes only to a 0600 file, stdout names its scope, the key never shows."""
import json

from golden_road.cli import main
from test_github_app import PEM, TOKEN, FakeGitHub


def run(monkeypatch, tmp_path, capsys, *extra, key=PEM):
    monkeypatch.setattr("golden_road.checks._open", FakeGitHub())
    monkeypatch.setenv("GOLDEN_ROAD_APP_KEY", key)
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.com")
    code = main(["app-token", "--app-id", "1234", "--repo", "o/app", "--permission", "contents=read", *extra, "--output", str(tmp_path / "t.json")])
    return code, capsys.readouterr()


def test_app_token_writes_a_private_token_file_and_prints_only_its_scope(monkeypatch, tmp_path, capsys):
    code, out = run(monkeypatch, tmp_path, capsys)
    assert code == 0 and (tmp_path / "t.json").stat().st_mode & 0o777 == 0o600
    assert json.loads((tmp_path / "t.json").read_text())["token"] == TOKEN
    assert json.loads(out.out) == {"repository": "o/app", "permissions": {"contents": "read"}, "expires_at": "2027-01-15T09:00:00Z"}
    assert TOKEN not in out.out + out.err and "PRIVATE" not in out.out + out.err


def test_app_token_without_the_key_fails_and_writes_nothing(monkeypatch, tmp_path, capsys):
    code, out = run(monkeypatch, tmp_path, capsys, key="")
    assert code == 2 and "GOLDEN_ROAD_APP_KEY" in out.err and not (tmp_path / "t.json").exists()


def test_app_token_refuses_a_disallowed_permission(monkeypatch, tmp_path, capsys):
    code, out = run(monkeypatch, tmp_path, capsys, "--permission", "administration=write")
    assert code == 2 and not (tmp_path / "t.json").exists()


def test_a_bad_key_never_reaches_the_output(monkeypatch, tmp_path, capsys):
    code, out = run(monkeypatch, tmp_path, capsys, key=PEM[:200])
    assert code == 2 and PEM[40:120] not in out.out + out.err


def test_app_token_replaces_an_earlier_token_file_only_by_removing_it_first(monkeypatch, tmp_path, capsys):
    (tmp_path / "t.json").write_text("old")
    code, _ = run(monkeypatch, tmp_path, capsys)
    assert code == 0 and json.loads((tmp_path / "t.json").read_text())["token"] == TOKEN

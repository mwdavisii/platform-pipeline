import json

import pytest

from golden_road.cli import main


def write(path, doc):
    path.write_text(json.dumps(doc))
    return str(path)


def test_gate_exit_codes(tmp_path, valid_finding):
    f = write(tmp_path / "f.json", [valid_finding])
    out = tmp_path / "gate.json"
    assert main(["gate", f, "--repo", "r/x", "--today", "2026-10-20", "--output", str(out)]) == 1
    assert json.loads(out.read_text())["status"] == "fail"
    ok = write(tmp_path / "ok.json", [{**valid_finding, "severity": "low"}])
    assert main(["gate", ok, "--repo", "r/x", "--today", "2026-10-20", "--output", str(out)]) == 0


def test_bad_input_exits_2_with_json_error(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    code = main(["gate", str(bad), "--repo", "r/x", "--output", str(tmp_path / "g.json")])
    assert code == 2
    assert "error" in json.loads(capsys.readouterr().err)


def test_bad_today_and_unknown_args_exit_2(tmp_path, valid_finding):
    f = write(tmp_path / "f.json", [valid_finding])
    assert main(["gate", f, "--repo", "r/x", "--today", "garbage", "--output", str(tmp_path / "g.json")]) == 2


def test_normalize_preflight_from_builder_summary(tmp_path, valid_finding):
    (tmp_path / "src").mkdir()
    sarif = {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "trivy", "rules": []}}, "results": []}]}
    s = write(tmp_path / "s.sarif", sarif)
    assert main(["normalize", "--category", "sca", "--repo-root", str(tmp_path / "src"), s, "--output", str(tmp_path / "n.json")]) == 0
    assert json.loads((tmp_path / "n.json").read_text()) == []
    assert main(["preflight", str(tmp_path / "src"), "--output", str(tmp_path / "p.json")]) == 0
    br = write(tmp_path / "br.json", {"command": "verify", "status": "ok", "builder_version": "0.1.0",
                                       "warnings": [], "questions": [], "errors": [], "findings": []})
    assert main(["from-builder", br, "--output", str(tmp_path / "d.json")]) == 0
    f = write(tmp_path / "f.json", [valid_finding])
    main(["gate", f, "--repo", "r/x", "--output", str(tmp_path / "g.json")])
    assert main(["summary", str(tmp_path / "g.json"), f, "--output", str(tmp_path / "s.md")]) == 0
    assert (tmp_path / "s.md").read_text().startswith("## Golden road gate")


def test_overrides_from_env(tmp_path, valid_finding, monkeypatch):
    od = tmp_path / "ov"
    od.mkdir()
    write(od / "o.json", {"finding_id": valid_finding["id"], "repo": "r/x", "approver": "infosec",
                          "justification": "Compensating control documented.", "created": "2026-10-19", "expires": "2026-11-01"})
    monkeypatch.setenv("GOLDEN_ROAD_OVERRIDES", str(od))
    f = write(tmp_path / "f.json", [valid_finding])
    assert main(["gate", f, "--repo", "r/x", "--today", "2026-10-20", "--output", str(tmp_path / "g.json")]) == 0


def test_explicit_overrides_path_must_exist_and_be_a_directory(tmp_path, valid_finding, monkeypatch):
    f = write(tmp_path / "f.json", [{**valid_finding, "severity": "low"}])
    out = str(tmp_path / "g.json")
    base = ["gate", f, "--repo", "r/x", "--today", "2026-10-20", "--output", out]
    assert main(base + ["--overrides", str(tmp_path / "missing")]) == 2
    a_file = tmp_path / "file"
    a_file.write_text("x")
    assert main(base + ["--overrides", str(a_file)]) == 2
    monkeypatch.setenv("GOLDEN_ROAD_OVERRIDES", str(tmp_path / "missing"))
    assert main(base) == 2
    monkeypatch.setenv("GOLDEN_ROAD_OVERRIDES", str(a_file))
    assert main(base) == 2


def test_failed_run_removes_stale_output(tmp_path, valid_finding):
    out = tmp_path / "gate.json"
    out.write_text('{"status": "pass"}')
    bad = tmp_path / "bad.json"
    bad.write_text("{nope")
    assert main(["gate", str(bad), "--repo", "r/x", "--output", str(out)]) == 2
    assert not out.exists()


def test_unexpected_exception_exits_2(tmp_path, valid_finding, monkeypatch):
    def boom(*a, **k):
        raise AttributeError("boom")

    monkeypatch.setattr("golden_road.gate.evaluate", boom)
    monkeypatch.setattr("golden_road.cli.evaluate", boom)
    f = write(tmp_path / "f.json", [valid_finding])
    assert main(["gate", f, "--repo", "r/x", "--output", str(tmp_path / "g.json")]) == 2


def test_relabelled_finding_id_is_rejected(tmp_path, valid_finding):
    # The attacker relabels a High finding to an ID that a real, active override already covers.
    covered = "c" * 64
    od = tmp_path / "ov"
    od.mkdir()
    write(od / "o.json", {"finding_id": covered, "repo": "r/x", "approver": "infosec",
                          "justification": "Compensating control documented.", "created": "2026-10-19", "expires": "2026-11-01"})
    f = write(tmp_path / "f.json", [{**valid_finding, "id": covered}])
    out = tmp_path / "g.json"
    assert main(["gate", f, "--repo", "r/x", "--today", "2026-10-20", "--overrides", str(od), "--output", str(out)]) == 2
    assert not out.exists()


def test_error_messages_are_capped_and_hide_secret_values(tmp_path, valid_finding, capsys):
    secret = "AKIA" + "Z" * 800
    bad = {**valid_finding, "category": "secrets", "severity": "huge", "title": secret}
    f = write(tmp_path / "f.json", [bad])
    assert main(["gate", f, "--repo", "r/x", "--output", str(tmp_path / "g.json")]) == 2
    err = capsys.readouterr().err
    assert "ZZZZ" not in err and len(err) < 700
    long_bad = {**valid_finding, "severity": "x" * 5000}
    f = write(tmp_path / "f2.json", [long_bad])
    assert main(["gate", f, "--repo", "r/x", "--output", str(tmp_path / "g.json")]) == 2
    assert len(json.loads(capsys.readouterr().err)["error"]) <= 500


def test_summary_requires_a_findings_file_and_unknown_command_errors(tmp_path, valid_finding):
    from golden_road import cli
    import argparse
    import pytest
    from golden_road.io import InputError

    g = write(tmp_path / "g.json", {"status": "pass", "counts": {"by_severity": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}, "by_layer": {}}, "blocking": [], "overrides_applied": []})
    assert main(["summary", g, "--output", str(tmp_path / "s.md")]) == 2
    with pytest.raises(InputError):
        cli._run(argparse.Namespace(command="bogus", output=tmp_path / "o"))


def test_failed_output_write_leaves_nothing(tmp_path, valid_finding, monkeypatch):
    f = write(tmp_path / "f.json", [valid_finding])
    out = tmp_path / "g.json"

    def bad_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", bad_replace)
    assert main(["gate", f, "--repo", "r/x", "--output", str(out)]) == 2
    assert not out.exists() and [p.name for p in tmp_path.iterdir()] == ["f.json"]


def test_annotate_prints_commands(tmp_path, valid_finding, capsys):
    f = write(tmp_path / "f.json", [valid_finding])
    assert main(["annotate", f]) == 0
    assert capsys.readouterr().out.startswith("::error ")


def test_record_exit_code(tmp_path):
    drift = write(tmp_path / "d.json", {"status": "pass", "counts": {"by_severity": {s: 0 for s in ("critical", "high", "medium", "low", "info")}, "by_layer": {}}, "blocking": [], "overrides_applied": []})
    checks = write(tmp_path / "c.json", {"passed": True, "required_checks": []})
    authz = write(tmp_path / "a.json", {"status": "fail", "cases": 1, "denied": 1, "failures": [{"method": "POST", "path": "/x", "roles": None, "status": 200}]})
    out = tmp_path / "r.json"
    manifests = tmp_path / "manifests.yaml"
    manifests.write_bytes(b"kind: Deployment\n")
    code = main(["record", "--release", "test-20261020153000-ccccccc", "--manifests", str(manifests), "--repo", "org/app", "--commit", "c" * 40, "--image", "ghcr.io/org/app@sha256:" + "d" * 64,
                 "--builder-image", "ghcr.io/org/b@sha256:" + "e" * 64, "--drift", drift, "--checks", checks, "--authz", authz, "--output", str(out)])
    assert code == 1 and json.loads(out.read_text())["status"] == "fail"
    import hashlib
    assert json.loads(out.read_text())["manifests_sha256"] == hashlib.sha256(b"kind: Deployment\n").hexdigest()
    assert json.loads(out.read_text())["release"] == "test-20261020153000-ccccccc"
    missing = ["record", "--release", "test-20261020153000-ccccccc", "--manifests", str(tmp_path / "nope.yaml"), "--repo", "org/app", "--commit", "c" * 40,
               "--image", "ghcr.io/org/app@sha256:" + "d" * 64, "--builder-image", "ghcr.io/org/b@sha256:" + "e" * 64,
               "--drift", drift, "--checks", checks, "--authz", authz, "--output", str(tmp_path / "r2.json")]
    assert main(missing) == 2 and not (tmp_path / "r2.json").exists()


def test_authz_probe_command_not_declared(tmp_path, python_app, notes_spec):
    del notes_spec["authz"]
    (python_app / "builder-spec.json").write_text(json.dumps(notes_spec))
    out = tmp_path / "a.json"
    assert main(["authz-probe", "--repo-root", str(python_app), "--base-url", "http://127.0.0.1:9", "--output", str(out)]) == 0
    assert json.loads(out.read_text()) == {"status": "not-declared"}


def test_authz_probe_command_fail_exits_1(tmp_path, python_app, notes_spec):
    (python_app / "builder-spec.json").write_text(json.dumps(notes_spec))
    out = tmp_path / "a.json"
    code = main(["authz-probe", "--repo-root", str(python_app), "--base-url", "http://127.0.0.1:9", "--ready-timeout", "0.1", "--output", str(out)])
    assert code == 1 and json.loads(out.read_text())["status"] == "fail"


def test_render_command_writes_manifests(tmp_path):
    import shutil
    from pathlib import Path

    app = tmp_path / "app"
    shutil.copytree(Path(__file__).resolve().parents[1] / "builder/golden/python-fastapi", app)
    out = tmp_path / "manifests.yaml"
    ref = "ghcr.io/org/notes-api@sha256:" + "b" * 64
    assert main(["render", "--repo-root", str(app), "--image", ref, "--output", str(out)]) == 0
    assert ref in out.read_text()
    assert main(["render", "--repo-root", str(app), "--image", "ghcr.io/org/notes-api:latest", "--output", str(out)]) == 2
    assert not out.exists()


def test_promotion_record_command(tmp_path):
    approvals = write(tmp_path / "ap.json", [{"state": "approved", "comment": "", "user": {"login": "deputy"}, "environments": [{"name": "prod"}]}])
    out = tmp_path / "p.json"
    manifests = tmp_path / "manifests.yaml"
    manifests.write_bytes(b"kind: Deployment\n")
    args = ["promotion-record", "--manifests", str(manifests), "--release", "test-20261020153000-ccccccc", "--image", "ghcr.io/org/app@sha256:" + "d" * 64,
            "--approvals", approvals, "--actor", "mike", "--run-attempt", "1", "--run-url", "https://github.com/org/app/actions/runs/1", "--output", str(out)]
    assert main([*args, "--triggered-by", "mike"]) == 0
    assert json.loads(out.read_text())["approvals"] == [{"login": "deputy", "comment": ""}]
    assert json.loads(out.read_text())["self_approved"] is False and json.loads(out.read_text())["gate2_single_approver"] is False
    import hashlib
    assert json.loads(out.read_text())["manifests_sha256"] == hashlib.sha256(b"kind: Deployment\n").hexdigest()
    assert main([*args, "--triggered-by", "Deputy"]) == 2
    assert not out.exists()
    assert main([*("2" if a == "1" else a for a in args), "--triggered-by", "mike"]) == 2
    assert not out.exists()


def test_promotion_record_single_approver_comes_from_the_pinned_platform_config(tmp_path, monkeypatch):
    from builder.config import load_platform

    approvals = write(tmp_path / "ap.json", [{"state": "approved", "comment": "", "user": {"login": "mike"}, "environments": [{"name": "prod"}]}])
    out = tmp_path / "p.json"
    manifests = tmp_path / "manifests.yaml"
    manifests.write_bytes(b"kind: Deployment\n")
    args = ["promotion-record", "--manifests", str(manifests), "--release", "test-20261020153000-ccccccc", "--image", "ghcr.io/org/app@sha256:" + "d" * 64,
            "--approvals", approvals, "--actor", "mike", "--triggered-by", "mike", "--run-attempt", "1",
            "--run-url", "https://github.com/org/app/actions/runs/1", "--output", str(out)]
    assert main(args) == 2  # the default platform config: self-approval refused
    assert not out.exists()
    for value, code in ((False, 2), (True, 0), ("true", 2)):
        cfg = tmp_path / "platform.json"
        cfg.write_text(json.dumps({**load_platform(), "gate2_single_approver": value}))
        monkeypatch.setenv("BUILDER_PLATFORM_CONFIG", str(cfg))
        assert main(args) == code
        if code == 0:
            assert json.loads(out.read_text())["self_approved"] is True and json.loads(out.read_text())["gate2_single_approver"] is True
        else:
            assert not out.exists()


def test_checks_command_warns_on_empty_list(tmp_path, monkeypatch, capsys):
    from golden_road import checks

    required = write(tmp_path / "r.json", {"required": []})
    out = tmp_path / "c.json"
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    seen = {}

    def fake_wait(api, repo, sha, entries, **kwargs):
        seen["entries"] = entries
        return True, []

    monkeypatch.setattr(checks, "wait", fake_wait)
    assert main(["checks", "wait", "--repo", "org/app", "--sha", "c" * 40, "--required-file", required, "--output", str(out)]) == 0
    assert "no external required checks" in capsys.readouterr().err
    assert json.loads(out.read_text()) == {"passed": True, "required_checks": [], "sha": "c" * 40}
    assert seen["entries"] == []


def test_checks_command_without_token_exits_2(tmp_path, monkeypatch):
    required = write(tmp_path / "r.json", {"required": []})
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert main(["checks", "wait", "--repo", "org/app", "--sha", "c" * 40, "--required-file", required, "--output", str(tmp_path / "c.json")]) == 2


def test_checks_command_requires_a_file(tmp_path, monkeypatch):
    monkeypatch.delenv("GOLDEN_ROAD_REQUIRED_CHECKS", raising=False)
    assert main(["checks", "wait", "--repo", "org/app", "--sha", "c" * 40, "--output", str(tmp_path / "c.json")]) == 2


@pytest.mark.parametrize("doc", [[], "text", 3, None])
def test_render_command_spec_not_an_object(tmp_path, doc, capsys):
    app = tmp_path / "app"
    app.mkdir()
    (app / "builder-spec.json").write_text(json.dumps(doc))
    out = tmp_path / "m.yaml"
    code = main(["render", "--repo-root", str(app), "--image", "ghcr.io/org/a@sha256:" + "b" * 64, "--output", str(out)])
    assert code == 2 and "AttributeError" not in capsys.readouterr().err

import json
import subprocess
import sys

import pytest

from builder.cli import main
from contracts.schema import validate


def run(capsys, *argv):
    code = main(list(argv))
    out = json.loads(capsys.readouterr().out)
    assert validate("builder-result.v1", out) == [], out
    return code, out


def write_spec(root, spec):
    (root / "builder-spec.json").write_text(json.dumps(spec))


def test_detect(capsys, python_app):
    code, out = run(capsys, "detect", str(python_app))
    assert code == 0 and out["command"] == "detect"
    assert out["draft_spec"]["app_type"] == "python"


def test_generate_then_verify_clean(capsys, python_app, notes_spec):
    write_spec(python_app, notes_spec)
    code, out = run(capsys, "generate", str(python_app))
    assert code == 0 and "Dockerfile" in out["files"]
    code, out = run(capsys, "verify", str(python_app))
    assert (code, out["status"], out["findings"]) == (0, "ok", [])


def test_generate_with_input_flag(capsys, python_app, notes_spec, tmp_path):
    spec_path = tmp_path / "elsewhere.json"
    spec_path.write_text(json.dumps(notes_spec))
    code, _ = run(capsys, "generate", str(python_app), "--input", str(spec_path))
    assert code == 0 and (python_app / "builder-spec.json").is_file()


def test_verify_drift_exit_4(capsys, python_app, notes_spec):
    write_spec(python_app, notes_spec)
    run(capsys, "generate", str(python_app))
    (python_app / "Dockerfile").write_text("FROM scratch\n")
    code, out = run(capsys, "verify", str(python_app))
    assert code == 4 and out["status"] == "drift" and out["findings"]


def test_invalid_exit_2(capsys, python_app, notes_spec):
    notes_spec["authz"]["rules"][1]["roles"] = ["reader"]
    write_spec(python_app, notes_spec)
    code, out = run(capsys, "generate", str(python_app))
    assert code == 2 and out["status"] == "invalid" and out["errors"]


def test_needs_input_exit_3(capsys, python_app, notes_spec):
    (python_app / "requirements.txt").unlink()
    write_spec(python_app, notes_spec)
    code, out = run(capsys, "generate", str(python_app))
    assert code == 3 and out["status"] == "needs_input" and out["questions"]


def test_missing_spec_and_bad_json(capsys, python_app):
    code, out = run(capsys, "generate", str(python_app))
    assert code == 2 and "builder-spec.json" in out["errors"][0]
    (python_app / "builder-spec.json").write_text("{not json")
    code, out = run(capsys, "generate", str(python_app))
    assert code == 2 and "not valid JSON" in out["errors"][0]


def test_not_a_directory(capsys, tmp_path):
    code, out = run(capsys, "verify", str(tmp_path / "missing"))
    assert code == 2 and out["status"] == "invalid"


def test_module_entry_exit_code(python_app):
    proc = subprocess.run([sys.executable, "-m", "builder.cli", "verify", str(python_app)], capture_output=True, text=True)
    assert proc.returncode == 4
    assert json.loads(proc.stdout)["findings"][0]["rule_id"] == "builder.lock-missing"


def _spec_text(text):
    return lambda root, spec: (root / "builder-spec.json").write_bytes(text if isinstance(text, bytes) else text.encode())


def _spec_dir(root, spec):
    (root / "builder-spec.json").mkdir()


def _spec_link(root, spec):
    (root.parent / "real.json").write_text(json.dumps(spec))
    (root / "builder-spec.json").symlink_to(root.parent / "real.json")


def _spec_unreadable(root, spec):
    (root / "builder-spec.json").write_text(json.dumps(spec))
    (root / "builder-spec.json").chmod(0)


def _deploy_is_file(root, spec):
    write_spec(root, spec)
    (root / "deploy").write_text("x")


FAILURES = {
    "directory": _spec_dir,
    "non-utf8": _spec_text(b'{"app_name": "\xff\xfe"}'),
    "oversize": _spec_text(b'{"a": "' + b"x" * 1_100_000 + b'"}'),
    "deep-array": _spec_text("[" * 100000 + "]" * 100000),
    "deep-nested-object": _spec_text('{"egress": ' + "[" * 900 + "]" * 900 + "}"),
    "symlink": _spec_link,
    "unreadable": _spec_unreadable,
    "list": _spec_text("[]"),
    "null": _spec_text("null"),
    "number": _spec_text("5"),
    "nul-bytes": _spec_text(b'{"a": 1}\x00'),
    "deploy-is-file": _deploy_is_file,
}


@pytest.mark.parametrize("case", sorted(FAILURES))
def test_generate_failure_matrix_is_exit_2_json(capsys, python_app, notes_spec, case):
    FAILURES[case](python_app, notes_spec)
    code, out = run(capsys, "generate", str(python_app))
    assert code in (2, 3) and out["status"] in ("invalid", "needs_input")
    assert out["errors"] or out["questions"]


@pytest.mark.parametrize("argv", [
    [], ["frob", "."], ["generate"], ["verify", "a", "b"], ["generate", ".", "--nope"], ["generate", ".", "--input"],
])
def test_usage_errors_are_exit_2_json(capsys, argv):
    code, out = run(capsys, *argv)
    assert code == 2 and out["status"] == "invalid" and out["errors"]
    assert out["command"] in ("detect", "generate", "verify")


def test_usage_error_reports_parsed_command(capsys):
    code, out = run(capsys, "generate")
    assert code == 2 and out["command"] == "generate"


def test_input_missing_file(capsys, python_app, tmp_path):
    code, out = run(capsys, "generate", str(python_app), "--input", str(tmp_path / "nope" / "spec.json"))
    assert code == 2 and out["status"] == "invalid"


def test_oserror_from_generate_is_exit_2_json(capsys, python_app, notes_spec, monkeypatch):
    write_spec(python_app, notes_spec)
    monkeypatch.setattr("builder.cli.generate", lambda root, spec: (_ for _ in ()).throw(OSError(13, "denied")))
    code, out = run(capsys, "generate", str(python_app))
    assert code == 2 and out["status"] == "invalid"


@pytest.mark.parametrize("command", ["verify", "detect"])
def test_oserror_from_other_commands_is_exit_2_json(capsys, python_app, monkeypatch, command):
    monkeypatch.setattr(f"builder.cli.{command}", lambda root: (_ for _ in ()).throw(OSError(5, "io")))
    code, out = run(capsys, command, str(python_app))
    assert code == 2 and out["status"] == "invalid"


def test_verify_over_hostile_trees_still_emits_valid_json(capsys, python_app, notes_spec):
    write_spec(python_app, notes_spec)
    run(capsys, "generate", str(python_app))
    (python_app / "builder-spec.json").write_text("[" * 100000)
    code, out = run(capsys, "verify", str(python_app))
    assert code == 4 and out["status"] == "drift"


@pytest.mark.parametrize("command", ["detect", "generate", "verify"])
def test_nul_byte_in_path_is_exit_2_json(capsys, command):
    code, out = run(capsys, command, "bad\0path")
    assert code == 2 and out["status"] == "invalid" and out["command"] == command


def test_nul_byte_in_input_is_exit_2_json(capsys, python_app):
    code, out = run(capsys, "generate", str(python_app), "--input", "spec\0.json")
    assert code == 2 and out["status"] == "invalid"


@pytest.mark.parametrize("content", ["{not json", "[]", '{"identity_header": 5}'])
def test_malformed_platform_config_is_json_not_traceback(capsys, python_app, notes_spec, tmp_path, monkeypatch, content):
    write_spec(python_app, notes_spec)
    run(capsys, "generate", str(python_app))
    config = tmp_path / "platform.json"
    config.write_text(content)
    monkeypatch.setenv("BUILDER_PLATFORM_CONFIG", str(config))
    for command in ("generate", "verify"):
        code, out = run(capsys, command, str(python_app))
        if command == "generate":
            assert code == 2 and out["status"] == "invalid" and out["errors"], out
        else:  # verify fails closed: a config problem is an error or a blocking finding, never a pass or a traceback
            assert code in (2, 4) and out["status"] in ("invalid", "drift"), out


def test_unexpected_exception_is_internal_error_json(capsys, python_app, monkeypatch):
    monkeypatch.setattr("builder.cli.detect", lambda root: (_ for _ in ()).throw(KeyError("boom")))
    code, out = run(capsys, "detect", str(python_app))
    assert (code, out["errors"]) == (2, ["internal error: KeyError"])

"""INSTALL_FOR_AGENTS.md and the stub's SKILL.md: their commands, pulled out of the files and run as written against the
fake gh and uv."""

import json
import re
import subprocess

import pytest
import yaml

from kit_fakes import AGENTS_REPO, HOST, ISSUER, PY, SKILL_SIGNER, bundle_tar, identity, load, publish_skill, run_script

TAG_COMMAND = f"gh release view --repo {AGENTS_REPO} --json tagName --jq .tagName"


def commands(path):
    """Every non-empty line of every ```text block, in order, without its indentation (a block may sit in a list item)."""
    blocks = re.findall(r"```text\n(.*?)```", path.read_text(), re.S)
    return [line.strip() for block in blocks for line in block.splitlines() if line.strip()]


def stub_assets(kit):
    return {"fetch_skill.py": (kit / "stub/golden-road/fetch_skill.py").read_bytes(),
            "golden-road-stub.md": (kit / "stub/golden-road/SKILL.md").read_bytes()}


def follow(kit, cwd, agent):
    """Run the install doc's commands as written (in a POSIX shell, which sets $HOME and $PWD as PowerShell does), putting the
    tag the first command printed where the doc says <tag>."""
    tag = None
    for line in commands(kit / "INSTALL_FOR_AGENTS.md"):
        line = line.replace("<claude-code|codex|cursor>", agent).replace("<tag>", tag or "<tag>")
        out = subprocess.run(line, shell=True, cwd=cwd, capture_output=True, text=True, executable="/bin/sh")
        assert out.returncode == 0, (line, out.stdout, out.stderr)
        if line == TAG_COMMAND:
            tag = out.stdout.strip()
    return tag


def test_the_install_doc_runs_exactly_these_commands(kit):
    ident = f"https://{SKILL_SIGNER}@refs/tags/<tag>"
    assert commands(kit / "INSTALL_FOR_AGENTS.md") == [
        f"gh auth status --hostname {HOST}",
        f"gh repo view {AGENTS_REPO} --json nameWithOwner",
        "uv --version",
        TAG_COMMAND,
        f"gh release download <tag> --repo {AGENTS_REPO} --pattern fetch_skill.py --dir .golden-road-install --clobber",
        f"gh attestation verify .golden-road-install/fetch_skill.py --repo {AGENTS_REPO} --cert-identity {ident} --source-ref refs/tags/<tag>"
        f" --cert-oidc-issuer {ISSUER}",
        f'{PY} "$PWD/.golden-road-install/fetch_skill.py" install --agent <claude-code|codex|cursor>',
    ]


def test_the_install_doc_trusts_the_repo_signer_and_issuer_the_stub_hard_codes(kit):
    stub = load(kit / "stub/golden-road/fetch_skill.py")
    assert stub.REPO == AGENTS_REPO and f"{stub.HOST}/{stub.SIGNER}" == SKILL_SIGNER and stub.OIDC_ISSUER == ISSUER


def test_the_install_doc_names_each_agent_s_folder_and_what_to_do_on_failure(kit):
    text = " ".join((kit / "INSTALL_FOR_AGENTS.md").read_text().split())
    for needle in ("`~/.claude/skills/`", "`~/.agents/skills/`", "`~/.cursor/skills/`", "they type it, never you",
                   "without running anything in it", "not to continue", "support channel", "PowerShell",
                   "exactly `skill-vX.Y.Z`", "digits and dots only", "is never executed", "CONDA_PREFIX"):
        assert needle in text, needle


@pytest.mark.parametrize("agent, folder", [("claude-code", ".claude/skills"), ("codex", ".agents/skills"), ("cursor", ".cursor/skills")])
def test_following_the_install_doc_installs_the_verified_stub(kit, world, tmp_path, agent, folder):
    assets = stub_assets(kit)
    publish_skill(world, "1.0.0", extra=assets)
    app = tmp_path / "app"
    app.mkdir()
    assert follow(kit, app, agent) == "skill-v1.0.0"
    target = world.home / folder / "golden-road"
    assert (target / "SKILL.md").read_bytes() == assets["golden-road-stub.md"]
    assert (target / "fetch_skill.py").read_bytes() == assets["fetch_skill.py"]
    assert list(app.iterdir()) == []  # .golden-road-install is gone


def test_the_install_doc_verifies_the_exact_workflow_run_of_the_release_it_downloaded(kit, world, tmp_path):
    assets = stub_assets(kit)
    publish_skill(world, "1.0.0", extra=assets, attest=False)
    world.attest(assets["fetch_skill.py"], AGENTS_REPO, SKILL_SIGNER, "skill-v0.9.0")  # attested for another tag
    app = tmp_path / "app"
    app.mkdir()
    with pytest.raises(AssertionError):
        follow(kit, app, "codex")
    assert not (world.home / ".agents").exists()


def planted_interpreters(tmp_path):
    """An app folder whose .venv, and whose parent folder's .venv, hold a python that leaves a marker if anything runs it."""
    marker = tmp_path / "marker"
    app = tmp_path / "proj/app"
    app.mkdir(parents=True)
    for folder in (app, app.parent):
        python = folder / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text(f'#!/bin/sh\necho "$0" >> "{marker}"\n')
        python.chmod(0o755)
    return app, marker


def test_uv_never_runs_an_interpreter_found_in_the_app_folder_or_above_it(kit, world, tmp_path):
    """uv executes a .venv/bin/python it finds from its working directory up, before anything is verified. The doc and the
    stub run uv from the home folder, with only-managed Pythons, so nothing planted in the app folder runs. (On Windows the
    planted file would be .venv\\Scripts\\python.exe: untested until 5c.)"""
    app, marker = planted_interpreters(tmp_path)
    assets = stub_assets(kit)
    publish_skill(world, "1.0.0", extra=assets)
    follow(kit, app, "claude-code")
    skill_dir = world.home / ".claude/skills/golden-road"
    [line] = commands(skill_dir / "SKILL.md")
    out = subprocess.run(line.replace("<skill-dir>", str(skill_dir)), shell=True, cwd=app, capture_output=True, text=True, executable="/bin/sh")
    assert out.returncode == 0, out.stderr
    assert not marker.exists()
    uv_calls = world.calls("uv")
    assert [c["args"] for c in uv_calls if c["args"] != ["--version"]] and all(
        "--python-preference" in c["args"] and "only-managed" in c["args"] and "--directory" in c["args"] for c in uv_calls if c["args"] != ["--version"])


def test_the_fake_uv_does_run_a_planted_interpreter_from_the_app_folder(kit, world, tmp_path):
    """Guards the test above: from the app folder the same fake uv does run the planted file."""
    app, marker = planted_interpreters(tmp_path)
    subprocess.run("uv run --no-project --no-config --python 3.12 python -I -c pass", shell=True, cwd=app, capture_output=True, executable="/bin/sh")
    assert marker.exists()


def test_install_writes_nothing_when_a_stub_file_does_not_verify(kit, world, tmp_path):
    assets = stub_assets(kit)
    publish_skill(world, "1.0.0", extra=assets, attest=False)
    world.attest(assets["fetch_skill.py"], AGENTS_REPO, SKILL_SIGNER, "skill-v1.0.0")
    folder = tmp_path / "app/.golden-road-install"
    folder.mkdir(parents=True)
    (folder / "fetch_skill.py").write_bytes(assets["fetch_skill.py"])
    code, doc = run_script(folder / "fetch_skill.py", "install", "--agent", "codex", cwd=tmp_path / "app")
    assert code == 1 and doc["status"] == "stop" and not (world.home / ".agents").exists()


def test_install_takes_the_newest_skill_release_not_the_latest_flag(kit, world, tmp_path):
    publish_skill(world, "1.0.0", extra=stub_assets(kit))
    publish_skill(world, "1.1.0", extra=stub_assets(kit))
    world.set(latest={AGENTS_REPO: "skill-v1.0.0"})
    code, doc = run_script(kit / "stub/golden-road/fetch_skill.py", "install", "--agent", "cursor", cwd=tmp_path)
    assert code == 0 and doc["version"] == "1.1.0" and doc["removed"] is None
    assert (kit / "stub/golden-road/fetch_skill.py").exists()


@pytest.mark.parametrize("args", [["install", "--agent", "vim"], ["install"], ["remove"]])
def test_install_usage(kit, world, tmp_path, args):
    assert run_script(kit / "stub/golden-road/fetch_skill.py", *args, cwd=tmp_path)[0] == 2


def test_the_stub_skill_is_discoverable_by_every_agent(kit):
    text = (kit / "stub/golden-road/SKILL.md").read_text()
    front = yaml.safe_load(text.split("---")[1])
    assert front["name"] == "golden-road" == (kit / "stub/golden-road").name  # Cursor: the name must be the folder's
    assert len(front["description"]) > 80 and "golden road" in front["description"]


def test_the_stub_s_command_loads_the_skill(kit, world, tmp_path):
    publish_skill(world, "1.0.0")
    [line] = commands(kit / "stub/golden-road/SKILL.md")
    assert line == f'{PY} "<skill-dir>/fetch_skill.py" load'
    folder = tmp_path / "a folder with spaces"
    folder.mkdir()
    (folder / "fetch_skill.py").write_bytes((kit / "stub/golden-road/fetch_skill.py").read_bytes())
    out = subprocess.run(line.replace("<skill-dir>", str(folder)), shell=True, cwd=tmp_path, capture_output=True, text=True, executable="/bin/sh")
    assert out.returncode == 0 and json.loads(out.stdout)["version"] == "1.0.0"


def test_the_stub_says_what_each_exit_code_means(kit):
    text = " ".join((kit / "stub/golden-road/SKILL.md").read_text().split())
    for needle in ("Exit 0", "Exit 1", "Exit 2", "Exit 3", "not to continue", "support channel", "never use another copy of the bundle",
                   "GOLDEN_ROAD_SKILL_VERSION", "`bundle_dir`", "`offline`", "in quotes"):
        assert needle in text, needle

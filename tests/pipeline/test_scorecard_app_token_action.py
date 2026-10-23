"""The factory's app-token composite action: the scorecard App's token for one repository, minted in the pinned builder image.
The key reaches only that container; the token is masked before it becomes an output; the temp directory is removed."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ACTION = ROOT / "bootstrap/scorecard-factory/.github/actions/app-token/action.yml"
REAL_IMAGE = "ghcr.io/example/golden-road-builder@sha256:" + "ab" * 32
TOKEN = "ghs_" + "a" * 36
HARDENING = "--cap-drop ALL --security-opt no-new-privileges --pids-limit 512 --read-only --tmpfs /tmp --user"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")

FAKE_DOCKER = r'''#!/usr/bin/env bash
printf '%s\n' "$*" > "@CALLS@"
env | sort > "@CALLS@.env"
out=""
prev=""
for a in "$@"; do
  case "$prev" in -v) case "$a" in *:/out) out="${a%%:/out}";; esac;; esac
  prev="$a"
done
[ -n "$out" ] || { echo "no /out mount" >&2; exit 99; }
cp "@DOC@" "$out/token.json"
'''


def step():
    doc = yaml.safe_load(ACTION.read_text())
    [s] = doc["runs"]["steps"]
    return doc, s


def run(tmp_path, *, app_id="1234", key="-----BEGIN KEY-----", repo="o/app", perms="contents=read", image=REAL_IMAGE, token_doc=None):
    _, s = step()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (tmp_path / "doc.json").write_text(json.dumps(token_doc if token_doc is not None else {"token": TOKEN, "expires_at": "2027-01-15T09:00:00Z", "app_slug": "scorecard-x"}))
    (bin_dir / "docker").write_text(FAKE_DOCKER.replace("@CALLS@", str(tmp_path / "calls")).replace("@DOC@", str(tmp_path / "doc.json")))
    (bin_dir / "docker").chmod(0o755)
    temp = tmp_path / "temp"
    temp.mkdir(exist_ok=True)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), "APP_ID": app_id, "APP_KEY": key, "TARGET": repo, "WANT": perms,
           "BUILDER_IMAGE": image, "RUNNER_TEMP": str(temp), "GITHUB_API_URL": "https://api.github.com", "GITHUB_OUTPUT": str(tmp_path / "output"),
           # what a runner has in every step; none of it may reach the container's docker client
           "GITHUB_TOKEN": "runner-token", "ACTIONS_RUNTIME_TOKEN": "x", "RUNNER_NAME": "r", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "y"}
    out = subprocess.run(["bash", "-c", s["run"]], cwd=tmp_path, capture_output=True, text=True, env=env)
    read = lambda name: (tmp_path / name).read_text() if (tmp_path / name).exists() else ""  # noqa: E731
    return out, read("output"), read("calls"), read("calls.env"), temp


def test_the_action_is_one_bash_step_using_only_its_inputs_as_env():
    doc, s = step()
    assert doc["runs"]["using"] == "composite" and "uses" not in s and s["shell"] == "bash"
    assert s["env"] == {"APP_ID": "${{ inputs.app-id }}", "APP_KEY": "${{ inputs.private-key }}", "TARGET": "${{ inputs.repository }}",
                        "WANT": "${{ inputs.permissions }}"}
    assert set(doc["inputs"]) == {"app-id", "private-key", "repository", "permissions"} and all(i["required"] for i in doc["inputs"].values())
    assert doc["outputs"]["token"]["value"] == "${{ steps.mint.outputs.token }}" and s["id"] == "mint"
    assert doc["outputs"]["slug"]["value"] == "${{ steps.mint.outputs.slug }}"
    assert "${{" not in s["run"] and s["run"].lstrip().startswith("set -euo pipefail")


def test_the_token_is_masked_before_it_is_an_output(tmp_path):
    _, s = step()
    assert s["run"].index("::add-mask::") < s["run"].index("GITHUB_OUTPUT")
    out, output, *_ = run(tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.splitlines() == [f"::add-mask::{TOKEN}"] and output == f"token={TOKEN}\nslug=scorecard-x\n"


def test_docker_gets_only_the_key_and_the_api_url(tmp_path):
    out, _, argv, env, _ = run(tmp_path)
    assert out.returncode == 0, out.stdout + out.stderr
    assert re.findall(r"-e (\S+)", argv) == ["GOLDEN_ROAD_APP_KEY", "GITHUB_API_URL"] and HARDENING in argv
    assert argv.split()[0] == "run" and REAL_IMAGE in argv and "golden-road app-token --app-id 1234 --repo o/app" in argv
    names = {line.split("=", 1)[0] for line in env.splitlines()} - {"PWD", "SHLVL", "_", "OLDPWD"}
    assert names == {"PATH", "HOME", "GOLDEN_ROAD_APP_KEY", "GITHUB_API_URL"}
    assert not [n for n in names if n == "GITHUB_TOKEN" or n.startswith(("ACTIONS_", "RUNNER_"))]
    assert "APP_KEY=" not in argv and "-----BEGIN" not in argv, "the key never rides argv"


def test_the_permissions_become_repeated_flags(tmp_path):
    out, _, argv, *_ = run(tmp_path, perms="contents=read checks=read")
    assert out.returncode == 0 and "--permission contents=read --permission checks=read" in argv


def test_the_token_directory_is_deleted(tmp_path):
    *_, temp = run(tmp_path)
    assert list(temp.iterdir()) == []


def test_the_token_directory_is_deleted_when_the_token_is_refused(tmp_path):
    *_, temp = run(tmp_path, token_doc={"token": "bad"})
    assert list(temp.iterdir()) == []


@pytest.mark.parametrize("kwargs", [{"app_id": ""}, {"app_id": "abc"}, {"app_id": "0"}, {"app_id": "12345678901"}, {"key": ""}, {"repo": "o/.."},
                                    {"repo": "o"}, {"repo": "o/a b"}, {"perms": "contents=admin"}, {"perms": ""}, {"perms": "contents"},
                                    {"image": "ghcr.io/example/golden-road-builder@sha256:" + "0" * 64}, {"image": "ghcr.io/x/y:latest"}])
def test_odd_inputs_fail_before_docker(tmp_path, kwargs):
    out, output, argv, *_ = run(tmp_path, **kwargs)
    assert out.returncode == 1 and "::error::app-token:" in out.stdout and argv == "" and output == ""


@pytest.mark.parametrize("doc", [{"token": "ghs_short"}, {"token": "not a token; echo pwned"}, {"token": ""}, {}, {"token": 5}])
def test_an_odd_token_is_refused_and_not_echoed(tmp_path, doc):
    out, output, *_ = run(tmp_path, token_doc=doc)
    assert out.returncode == 1 and output == "" and "pwned" not in out.stdout + out.stderr and "ghs_short" not in out.stdout + out.stderr
    assert "::add-mask::" not in out.stdout


@pytest.mark.parametrize("slug", ["", "Has Space", "../x", "x; echo pwned", "UPPER", "a" * 40, None])
def test_an_odd_app_slug_is_refused_and_the_token_is_not_handed_on(tmp_path, slug):
    out, output, *_ = run(tmp_path, token_doc={"token": TOKEN, "expires_at": "2027-01-15T09:00:00Z", "app_slug": slug})
    assert out.returncode == 1 and output == "" and "pwned" not in out.stdout.replace("::error::app-token: the App slug has an unexpected form", "")

"""The bundle's install_builder.py against a fake gh and uv: the pin, the hashes, the venv, the caches, and when it stops."""

import ast
import json
from pathlib import Path

import pytest

from kit_fakes import AGENTS_REPO, BUILDER_SIGNER, HOST, ORG, PIPELINE_REPO, ROOT, SKILL_SIGNER, UV_GUARD, identity, load, run_script, sha

PLATFORM_SHA = "e" * 64
REQUIREMENTS = b"attrs==26.1.0 \\\n    --hash=sha256:" + b"1" * 64 + b"\n"


def publish_builder(world, version, attest=True, pin=None, assets=None):
    """A builder-v<version> release as release-builder.yml publishes it, each file attested by that workflow."""
    wheel, wheel_name, req_name = f"wheel {version}".encode(), f"golden_road-{version}-py3-none-any.whl", f"golden_road-{version}-requirements.txt"
    doc = {"builder_version": version, "wheel": wheel_name, "wheel_sha256": sha(wheel), "requirements": req_name,
           "requirements_sha256": sha(REQUIREMENTS), "image": f"ghcr.io/{ORG.lower()}/golden-road-builder@sha256:" + "c" * 64,
           "platform_config_sha256": PLATFORM_SHA, **(pin or {})}
    files = {"platform-pin.json": json.dumps(doc).encode(), wheel_name: wheel, req_name: REQUIREMENTS, f"builder-v{version}.sigstore.json": b"{}"}
    if attest:
        for name in ("platform-pin.json", wheel_name, req_name):
            world.attest(files[name], PIPELINE_REPO, BUILDER_SIGNER, f"builder-v{version}", needs_bundle=True)
    world.release(AGENTS_REPO, f"builder-v{version}", {**files, **(assets or {})})
    return files


def reports(world, version, platform_sha=PLATFORM_SHA, fail=None):
    """What the builder the fake uv installs will report, and which uv step fails."""
    world.set(uv={"version": version, "platform_sha": platform_sha, **({"fail": fail} if fail else {})})


def install(kit, cwd):
    return run_script(kit / "skill/install_builder.py", cwd=cwd)


def venv(world, version):
    return world.home / ".local/share/golden-road" / f"builder-{version}"


def doctor_pin(world):
    return world.home / ".cache/golden-road/platform-pin.json"


def builder_cache(world):
    return world.home / ".cache/golden-road/builder"


def test_installs_the_newest_published_builder(kit, world, tmp_path):
    publish_builder(world, "0.3.0")
    files = publish_builder(world, "0.10.0")
    world.release(AGENTS_REPO, "builder-v0.11.0", {}, draft=True)
    world.release(AGENTS_REPO, "builder-v0.12.0", {}, prerelease=True)
    world.release(AGENTS_REPO, "skill-v9.0.0", {})
    reports(world, "0.10.0")
    code, doc = install(kit, tmp_path)
    assert code == 0, doc
    v = venv(world, "0.10.0")
    assert doc["builder_version"] == "0.10.0" and doc["org"] == ORG and doc["host"] == HOST
    assert doc["builder"] == str(v / "bin/builder") and doc["golden_road"] == str(v / "bin/golden-road")
    assert doctor_pin(world).read_bytes() == files["platform-pin.json"] and doc["pin"] == str(doctor_pin(world))
    assert doc["offline"] is False and doc["cached"] is False and doc["image"].endswith("c" * 64)
    assert json.loads((v / "installed.json").read_text())["version"] == "0.10.0"


def test_every_file_is_verified_against_the_platform_release_workflow_and_the_tag(kit, world, tmp_path):
    publish_builder(world, "0.3.0")
    reports(world, "0.3.0")
    assert install(kit, tmp_path)[0] == 0
    verified = [c["args"] for c in world.calls() if c["args"][:2] == ["attestation", "verify"]]
    assert [Path(a[2]).name for a in verified] == ["platform-pin.json", "golden_road-0.3.0-py3-none-any.whl", "golden_road-0.3.0-requirements.txt"]
    for a in verified:
        assert a[a.index("--repo") + 1] == PIPELINE_REPO and a[a.index("--cert-identity") + 1] == identity(BUILDER_SIGNER, "builder-v0.3.0")
        assert a[a.index("--source-ref") + 1] == "refs/tags/builder-v0.3.0"
        assert Path(a[a.index("--bundle") + 1]).name == "builder-v0.3.0.sigstore.json"
    downloads = [c["args"] for c in world.calls() if c["args"][:2] == ["release", "download"]]
    assert downloads and all(a[2] == "builder-v0.3.0" and a[a.index("--repo") + 1] == AGENTS_REPO for a in downloads)


def test_the_venv_is_built_with_hashes_and_the_wheel_without_dependencies(kit, world, tmp_path):
    publish_builder(world, "0.3.0")
    reports(world, "0.3.0")
    install(kit, tmp_path)
    calls = [c["args"] for c in world.calls("uv")]
    guard = ["--no-config", "--directory", str(world.home), *UV_GUARD]  # uv runs from home, never the app folder, with managed Pythons only
    assert len(calls) == 3 and all(a[:len(guard)] == guard for a in calls)
    assert calls[0][len(guard):] == ["venv", "--no-project", "--python", "3.12", str(venv(world, "0.3.0"))]
    reqs, wheel = [a[len(guard) - 1:] for a in calls[1:]]
    assert reqs[1:3] == ["pip", "install"] and "--require-hashes" in reqs and reqs[reqs.index("--only-binary") + 1] == ":all:"
    assert Path(reqs[reqs.index("-r") + 1]).name == "golden_road-0.3.0-requirements.txt"
    assert wheel[1:3] == ["pip", "install"] and "--no-deps" in wheel and "--no-index" in wheel
    assert Path(wheel[-1]).name == "golden_road-0.3.0-py3-none-any.whl"
    assert reqs[reqs.index("--python") + 1] == wheel[wheel.index("--python") + 1] == str(venv(world, "0.3.0") / "bin/python")


@pytest.mark.parametrize("suffix", [".whl", "-requirements.txt"])
def test_a_file_that_does_not_match_the_pin_stops_before_anything_is_installed(kit, world, tmp_path, suffix):
    files = publish_builder(world, "0.3.0")
    name = next(n for n in files if n.endswith(suffix))
    publish_builder(world, "0.3.0", assets={name: files[name] + b" tampered"})
    reports(world, "0.3.0")
    code, doc = install(kit, tmp_path)
    assert code == 1 and "SHA-256" in doc["message"] and "support channel" in doc["fix"]
    assert not venv(world, "0.3.0").exists() and not doctor_pin(world).exists() and world.calls("uv") == []


def test_an_unattested_pin_stops_and_is_remembered(kit, world, tmp_path):
    publish_builder(world, "0.3.0", attest=False)
    reports(world, "0.3.0")
    code, doc = install(kit, tmp_path)
    assert code == 1 and doc["status"] == "stop" and not doctor_pin(world).exists()
    assert (builder_cache(world) / "failed-0.3.0").exists() and not (builder_cache(world) / "0.3.0").exists()


def test_a_pin_attested_by_another_workflow_is_not_trusted(kit, world, tmp_path):
    files = publish_builder(world, "0.3.0", attest=False)
    for name in ("platform-pin.json", "golden_road-0.3.0-py3-none-any.whl", "golden_road-0.3.0-requirements.txt"):
        world.attest(files[name], AGENTS_REPO, SKILL_SIGNER, "builder-v0.3.0", needs_bundle=True)
    reports(world, "0.3.0")
    assert install(kit, tmp_path)[0] == 1


def test_a_builder_release_attested_with_another_issuer_stops(kit, world, tmp_path):
    files = publish_builder(world, "0.3.0", attest=False)
    for name in ("platform-pin.json", "golden_road-0.3.0-py3-none-any.whl", "golden_road-0.3.0-requirements.txt"):
        world.attest(files[name], PIPELINE_REPO, BUILDER_SIGNER, "builder-v0.3.0", needs_bundle=True, issuer="https://token.actions.other.ghe.com")
    reports(world, "0.3.0")
    assert install(kit, tmp_path)[0] == 1


@pytest.mark.parametrize("changes", [
    {"builder_version": "0.2.0"}, {"wheel": "golden_road-0.2.0-py3-none-any.whl"}, {"wheel": "../x.whl"},
    {"requirements": "../../requirements.txt"}, {"requirements": "platform-pin.json"}, {"extra": "x"},
    {"image": "ghcr.io/o/b:latest"}, {"platform_config_sha256": "E" * 64},
], ids=["other-version", "other-wheel", "wheel-path", "requirements-path", "requirements-is-pin", "extra-field", "image-tag", "uppercase-sha"])
def test_a_pin_that_does_not_describe_its_release_stops(kit, world, tmp_path, changes):
    publish_builder(world, "0.3.0", pin=changes)
    reports(world, "0.3.0")
    assert install(kit, tmp_path)[0] == 1 and not doctor_pin(world).exists()


def test_a_second_run_uses_the_cache_and_the_venv(kit, world, tmp_path):
    publish_builder(world, "0.3.0")
    reports(world, "0.3.0")
    install(kit, tmp_path)
    gh_before, uv_before = len(world.calls()), len(world.calls("uv"))
    code, doc = install(kit, tmp_path)
    assert code == 0 and doc["cached"] is True
    assert [c["args"][:2] for c in world.calls()[gh_before:]] == [["release", "list"]] and len(world.calls("uv")) == uv_before


def test_a_cached_wheel_that_no_longer_matches_is_downloaded_again(kit, world, tmp_path):
    publish_builder(world, "0.3.0")
    reports(world, "0.3.0")
    install(kit, tmp_path)
    (builder_cache(world) / "0.3.0/golden_road-0.3.0-py3-none-any.whl").write_bytes(b"changed")
    before = len(world.calls())
    assert install(kit, tmp_path)[0] == 0
    assert any(c["args"][:2] == ["release", "download"] for c in world.calls()[before:])


def test_a_builder_that_reports_another_version_or_platform_is_removed(kit, world, tmp_path):
    publish_builder(world, "0.3.0")
    reports(world, "0.2.9")
    code, doc = install(kit, tmp_path)
    assert code == 1 and not venv(world, "0.3.0").exists() and not doctor_pin(world).exists()
    reports(world, "0.3.0", platform_sha="f" * 64)
    assert install(kit, tmp_path)[0] == 1 and not venv(world, "0.3.0").exists()


@pytest.mark.parametrize("step", ["venv", "requirements", "wheel"])
def test_a_uv_failure_cannot_continue_and_leaves_no_half_venv(kit, world, tmp_path, step):
    publish_builder(world, "0.3.0")
    reports(world, "0.3.0", fail=step)
    code, doc = install(kit, tmp_path)
    assert code == 3 and "package index" in doc["fix"] and not venv(world, "0.3.0").exists() and not doctor_pin(world).exists()


def test_a_half_built_venv_is_rebuilt(kit, world, tmp_path):
    publish_builder(world, "0.3.0")
    reports(world, "0.3.0")
    (venv(world, "0.3.0") / "bin").mkdir(parents=True)
    assert install(kit, tmp_path)[0] == 0 and (venv(world, "0.3.0") / "installed.json").exists()


def test_offline_uses_the_newest_verified_cache_but_never_an_older_one_after_a_failure(kit, world, tmp_path):
    publish_builder(world, "0.3.0")
    reports(world, "0.3.0")
    assert install(kit, tmp_path)[0] == 0
    world.set(offline=True)
    code, doc = install(kit, tmp_path)
    assert code == 0 and doc["offline"] is True and doc["builder_version"] == "0.3.0" and doc["warnings"]
    world.set(offline=False)
    publish_builder(world, "0.4.0", attest=False)
    assert install(kit, tmp_path)[0] == 1
    world.set(offline=True)
    code, doc = install(kit, tmp_path)
    assert code == 1 and "0.4.0" in doc["message"]


def test_offline_with_nothing_cached_cannot_continue(kit, world, tmp_path):
    world.set(offline=True)
    assert install(kit, tmp_path)[0] == 3


def test_the_environment_cannot_redirect_or_weaken_the_install(kit, world, tmp_path, monkeypatch):
    publish_builder(world, "0.3.0")
    reports(world, "0.3.0")
    for name, value in (("GH_REPO", "evil/golden-road-agents"), ("BUILDER_PLATFORM_CONFIG", str(tmp_path / "evil.json")),
                        ("UV_NO_VERIFY_HASHES", "1"), ("UV_PYTHON", "3.8"), ("VIRTUAL_ENV", str(tmp_path / "other")), ("CONDA_PREFIX", str(tmp_path / "conda")),
                        ("UV_INDEX_URL", "https://proxy.example/simple")):
        monkeypatch.setenv(name, value)
    assert install(kit, tmp_path)[0] == 0
    assert all(c["gh_repo"] is None for c in world.calls())
    for call in world.calls("uv"):
        assert call["uv_env"] == {"UV_INDEX_URL": "https://proxy.example/simple"}
        assert "BUILDER_PLATFORM_CONFIG" not in call["env_keys"] and "VIRTUAL_ENV" not in call["env_keys"]
        assert "CONDA_PREFIX" not in call["env_keys"]


def test_an_unconfigured_bundle_refuses_to_run(kit, world, tmp_path):
    path = kit / "skill/install_builder.py"
    path.write_text(path.read_text().replace(f'ORG = "{ORG}"', 'ORG = "ORG_' + 'PLACEHOLDER"'))
    code, doc = run_script(path, cwd=tmp_path)
    assert code == 3 and "not configured" in doc["message"] and world.calls() == []


def test_the_pin_rules_are_the_platform_pin_schema_s(kit):
    module = load(kit / "skill/install_builder.py")
    schema = json.loads((ROOT / "src/contracts/schemas/platform-pin.v1.json").read_text())
    want = {k: v.get("pattern") or schema["$defs"]["sha256"]["pattern"] for k, v in schema["properties"].items()}
    assert module.PIN_FIELDS == want and set(schema["required"]) == set(want) and schema["additionalProperties"] is False


def test_the_pin_goes_where_doctor_reads_it(kit, world):
    from golden_road import doctor
    assert load(kit / "skill/install_builder.py").doctor_pin() == doctor.default_pin()


def test_the_venv_layout_on_windows(kit):
    module = load(kit / "skill/install_builder.py")
    root = Path("/v")
    assert module.venv_exe(root, "builder", nt=True) == root / "Scripts/builder.exe"
    assert module.venv_exe(root, "builder", nt=False) == root / "bin/builder"


SHARED = ("Stop", "gh_env", "run", "gh", "first_line", "key", "newest", "list_releases", "download", "verify", "sha256",
          "write_atomic", "read_json", "failed_versions", "highest", "record_highest", "rollback", "emit")


def test_the_stub_and_the_bundle_share_the_same_helpers_and_constants(kit):
    def definitions(path):
        return {n.name: ast.dump(n) for n in ast.parse(path.read_text()).body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    stub, bundle = definitions(kit / "stub/golden-road/fetch_skill.py"), definitions(kit / "skill/install_builder.py")
    for name in SHARED:
        assert stub[name] == bundle[name], name
    a, b = load(kit / "stub/golden-road/fetch_skill.py"), load(kit / "skill/install_builder.py")
    for name in ("OK", "STOP", "USAGE", "CANT", "HOST", "ORG", "UNCONFIGURED", "SUPPORT", "RETRY", "MAX_DOWNLOAD"):
        assert getattr(a, name) == getattr(b, name), name
    assert a.VERSION.pattern == b.VERSION.pattern


def test_uv_never_runs_an_interpreter_planted_in_the_app_folder_or_above_it(kit, world, tmp_path):
    publish_builder(world, "0.3.0")
    reports(world, "0.3.0")
    marker = tmp_path / "marker"
    app = tmp_path / "proj/app"
    for folder in (app, app.parent):
        python = folder / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text(f'#!/bin/sh\necho "$0" >> "{marker}"\n')
        python.chmod(0o755)
    assert install(kit, app)[0] == 0
    assert not marker.exists()


def test_gh_exiting_zero_with_no_attestations_is_a_failure(kit, world, tmp_path):
    publish_builder(world, "0.3.0")
    reports(world, "0.3.0")
    world.set(verify_empty=True)
    code, doc = install(kit, tmp_path)
    assert code == 1 and "failed verification" in doc["message"] and not venv(world, "0.3.0").exists()


def test_a_pin_that_changed_in_the_cache_is_not_used(kit, world, tmp_path):
    publish_builder(world, "0.3.0")
    reports(world, "0.3.0")
    install(kit, tmp_path)
    pin = builder_cache(world) / "0.3.0/platform-pin.json"
    doc = json.loads(pin.read_text())
    doc["image"] = "ghcr.io/evil/golden-road-builder@sha256:" + "d" * 64  # still a valid pin, no longer the verified one
    pin.write_text(json.dumps(doc))
    world.set(offline=True)
    assert install(kit, tmp_path)[0] == 3
    world.set(offline=False)
    before = len(world.calls())
    code, out = install(kit, tmp_path)
    assert code == 0 and out["image"].endswith("c" * 64) and any(c["args"][:2] == ["release", "download"] for c in world.calls()[before:])


def test_a_cache_record_naming_another_signer_is_not_trusted(kit, world, tmp_path):
    publish_builder(world, "0.3.0")
    reports(world, "0.3.0")
    install(kit, tmp_path)
    record = builder_cache(world) / "0.3.0/verified.json"
    record.write_text(json.dumps({**json.loads(record.read_text()), "signer": SKILL_SIGNER.removeprefix("github.com/")}))
    world.set(offline=True)
    assert install(kit, tmp_path)[0] == 3


def test_a_cache_folder_that_cannot_be_written_cannot_continue_cleanly(kit, world, tmp_path):
    publish_builder(world, "0.3.0")
    (world.home / ".cache").write_text("a file where the cache folder should be")
    code, doc = install(kit, tmp_path)
    assert code == 3 and doc["status"] == "error" and doc["fix"]


def test_a_concurrent_install_that_finished_first_is_used(kit, world, tmp_path):
    publish_builder(world, "0.3.0")
    reports(world, "0.3.0")
    assert install(kit, tmp_path)[0] == 0
    module = load(kit / "skill/install_builder.py")
    folder = module.fetch(builder_cache(world), "0.3.0")  # already there and verified: replacing it fails, so it is used
    assert folder == builder_cache(world) / "0.3.0" and module.cached(builder_cache(world), "0.3.0") == folder


def test_a_lower_newest_builder_than_one_verified_before_is_refused(kit, world, tmp_path):
    publish_builder(world, "0.4.0")
    reports(world, "0.4.0")
    assert install(kit, tmp_path)[0] == 0
    world.set(releases={AGENTS_REPO: []})
    publish_builder(world, "0.3.0")
    reports(world, "0.3.0")
    code, doc = install(kit, tmp_path)
    assert code == 1 and doc["status"] == "stop" and "0.4.0" in doc["message"] and "0.3.0" in doc["message"] and "support channel" in doc["fix"]
    assert not venv(world, "0.3.0").exists()

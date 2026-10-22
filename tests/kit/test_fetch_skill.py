"""The stub's fetch_skill.py load against a fake gh: what it trusts, what it caches, and when it stops."""

import io
import json
import tarfile
from pathlib import Path

import pytest

from kit_fakes import AGENTS_REPO, HOST, ORG, SKILL_SIGNER, bundle_tar, identity, publish_skill, run_script
from kit_fakes import load as import_script


def stub(kit):
    return kit / "stub/golden-road/fetch_skill.py"


def cache(world):
    return world.home / ".cache/golden-road/skill"


def load(kit, cwd=None):
    return run_script(stub(kit), "load", cwd=cwd)


def test_loads_the_newest_verified_release(kit, world):
    for version in ("1.2.0", "1.10.0", "1.9.0"):
        publish_skill(world, version)
    code, doc = load(kit)
    assert code == 0, doc
    assert doc["status"] == "ok" and doc["version"] == "1.10.0" and doc["offline"] is False and doc["cached"] is False
    assert Path(doc["skill"]) == cache(world) / "1.10.0/bundle/SKILL.md" and Path(doc["skill"]).read_text() == "# skill\n"
    assert Path(doc["bundle_dir"]) == cache(world) / "1.10.0/bundle"
    [args] = [c["args"] for c in world.calls() if c["args"][:2] == ["attestation", "verify"]]
    assert args[args.index("--repo") + 1] == AGENTS_REPO and args[args.index("--cert-identity") + 1] == identity(SKILL_SIGNER, "skill-v1.10.0")
    assert args[args.index("--source-ref") + 1] == "refs/tags/skill-v1.10.0" and "--bundle" not in args
    assert all(c["gh_host"] == HOST and c["prompt_disabled"] == "1" for c in world.calls())


def test_drafts_prereleases_and_other_tags_are_never_candidates(kit, world):
    publish_skill(world, "1.0.0")
    world.release(AGENTS_REPO, "skill-v2.0.0", {"golden-road-skill-2.0.0.tar.gz": bundle_tar("2.0.0")}, prerelease=True)
    world.release(AGENTS_REPO, "skill-v3.0.0", {}, draft=True)
    world.release(AGENTS_REPO, "builder-v9.0.0", {})
    world.release(AGENTS_REPO, "skill-v4.0", {})
    world.release(AGENTS_REPO, "skill-v05.0.0", {})
    assert load(kit)[1]["version"] == "1.0.0"


def test_a_cached_version_is_used_without_downloading(kit, world):
    publish_skill(world, "1.0.0")
    assert load(kit)[0] == 0
    before = len(world.calls())
    code, doc = load(kit)
    assert code == 0 and doc["cached"] is True
    assert [c["args"][:2] for c in world.calls()[before:]] == [["release", "list"]]


def test_a_failed_attestation_stops_and_caches_nothing(kit, world):
    publish_skill(world, "1.0.0", attest=False)
    code, doc = load(kit)
    assert code == 1 and doc["status"] == "stop" and "Don't continue" in doc["fix"] and "support channel" in doc["fix"]
    assert not (cache(world) / "1.0.0").exists() and (cache(world) / "failed-1.0.0").exists()


@pytest.mark.parametrize("repo, signer, tag", [
    (f"{ORG}/someone-else", SKILL_SIGNER, "skill-v1.0.0"),
    (AGENTS_REPO, f"{HOST}/{AGENTS_REPO}/.github/workflows/other.yml", "skill-v1.0.0"),
    (AGENTS_REPO, SKILL_SIGNER, "skill-v0.9.0"),
], ids=["other-repo", "other-workflow", "other-tag"])
def test_only_the_hard_coded_repo_workflow_and_this_release_s_tag_are_trusted(kit, world, repo, signer, tag):
    data = publish_skill(world, "1.0.0", attest=False)
    world.attest(data, repo, signer, tag)
    assert load(kit)[0] == 1


def test_an_attestation_from_another_issuer_stops(kit, world):
    data = publish_skill(world, "1.0.0", attest=False)
    world.attest(data, AGENTS_REPO, SKILL_SIGNER, "skill-v1.0.0", issuer="https://token.actions.other.ghe.com")
    code, doc = load(kit)
    assert code == 1 and doc["status"] == "stop", doc


def test_the_environment_cannot_change_what_is_trusted(kit, world, monkeypatch):
    data = publish_skill(world, "1.0.0", attest=False)
    world.attest(data, "evil/golden-road-agents", f"{HOST}/evil/golden-road-agents/.github/workflows/release-skill.yml", "skill-v1.0.0")
    for name, value in (("GH_REPO", "evil/golden-road-agents"), ("GH_HOST", "evil.example"), ("GOLDEN_ROAD_SIGNER", "evil"), ("GOLDEN_ROAD_REPO", "evil")):
        monkeypatch.setenv(name, value)
    assert load(kit)[0] == 1
    assert world.calls() and all(c["gh_repo"] is None and c["gh_host"] == HOST for c in world.calls())


def test_a_newer_release_that_fails_never_falls_back_to_an_older_cache(kit, world):
    publish_skill(world, "1.0.0")
    assert load(kit)[0] == 0
    publish_skill(world, "1.1.0", attest=False)
    code, doc = load(kit)
    assert code == 1 and doc["status"] == "stop" and "skill" not in doc


def test_offline_uses_the_newest_verified_cache_and_says_so(kit, world):
    publish_skill(world, "1.0.0")
    load(kit)
    publish_skill(world, "1.1.0")
    load(kit)
    world.set(offline=True)
    code, doc = load(kit)
    assert code == 0 and doc["offline"] is True and doc["version"] == "1.1.0"
    assert "may be out of date" in " ".join(doc["warnings"])


def test_offline_refuses_an_older_cache_when_a_newer_version_failed_here(kit, world):
    publish_skill(world, "1.0.0")
    load(kit)
    publish_skill(world, "1.1.0", attest=False)
    assert load(kit)[0] == 1
    world.set(offline=True)
    code, doc = load(kit)
    assert code == 1 and "1.1.0" in doc["message"]


def test_a_version_that_verifies_later_clears_its_failure(kit, world):
    data = publish_skill(world, "1.0.0", attest=False)
    assert load(kit)[0] == 1
    world.attest(data, AGENTS_REPO, SKILL_SIGNER, "skill-v1.0.0")
    assert load(kit)[0] == 0 and not (cache(world) / "failed-1.0.0").exists()


def test_offline_with_nothing_cached_cannot_continue(kit, world):
    world.set(offline=True)
    code, doc = load(kit)
    assert code == 3 and doc["status"] == "error" and doc["fix"]


def test_a_cache_entry_without_its_verification_record_is_never_used(kit, world):
    (cache(world) / "2.0.0/bundle").mkdir(parents=True)
    (cache(world) / "2.0.0/bundle/SKILL.md").write_text("# planted\n")
    world.set(offline=True)
    assert load(kit)[0] == 3
    world.set(offline=False)
    publish_skill(world, "2.0.0")
    code, doc = load(kit)
    assert code == 0 and Path(doc["skill"]).read_text() == "# skill\n"


def test_the_debug_version_pin_still_verifies(kit, world, monkeypatch):
    publish_skill(world, "1.0.0")
    publish_skill(world, "1.1.0", attest=False)
    monkeypatch.setenv("GOLDEN_ROAD_SKILL_VERSION", "1.0.0")
    code, doc = load(kit)
    assert code == 0 and doc["version"] == "1.0.0" and doc["warnings"]
    monkeypatch.setenv("GOLDEN_ROAD_SKILL_VERSION", "1.1.0")
    assert load(kit)[0] == 1
    monkeypatch.setenv("GOLDEN_ROAD_SKILL_VERSION", "../1.0.0")
    assert load(kit)[0] == 2


def with_member(version, member, data=b""):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name, body in (("SKILL.md", b"# s\n"), ("VERSION", f"{version}\n".encode())):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
        member.size = len(data) if member.isfile() else 0
        tar.addfile(member, io.BytesIO(data) if member.isfile() else None)
    return raw.getvalue()


def entry(name, kind=tarfile.REGTYPE, target=""):
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, target
    return info


@pytest.mark.parametrize("member", [
    entry("evil", tarfile.SYMTYPE, "/etc/passwd"), entry("hard", tarfile.LNKTYPE, "SKILL.md"), entry("templates", tarfile.DIRTYPE),
    entry("../escape"), entry("/abs"), entry("a/../../b"), entry(".hidden"), entry("dev", tarfile.CHRTYPE),
], ids=["symlink", "hardlink", "folder", "dotdot", "absolute", "nested-dotdot", "dotfile", "device"])
def test_a_bundle_with_an_entry_it_must_not_hold_stops(kit, world, member):
    publish_skill(world, "1.0.0", data=with_member("1.0.0", member, b"x"))
    assert load(kit)[0] == 1
    assert not (cache(world) / "1.0.0").exists() and not (world.home.parent / "escape").exists()


def test_a_bundle_of_another_version_stops(kit, world):
    publish_skill(world, "1.0.0", data=bundle_tar("0.9.0"))
    assert load(kit)[0] == 1


def test_a_release_without_the_bundle_stops(kit, world):
    world.release(AGENTS_REPO, "skill-v1.0.0", {"other.txt": b"x"}, latest=True)
    assert load(kit)[0] == 3


def test_an_unconfigured_stub_refuses_to_run(kit, world):
    path = stub(kit)
    path.write_text(path.read_text().replace(f'ORG = "{ORG}"', 'ORG = "ORG_' + 'PLACEHOLDER"'))
    code, doc = run_script(path, "load")
    assert code == 3 and "not configured" in doc["message"] and world.calls() == []


def test_load_is_the_default_command(kit, world):
    publish_skill(world, "1.0.0")
    assert run_script(stub(kit))[1]["version"] == "1.0.0"


def test_the_bundle_is_verified_before_it_is_unpacked(kit, world):
    publish_skill(world, "1.0.0", data=with_member("1.0.0", entry("../escape"), b"x"), attest=False)
    code, doc = load(kit)
    assert code == 1 and "failed verification" in doc["message"] and "holds an entry" not in doc["message"]
    assert [p.name for p in cache(world).iterdir()] == ["failed-1.0.0"]  # no version folder, no bundle, no download folder


def test_gh_exiting_zero_with_no_attestations_is_a_failure(kit, world):
    publish_skill(world, "1.0.0")
    world.set(verify_empty=True)
    code, doc = load(kit)
    assert code == 1 and "failed verification" in doc["message"] and not (cache(world) / "1.0.0").exists()


def test_an_oversized_bundle_stops(kit, world):
    publish_skill(world, "1.0.0", data=bundle_tar("1.0.0", {"big.bin": b"\0" * (21 * 1024 * 1024)}))
    code, doc = load(kit)
    assert code == 1 and "too large" in doc["message"] and not (cache(world) / "1.0.0").exists()


def test_the_data_filter_strips_special_modes(kit, world):
    info = entry("tool.sh")
    info.mode = 0o4777
    publish_skill(world, "1.0.0", data=with_member("1.0.0", info, b"x"))
    code, doc = load(kit)
    assert code == 0 and (Path(doc["bundle_dir"]) / "tool.sh").stat().st_mode & 0o7022 == 0


def test_a_cache_record_naming_another_signer_is_not_trusted(kit, world):
    publish_skill(world, "1.0.0")
    load(kit)
    record = cache(world) / "1.0.0/verified.json"
    record.write_text(json.dumps({**json.loads(record.read_text()), "signer": f"{ORG}/golden-road-agents/.github/workflows/other.yml"}))
    world.set(offline=True)
    assert load(kit)[0] == 3
    world.set(offline=False)
    code, doc = load(kit)
    assert code == 0 and doc["cached"] is False


@pytest.mark.parametrize("damage", ["edit", "add", "tarball"])
def test_a_cached_bundle_that_changed_is_not_used(kit, world, damage):
    publish_skill(world, "1.0.0")
    load(kit)
    folder = cache(world) / "1.0.0"
    if damage == "edit":
        (folder / "bundle/SKILL.md").write_text("# tampered\n")
    elif damage == "add":
        (folder / "bundle/write_callers.py").write_text("print('planted')\n")
    else:
        (folder / "golden-road-skill-1.0.0.tar.gz").write_bytes(b"changed")
    world.set(offline=True)
    assert load(kit)[0] == 3
    world.set(offline=False)
    code, doc = load(kit)
    assert code == 0 and doc["cached"] is False and Path(doc["skill"]).read_text() == "# skill\n"
    assert sorted(p.name for p in (folder / "bundle").iterdir()) == ["SKILL.md", "VERSION"]


def test_a_cache_folder_that_cannot_be_written_cannot_continue_cleanly(kit, world):
    publish_skill(world, "1.0.0")
    (world.home / ".cache").write_text("a file where the cache folder should be")
    code, doc = load(kit)
    assert code == 3 and doc["status"] == "error" and doc["fix"]


def test_a_concurrent_load_that_finished_first_is_used(kit, world):
    publish_skill(world, "1.0.0")
    assert load(kit)[0] == 0
    module = import_script(stub(kit))
    skill = module.fetch(cache(world), "1.0.0")  # the version is already there and verified: replacing it fails, so it is used
    assert skill == cache(world) / "1.0.0/bundle/SKILL.md" and module.cached(cache(world), "1.0.0") == skill
    assert sorted(p.name for p in cache(world).iterdir()) == ["1.0.0", "highest.json"]


def test_a_debug_pin_never_records_a_highest_version(kit, world, monkeypatch):
    publish_skill(world, "1.0.0")
    monkeypatch.setenv("GOLDEN_ROAD_SKILL_VERSION", "1.0.0")
    assert load(kit)[0] == 0
    assert not (cache(world) / "highest.json").exists()  # a debug pin must not lock out the published skill


def test_a_lower_newest_version_than_one_verified_before_is_refused(kit, world, monkeypatch):
    publish_skill(world, "1.1.0")
    assert load(kit)[0] == 0
    world.set(releases={AGENTS_REPO: []})
    publish_skill(world, "1.0.0")
    code, doc = load(kit)
    assert code == 1 and doc["status"] == "stop" and "1.1.0" in doc["message"] and "1.0.0" in doc["message"] and "support channel" in doc["fix"]
    monkeypatch.setenv("GOLDEN_ROAD_SKILL_VERSION", "1.0.0")
    code, doc = load(kit)
    assert code == 0 and doc["version"] == "1.0.0"
    assert json.loads((cache(world) / "highest.json").read_text())["version"] == "1.1.0"  # a pinned older run never lowers it

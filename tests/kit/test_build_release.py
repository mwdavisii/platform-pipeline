"""scripts/build_release.py: the assets of a skill release, byte for byte the same from the same tree, and what it refuses.
The last test loads a built release through the stub, end to end."""

import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from kit_fakes import AGENTS_REPO, SKILL_SIGNER, run_script


def build(kit, version, out):
    return subprocess.run([sys.executable, "-I", str(kit / "scripts/build_release.py"), version, str(out)], capture_output=True, text=True)


def version(kit):
    return (kit / "skill/VERSION").read_text().strip()


def assets(version):
    return sorted([f"golden-road-skill-{version}.tar.gz", "fetch_skill.py", "golden-road-stub.md"])


def test_builds_the_bundle_and_the_stub_files(kit, tmp_path):
    v = version(kit)
    out = build(kit, v, tmp_path / "dist")
    assert out.returncode == 0, out.stderr
    dist = tmp_path / "dist"
    assert sorted(p.name for p in dist.iterdir()) == assets(v)
    assert (dist / "golden-road-stub.md").read_bytes() == (kit / "stub/golden-road/SKILL.md").read_bytes()
    assert (dist / "fetch_skill.py").read_bytes() == (kit / "stub/golden-road/fetch_skill.py").read_bytes()
    with tarfile.open(dist / f"golden-road-skill-{v}.tar.gz") as tar:
        members = tar.getmembers()
    assert all(m.isfile() and m.mode == 0o644 and m.mtime == 0 and (m.uid, m.gid, m.uname, m.gname) == (0, 0, "", "") for m in members)
    want = sorted(p.relative_to(kit / "skill").as_posix() for p in (kit / "skill").rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    assert [m.name for m in members] == want  # in name order, whatever order the filesystem lists them
    assert (dist / f"golden-road-skill-{v}.tar.gz").read_bytes()[4:8] == b"\0\0\0\0"  # the gzip header's file time
    assert {"SKILL.md", "VERSION", "install_builder.py", "write_callers.py", "templates/workflows/ci.yml"} <= set(want)


def test_the_same_tree_gives_the_same_bytes(kit, tmp_path):
    v = version(kit)
    assert build(kit, v, tmp_path / "a").returncode == 0
    (kit / "skill/SKILL.md").touch()  # a newer file time must not change the bundle
    (kit / "skill/__pycache__").mkdir()
    (kit / "skill/__pycache__/x.pyc").write_bytes(b"\0")
    assert build(kit, v, tmp_path / "b").returncode == 0
    for name in assets(v):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes(), name


def test_file_times_never_reach_the_bytes(kit, tmp_path):
    v = version(kit)
    for name in ("old", "new"):
        stamp = 1_000_000_000 if name == "old" else 1_900_000_000
        for path in (kit / "skill").rglob("*"):
            os.utime(path, (stamp, stamp))
        assert build(kit, v, tmp_path / name).returncode == 0
    for name in assets(v):
        assert (tmp_path / "old" / name).read_bytes() == (tmp_path / "new" / name).read_bytes(), name


@pytest.mark.parametrize("args", [[], ["0.1.0"], ["v0.1.0", "out"], ["01.0.0", "out"], ["0.1.0", "out", "extra"]])
def test_usage(kit, tmp_path, args):
    out = subprocess.run([sys.executable, "-I", str(kit / "scripts/build_release.py"), *args], capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode != 0 and "usage" in out.stderr


def test_refuses_a_version_that_is_not_skill_version(kit, tmp_path):
    out = build(kit, "9.9.9", tmp_path / "dist")
    assert out.returncode != 0 and "skill/VERSION" in out.stderr and not (tmp_path / "dist").exists()


@pytest.mark.parametrize("rel", ["skill/templates/workflows/ci.yml", "stub/golden-road/fetch_skill.py", "skill/SKILL.md"])
def test_refuses_an_unconfigured_tree(kit, tmp_path, rel):
    (kit / rel).write_text((kit / rel).read_text() + "\n# ORG_" + "PLACEHOLDER\n")
    out = build(kit, version(kit), tmp_path / "dist")
    assert out.returncode != 0 and "configure.py" in out.stderr and not (tmp_path / "dist").exists()


def test_refuses_links_and_an_output_folder_that_is_not_empty(kit, tmp_path):
    (kit / "skill/link.md").symlink_to(kit / "README.md")
    assert build(kit, version(kit), tmp_path / "dist").returncode != 0
    (kit / "skill/link.md").unlink()
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist/old").write_text("x")
    assert build(kit, version(kit), tmp_path / "dist").returncode != 0


def test_a_built_release_loads_through_the_stub(kit, world, tmp_path):
    v = version(kit)
    assert build(kit, v, tmp_path / "dist").returncode == 0
    files = {p.name: p.read_bytes() for p in (tmp_path / "dist").iterdir()}
    world.release(AGENTS_REPO, f"skill-v{v}", files, latest=True)
    for data in files.values():
        world.attest(data, AGENTS_REPO, SKILL_SIGNER, f"skill-v{v}")
    code, doc = run_script(kit / "stub/golden-road/fetch_skill.py", "load", cwd=tmp_path)
    assert code == 0, doc
    bundle = Path(doc["bundle_dir"])
    assert (bundle / "SKILL.md").read_bytes() == (kit / "skill/SKILL.md").read_bytes()
    assert (bundle / "install_builder.py").is_file() and (bundle / "templates/workflows/ci.yml").is_file()

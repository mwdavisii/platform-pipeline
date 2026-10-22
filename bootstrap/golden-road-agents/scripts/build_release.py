#!/usr/bin/env python3
"""Build a skill release's assets into a new folder: the bundle tarball and the stub's two files.

    python3 -I scripts/build_release.py <X.Y.Z> <out folder>

The same tree always gives the same bytes (regular files only, sorted, file time 0, owner 0, mode 0644, gzip without a
timestamp), so a release can be rebuilt and compared. Refuses a version that isn't skill/VERSION, a tree configure.py
hasn't run on, links and other non-regular files, and an output folder that isn't empty. __pycache__ is left out.
"""

import gzip
import io
import re
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")
PLACEHOLDERS = (b"ORG_" + b"PLACEHOLDER", b"replace-" + b"me")  # split, so configure.py and push-bootstrap.sh leave them alone
STUB = (("stub/golden-road/fetch_skill.py", "fetch_skill.py"), ("stub/golden-road/SKILL.md", "golden-road-stub.md"))
SAFE_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,99}")  # the names fetch_skill.py accepts


class Refused(Exception):
    pass


def skill_files():
    """(name inside the bundle, bytes) for every regular file under skill/, sorted. Anything else is refused."""
    files = []
    for path in sorted((ROOT / "skill").rglob("*")):
        rel = path.relative_to(ROOT / "skill")
        if "__pycache__" in rel.parts:
            continue
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise Refused(f"skill/{rel.as_posix()} is a link or not a regular file")
        if path.is_file():
            if not all(SAFE_PART.fullmatch(part) for part in rel.parts):
                raise Refused(f"skill/{rel.as_posix()} has a name the stub would refuse")
            files.append((rel.as_posix(), path.read_bytes()))
    return sorted(files)


def tarball(files):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in files:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0, compresslevel=9) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def build(version, out):
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise Refused(f"{out} exists and is not an empty folder")
    files = skill_files()
    found = dict(files).get("VERSION", b"").decode("utf-8", "replace").strip()
    if found != version:
        raise Refused(f"skill/VERSION is {found or 'missing'}, not {version}: bump skill/VERSION by pull request before tagging")
    stub = []
    for rel, name in STUB:
        path = ROOT / rel
        if path.is_symlink() or not path.is_file():
            raise Refused(f"{rel} is missing, a link or not a regular file")
        stub.append((name, path.read_bytes()))
    for name, data in files + stub:
        if any(marker in data for marker in PLACEHOLDERS):
            raise Refused(f"{name} still holds a placeholder: run scripts/configure.py on the platform tree before publishing this repo")
    out.mkdir(parents=True, exist_ok=True)
    (out / f"golden-road-skill-{version}.tar.gz").write_bytes(tarball(files))
    for name, data in stub:
        (out / name).write_bytes(data)
    return sorted(p.name for p in out.iterdir())


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or not VERSION.fullmatch(argv[0]):
        print("usage: build_release.py <X.Y.Z> <out folder>", file=sys.stderr)
        return 2
    try:
        names = build(argv[0], Path(argv[1]))
    except Refused as exc:
        print(f"build_release: {exc}", file=sys.stderr)
        return 1
    print("\n".join(names))
    return 0


if __name__ == "__main__":
    sys.exit(main())

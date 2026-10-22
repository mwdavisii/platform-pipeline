"""One SHA-256 over the factory inputs a run read: config/, intake/, na/ and suppressions/.

The assess and score jobs each get the factory from the runner's download of `main`, so a merge to the factory could land
between them. Each computes this digest from its own copy; `score` refuses when it differs from the one `assess` published.

Input, canonically: the tag `factory-digest/v1`, then for every regular file under the four directories, in the order of its
`/`-separated path relative to the factory root (sorted as UTF-8 bytes), the path's byte length (8 bytes, big-endian), the path,
the content's byte length (8 bytes, big-endian) and the content. Lengths keep a name and a content from running together.
Empty directories don't count. A symlink anywhere (a directory itself, or inside one), or anything that isn't a regular file or a
directory, is an error.
"""

import hashlib
import os
from pathlib import Path

from golden_road.io import InputError

DIRECTORIES = ("config", "intake", "na", "suppressions")
TAG = b"factory-digest/v1"


def _files(root: Path) -> list[str]:
    found = []
    for kind in DIRECTORIES:
        top = root / kind
        if top.is_symlink():
            raise InputError(f"{kind} is a symlink")
        if not top.exists():
            continue
        errors = []
        for dirpath, dirnames, filenames in os.walk(top, followlinks=False, onerror=errors.append):
            for name in dirnames + filenames:
                path = Path(dirpath) / name
                rel = path.relative_to(root).as_posix()
                try:
                    rel.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise InputError(f"{rel.encode('utf-8', 'backslashreplace').decode()} has a name that is not valid UTF-8") from exc
                if path.is_symlink():
                    raise InputError(f"{rel} is a symlink")
                if name in filenames and not path.is_file():
                    raise InputError(f"{rel} is not a regular file")
                if name in filenames:
                    found.append(rel)
        if errors:
            raise InputError(f"can't scan {kind}: {errors[0].strerror}")
    return sorted(found, key=lambda rel: rel.encode("utf-8"))


def factory_digest(factory) -> str:
    root = Path(factory)
    if not (root / "config" / "scorecard.yaml").is_file():
        raise InputError(f"{root} has no config/scorecard.yaml")
    digest = hashlib.sha256(TAG)
    for rel in _files(root):
        name, data = rel.encode("utf-8"), (root / rel).read_bytes()
        for part in (name, data):
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
    return digest.hexdigest()

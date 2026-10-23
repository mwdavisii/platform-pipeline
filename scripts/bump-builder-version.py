#!/usr/bin/env python3
"""Raise the builder version and regenerate bootstrap/hello-app and bootstrap/golden-road-portal, so the tree keeps passing its own tests.

Usage: scripts/bump-builder-version.py <X.Y.Z> [--root DIR]

A builder release is tagged builder-v<VERSION>, and the lock and Dockerfile of hello-app and of the portal record the version, so a version change by hand
(one `sed`) leaves them stale and the drift test failing. This sets VERSION in src/builder/__init__.py and then regenerates
both apps with the tree's own platform config: in an unconfigured tree that is the committed placeholder state, in a configured tree
what re-running scripts/configure.py would produce (it is the same regeneration code). A version that is not higher than the current
one is refused; the current version again is accepted and only regenerates, so a run that stopped half-way can be repeated.
Docs and doc tests read the version from the source and need no edit.
"""

import argparse
import importlib.util
import re
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # a .pyc in the tree would hold values configure.py later has to rewrite
NUMBER = r"(0|[1-9][0-9]*)"  # no leading zeros: 0.03.2 would order and read ambiguously
VERSION_LINE = re.compile(rf'^VERSION = "{NUMBER}\.{NUMBER}\.{NUMBER}"$', re.M)


def parse(text: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(rf"{NUMBER}\.{NUMBER}\.{NUMBER}", text)
    return tuple(int(part) for part in match.groups()) if match else None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("version", help="the new builder version, X.Y.Z, higher than the current one")
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = p.parse_args()
    root = args.root.resolve()
    new = parse(args.version)
    if new is None:
        sys.exit(f"{args.version!r} is not a version: use digits and dots, X.Y.Z")
    init = root / "src/builder/__init__.py"
    if init.is_symlink() or not init.is_file():
        sys.exit(f"{init} must be a regular file")
    text = init.read_text(encoding="utf-8")
    found = VERSION_LINE.search(text)
    if not found:
        sys.exit(f'{init} has no line VERSION = "X.Y.Z"')
    current = tuple(int(part) for part in found.groups())
    if new < current:
        sys.exit(f"{args.version} is lower than the current version {'.'.join(map(str, current))}: a tag never moves back")
    spec = importlib.util.spec_from_file_location("configure_for_bump", root / "scripts/configure.py")
    configure = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(configure)
    sys.path.insert(0, str(root / "src"))
    # Everything the regeneration needs is imported before VERSION is written, so a broken tree stops here, and the modules are dropped
    # afterwards so the regeneration sees the new version. Any failure after the write restores the old text.
    import builder.errors
    import builder.generate  # noqa: F401

    written = False
    try:
        if new != current:
            init.write_text(VERSION_LINE.sub(f'VERSION = "{args.version}"', text, count=1), encoding="utf-8")
            written = True
            for name in [n for n in sys.modules if n == "builder" or n.startswith("builder.")]:
                del sys.modules[name]
        configure.regenerate_apps(root)
    except BaseException as exc:
        if written:
            init.write_text(text, encoding="utf-8")
        detail = getattr(exc, "errors", None) or getattr(exc, "questions", None) or exc
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        sys.exit(f"regenerate failed, version restored: {detail}")
    print(f"builder version {args.version}; regenerated bootstrap/hello-app and bootstrap/golden-road-portal")
    return 0


if __name__ == "__main__":
    sys.exit(main())

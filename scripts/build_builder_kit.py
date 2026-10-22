#!/usr/bin/env python3
"""Build the builder kit for a builder-vX.Y.Z release: the wheel, its hash-pinned requirements, and platform-pin.json.

    <build python> -I scripts/build_builder_kit.py --tag builder-vX.Y.Z --image <ref@sha256:...> --out dist \
                   [--source-date-epoch N] [--self-test]

Run it with a Python that has setuptools and jsonschema: release-builder.yml installs images/builder/requirements.txt by
hash into a venv first. The tag must be builder-v<builder.VERSION>; the version is never rewritten. The wheel carries the
configured platform/platform.json as the builder's default platform config, byte for byte, so the agents' builder and the
image report the same platform_config_sha256. The requirements are images/builder/requirements.txt, the file the image
installs. --self-test installs the kit into a new venv the way the golden-road skill does (hashes required, the wheel
without dependencies) and checks the builder's version and platform config against the pin.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "images/builder/requirements.txt"
PLATFORM = ROOT / "platform/platform.json"
PLACEHOLDERS = (b"replace-" + b"me", b"ORG_" + b"PLACEHOLDER")  # split, so configure.py and push-bootstrap.sh leave them alone
IMAGE = re.compile(r"[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}")  # platform-pin/v1's image


def fail(message):
    raise SystemExit(f"build_builder_kit: {message}")


def builder_version():
    match = re.search(r'(?m)^VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$', (ROOT / "src/builder/__init__.py").read_text(encoding="utf-8"))
    if not match:
        fail('src/builder/__init__.py has no VERSION = "X.Y.Z" line')
    return match.group(1)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_wheel(version, out, epoch):
    """pip wheel from a copy of the source whose default platform config is the configured one. No index, no isolation:
    setuptools comes from the hash-pinned build venv."""
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "golden-road"
        tree.mkdir()
        shutil.copy2(ROOT / "pyproject.toml", tree / "pyproject.toml")
        shutil.copytree(ROOT / "src", tree / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
        (tree / "src/builder/data/platform.json").write_bytes(PLATFORM.read_bytes())
        env = {**os.environ, "SOURCE_DATE_EPOCH": str(epoch), "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
        subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--no-index", "--wheel-dir", str(out), str(tree)],
                       check=True, env=env, stdout=sys.stderr)
    wheel = out / f"golden_road-{version}-py3-none-any.whl"
    if sorted(p.name for p in out.glob("*.whl")) != [wheel.name]:
        fail(f"the build made {sorted(p.name for p in out.glob('*.whl'))}, not {wheel.name}")
    return wheel


def self_test(out, pin):
    """Install the kit as the skill does and check the builder against the pin."""
    with tempfile.TemporaryDirectory() as tmp:
        venv = Path(tmp) / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        python = venv / "bin/python"
        subprocess.run([str(python), "-m", "pip", "install", "-q", "--disable-pip-version-check", "--require-hashes", "--only-binary=:all:",
                        "-r", str(out / pin["requirements"])], check=True, stdout=sys.stderr)
        subprocess.run([str(python), "-m", "pip", "install", "-q", "--disable-pip-version-check", "--no-deps", "--no-index",
                        str(out / pin["wheel"])], check=True, stdout=sys.stderr)
        env = {k: v for k, v in os.environ.items() if k not in ("BUILDER_PLATFORM_CONFIG", "PYTHONPATH")}
        guide = subprocess.run([str(venv / "bin/builder"), "guide"], check=True, capture_output=True, text=True, env=env)
        config = subprocess.run([str(python), "-I", "-c", "from builder.config import platform_sha256; print(platform_sha256())"],
                                check=True, capture_output=True, text=True, env=env)
    reported, sha = json.loads(guide.stdout).get("builder_version"), config.stdout.strip()
    if reported != pin["builder_version"] or sha != pin["platform_config_sha256"]:
        fail(f"self-test: the installed builder reports version {reported} and platform config {sha}, not the pin's")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="build_builder_kit.py", description=__doc__.splitlines()[0])
    parser.add_argument("--tag", required=True)
    parser.add_argument("--image", required=True, help="the builder image this release pushed, by digest")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--source-date-epoch", type=int, default=0, help="the commit time, so the wheel's file times are reproducible")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    version = builder_version()
    if args.tag != f"builder-v{version}":
        fail(f"the tag is {args.tag}, but builder.VERSION is {version}: tag builder-v{version}, or bump the version in a reviewed change")
    if not IMAGE.fullmatch(args.image):
        fail(f"--image must be the pushed image by digest (<registry>/<path>@sha256:<64 hex>), not {args.image!r}")
    platform = PLATFORM.read_bytes()
    if any(marker in platform for marker in PLACEHOLDERS) or "org" not in json.loads(platform):
        fail("platform/platform.json is not configured: run scripts/configure.py first")
    if args.out.exists() and (not args.out.is_dir() or any(args.out.iterdir())):
        fail(f"{args.out} exists and is not an empty folder")
    args.out.mkdir(parents=True, exist_ok=True)
    wheel = build_wheel(version, args.out, args.source_date_epoch)
    requirements = args.out / f"golden_road-{version}-requirements.txt"
    shutil.copyfile(REQUIREMENTS, requirements)
    pin = {"builder_version": version, "wheel": wheel.name, "wheel_sha256": sha256(wheel), "requirements": requirements.name,
           "requirements_sha256": sha256(requirements), "image": args.image, "platform_config_sha256": hashlib.sha256(platform).hexdigest()}
    sys.path.insert(0, str(ROOT / "src"))
    from contracts.schema import validate

    errors = validate("platform-pin.v1", pin)
    if errors:
        fail(f"the pin is not a valid platform-pin/v1 document: {errors}")
    (args.out / "platform-pin.json").write_text(json.dumps(pin, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.self_test:
        self_test(args.out, pin)
    print(json.dumps(pin, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

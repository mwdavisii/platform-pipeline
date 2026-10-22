"""Fakes for the golden-road kit's scripts.

The fake `gh` answers from a JSON world file: each repo's releases and their assets, which release of a repo is marked
latest, and which file digests are attested by which repo, signing workflow and tag (what a real attestation binds). The
fake `uv` runs `uv run ... python` with this Python and builds a fake venv whose `builder` and `python` print what the
world says. Both log every call with the environment they saw. Nothing here reaches GitHub or a package index.
"""

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENTS = ROOT / "bootstrap/golden-road-agents"
PLACEHOLDER = "ORG_" + "PLACEHOLDER"
LIVE = json.loads((ROOT / "platform/platform.json").read_text())
LIVE_ORG = LIVE.get("org")
ORG = LIVE_ORG or "example-org"
HOST = LIVE.get("github_host") or "github.com"
ISSUER = LIVE.get("actions_oidc_issuer") or "https://token.actions.githubusercontent.com"
AGENTS_REPO = f"{ORG}/golden-road-agents"
PIPELINE_REPO = f"{ORG}/platform-pipeline"
SKILL_SIGNER = f"{HOST}/{AGENTS_REPO}/.github/workflows/release-skill.yml"
BUILDER_SIGNER = f"{HOST}/{PIPELINE_REPO}/.github/workflows/release-builder.yml"
# uv runs from the user's home, never the app folder: during interpreter discovery uv executes a .venv/bin/python it finds in the
# working directory or a parent, before anything is verified.
PY = 'uv --directory "$HOME" --python-preference only-managed run --no-project --no-config --python 3.12 python -I'
UV_GUARD = ["--python-preference", "only-managed"]


def identity(signer, tag):
    """The certificate identity of a workflow run: what gh attestation verify --cert-identity compares."""
    return f"https://{signer}@refs/tags/{tag}"

FAKE_GH = "#!" + sys.executable + "\n" + textwrap.dedent(r'''
    import fnmatch, hashlib, json, os, shutil, sys
    args = sys.argv[1:]
    world = json.load(open(os.environ["FAKE_WORLD"]))
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(json.dumps({"tool": "gh", "args": args, "cwd": os.getcwd(), "gh_host": os.environ.get("GH_HOST"),
                              "gh_repo": os.environ.get("GH_REPO"), "prompt_disabled": os.environ.get("GH_PROMPT_DISABLED")}) + "\n")

    def opt(name):
        return [args[i + 1] for i, a in enumerate(args[:-1]) if a == name]

    def fail(message, code=1):
        sys.stderr.write(message + "\n")
        sys.exit(code)

    def network():
        if world["offline"]:
            fail("error connecting to api.github.com")

    if args[:2] == ["auth", "status"]:
        sys.exit(0 if world["signed_in"] else 1)
    if args[:2] == ["repo", "view"]:
        network()
        print(json.dumps({"nameWithOwner": args[2]}))
        sys.exit(0)
    if args[:2] == ["release", "list"]:
        network()
        shown = [r for r in world["releases"].get(opt("--repo")[-1], [])
                 if not (r["draft"] and "--exclude-drafts" in args) and not (r["prerelease"] and "--exclude-pre-releases" in args)]
        print(json.dumps([{"tagName": r["tag"]} for r in shown]))
        sys.exit(0)
    if args[:2] == ["release", "view"]:
        network()
        tag = world["latest"].get(opt("--repo")[-1])
        if tag is None:
            fail("release not found")
        print(tag)
        sys.exit(0)
    if args[:2] == ["release", "download"]:
        network()
        repo = opt("--repo")[-1]
        tag = args[2] if len(args) > 2 and not args[2].startswith("-") else world["latest"].get(repo)
        release = next((r for r in world["releases"].get(repo, []) if r["tag"] == tag), None)
        if release is None:
            fail("release not found")
        names = [n for n in release["assets"] if any(fnmatch.fnmatchcase(n, p) for p in opt("--pattern"))]
        if not names:
            fail("no assets match the file pattern")
        dest = (opt("--dir") or ["."])[-1]
        os.makedirs(dest, exist_ok=True)
        for name in names:
            target = os.path.join(dest, name)
            if os.path.exists(target) and "--clobber" not in args:
                fail(f"{target} already exists")
            shutil.copyfile(release["assets"][name], target)
        sys.exit(0)
    if args[:2] == ["attestation", "verify"]:
        network()
        digest = hashlib.sha256(open(args[2], "rb").read()).hexdigest()
        claim = world["attested"].get(digest)
        who = "https://" + claim["signer"] + "@" + claim["ref"] if claim else None
        ok = (claim is not None and opt("--repo") == [claim["repo"]] and opt("--cert-identity") == [who]
              and opt("--cert-oidc-issuer") == [claim["issuer"]]
              and opt("--source-ref") in ([], [claim["ref"]]) and (not claim["needs_bundle"] or len(opt("--bundle")) == 1))
        if not ok:
            fail('Error: verifying with issuer "sigstore.dev": no matching attestations found')
        if world.get("verify_empty"):
            print("[]")
            sys.exit(0)
        print(json.dumps([{"verificationResult": {"statement": {"subject": [{"digest": {"sha256": digest}}]}}}]))
        sys.exit(0)
    fail("fake gh: no rule for " + " ".join(args), 99)
''')

FAKE_UV = "#!" + sys.executable + "\n" + textwrap.dedent(r'''
    import json, os, sys
    args = sys.argv[1:]
    world = json.load(open(os.environ["FAKE_WORLD"]))
    with open(os.environ["FAKE_LOG"], "a") as log:
        log.write(json.dumps({"tool": "uv", "args": args, "cwd": os.getcwd(), "env_keys": sorted(os.environ),
                              "uv_env": {k: v for k, v in os.environ.items() if k.startswith("UV_")}}) + "\n")
    uv = world["uv"]
    if args == ["--version"]:
        print("uv 0.13.1")
        sys.exit(0)
    rest, opts, i = [], {}, 0
    while i < len(args):
        if args[i] in ("--directory", "--python-preference") and i + 1 < len(args):
            opts[args[i]] = args[i + 1]
            i += 2
        elif args[i] == "--no-config" and not rest:
            i += 1
        else:
            rest.append(args[i])
            i += 1
    rest = [a for a in rest if a != "--no-config"]
    if "--directory" in opts:
        os.chdir(opts["--directory"])
    # What real uv does first: look for a virtual environment from the working directory up, and run its python to ask what it is.
    folder = os.getcwd()
    while True:
        found = os.path.join(folder, ".venv", "bin", "python")
        if os.access(found, os.X_OK):
            import subprocess
            subprocess.run([found, "--version"], capture_output=True)
            break
        if os.path.dirname(folder) == folder:
            break
        folder = os.path.dirname(folder)
    if rest[:1] == ["run"]:
        i = rest.index("python")
        os.execv(sys.executable, [sys.executable, *rest[i + 1:]])
    if rest[:1] == ["venv"]:
        if uv.get("fail") == "venv":
            sys.stderr.write("error: No interpreter found for Python 3.12\n")
            sys.exit(2)
        python = os.path.join(rest[-1], "bin", "python")
        os.makedirs(os.path.dirname(python))
        with open(python, "w") as f:
            f.write("#!/bin/sh\necho %s\n" % uv.get("platform_sha", ""))
        os.chmod(python, 0o755)
        sys.exit(0)
    if rest[:2] == ["pip", "install"]:
        if uv.get("fail") == ("wheel" if "--no-deps" in rest else "requirements"):
            sys.stderr.write("error: Failed to fetch: https://pypi.org/simple/attrs/\n")
            sys.exit(2)
        bindir = os.path.dirname(rest[rest.index("--python") + 1])
        if "--no-deps" in rest:
            for name, body in (("builder", json.dumps({"builder_version": uv.get("version", "")})), ("golden-road", "{}")):
                path = os.path.join(bindir, name)
                with open(path, "w") as f:
                    f.write("#!/bin/sh\necho '%s'\n" % body)
                os.chmod(path, 0o755)
        sys.exit(0)
    sys.stderr.write("fake uv: no rule for " + " ".join(args) + "\n")
    sys.exit(99)
''')


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class World:
    """What the fake gh and uv answer from. Every change is saved at once, so a running script sees it."""

    def __init__(self, tmp_path):
        self.path, self.log, self.store = tmp_path / "world.json", tmp_path / "calls.log", tmp_path / "release-assets"
        self.data = {"releases": {}, "latest": {}, "attested": {}, "offline": False, "signed_in": True, "uv": {}}
        self.home = None
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.data))

    def set(self, **changes):
        self.data.update(changes)
        self.save()

    def release(self, repo, tag, assets, draft=False, prerelease=False, latest=False):
        """Publish (or replace) a release whose assets are {name: bytes}."""
        folder = self.store / repo.replace("/", "--") / tag
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.iterdir():
            old.unlink()
        paths = {}
        for name, data in assets.items():
            (folder / name).write_bytes(data)
            paths[name] = str(folder / name)
        others = [r for r in self.data["releases"].get(repo, []) if r["tag"] != tag]
        self.data["releases"][repo] = others + [{"tag": tag, "assets": paths, "draft": draft, "prerelease": prerelease}]
        if latest:
            self.data["latest"][repo] = tag
        self.save()

    def attest(self, data, repo, signer, tag, needs_bundle=False, issuer=ISSUER):
        """What a real attestation records: these bytes were built by signer, in repo, from refs/tags/<tag>, with a token from issuer."""
        self.data["attested"][sha(data)] = {"repo": repo, "signer": signer, "ref": f"refs/tags/{tag}", "needs_bundle": needs_bundle, "issuer": issuer}
        self.save()

    def calls(self, tool="gh"):
        lines = self.log.read_text().splitlines() if self.log.exists() else []
        return [call for call in map(json.loads, lines) if call["tool"] == tool]


def bundle_tar(version, files=None):
    """A skill bundle tarball laid out as build_release.py lays it out: regular files at the root, no folders."""
    files = {"SKILL.md": b"# skill\n", "VERSION": f"{version}\n".encode(), **(files or {})}
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def publish_skill(world, version, data=None, attest=True, extra=None, latest=True):
    """A skill-v<version> release with its bundle (and any extra assets), each attested by release-skill.yml unless attest is False."""
    data = bundle_tar(version) if data is None else data
    assets = {f"golden-road-skill-{version}.tar.gz": data, **(extra or {})}
    world.release(AGENTS_REPO, f"skill-v{version}", assets, latest=latest)
    if attest:
        for blob in assets.values():
            world.attest(blob, AGENTS_REPO, SKILL_SIGNER, f"skill-v{version}")
    return data


def run_script(path, *args, cwd=None):
    """Run a kit script the way uv runs it (python -I), returning (exit code, the JSON it printed)."""
    out = subprocess.run([sys.executable, "-I", str(path), *args], capture_output=True, text=True, cwd=cwd)
    try:
        doc = json.loads(out.stdout)
    except ValueError:
        doc = {"stdout": out.stdout, "stderr": out.stderr}
    return out.returncode, doc


def load(path):
    """Import a kit script from its path, without writing bytecode next to it."""
    before, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec = importlib.util.spec_from_file_location(f"kit_{Path(path).stem}_{abs(hash(str(path)))}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = before

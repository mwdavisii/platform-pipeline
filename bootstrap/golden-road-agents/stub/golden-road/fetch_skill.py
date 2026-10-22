#!/usr/bin/env python3
"""The golden-road stub: download, verify, cache and load the golden-road skill bundle; or install this stub.

    uv run --no-project --no-config --python 3.12 python -I <skill-dir>/fetch_skill.py load
    uv run --no-project --no-config --python 3.12 python -I fetch_skill.py install --agent claude-code|codex|cursor

Standard library only, so it runs the same on macOS, Linux and Windows. The host, the repository and the signing workflow
are written here and nowhere else: never read from the bundle, the environment or the landing page, so a changed paste
block or bundle can't change what this file trusts.

Output: one JSON object on stdout. Exit 0: done. Exit 1: a download failed verification: stop, don't continue, and tell
the user to contact the platform team. Exit 2: usage error. Exit 3: can't continue (network, access, no release); the
message and the fix say what to do.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

OK, STOP, USAGE, CANT = 0, 1, 2, 3
HOST = "github.com"
OIDC_ISSUER = "https://token.actions.githubusercontent.com"  # the Actions OIDC issuer of HOST; configure.py rewrites both together
ORG = "mwdavisii"
UNCONFIGURED = "ORG_" + "PLACEHOLDER"  # split, so configure.py's rewrite of the placeholder leaves this check alone
SUPPORT = "Don't continue. Tell the user to contact the platform team's support channel, and show them this message."
RETRY = "Check the network and `gh auth status`, then run this again."
VERSION = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")
MAX_DOWNLOAD = 50 * 1024 * 1024

REPO = f"{ORG}/golden-road-agents"
SIGNER = f"{REPO}/.github/workflows/release-skill.yml"
TAG_PREFIX = "skill-v"
BUNDLE = "golden-road-skill-{version}.tar.gz"
STUB_FILES = {"fetch_skill.py": "fetch_skill.py", "golden-road-stub.md": "SKILL.md"}  # release asset: installed name
AGENT_DIRS = {"claude-code": (".claude", "skills"), "codex": (".agents", "skills"), "cursor": (".cursor", "skills")}
INSTALL_DIR = ".golden-road-install"
MAX_UNPACKED = 20 * 1024 * 1024
SAFE_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,99}")


# ---- shared with the bundle's install_builder.py (a test keeps these identical) ----

class Stop(Exception):
    """Stop with an exit code, a message and the fix."""

    def __init__(self, code, message, fix):
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix


def gh_env():
    """gh talks only to the platform host and never prompts. GH_REPO, GH_DEBUG and DEBUG would point it at another repo or print HTTP traffic."""
    env = {k: v for k, v in os.environ.items() if k not in ("GH_HOST", "GH_REPO", "GH_DEBUG", "DEBUG")}
    env.update(GH_HOST=HOST, GH_PROMPT_DISABLED="1", NO_COLOR="1")
    return env


def run(cmd, env=None, timeout=900):
    """Run a command. A missing program, a timeout or an OS error is a failed result, never an exception."""
    try:
        return subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]} is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"{cmd[0]} timed out after {timeout} seconds")
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 126, "", f"{cmd[0]} could not run: {exc.strerror or type(exc).__name__}")


def gh(*args):
    return run(["gh", *args], env=gh_env())


def first_line(text):
    """The first non-empty line, without control characters, at most 300 characters: it may come from GitHub."""
    line = next((part.strip() for part in str(text).splitlines() if part.strip()), "no output")
    return re.sub("[\x00-\x1f\x7f-\x9f]", " ", line)[:300]


def key(version):
    return tuple(int(part) for part in version.split("."))


def newest(tags, prefix):
    """The highest X.Y.Z among tags named <prefix>X.Y.Z, in number order (0.10.0 is newer than 0.9.0), or None."""
    versions = [t[len(prefix):] for t in tags if isinstance(t, str) and t.startswith(prefix) and VERSION.fullmatch(t[len(prefix):])]
    return max(versions, key=key) if versions else None


def list_releases(repo, prefix):
    """The newest published <prefix>X.Y.Z release of repo (drafts and prereleases are never candidates), or None.
    Raises Stop(CANT) when the list can't be read; callers treat that as offline."""
    result = gh("release", "list", "--repo", repo, "--exclude-drafts", "--exclude-pre-releases", "--limit", "100", "--json", "tagName")
    try:
        listed = json.loads(result.stdout) if result.returncode == 0 else None
    except ValueError:
        listed = None
    if not isinstance(listed, list):
        raise Stop(CANT, f"can't read the releases of {repo}: {first_line(result.stderr)}", RETRY)
    return newest([r.get("tagName") for r in listed if isinstance(r, dict)], prefix)


def download(tag, repo, names, dest):
    """Download these assets of one release into dest. A downloaded file that is missing, a link or too large: Stop(STOP)."""
    args = ["release", "download", tag, "--repo", repo, "--dir", str(dest), "--clobber"]
    for name in names:
        args += ["--pattern", name]
    result = gh(*args)
    if result.returncode != 0:
        raise Stop(CANT, f"can't download {', '.join(names)} from release {tag} of {repo}: {first_line(result.stderr)}", RETRY)
    for name in names:
        path = Path(dest) / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_DOWNLOAD:
            raise Stop(STOP, f"release {tag} of {repo} has no usable {name}", SUPPORT)


def verify(path, repo, signer, tag, bundle=None):
    """gh attestation verify: path was built by the workflow signer, in repo, from refs/tags/<tag>. The certificate identity
    is matched exactly (gh's --signer-workflow is a prefix match). Any doubt, or an empty answer: Stop(STOP)."""
    args = ["attestation", "verify", str(path), "--repo", repo, "--cert-identity", f"https://{HOST}/{signer}@refs/tags/{tag}",
            "--cert-oidc-issuer", OIDC_ISSUER, "--source-ref", f"refs/tags/{tag}", "--format", "json"]
    if bundle is not None:
        args += ["--bundle", str(bundle)]
    result = gh(*args)
    try:
        verified = json.loads(result.stdout) if result.returncode == 0 else None
    except ValueError:
        verified = None
    if not isinstance(verified, list) or not verified:
        raise Stop(STOP, f"{Path(path).name} from release {tag} failed verification against {signer}: {first_line(result.stderr)}", SUPPORT)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_atomic(path, data):
    """Replace path's content in one step (a reader never sees half a file); a link at path is replaced, never followed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_json(path):
    """A small JSON file's content, or None when it is missing, a link, not a regular file, too big or not JSON."""
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def failed_versions(root):
    """Versions that failed verification on this machine and have not verified since (failed-<version> markers in root)."""
    root = Path(root)
    if not root.is_dir():
        return []
    names = [p.name[len("failed-"):] for p in root.iterdir() if p.name.startswith("failed-")]
    return [v for v in names if VERSION.fullmatch(v)]


def highest(root):
    """The highest version verified and used on this machine, or None. A lower newest release than this is a rollback."""
    record = read_json(Path(root) / "highest.json")
    version = record.get("version") if isinstance(record, dict) else None
    return version if isinstance(version, str) and VERSION.fullmatch(version) else None


def record_highest(root, version):
    seen = highest(root)
    if seen is None or key(version) > key(seen):
        write_atomic(Path(root) / "highest.json", json.dumps({"version": version}).encode("utf-8"))


def rollback(kind, version, seen):
    return Stop(STOP, f"the newest published {kind} is {version}, older than {seen}, which was verified on this machine before; this may be a rollback",
                SUPPORT)


def emit(doc):
    print(json.dumps(doc, indent=2))


# ---- the skill bundle ----

def skill_cache():
    return Path.home() / ".cache" / "golden-road" / "skill"


def bundle_files(folder):
    """{name: SHA-256} of the unpacked bundle's files, or None if it holds anything but regular files."""
    found = {}
    for path in sorted(Path(folder).rglob("*")):
        if path.is_symlink() or not path.is_file():
            return None
        found[path.relative_to(folder).as_posix()] = sha256(path)
    return found


def cached(root, version):
    """The cached, verified SKILL.md of this version, or None. The record must name this file's repository and signer, and the
    cached tarball and unpacked files must still be the ones it recorded."""
    folder = root / version
    record = read_json(folder / "verified.json")
    skill, tarball = folder / "bundle" / "SKILL.md", folder / BUNDLE.format(version=version)
    if isinstance(record, dict) and record.get("version") == version and record.get("repo") == REPO and record.get("signer") == SIGNER \
            and skill.is_file() and not skill.is_symlink() and tarball.is_file() and not tarball.is_symlink() \
            and record.get("bundle_sha256") == sha256(tarball) and record.get("files") == bundle_files(folder / "bundle"):
        return skill
    return None


def unpack(tarball, dest, version):
    """Extract regular files with plain names only, within a size bound; the bundle's VERSION must be the release's."""
    try:
        with tarfile.open(tarball, "r:gz") as tar:
            members = tar.getmembers()
            for member in members:
                if not member.isfile() or not all(SAFE_PART.fullmatch(part) for part in member.name.split("/")):
                    raise Stop(STOP, f"the skill bundle holds an entry it must not: {first_line(member.name)}", SUPPORT)
            if sum(member.size for member in members) > MAX_UNPACKED:
                raise Stop(STOP, "the skill bundle is too large", SUPPORT)
            tar.extractall(dest, members=members, filter="data")
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        raise Stop(STOP, f"the skill bundle can't be read: {type(exc).__name__}", SUPPORT)
    found = Path(dest) / "VERSION"
    text = found.read_text(encoding="utf-8").strip() if found.is_file() else ""
    if text != version or not (Path(dest) / "SKILL.md").is_file():
        raise Stop(STOP, f"the skill bundle of release {TAG_PREFIX}{version} is not version {version}", SUPPORT)


def fetch(root, version):
    """Download, verify and unpack one version into the cache. A verification failure leaves a failed-<version> marker."""
    tag, name = TAG_PREFIX + version, BUNDLE.format(version=version)
    root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=root, prefix=".download-"))
    try:
        try:
            download(tag, REPO, [name], work)
            verify(work / name, REPO, SIGNER, tag)
            unpack(work / name, work / "bundle", version)
        except Stop as exc:
            if exc.code == STOP:
                write_atomic(root / f"failed-{version}", b"failed verification\n")
            raise
        record = {"version": version, "repo": REPO, "signer": SIGNER, "bundle_sha256": sha256(work / name), "files": bundle_files(work / "bundle")}
        write_atomic(work / "verified.json", json.dumps(record, indent=2).encode("utf-8"))  # inside the folder before it appears
        target = root / version
        if target.exists() and cached(root, version) is None:
            shutil.rmtree(target)
        try:
            os.replace(work, target)
        except OSError:
            skill = cached(root, version)  # a concurrent run finished first
            if skill is None:
                raise
            return skill
        (root / f"failed-{version}").unlink(missing_ok=True)
        return target / "bundle" / "SKILL.md"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def offline(root, reason):
    """The newest verified cached version, unless a newer one failed verification on this machine: then stop."""
    versions = [p.name for p in root.iterdir() if VERSION.fullmatch(p.name) and cached(root, p.name)] if root.is_dir() else []
    if not versions:
        raise Stop(CANT, f"{reason}; no verified golden-road skill is cached on this machine", RETRY)
    best = max(versions, key=key)
    failed = [v for v in failed_versions(root) if key(v) > key(best)]
    if failed:
        raise Stop(STOP, f"{reason}; skill {max(failed, key=key)} failed verification on this machine, so the older cached {best} is not used", SUPPORT)
    seen = highest(root)
    if seen is not None and key(best) < key(seen):
        raise rollback("cached skill", best, seen)
    return best


def load():
    root = skill_cache()
    pinned = os.environ.get("GOLDEN_ROAD_SKILL_VERSION", "")
    warnings, is_offline = [], False
    if pinned:
        if not VERSION.fullmatch(pinned):
            raise Stop(USAGE, "GOLDEN_ROAD_SKILL_VERSION must be a version such as 1.2.3", "Unset GOLDEN_ROAD_SKILL_VERSION: it is for debugging only.")
        version = pinned
        warnings.append(f"GOLDEN_ROAD_SKILL_VERSION pins skill {pinned}: for debugging only.")
    else:
        try:
            version = list_releases(REPO, TAG_PREFIX)
        except Stop as exc:
            version, is_offline = offline(root, exc.message), True
            warnings.append(f"{exc.message}. Using the cached skill {version}, which may be out of date: tell the user.")
        if version is None:
            raise Stop(CANT, f"{REPO} has no published skill release", "Tell the user to ask the platform team.")
        seen = highest(root)
        if not is_offline and seen is not None and key(version) < key(seen):
            raise rollback("skill", version, seen)
    skill = cached(root, version)
    was_cached = skill is not None
    if skill is None:
        skill = fetch(root, version)
    if not pinned:  # a debug pin must not lock out the published skill
        record_highest(root, version)
    return {"status": "ok", "version": version, "skill": str(skill), "bundle_dir": str(skill.parent),
            "cached": was_cached, "offline": is_offline, "warnings": warnings}


# ---- installing this stub ----

def install(agent):
    """Install the stub of the newest skill release into the agent's personal skills folder, each file verified first."""
    version = list_releases(REPO, TAG_PREFIX)
    if version is None:
        raise Stop(CANT, f"{REPO} has no published skill release", "Tell the user to ask the platform team.")
    tag, target = TAG_PREFIX + version, Path.home().joinpath(*AGENT_DIRS[agent], "golden-road")
    with tempfile.TemporaryDirectory() as tmp:
        download(tag, REPO, list(STUB_FILES), Path(tmp))
        for asset in STUB_FILES:
            verify(Path(tmp) / asset, REPO, SIGNER, tag)
        for asset, name in STUB_FILES.items():
            write_atomic(target / name, (Path(tmp) / asset).read_bytes())
    here, removed = Path(__file__).resolve().parent, None
    if here.name == INSTALL_DIR and here != target:  # the download folder INSTALL_FOR_AGENTS.md made in the app folder
        shutil.rmtree(here, ignore_errors=True)
        removed = str(here)
    return {"status": "ok", "agent": agent, "version": version, "installed": str(target / "SKILL.md"), "removed": removed, "warnings": []}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="fetch_skill.py", description="Load the verified golden-road skill, or install this stub.")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("load", help="download (or reuse), verify, and print the path of the skill to follow (the default)")
    install_command = commands.add_parser("install", help="install this stub into an agent's personal skills folder")
    install_command.add_argument("--agent", required=True, choices=sorted(AGENT_DIRS))
    args = parser.parse_args(argv)
    try:
        if ORG == UNCONFIGURED:
            raise Stop(CANT, "this copy of fetch_skill.py is not configured: it names no org",
                       "Install the stub from the platform's golden-road-agents releases (INSTALL_FOR_AGENTS.md).")
        doc = install(args.agent) if args.command == "install" else load()
    except Stop as exc:
        emit({"status": "stop" if exc.code == STOP else "error", "message": exc.message, "fix": exc.fix})
        return exc.code
    except OSError as exc:  # exit 1 is only for a failed verification
        emit({"status": "error", "message": f"can't read or write a file: {exc.strerror or type(exc).__name__}",
              "fix": "Check the folder permissions and free disk space, then run this again."})
        return CANT
    emit(doc)
    return OK


if __name__ == "__main__":
    sys.exit(main())

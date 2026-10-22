#!/usr/bin/env python3
"""Install the platform's pinned builder: download the platform pin, the wheel and its hash-pinned requirements from the
newest published builder release, verify each one, install them into a venv for that version, and put the pin where
golden-road doctor reads it.

    uv run --no-project --no-config --python 3.12 python -I <bundle>/install_builder.py

The pin is attested by the platform's builder release workflow (the repository and workflow below, never read from
anywhere else), using the attestation bundle the release ships. The wheel and the requirements must match the pin's
SHA-256 and carry the same attestation; dependencies are installed by hash only. Standard library only.

Output: one JSON object on stdout. Exit 0: done (it names the builder and golden-road commands to use). Exit 1: a
verification failed, or the installed builder doesn't match the pin: stop, don't continue, and tell the user to contact
the platform team. Exit 2: usage error. Exit 3: can't continue (network, access, the package index); the fix says what to do.
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

OK, STOP, USAGE, CANT = 0, 1, 2, 3
HOST = "github.com"
OIDC_ISSUER = "https://token.actions.githubusercontent.com"  # the Actions OIDC issuer of HOST; configure.py rewrites both together
ORG = "mwdavisii"
UNCONFIGURED = "ORG_" + "PLACEHOLDER"  # split, so configure.py's rewrite of the placeholder leaves this check alone
SUPPORT = "Don't continue. Tell the user to contact the platform team's support channel, and show them this message."
RETRY = "Check the network and `gh auth status`, then run this again."
VERSION = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")
MAX_DOWNLOAD = 50 * 1024 * 1024

AGENTS_REPO = f"{ORG}/golden-road-agents"
PIPELINE_REPO = f"{ORG}/platform-pipeline"
SIGNER = f"{PIPELINE_REPO}/.github/workflows/release-builder.yml"
TAG_PREFIX = "builder-v"
PIN = "platform-pin.json"
ATTESTATION = "builder-v{version}.sigstore.json"
PYTHON = "3.12"  # the builder image's Python: the requirements were compiled for it
# platform-pin/v1, as src/contracts/schemas/platform-pin.v1.json says (a test keeps them equal). No jsonschema here.
PIN_FIELDS = {
    "builder_version": "^[0-9]+\\.[0-9]+\\.[0-9]+$",
    "wheel": "^golden_road-[0-9]+\\.[0-9]+\\.[0-9]+-py3-none-any\\.whl$",
    "wheel_sha256": "^[0-9a-f]{64}$",
    "requirements": "^[A-Za-z0-9._-]+\\.txt$",
    "requirements_sha256": "^[0-9a-f]{64}$",
    "image": "^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$",
    "platform_config_sha256": "^[0-9a-f]{64}$",
}
# uv settings that pass through: where packages come from (PyPI or a proxy) and how to reach it. Hashes are required either
# way. Every other UV_ variable is dropped: UV_NO_VERIFY_HASHES, UV_PYTHON and the like would change what gets installed.
UV_KEPT = ("UV_INDEX", "UV_DEFAULT_INDEX", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL", "UV_CACHE_DIR", "UV_NATIVE_TLS", "UV_HTTP_TIMEOUT",
           "UV_PYTHON_INSTALL_MIRROR")
# What would make the install or the self-check use something other than the new venv and the wheel's own platform config.
DROPPED = ("VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "BUILDER_PLATFORM_CONFIG")
INDEX_FIX = "Check that this machine can reach the Python package index (PyPI, or the proxy the platform team names), then run this again."


# ---- shared with the stub's fetch_skill.py (a test keeps these identical) ----

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


# ---- the builder ----

def cache_root():
    return Path.home() / ".cache" / "golden-road"


def doctor_pin():
    """Where golden-road doctor reads the platform pin by default (its --pin)."""
    return cache_root() / "platform-pin.json"


def venv_dir(version):
    return Path.home() / ".local" / "share" / "golden-road" / f"builder-{version}"


def venv_exe(venv, name, nt=None):
    """A command in the venv: bin/<name> on macOS and Linux, Scripts\\<name>.exe on Windows (nt overrides the check, for tests)."""
    windows = os.name == "nt" if nt is None else nt
    return venv / "Scripts" / f"{name}.exe" if windows else venv / "bin" / name


def tool_env():
    return {k: v for k, v in os.environ.items() if k not in DROPPED and (not k.startswith("UV_") or k in UV_KEPT)}


def remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def check_pin(path, version):
    """The pin, if it is a platform-pin/v1 document for exactly this release; else Stop(STOP)."""
    pin = read_json(path)
    if not isinstance(pin, dict) or set(pin) != set(PIN_FIELDS) \
            or not all(isinstance(pin[k], str) and re.fullmatch(p, pin[k]) for k, p in PIN_FIELDS.items()):
        raise Stop(STOP, f"the platform pin of release {TAG_PREFIX}{version} is not a valid platform-pin/v1 document", SUPPORT)
    if pin["builder_version"] != version or pin["wheel"] != f"golden_road-{version}-py3-none-any.whl" \
            or pin["requirements"] in (PIN, ATTESTATION.format(version=version)):
        raise Stop(STOP, f"the platform pin of release {TAG_PREFIX}{version} doesn't describe that release", SUPPORT)
    return pin


def cached(root, version):
    """This version's cache folder, if its verification record is there and the pin, the wheel and the requirements are still the
    files it recorded and the pin describes."""
    folder = root / version
    record = read_json(folder / "verified.json")
    if not (isinstance(record, dict) and record.get("version") == version and record.get("repo") == PIPELINE_REPO and record.get("signer") == SIGNER):
        return None
    try:
        pin = check_pin(folder / PIN, version)
    except Stop:
        return None
    if record.get("pin_sha256") != sha256(folder / PIN):
        return None
    for name, field in ((pin["wheel"], "wheel_sha256"), (pin["requirements"], "requirements_sha256")):
        path = folder / name
        if path.is_symlink() or not path.is_file() or sha256(path) != pin[field]:
            return None
    return folder


def fetch(root, version):
    """Download and verify the pin, then the wheel and the requirements: the pin's SHA-256 first, then the attestation."""
    tag, attestation = TAG_PREFIX + version, ATTESTATION.format(version=version)
    root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=root, prefix=".download-"))
    try:
        try:
            download(tag, AGENTS_REPO, [PIN, attestation], work)
            verify(work / PIN, PIPELINE_REPO, SIGNER, tag, bundle=work / attestation)
            pin = check_pin(work / PIN, version)
            download(tag, AGENTS_REPO, [pin["wheel"], pin["requirements"]], work)
            for name, field in ((pin["wheel"], "wheel_sha256"), (pin["requirements"], "requirements_sha256")):
                if sha256(work / name) != pin[field]:
                    raise Stop(STOP, f"{name} doesn't match the SHA-256 in the platform pin", SUPPORT)
                verify(work / name, PIPELINE_REPO, SIGNER, tag, bundle=work / attestation)
        except Stop as exc:
            if exc.code == STOP:
                write_atomic(root / f"failed-{version}", b"failed verification\n")
            raise
        record = {"version": version, "repo": PIPELINE_REPO, "signer": SIGNER, "pin_sha256": sha256(work / PIN)}
        write_atomic(work / "verified.json", json.dumps(record, indent=2).encode("utf-8"))  # inside the folder before it appears
        target = root / version
        if target.exists() and cached(root, version) is None:
            shutil.rmtree(target)
        try:
            os.replace(work, target)
        except OSError:
            folder = cached(root, version)  # a concurrent run finished first
            if folder is None:
                raise
            return folder
        (root / f"failed-{version}").unlink(missing_ok=True)
        return target
    finally:
        shutil.rmtree(work, ignore_errors=True)


def newest_cached(root, reason):
    """Offline: the newest verified cached version, unless a newer one failed verification on this machine: then stop."""
    versions = [p.name for p in root.iterdir() if VERSION.fullmatch(p.name) and cached(root, p.name)] if root.is_dir() else []
    if not versions:
        raise Stop(CANT, f"{reason}; no verified builder is cached on this machine", RETRY)
    best = max(versions, key=key)
    failed = [v for v in failed_versions(root) if key(v) > key(best)]
    if failed:
        raise Stop(STOP, f"{reason}; builder {max(failed, key=key)} failed verification on this machine, so the older cached {best} is not used", SUPPORT)
    seen = highest(root)
    if seen is not None and key(best) < key(seen):
        raise rollback("cached builder", best, seen)
    return best


def install(folder, version, pin):
    """The venv for this version: reused when its record matches the pin, else built from scratch and checked."""
    venv = venv_dir(version)
    record = read_json(venv / "installed.json")
    if isinstance(record, dict) and record.get("version") == version and record.get("wheel_sha256") == pin["wheel_sha256"] \
            and record.get("platform_config_sha256") == pin["platform_config_sha256"] and venv_exe(venv, "builder").is_file():
        return venv, True
    remove(venv)
    env, python = tool_env(), str(venv_exe(venv, "python"))
    # uv starts in the home folder, never the app folder (it runs a .venv/bin/python it finds from its working directory up, before
    # anything is checked), and selects only a Python it manages itself (it downloads CPython 3.12 on first use), so the app
    # folder's interpreters are never executed. It may still look at ~/.venv; VIRTUAL_ENV and CONDA_PREFIX are dropped above.
    uv = ["uv", "--no-config", "--directory", str(Path.home()), "--python-preference", "only-managed"]
    steps = (
        ("venv", [*uv, "venv", "--no-project", "--python", PYTHON, str(venv)]),
        ("pip install", [*uv, "pip", "install", "--python", python, "--require-hashes", "--only-binary", ":all:", "-r", str(folder / pin["requirements"])]),
        ("pip install", [*uv, "pip", "install", "--python", python, "--no-deps", "--no-index", str(folder / pin["wheel"])]),
    )
    for name, cmd in steps:
        result = run(cmd, env=env)
        if result.returncode != 0:
            remove(venv)
            raise Stop(CANT, f"uv {name} failed: {first_line(result.stderr)}", INDEX_FIX)
    guide = run([str(venv_exe(venv, "builder")), "guide"], env=env)
    config = run([python, "-I", "-c", "from builder.config import platform_sha256; print(platform_sha256())"], env=env)
    try:
        reported = json.loads(guide.stdout).get("builder_version") if guide.returncode == 0 else None
    except (ValueError, AttributeError):
        reported = None
    if reported != version or config.returncode != 0 or config.stdout.strip() != pin["platform_config_sha256"]:
        remove(venv)
        raise Stop(STOP, f"the installed builder doesn't match the platform pin of {TAG_PREFIX}{version} (its version or platform config)", SUPPORT)
    record = {"version": version, "wheel_sha256": pin["wheel_sha256"], "platform_config_sha256": pin["platform_config_sha256"]}
    write_atomic(venv / "installed.json", json.dumps(record, indent=2).encode("utf-8"))
    return venv, False


def install_builder():
    root = cache_root() / "builder"
    warnings, is_offline = [], False
    try:
        version = list_releases(AGENTS_REPO, TAG_PREFIX)
    except Stop as exc:
        version, is_offline = newest_cached(root, exc.message), True
        warnings.append(f"{exc.message}. Using the cached builder {version}, which may not be the platform's current pin: tell the user.")
    if version is None:
        raise Stop(CANT, f"{AGENTS_REPO} has no published builder release", "Tell the user to ask the platform team.")
    seen = highest(root)
    if not is_offline and seen is not None and key(version) < key(seen):
        raise rollback("builder", version, seen)
    folder = cached(root, version)
    was_cached = folder is not None
    if folder is None:
        folder = fetch(root, version)
    pin = check_pin(folder / PIN, version)
    venv, venv_reused = install(folder, version, pin)
    write_atomic(doctor_pin(), (folder / PIN).read_bytes())
    record_highest(root, version)
    return {"status": "ok", "builder_version": version, "org": ORG, "host": HOST,
            "builder": str(venv_exe(venv, "builder")), "golden_road": str(venv_exe(venv, "golden-road")),
            "pin": str(doctor_pin()), "image": pin["image"], "cached": was_cached and venv_reused, "offline": is_offline, "warnings": warnings}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="install_builder.py", description="Install the platform's pinned builder, verified, into a venv for its version.")
    parser.parse_args(argv)
    try:
        if ORG == UNCONFIGURED:
            raise Stop(CANT, "this bundle is not configured: it names no org", "Tell the platform team: the skill release was built from an unconfigured tree.")
        doc = install_builder()
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

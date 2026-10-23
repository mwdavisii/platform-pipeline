"""Read-only git queries over the checked-out PR. Every ref is resolved to a SHA first, and paths are literal."""

import os
import re
import subprocess

from golden_road.io import InputError

MAX_BLOB_BYTES = 5_000_000
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_SHA = re.compile(r"^[0-9a-f]{40}$")
# Inherited GIT_* variables (GIT_DIR, GIT_WORK_TREE, GIT_EXTERNAL_DIFF, ...) are dropped; system config is ignored.
_ENV = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, "GIT_LITERAL_PATHSPECS": "1",
        "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_NOSYSTEM": "1"}
_HARDEN = ("-c", "core.quotepath=off", "-c", "core.fsmonitor=false", "-c", "diff.external=", "-c", "core.attributesFile=/dev/null")
# No external diff drivers or textconv filters: .gitattributes in the PR must not run anything.
# --text: a `-diff`/`binary` attribute or a NUL byte must not hide added lines from the scans.
_DIFF = ("diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--text")


class Unreadable(InputError):
    """The path exists at that commit but can't be read as text (too large, or not a regular blob)."""


def _git(repo, *args, check=True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(repo), *_HARDEN, *args], capture_output=True,
                              check=check, timeout=120, env=_ENV)
    except subprocess.CalledProcessError as exc:
        raise InputError(f"git {args[0]} failed: {exc.stderr.decode('utf-8', 'replace').strip()[:200]}") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InputError(f"git {args[0]} failed: {type(exc).__name__}") from exc


def _sha(value: str) -> str:
    if not _SHA.match(value):
        raise InputError("git did not return a commit SHA")
    return value


def rev(repo, ref: str) -> str:
    out = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").stdout
    return _sha(out.decode().strip())


def parents(repo, sha: str) -> list[str]:
    """The parent commits of a commit, in order (a merge commit has two: base first, then the PR head)."""
    out = _git(repo, "rev-list", "--parents", "-n", "1", _sha(sha)).stdout.decode().split()
    return [_sha(p) for p in out[1:]]


def tree(repo, sha: str) -> str:
    return _sha(_git(repo, "rev-parse", "--verify", f"{_sha(sha)}^{{tree}}").stdout.decode().strip())


def changes(repo, base: str, head: str) -> list[dict]:
    """Changed paths between two commits: status A, M, D, or T (no rename detection, so a rename is D plus A)."""
    out = _git(repo, *_DIFF, "--name-status", "-z", _sha(base), _sha(head)).stdout
    parts = [os.fsdecode(part) for part in out.split(b"\0")]
    result = []
    for status, path in zip(parts[0::2], parts[1::2]):
        if status:
            result.append({"status": status[0], "path": path})
    return result


def numstat(repo, base: str, head: str) -> list[tuple[int | None, int | None, str]]:
    """(added, removed, path) for every changed path; a binary file is (None, None, path). Unlike `_DIFF` there is no --text,
    so git can say a file is binary; the other hardening is the same (no drivers, no textconv, no renames)."""
    out = _git(repo, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--numstat", "-z",
               _sha(base), _sha(head)).stdout
    rows = []
    for record in out.split(b"\0"):
        if not record:
            continue
        try:
            added, removed, path = record.split(b"\t", 2)
            rows.append((None if added == b"-" else int(added), None if removed == b"-" else int(removed), os.fsdecode(path)))
        except ValueError as exc:
            raise InputError("git diff --numstat returned a line the scorecard can't read") from exc
    return rows


def show(repo, sha: str, path: str) -> str | None:
    """A file's text at a commit, or None if it doesn't exist there. Raises Unreadable if it exists but is too large
    or isn't a regular blob, so callers can fail toward scrutiny instead of treating it as absent."""
    spec = f"{_sha(sha)}:{path}"
    kind = _git(repo, "cat-file", "-t", spec, check=False)
    if kind.returncode != 0:
        return None
    if kind.stdout.strip() != b"blob":
        raise Unreadable(f"{path} is not a regular file")
    if int(_git(repo, "cat-file", "-s", spec).stdout.strip()) > MAX_BLOB_BYTES:
        raise Unreadable(f"{path} is larger than {MAX_BLOB_BYTES} bytes")
    return _git(repo, "cat-file", "blob", spec).stdout.decode("utf-8", "replace")


REGULAR_MODES = (b"100644", b"100755")


def stored_file(repo, sha: str, path: str, cap: int) -> bytes | None:
    """The stored bytes of a root file at a commit (never the working tree, which .gitattributes can re-encode), or None if there is
    no such entry. Raises Unreadable for a symlink (120000), a submodule (160000), a directory, or a blob over `cap` bytes. The
    blob is read by its object ID with `cat-file blob`, which runs no filter, driver or textconv."""
    out = _git(repo, "ls-tree", "-z", _sha(sha), "--", path).stdout
    if not out:
        return None
    meta, _, name = out.rstrip(b"\0").partition(b"\t")
    mode, _, rest = meta.partition(b" ")
    kind, _, blob = rest.partition(b" ")
    if os.fsdecode(name) != path:
        return None
    if mode not in REGULAR_MODES or kind != b"blob":
        raise Unreadable(f"{path} is not a regular file")
    blob_id = _sha(blob.decode())
    if int(_git(repo, "cat-file", "-s", blob_id).stdout.strip()) > cap:
        raise Unreadable(f"{path} is larger than {cap} bytes")
    return _git(repo, "cat-file", "blob", blob_id).stdout


def added_lines(repo, base: str, head: str, path: str) -> list[tuple[int, str]]:
    """(line number in head, text) for every line the change adds to `path`."""
    out = _git(repo, *_DIFF, "-U0", _sha(base), _sha(head), "--", path).stdout.decode("utf-8", "replace")
    lines, number = [], None
    for line in out.splitlines():
        hunk = _HUNK.match(line)
        if hunk:
            number = int(hunk.group(1))
        elif number is not None and line.startswith("+"):
            lines.append((number, line[1:]))
            number += 1
    return lines


def diff_capped(repo, base: str, head: str, max_bytes: int | None = None) -> tuple[str, bool]:
    """(the unified diff between two commits, whether it was cut at `max_bytes`), with the same hardening as `changes`
    (no drivers, no textconv, --text). The output is streamed: at the cap the process is killed, so a huge diff never sits in memory."""
    import threading

    args = ["git", "-C", str(repo), *_HARDEN, *_DIFF, "-U3", _sha(base), _sha(head)]
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_ENV)
    except OSError as exc:
        raise InputError(f"git diff failed: {type(exc).__name__}") from exc
    timer = threading.Timer(120, proc.kill)
    timer.start()
    try:
        data = proc.stdout.read() if max_bytes is None else proc.stdout.read(max_bytes + 1)
        cut = max_bytes is not None and len(data) > max_bytes
        if cut:
            proc.kill()
        code = proc.wait()
    finally:
        timer.cancel()
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if not cut and code != 0:
        raise InputError("git diff failed")
    return (data[:max_bytes] if cut else data).decode("utf-8", "replace"), cut


def diff(repo, base: str, head: str) -> str:
    """The whole unified diff between two commits (uncapped; kept for tests, `assess` uses `diff_capped`)."""
    return diff_capped(repo, base, head)[0]

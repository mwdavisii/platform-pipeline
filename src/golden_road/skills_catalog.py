"""The two readers of the platform's skills catalog, for golden-road compose and deploy-verify (spec 2026-10-31 S1, S5). Each returns
(skill.yaml as a dict, {path: text}) for a skill at a catalog commit, the files without skill.yaml, which is the catalog's metadata
and is never mounted. compose checks the files themselves (golden_road.compose.skill_files_problem); a reader checks the catalog.

MockCatalog reads the fixture bundled in the wheel (data/mock_skills_catalog.json), so deploy-verify works inside the builder image
with --network none; the portal holds the same bytes. GitCatalog reads a checkout of the real catalog repo (skills/<name>/...) at a
commit on its main branch (a reviewed, merged change), never its working tree. default_catalog picks the checkout named by GOLDEN_ROAD_SKILLS_CATALOG, else the mock. Offline."""

import json
import os
import re
import subprocess
from pathlib import Path

import yaml

from contracts.schema import validate

from .compose import SKILL_MAX_BYTES, SKILL_MAX_FILES, UniqueKeyLoader

MOCK = Path(__file__).parent / "data/mock_skills_catalog.json"
ENV = "GOLDEN_ROAD_SKILLS_CATALOG"
MOCK_SCHEMA = "mock-skills-catalog/v1"
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
COMMIT = re.compile(r"[0-9a-f]{40}")
MAIN_REF = "refs/remotes/origin/main"  # what actions/checkout with fetch-depth: 0 leaves for the catalog's main
REF = re.compile(r"refs/[A-Za-z0-9._/-]+")
META = "skill.yaml"


class CatalogError(ValueError):
    pass


def _not_found(name, commit) -> CatalogError:
    return CatalogError(f"skill {str(name)[:80]} is not in the catalog at {str(commit)[:12]}")


def _meta(name: str, text: str, where: str) -> dict:
    """skill.yaml's text as a valid skill/v1 naming exactly this skill."""
    try:
        doc = yaml.load(text, Loader=UniqueKeyLoader)
    except (yaml.YAMLError, RecursionError) as exc:
        raise CatalogError(f"{where}: {META} is not YAML ({type(exc).__name__})") from None
    errors = validate("skill.v1", doc)
    if errors:
        raise CatalogError(f"{where}: {META} is not a valid skill/v1: {errors[0]}"[:300])
    if doc["name"] != name:
        raise CatalogError(f"{where}: {META} names {doc['name']}, not {name}")
    return doc


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise CatalogError(f"the mock skills catalog names {str(key)[:80]!r} twice")
        out[key] = value
    return out


class MockCatalog:
    """The bundled fixture: {"schema", "repo", "main", "commits": {commit: {"skills": {name: {path: text}}}}}, checked whole on load."""

    name = "mock-skills-catalog"

    def __init__(self, commits: dict):
        self._commits = commits

    @classmethod
    def load(cls, path=MOCK) -> "MockCatalog":
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_no_duplicates)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CatalogError(f"the mock skills catalog can't be read ({type(exc).__name__})") from None
        if not isinstance(doc, dict) or doc.get("schema") != MOCK_SCHEMA:
            raise CatalogError(f"the mock skills catalog is not {MOCK_SCHEMA}")
        commits = doc.get("commits")
        if not isinstance(commits, dict) or not all(isinstance(c, str) and COMMIT.fullmatch(c) for c in commits):
            raise CatalogError("the mock skills catalog's commits are not keyed by 40-hex commit")
        if not isinstance(doc.get("main"), str) or doc["main"] not in commits:
            raise CatalogError("the mock skills catalog's main is not one of its commits")
        for commit, entry in commits.items():
            skills = entry.get("skills") if isinstance(entry, dict) else None
            if not isinstance(skills, dict):
                raise CatalogError(f"the mock skills catalog's commit {commit[:12]} has no skills mapping")
            for name, files in skills.items():
                where = f"the mock skills catalog's {str(name)[:80]} at {commit[:12]}"
                if not NAME.fullmatch(name):
                    raise CatalogError(f"{where}: not a catalog name")
                if not isinstance(files, dict) or not all(isinstance(t, str) for t in files.values()):
                    raise CatalogError(f"{where}: its files are not a mapping of path to text")
                if META not in files:
                    raise CatalogError(f"{where}: no {META}")
                _meta(name, files[META], where)
        return cls(commits)

    def skill(self, name: str, commit: str) -> tuple[dict, dict]:
        if not (isinstance(name, str) and NAME.fullmatch(name) and isinstance(commit, str) and COMMIT.fullmatch(commit)):
            raise _not_found(name, commit)
        files = self._commits.get(commit, {}).get("skills", {}).get(name)
        if files is None:
            raise _not_found(name, commit)
        meta = _meta(name, files[META], f"skill {name} at {commit[:12]}")
        return meta, {path: text for path, text in files.items() if path != META}


class GitCatalog:
    """A checkout of the catalog repo (full history, read-only). Reads skills/<name>/ at a commit through git's object store. The pin
    must be a commit object (not a tree or a tag) reachable from main_ref, so a commit on an unmerged branch, which security never
    approved, is "not in the catalog". Only regular files (mode 100644; a symlink, an executable or a submodule is refused), UTF-8,
    each within compose's limits.

    Not wired yet. Wiring it means binding/v2's $defs.skill_verified_by gains "skills-catalog" in the same change (today it is only
    "mock-skills-catalog"), in src/contracts and the portal's vendored copy, with the portal's real catalog reader: deploy-verify
    refuses a pin another catalog verified, so a schema that only knows the mock would refuse every real pin."""

    name = "skills-catalog"

    def __init__(self, path, main_ref: str = MAIN_REF):
        if not isinstance(main_ref, str) or not REF.fullmatch(main_ref) or ".." in main_ref:
            raise CatalogError(f"{str(main_ref)[:80]!r} is not a git ref under refs/")
        self.path = Path(path)
        self.main_ref = main_ref

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        # An argument list, never a shell; every argument was checked first. safe.directory: the checkout may be mounted read-only
        # into a container under another uid. fsmonitor off: reading objects never runs a hook.
        cmd = ["git", "-c", f"safe.directory={self.path.resolve()}", "-c", "core.fsmonitor=false", "-C", str(self.path), *args]
        return subprocess.run(cmd, capture_output=True)

    def _git(self, *args: str) -> bytes:
        result = self._run(*args)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, args)
        return result.stdout

    def skill(self, name: str, commit: str) -> tuple[dict, dict]:
        if not (isinstance(name, str) and NAME.fullmatch(name) and isinstance(commit, str) and COMMIT.fullmatch(commit)):
            raise _not_found(name, commit)
        try:
            kind = self._git("cat-file", "-t", commit).strip()
            on_main = self._run("merge-base", "--is-ancestor", commit, self.main_ref).returncode == 0
        except (subprocess.CalledProcessError, OSError):
            raise _not_found(name, commit) from None
        if kind != b"commit" or not on_main:
            raise _not_found(name, commit)
        prefix = f"skills/{name}/"
        try:
            listing = self._git("ls-tree", "-r", "-l", "-z", "--full-tree", commit, "--", prefix)
        except (subprocess.CalledProcessError, OSError):
            raise _not_found(name, commit) from None
        where = f"skill {name} at {commit[:12]}"
        entries = []
        for line in filter(None, listing.split(b"\0")):
            head, _, raw_path = line.partition(b"\t")
            mode, kind, sha, size = head.decode("ascii").split()
            path = raw_path.decode("utf-8", errors="replace")[len(prefix):]
            if mode == "120000":
                raise CatalogError(f"{where}: {path[:80]} is a symlink")
            if mode != "100644" or kind != "blob":
                raise CatalogError(f"{where}: {path[:80]} is not a regular file (mode {mode})")
            if int(size) > SKILL_MAX_BYTES:
                raise CatalogError(f"{where}: {path[:80]} is more than {SKILL_MAX_BYTES // 1024} KiB")
            entries.append((path, sha))
        if not entries:
            raise _not_found(name, commit)
        if len(entries) > SKILL_MAX_FILES + 1:  # the files and skill.yaml
            raise CatalogError(f"{where}: more than {SKILL_MAX_FILES} files")
        files = {}
        for path, sha in entries:
            try:
                files[path] = self._git("cat-file", "blob", sha).decode("utf-8")
            except UnicodeDecodeError:
                raise CatalogError(f"{where}: {path[:80]} is not UTF-8") from None
            except (subprocess.CalledProcessError, OSError):
                raise CatalogError(f"{where}: {path[:80]} can't be read from git") from None
        if META not in files:
            raise CatalogError(f"{where}: no {META}")
        meta = _meta(name, files.pop(META), where)
        return meta, files


def default_catalog():
    """GitCatalog over the checkout GOLDEN_ROAD_SKILLS_CATALOG names (a directory), else the bundled MockCatalog."""
    path = os.environ.get(ENV)
    if path:
        if not Path(path).is_dir():
            raise CatalogError(f"{ENV} is {path[:200]!r}, which is not a directory")
        return GitCatalog(path)
    return MockCatalog.load()

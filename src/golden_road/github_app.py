"""GitHub App installation tokens, minted in the builder image (central review C7): one repository and only the named
permissions. The private key comes from GOLDEN_ROAD_APP_KEY, signs one short JWT, and is never printed, logged or written."""

import json
import os
import re
import time

from . import checks
from .io import InputError

KEY_ENV = "GOLDEN_ROAD_APP_KEY"
# What the scorecard's steps may ask for (C1 plus commit statuses for Gate 1). Anything else is refused before any call.
ALLOWED = {"contents": ("read",), "pull_requests": ("read", "write"), "checks": ("read", "write"), "statuses": ("read",), "issues": ("write",)}
# The deploy App's one set (golden-road-deploy's bump workflow): it pushes a pin branch and opens its pull request. contents=write is
# accepted only as exactly this set, never alone or alongside anything the scorecard asks for, and only for a repo named
# golden-road-deploy.
DEPLOY_PIN = {"contents": "write", "pull_requests": "write"}
DEPLOY_REPO_SUFFIX = "/golden-road-deploy"
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# Installation tokens are opaque: since GitHub's stateless format (2026-10) they are ghs_<app id>_<JWT>, about 520 characters with dots.
# Only the prefix, a URL-safe alphabet (no whitespace, so the token can't break a header or an output line) and a sane length are checked.
_TOKEN = re.compile(r"ghs_[A-Za-z0-9_.-]{20,4096}")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}$")
_EXPIRES = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def parse_permissions(pairs, repo=None) -> dict:
    perms: dict[str, str] = {}
    for pair in pairs:
        name, _, level = pair.partition("=")
        if name in perms or (level not in ALLOWED.get(name, ()) and DEPLOY_PIN.get(name) != level):
            raise InputError(f"permission {pair!r} is not one the scorecard or the deploy App may ask for")
        perms[name] = level
    if not perms:
        raise InputError("name at least one permission (name=read or name=write)")
    if perms.get("contents") == "write":
        if perms != DEPLOY_PIN:
            raise InputError("contents=write is only for the deploy App, with exactly contents=write and pull_requests=write")
        if not isinstance(repo, str) or not repo.lower().endswith(DEPLOY_REPO_SUFFIX):
            raise InputError("contents=write is only for the deploy App's own repo, <org>/golden-road-deploy")
    return perms


def app_jwt(app_id: int, key_pem: str, now: int) -> str:
    try:
        import jwt
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}, key_pem, algorithm="RS256")
    except Exception:  # noqa: BLE001 - a key-parsing message could quote the key
        raise InputError("the App private key can't sign a token (it must be the App's RSA private key in PEM form)") from None


def _repo(repo) -> tuple[str, str]:
    if not isinstance(repo, str) or not _REPO.match(repo) or any(p in (".", "..") for p in repo.split("/")):
        raise InputError("repo must be owner/name")
    owner, name = repo.split("/")
    return owner, name


def installation_token(api_url: str, app_id: int, key_pem: str, repo: str, permissions: dict, *, now=None, opener=None) -> dict:
    if type(app_id) is not int or app_id <= 0:
        raise InputError("the App id must be a positive number")
    owner, name = _repo(repo)
    if permissions.get("contents") == "write" and (permissions != DEPLOY_PIN or not repo.lower().endswith(DEPLOY_REPO_SUFFIX)):
        raise InputError("contents=write is only for the deploy App's own repo, <org>/golden-road-deploy")
    if not key_pem:
        raise InputError(f"{KEY_ENV} is not set")
    app = checks.GitHubAPI(api_url, app_jwt(app_id, key_pem, int(time.time() if now is None else now)), opener=opener or checks._open)
    # The App's slug, read once with the App JWT: its comments are authored by `<slug>[bot]`, which the report job and remediate match on.
    info = app.get("/app")
    slug = info.get("slug") if isinstance(info, dict) else None
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise InputError("GitHub returned no usable App slug")
    try:
        found = app.get(f"/repos/{owner}/{name}/installation")
    except checks.APIError as exc:
        if exc.status == 404:
            raise InputError(f"the App is not installed on {repo}") from None
        raise
    installation = found.get("id") if isinstance(found, dict) else None
    if type(installation) is not int or installation <= 0:
        raise InputError("GitHub returned an unexpected installation")
    doc = app.send("POST", f"/app/installations/{installation}/access_tokens", {"repositories": [name], "permissions": permissions})
    doc = doc if isinstance(doc, dict) else {}
    token, expires = doc.get("token"), doc.get("expires_at")
    if not isinstance(token, str) or not _TOKEN.fullmatch(token) or not isinstance(expires, str) or not _EXPIRES.match(expires):
        raise InputError("GitHub returned no installation token")
    granted = dict(doc.get("permissions") or {})
    if granted.get("metadata") == "read":
        granted.pop("metadata")  # GitHub adds it to every token
    repos = [r.get("full_name", "").lower() for r in doc.get("repositories") or [] if isinstance(r, dict)]
    if granted != permissions or repos != [repo.lower()]:
        raise InputError("GitHub granted a token broader than asked for; refusing it")
    return {"token": token, "expires_at": expires, "repository": repo, "permissions": dict(permissions), "app_slug": slug}


def write_token(path, doc: dict) -> None:
    """The token file: created new, 0600, never replaced in place."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(doc, sort_keys=True) + "\n")

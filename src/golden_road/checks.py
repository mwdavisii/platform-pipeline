"""Gate 1 input: wait for every required check on a commit to finish, then pass only if all succeeded.

A required entry names its source. `{"name", "app_id"}` matches only check runs created by that
GitHub App; `{"context", "creator"}` matches only statuses created by that login. Items from any
other source are ignored, so a workflow with `checks: write` can't post a newer success under a
required name. Anything that is not a success when the wait ends (failed, still pending, or never
reported) fails the gate.
"""

import json
import math
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from .io import InputError, load_json

MAX_PAGES = 10
MAX_REQUIRED = 50
_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')
_STATUS_STATE = {"success": "success", "pending": "pending", "failure": "failure", "error": "failure"}
_BAD_URL_CHARS = re.compile(r"[\x00-\x20\x7f\\]")
_CREATOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*(\[bot\])?$")
MAX_BACKOFF = 30
DEFAULT_RETRY_SECONDS = 120
_SEVERITY = {"success": 0, "pending": 1, "missing": 2, "failure": 3}


class APIError(InputError):
    """A GitHub API failure that carries the HTTP status, so a caller can tell a 404 from a 502."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The token must never ride a redirect to another host, so no redirect is followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(request, timeout):
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


def _text_ok(value) -> bool:
    return (isinstance(value, str) and bool(value) and value == value.strip()
            and not any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in value))


def validate_required(entries, where: str = "required checks") -> list[dict]:
    """Strictly validate required-check entries; anything unexpected is an error."""
    if not isinstance(entries, list) or len(entries) > MAX_REQUIRED:
        raise InputError(f"{where} must be a list of at most {MAX_REQUIRED} entries")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise InputError(f"{where} has an entry that is not an object")
        keys = set(entry)
        if keys == {"name", "app_id"}:
            app_id = entry["app_id"]
            ok = _text_ok(entry["name"]) and type(app_id) is int and app_id > 0
            ident = ("run", entry["name"], app_id if ok else None)
        elif keys == {"context", "creator"}:
            ok = _text_ok(entry["context"]) and isinstance(entry["creator"], str) and bool(_CREATOR.fullmatch(entry["creator"]))
            ident = ("status", entry["context"], entry["creator"] if ok else None)
        else:
            ok, ident = False, None
        if not ok or ident in seen:
            raise InputError(f"{where} has an invalid or duplicate entry")
        seen.add(ident)
    return entries


def load_required(path) -> list[dict]:
    doc = load_json(path)
    if not isinstance(doc, dict) or set(doc) != {"required"}:
        raise InputError(f'{path} must be {{"required": [entries]}}')
    return validate_required(doc["required"], str(path))


class GitHubAPI:
    def __init__(self, api_url: str, token: str, opener=_open, sleep=time.sleep, clock=time.monotonic):
        if not token:
            raise InputError("GITHUB_TOKEN is not set")
        self.api_url = api_url.rstrip("/")
        try:
            base = urllib.parse.urlsplit(self.api_url)
            valid = ("?" not in api_url and "#" not in api_url and base.scheme == "https" and bool(base.hostname) and "@" not in base.netloc
                     and not base.query and not base.fragment and not _BAD_URL_CHARS.search(self.api_url))
            base.port  # noqa: B018 - raises ValueError on a bad port
        except ValueError:
            valid = False
        if not valid:
            raise InputError("the GitHub API URL must be an https URL without credentials, query, or fragment")
        self._base = base
        self.token = token
        self.opener = opener
        self.sleep = sleep
        self.clock = clock
        self.deadline: float | None = None

    def set_deadline(self, seconds: float) -> None:
        """Bound transient-error retries to this many seconds from now."""
        self.deadline = self.clock() + seconds

    def _inside_base(self, url: str) -> bool:
        if not url.startswith(self.api_url + "/") or _BAD_URL_CHARS.search(url):
            return False
        try:
            parts = urllib.parse.urlsplit(url)
            parts.port  # noqa: B018
        except ValueError:
            return False
        return (parts.scheme == self._base.scheme and parts.netloc.lower() == self._base.netloc.lower()
                and (parts.path + "/").startswith(self._base.path + "/"))

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28"}

    def _get(self, url: str):
        if not self._inside_base(url):
            raise InputError("refusing to follow a GitHub API link to another host")
        request = urllib.request.Request(url, headers=self._headers())
        deadline = self.deadline if self.deadline is not None else self.clock() + DEFAULT_RETRY_SECONDS
        delay = 1
        while True:
            try:
                with self.opener(request, timeout=30) as response:
                    body = response.read(20_000_000)
                    link = response.headers.get("Link", "") or ""
                break
            except urllib.error.HTTPError as exc:
                if not (exc.code >= 500 or exc.code == 429):
                    raise APIError(f"GitHub API returned {exc.code}", exc.code) from exc
                failure = APIError(f"GitHub API returned {exc.code}", exc.code)
                cause: BaseException = exc
            except (urllib.error.URLError, OSError) as exc:
                failure = InputError(f"GitHub API unreachable: {type(exc).__name__}")
                cause = exc
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise failure from cause
            self.sleep(min(delay, MAX_BACKOFF, remaining))
            delay = min(delay * 2, MAX_BACKOFF)
        try:
            return json.loads(body), link
        except (ValueError, RecursionError) as exc:
            raise InputError("GitHub API returned invalid JSON") from exc

    def get(self, path: str):
        return self._get(self.api_url + path)[0]

    def send(self, method: str, path: str, body: dict | None = None):
        """One POST, PATCH or PUT, never retried: a POST that reached GitHub must not be made twice. The JSON answer, or None."""
        if method not in ("POST", "PATCH", "PUT"):
            raise InputError(f"unsupported method {method!r}")
        url = f"{self.api_url}{path}"
        if not self._inside_base(url):
            raise InputError("refusing to send to a URL outside the GitHub API")
        request = urllib.request.Request(url, data=json.dumps(body or {}).encode("utf-8"), method=method,
                                         headers={**self._headers(), "Content-Type": "application/json"})
        try:
            with self.opener(request, timeout=30) as response:
                raw = response.read(5_000_000)
        except urllib.error.HTTPError as exc:
            raise APIError(f"GitHub API returned {exc.code}", exc.code) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise InputError(f"GitHub API unreachable: {type(exc).__name__}") from exc
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise InputError("GitHub API returned invalid JSON") from exc

    def paged(self, path: str, key: str | None = None) -> list:
        url = f"{self.api_url}{path}{'&' if '?' in path else '?'}per_page=100"
        items: list = []
        for _ in range(MAX_PAGES):
            doc, link = self._get(url)
            if key:
                page = doc.get(key) if isinstance(doc, dict) else None
            else:
                page = doc
            if not isinstance(page, list):
                raise InputError(f"unexpected GitHub API response for {path}")
            items.extend(page)
            match = _NEXT.search(link)
            if not match:
                return items
            url = match.group(1)
        raise InputError(f"too many pages for {path}")


def _valid_id(item) -> bool:
    value = item.get("id")
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _latest_state(items: list[dict], state_of) -> str | None:
    """State of the newest item. A bad id, or a tie at the highest id, resolves to the worst state."""
    if not items:
        return None
    if not all(_valid_id(i) for i in items):
        return "failure"
    top = max(i["id"] for i in items)
    return max((state_of(i) for i in items if i["id"] == top), key=_SEVERITY.__getitem__)


def _run_state(item) -> str:
    if item.get("status") != "completed":
        return "pending"
    return "success" if item.get("conclusion") == "success" else "failure"


def _status_state(item) -> str:
    return _STATUS_STATE.get(item.get("state"), "failure")


def _check_target(repo: str, sha: str) -> None:
    if not isinstance(repo, str) or not _REPO.match(repo) or any(part in (".", "..") for part in repo.split("/")):
        raise InputError("invalid repository name")
    if not isinstance(sha, str) or not _SHA.match(sha):
        raise InputError("invalid commit SHA")


def _from_app(item, app_id: int) -> bool:
    app = item.get("app")
    return isinstance(app, dict) and type(app.get("id")) is int and app["id"] == app_id


def _from_creator(item, login: str) -> bool:
    creator = item.get("creator")
    return isinstance(creator, dict) and isinstance(creator.get("login"), str) and creator["login"] == login


def snapshot(api, repo: str, sha: str, required: list[dict]) -> list[dict]:
    _check_target(repo, sha)
    validate_required(required)
    runs = [i for i in api.paged(f"/repos/{repo}/commits/{sha}/check-runs", key="check_runs") if isinstance(i, dict)]
    statuses = [i for i in api.paged(f"/repos/{repo}/commits/{sha}/statuses") if isinstance(i, dict)]
    results = []
    for entry in required:
        if "name" in entry:
            # Other sources and other commits are ignored before the latest is picked.
            items = [i for i in runs if i.get("name") == entry["name"] and _from_app(i, entry["app_id"])
                     and i.get("head_sha", sha) == sha]
            state = _latest_state(items, _run_state)
            label = entry["name"]
        else:
            items = [i for i in statuses if i.get("context") == entry["context"] and _from_creator(i, entry["creator"])]
            state = _latest_state(items, _status_state)
            label = entry["context"]
        results.append({"name": label, "state": state or "missing"})
    return results


def wait(api, repo: str, sha: str, required: list[dict], *, timeout: float, interval: float,
         sleep=time.sleep, clock=time.monotonic) -> tuple[bool, list[dict]]:
    _check_target(repo, sha)
    validate_required(required)
    if not (isinstance(timeout, (int, float)) and math.isfinite(timeout) and timeout >= 0):
        raise InputError("timeout must be a finite number of seconds, zero or more")
    if not (isinstance(interval, (int, float)) and math.isfinite(interval) and interval > 0):
        raise InputError("interval must be a finite number of seconds, above zero")
    deadline = clock() + timeout
    if hasattr(api, "set_deadline"):
        api.set_deadline(timeout)
    while True:
        results = snapshot(api, repo, sha, required)
        if all(r["state"] == "success" for r in results):
            return True, results
        if any(r["state"] == "failure" for r in results) or clock() >= deadline:
            return False, results
        sleep(min(interval, max(0, deadline - clock())))

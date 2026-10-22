"""Package registry lookups for VC-3, behind a small client so tests never touch the network.

Only two hosts are ever contacted (the PyPI JSON API and the npm registry), redirects are not
followed, and any failure is a LookupFailed, which the scorer turns into T-14, never a pass.
"""

import datetime as dt
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from golden_road.checks import _open

MAX_BYTES = 50_000_000
_PYPI_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
# npm allowed uppercase letters in names historically, so they are looked up rather than refused.
_NPM_NAME = re.compile(r"^(@[A-Za-z0-9][A-Za-z0-9._~-]*/)?[A-Za-z0-9][A-Za-z0-9._~-]*$")
DEADLINE_SECONDS = 120.0


class LookupFailed(Exception):
    pass


def _date(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def version_key(version: str) -> str:
    """A simple PEP 440-style comparison key: lowercase, no leading `v`, trailing `.0` components dropped."""
    parts = version.strip().lower().removeprefix("v").split(".")
    while len(parts) > 1 and parts[-1] == "0":
        parts.pop()
    return ".".join(parts)


class RegistryClient:
    """`timeout` is per request; `deadline_seconds` bounds all lookups together, counted from the first one.
    Once it has passed, every further lookup fails (and is reported), so a slow registry can't stall the job."""

    def __init__(self, timeout: float = 10.0, opener=_open, deadline_seconds: float = DEADLINE_SECONDS, clock=time.monotonic):
        self.timeout = timeout
        self.opener = opener
        self.deadline_seconds = deadline_seconds
        self.clock = clock
        self._deadline = None

    def _get(self, url: str) -> dict | None:
        now = self.clock()
        if self._deadline is None:
            self._deadline = now + self.deadline_seconds
        remaining = self._deadline - now
        if remaining <= 0:
            raise LookupFailed("registry lookups ran past the overall deadline")
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "golden-road-scorecard"})
        try:
            with self.opener(request, timeout=min(self.timeout, remaining)) as response:
                body = response.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise LookupFailed(f"registry returned {exc.code}") from exc
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
            raise LookupFailed(f"registry unreachable: {type(exc).__name__}") from exc
        if len(body) > MAX_BYTES:
            raise LookupFailed("registry response too large")
        try:
            doc = json.loads(body)
        except (ValueError, RecursionError) as exc:
            raise LookupFailed("registry returned invalid JSON") from exc
        if not isinstance(doc, dict):
            raise LookupFailed("registry returned an unexpected document")
        return doc

    def lookup(self, ecosystem: str, name: str, version: str) -> dict:
        """Facts: exists, version_exists, first_release (ISO date or None), maintainers (int or None), has_wheel (bool or None)."""
        if ecosystem == "pypi":
            return self._pypi(name, version)
        if ecosystem == "npm":
            return self._npm(name, version)
        raise LookupFailed(f"unknown ecosystem {ecosystem!r}")

    def _pypi(self, name: str, version: str) -> dict:
        if not _PYPI_NAME.match(name):
            raise LookupFailed("not a normalized PyPI name")
        doc = self._get(f"https://pypi.org/pypi/{name}/json")
        if doc is None:
            return {"exists": False, "version_exists": False, "first_release": None, "maintainers": None, "has_wheel": None}
        releases = doc.get("releases") if isinstance(doc.get("releases"), dict) else {}
        uploads = [_date(f.get("upload_time_iso_8601")) for files in releases.values() if isinstance(files, list)
                   for f in files if isinstance(f, dict)]
        first = min((u for u in uploads if u), default=None)
        if first is None:
            raise LookupFailed("the registry knows the package but gave no first-release date")
        key = version if version in releases else next((k for k in releases if version_key(k) == version_key(version)), None)
        files = releases.get(key) if key is not None else None
        wheel = None if not isinstance(files, list) else any(isinstance(f, dict) and f.get("packagetype") == "bdist_wheel" for f in files)
        return {"exists": True, "version_exists": isinstance(files, list) and bool(files), "first_release": first,
                # PyPI has no reliable maintainer list, so this is unknown and never fires.
                "maintainers": None, "has_wheel": wheel}

    def _npm(self, name: str, version: str) -> dict:
        if not _NPM_NAME.match(name):
            raise LookupFailed("not a valid npm package name")
        doc = self._get("https://registry.npmjs.org/" + urllib.parse.quote(name, safe="@"))
        if doc is None:
            return {"exists": False, "version_exists": False, "first_release": None, "maintainers": None, "has_wheel": None}
        versions = doc.get("versions") if isinstance(doc.get("versions"), dict) else {}
        times = doc.get("time") if isinstance(doc.get("time"), dict) else {}
        maintainers = doc.get("maintainers")
        first = _date(times.get("created"))
        if first is None:
            raise LookupFailed("the registry knows the package but gave no first-release date")
        return {"exists": True, "version_exists": version in versions, "first_release": first,
                "maintainers": len(maintainers) if isinstance(maintainers, list) else None, "has_wheel": None}

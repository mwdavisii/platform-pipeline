"""VC-3 (T-5): packages added or changed in the lockfiles that are hallucinated, brand new, single-maintainer, or run install code.

The lockfile is part of the PR, so nothing in it is trusted to say what a package is. An npm entry must be resolved from
the public registry under its own name (or from a configured internal registry); anything else is reported as a lookup
failure (T-14), never skipped. The install-script flag is read from the lockfile and reported before any skip.
"""

import datetime as dt
import urllib.parse

from ..lockfiles import normalize
from ..registry import LookupFailed
from . import signal

FIX = "Check that this is the package you meant, and that it is maintained and trusted."
NPM_REGISTRY = "https://registry.npmjs.org/"


def _internal_name(pkg: dict, registry_config: dict) -> bool:
    names = registry_config["internal_packages"][pkg["ecosystem"]]
    if pkg["ecosystem"] == "pypi":
        return normalize(pkg["name"]) in {normalize(n) for n in names}
    return pkg["name"] in names or any(pkg["name"].startswith(scope + "/") for scope in registry_config["internal_npm_scopes"])


_BAD_URL_PARTS = ("..", "%2e", "?", "#", "\\", "%5c")


def _tarball_tail(pkg: dict) -> str:
    """npm's canonical tarball path: `@scope/name/-/name-1.2.3.tgz` (the scope stays in the directory part only)."""
    return urllib.parse.quote(pkg["name"], safe="@/") + "/-/" + pkg["name"].rsplit("/", 1)[-1] + "-" + pkg["version"] + ".tgz"


def _npm_source_problem(pkg: dict, registry_config: dict) -> str | None:
    """Why this npm entry can't be trusted to be the package it names, None when it can be looked up publicly,
    or "internal" when it is a genuinely internal package.

    `resolved` must equal the canonical tarball URL exactly, on the public registry or on a configured internal
    registry (a proxy). Skipping as internal needs both an internal name and an internal-registry URL.
    """
    resolved, integrity = pkg.get("resolved"), pkg.get("integrity")
    if not resolved:
        return "the lockfile entry has no resolved URL, so the package source is unknown"
    if any(part in resolved.lower() for part in _BAD_URL_PARTS):
        return f"resolved URL {resolved[:120]!r} has path tricks or a query"
    tail = _tarball_tail(pkg)
    on_internal = any(resolved == prefix + tail for prefix in registry_config["internal_registries"]["npm"])
    if _internal_name(pkg, registry_config):
        if not on_internal:
            return "the name is internal but the package was not resolved from the internal registry (dependency confusion?)"
        return "internal"
    if resolved != NPM_REGISTRY + tail and not on_internal:
        return f"resolved from {resolved[:120]!r}, not the exact tarball URL for this name and version"
    if not integrity:
        return "the lockfile entry has no integrity hash"
    return None


def dependency_signals(base_packages: list[dict], head_packages: list[dict], client, registry_config: dict, *,
                       today: dt.date, now: str) -> tuple[list[dict], list[dict], list[dict]]:
    """Return (findings, skipped internal packages, lookup failures and untrusted sources; both fire T-14)."""

    def key(p):
        return (p["ecosystem"], p["name"], p["version"], p.get("resolved"))

    before = {key(p) for p in base_packages}
    findings, skipped, failures, seen = [], [], [], set()
    for pkg in head_packages:
        if key(pkg) in before or key(pkg) in seen:
            continue
        seen.add(key(pkg))
        sep = "==" if pkg["ecosystem"] == "pypi" else "@"
        label = f"{pkg['name']}{sep}{pkg['version']}"

        def add(rule, title, rationale):
            findings.append(signal(rule_id=f"VC-3/{rule}", category="dependency", severity="high", file=pkg["file"], line=pkg["line"],
                                   snippet=label, title=title, rationale=rationale, fix=FIX, now=now, vc="VC-3"))

        # Before any skip: an internal-looking name must not hide an install script.
        if pkg["install_script"]:
            add("install-script", f"{label} runs an install script", "Install scripts run arbitrary code at install time.")
        if pkg["ecosystem"] == "npm":
            problem = _npm_source_problem(pkg, registry_config)
            if problem == "internal":
                skipped.append({"ecosystem": "npm", "name": pkg["name"], "reason": "internal registry"})
                continue
            if problem:
                failures.append({"source": f"npm:{label}", "detail": f"untrusted package source: {problem}"[:300]})
                continue
        elif _internal_name(pkg, registry_config):
            # Assumes the build resolves internal names only from the internal index (no public fallback).
            # Future: check public PyPI for a same-named package (name collision).
            skipped.append({"ecosystem": "pypi", "name": pkg["name"], "reason": "internal package name"})
            continue
        try:
            facts = client.lookup(pkg["ecosystem"], pkg["name"], pkg["version"])
        except LookupFailed as exc:
            failures.append({"source": f"{pkg['ecosystem']}:{label}", "detail": f"lookup failed: {exc}"[:300]})
            continue
        if not facts["exists"] or not facts["version_exists"]:
            add("hallucinated", f"{label} is not in the registry", "A package that doesn't exist can be registered later by anyone.")
            continue
        first = facts["first_release"]
        if first and (today - dt.date.fromisoformat(first)).days < registry_config["new_package_days"]:
            add("new-package", f"{label} was first published on {first}", "Very new packages have no track record.")
        if facts["maintainers"] == 1:
            add("single-maintainer", f"{label} has a single maintainer", "One compromised account can publish a malicious release.")
        if facts["has_wheel"] is False:
            add("install-script", f"{label} has no wheel", "Installing from an sdist runs the package's build code.")
    return findings, skipped, failures

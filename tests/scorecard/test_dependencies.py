import datetime as dt
import io
import json
import urllib.error

import pytest

from scorecard.registry import LookupFailed, RegistryClient
from scorecard.signals.dependencies import dependency_signals

NOW = "2026-10-21T12:00:00Z"
TODAY = dt.date(2026, 10, 21)
OLD = {"exists": True, "version_exists": True, "first_release": "2019-01-01", "maintainers": 5, "has_wheel": True}


REGISTRIES = {"npm": ["https://npm.internal.example/"], "pypi": []}
_UNSET = object()


def _pkg(name, version="1.0.0", ecosystem="pypi", install_script=None, line=1, resolved=_UNSET, integrity="sha512-abc"):
    if ecosystem == "pypi":
        return {"ecosystem": ecosystem, "name": name, "version": version, "file": "requirements.txt", "line": line, "install_script": install_script}
    if resolved is _UNSET:
        resolved = f"https://registry.npmjs.org/{name}/-/{name.rsplit('/', 1)[-1]}-{version}.tgz"
    return {"ecosystem": ecosystem, "name": name, "version": version, "file": "package-lock.json", "line": line,
            "install_script": bool(install_script), "resolved": resolved, "integrity": integrity}


def _config(**overrides):
    return {"new_package_days": 90, "internal_npm_scopes": ["@acme"], "internal_packages": {"pypi": [], "npm": []},
            "internal_registries": REGISTRIES, **overrides}


def _run(head, client, base=(), config=None):
    config = config or _config()
    return dependency_signals(list(base), head, client, config, today=TODAY, now=NOW)


def test_hallucinated_package_is_vc3(fake_registry):
    """AC-4, with a stubbed registry client."""
    client = fake_registry({"fastapi-auth-helperz": {"exists": False, "version_exists": False, "first_release": None, "maintainers": None, "has_wheel": None}})
    findings, skipped, failures = _run([_pkg("fastapi-auth-helperz", line=7)], client)
    assert [f["rule_id"] for f in findings] == ["VC-3/hallucinated"]
    assert findings[0]["location"] == {"kind": "line", "file": "requirements.txt", "start_line": 7, "end_line": 7}
    assert {"framework": "golden-road-vc", "id": "VC-3"} in findings[0]["standards"]


def test_new_single_maintainer_and_install_scripts(fake_registry):
    client = fake_registry({
        "leftpadx": {"exists": True, "version_exists": True, "first_release": "2026-09-30", "maintainers": 1, "has_wheel": None},
        "sdistonly": {**OLD, "has_wheel": False},
    })
    head = [_pkg("leftpadx", ecosystem="npm", install_script=True), _pkg("sdistonly")]
    findings, _, _ = _run(head, client)
    assert sorted(f["rule_id"] for f in findings) == ["VC-3/install-script", "VC-3/install-script", "VC-3/new-package", "VC-3/single-maintainer"]


def test_only_added_or_changed_packages_are_looked_up(fake_registry):
    client = fake_registry({})
    _run([_pkg("fastapi", "0.115.0"), _pkg("uvicorn", "0.31.0")], client, base=[_pkg("fastapi", "0.115.0"), _pkg("uvicorn", "0.30.0")])
    assert client.calls == [("pypi", "uvicorn", "0.31.0")]


def test_internal_packages_are_skipped_and_listed(fake_registry):
    client = fake_registry({})
    internal = _pkg("@acme/ui", ecosystem="npm", resolved="https://npm.internal.example/@acme/ui/-/ui-1.0.0.tgz")
    findings, skipped, failures = _run([internal], client)
    assert findings == [] and failures == [] and client.calls == [] and skipped[0]["name"] == "@acme/ui"


def test_a_failed_lookup_is_reported_never_passed(fake_registry):
    findings, _, failures = _run([_pkg("requests")], fake_registry({"requests": LookupFailed("timeout")}))
    assert findings == [] and failures[0]["source"] == "pypi:requests==1.0.0"
    assert "lookup failed" in failures[0]["detail"]


def test_a_package_pinned_twice_is_looked_up_once(fake_registry):
    client = fake_registry({})
    _run([_pkg("nested", "1.0.0", ecosystem="npm", line=3), _pkg("nested", "1.0.0", ecosystem="npm", line=9)], client)
    assert client.calls == [("npm", "nested", "1.0.0")]


def test_internal_pypi_names_match_after_normalization(fake_registry):
    client = fake_registry({})
    config = _config(internal_packages={"pypi": ["Acme_Core"], "npm": ["Acme_Core"]})
    _, skipped, _ = _run([_pkg("acme-core"), _pkg("acme.core", ecosystem="npm")], client, config=config)
    # PEP 503 normalization applies to PyPI only: the npm name differs, so it is looked up.
    assert [s["name"] for s in skipped] == ["acme-core"] and client.calls == [("npm", "acme.core", "1.0.0")]


def test_npm_internal_names_match_exactly(fake_registry):
    client = fake_registry({})
    config = _config(internal_packages={"pypi": [], "npm": ["corp-lib"]})
    findings, skipped, failures = _run([_pkg("Corp-Lib", ecosystem="npm")], client, config=config)
    assert skipped == [] and client.calls == [("npm", "Corp-Lib", "1.0.0")]


def test_install_script_is_reported_before_an_internal_skip(fake_registry):
    client = fake_registry({})
    internal = _pkg("@acme/ui", ecosystem="npm", install_script=True, resolved="https://npm.internal.example/@acme/ui/-/ui-1.0.0.tgz")
    findings, skipped, _ = _run([internal], client)
    assert [f["rule_id"] for f in findings] == ["VC-3/install-script"] and skipped
    config = _config(internal_packages={"pypi": ["corp"], "npm": []})
    findings, skipped, _ = _run([{**_pkg("corp"), "install_script": True}], client, config=config)
    assert [f["rule_id"] for f in findings] == ["VC-3/install-script"] and skipped


def test_internal_name_resolved_elsewhere_is_a_problem_not_a_skip(fake_registry):
    client = fake_registry({})
    spoof = _pkg("@acme/ui", ecosystem="npm", install_script=True)  # resolved from the public registry
    findings, skipped, failures = _run([spoof], client)
    assert [f["rule_id"] for f in findings] == ["VC-3/install-script"] and skipped == [] and client.calls == []
    assert "@acme/ui@1.0.0" in failures[0]["source"] and "internal" in failures[0]["detail"]


def test_spoofed_name_and_foreign_resolved_urls_are_problems(fake_registry):
    client = fake_registry({})
    spoofed = _pkg("next-auth", ecosystem="npm", resolved="https://registry.npmjs.org/innocent/-/innocent-1.0.0.tgz")
    foreign = _pkg("lodash", ecosystem="npm", resolved="https://evil.example/lodash/-/lodash-1.0.0.tgz")
    git_dep = _pkg("tool", ecosystem="npm", resolved="git+ssh://git@example.com/x/tool.git#abc")
    findings, _, failures = _run([spoofed, foreign, git_dep], client)
    assert findings == [] and client.calls == []
    assert [f["source"] for f in failures] == ["npm:next-auth@1.0.0", "npm:lodash@1.0.0", "npm:tool@1.0.0"]
    assert all("untrusted package source" in f["detail"] for f in failures)


def test_resolved_must_be_the_exact_canonical_tarball_url(fake_registry):
    client = fake_registry({})
    bad = [
        _pkg("lodash", ecosystem="npm", resolved="https://registry.npmjs.org/lodash/-/../../evil/-/evil-6.6.6.tgz"),
        _pkg("left-pad", version="1.0.0", ecosystem="npm", resolved="https://registry.npmjs.org/left-pad/-/left-pad-0.0.1-mal.tgz"),
        _pkg("a", ecosystem="npm", resolved="https://registry.npmjs.org/a/-/a-1.0.0.tgz?x=1"),
        _pkg("b", ecosystem="npm", resolved="https://registry.npmjs.org/b/-/b-1.0.0.tgz#frag"),
        _pkg("c", ecosystem="npm", resolved="https://registry.npmjs.org/c/-/%2e%2E/c-1.0.0.tgz"),
        _pkg("d", ecosystem="npm", resolved="https://registry.npmjs.org/d/-/d-1.0.0.tgz\\x"),
        _pkg("e", ecosystem="npm", resolved="https://registry.npmjs.org/e/-/e-1.0.0.tgz/extra"),
    ]
    _, _, failures = _run(bad, client)
    assert len(failures) == len(bad) and client.calls == []
    good = _pkg("@scope/name", version="1.2.3", ecosystem="npm", resolved="https://registry.npmjs.org/@scope/name/-/name-1.2.3.tgz")
    _, _, failures = _run([good], client)
    assert failures == [] and client.calls == [("npm", "@scope/name", "1.2.3")]


def test_internal_skip_needs_an_internal_name_and_an_internal_url(fake_registry):
    client = fake_registry({})
    tail = "@acme/ui/-/ui-1.0.0.tgz"
    skip = _pkg("@acme/ui", ecosystem="npm", resolved="https://npm.internal.example/" + tail)
    assert _run([skip], client)[1] != [] and client.calls == []
    # internal registry prefix but a wrong tail: a problem, not a skip
    wrong = _pkg("@acme/ui", ecosystem="npm", resolved="https://npm.internal.example/@acme/ui/-/ui-9.9.9.tgz")
    _, skipped, failures = _run([wrong], client)
    assert skipped == [] and len(failures) == 1


def test_a_public_name_through_the_internal_proxy_is_still_looked_up(fake_registry):
    client = fake_registry({})
    proxied = _pkg("lodash", ecosystem="npm", resolved="https://npm.internal.example/lodash/-/lodash-1.0.0.tgz")
    _, skipped, failures = _run([proxied], client)
    assert skipped == [] and failures == [] and client.calls == [("npm", "lodash", "1.0.0")]
    off_tail = _pkg("lodash", ecosystem="npm", resolved="https://npm.internal.example/other/-/lodash-1.0.0.tgz")
    assert len(_run([off_tail], client)[2]) == 1


def test_missing_resolved_or_integrity_is_a_problem(fake_registry):
    client = fake_registry({})
    _, _, failures = _run([_pkg("a", ecosystem="npm", resolved=None), _pkg("b", ecosystem="npm", integrity=None)], client)
    assert [f["source"] for f in failures] == ["npm:a@1.0.0", "npm:b@1.0.0"] and client.calls == []


def test_a_changed_resolved_url_is_rechecked(fake_registry):
    client = fake_registry({})
    old = _pkg("lodash", ecosystem="npm", resolved="https://registry.npmjs.org/lodash/-/lodash-1.0.0.tgz")
    new = _pkg("lodash", ecosystem="npm", resolved="https://evil.example/lodash/-/lodash-1.0.0.tgz")
    _, _, failures = _run([new], client, base=[old])
    assert len(failures) == 1


def test_scoped_npm_names_resolve_under_the_scope(fake_registry):
    client = fake_registry({})
    _, _, failures = _run([_pkg("@scope/demo", ecosystem="npm")], client)
    assert failures == [] and client.calls == [("npm", "@scope/demo", "1.0.0")]


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(routes):
    seen = []

    def opener(request, timeout):
        seen.append(request.full_url)
        value = routes[request.full_url]
        if isinstance(value, int):
            raise urllib.error.HTTPError(request.full_url, value, "status", {}, None)
        return _Response(json.dumps(value).encode())

    opener.seen = seen
    return opener


def test_no_first_release_date_is_a_lookup_failure():
    pypi = {"releases": {"1.0.0": [{"packagetype": "bdist_wheel"}]}}
    npm = {"versions": {"2.0.0": {}}, "time": {}, "maintainers": [{"name": "a"}]}
    client = RegistryClient(opener=_opener({"https://pypi.org/pypi/demo/json": pypi, "https://registry.npmjs.org/demo": npm}))
    for ecosystem, version in (("pypi", "1.0.0"), ("npm", "2.0.0")):
        with pytest.raises(LookupFailed, match="first-release"):
            client.lookup(ecosystem, "demo", version)


def test_pypi_versions_match_after_simple_normalization():
    doc = {"releases": {"1.0": [{"packagetype": "bdist_wheel", "upload_time_iso_8601": "2020-01-01T00:00:00Z"}]}}
    client = RegistryClient(opener=_opener({"https://pypi.org/pypi/demo/json": doc}))
    assert client.lookup("pypi", "demo", "1.0.0")["version_exists"] is True
    assert client.lookup("pypi", "demo", "1.0.1")["version_exists"] is False


def test_uppercase_npm_names_are_looked_up():
    doc = {"versions": {"1.0.0": {}}, "time": {"created": "2012-01-01T00:00:00Z"}, "maintainers": []}
    opener = _opener({"https://registry.npmjs.org/JSONStream": doc})
    assert RegistryClient(opener=opener).lookup("npm", "JSONStream", "1.0.0")["exists"] is True


def test_timeout_and_overall_deadline_are_honoured():
    doc = {"versions": {"1.0.0": {}}, "time": {"created": "2012-01-01T00:00:00Z"}, "maintainers": []}
    timeouts, now = [], [0.0]

    def opener(request, timeout):
        timeouts.append(timeout)
        now[0] += 6
        return _Response(json.dumps(doc).encode())

    client = RegistryClient(timeout=10, opener=opener, deadline_seconds=10, clock=lambda: now[0])
    client.lookup("npm", "a", "1.0.0")
    client.lookup("npm", "b", "1.0.0")
    assert timeouts == [10, 4]
    with pytest.raises(LookupFailed, match="deadline"):
        client.lookup("npm", "c", "1.0.0")
    assert len(timeouts) == 2


def test_real_client_reads_pypi_and_npm_documents():
    pypi = {"releases": {"1.0.0": [{"packagetype": "sdist", "upload_time_iso_8601": "2026-10-01T00:00:00.000000Z"}],
                         "0.9.0": [{"packagetype": "bdist_wheel", "upload_time_iso_8601": "2026-09-01T00:00:00Z"}]}}
    npm = {"versions": {"2.0.0": {}}, "time": {"created": "2015-01-01T00:00:00.000Z"}, "maintainers": [{"name": "solo"}]}
    opener = _opener({"https://pypi.org/pypi/demo/json": pypi, "https://registry.npmjs.org/@scope%2Fdemo": npm,
                      "https://pypi.org/pypi/missing/json": 404, "https://pypi.org/pypi/broken/json": 503})
    client = RegistryClient(opener=opener)
    assert client.lookup("pypi", "demo", "1.0.0") == {"exists": True, "version_exists": True, "first_release": "2026-09-01",
                                                      "maintainers": None, "has_wheel": False}
    assert client.lookup("npm", "@scope/demo", "2.0.0")["maintainers"] == 1
    assert client.lookup("pypi", "missing", "1.0")["exists"] is False
    with pytest.raises(LookupFailed):
        client.lookup("pypi", "broken", "1.0")
    with pytest.raises(LookupFailed):
        client.lookup("pypi", "../etc", "1.0")
    assert all(u.startswith(("https://pypi.org/", "https://registry.npmjs.org/")) for u in opener.seen)

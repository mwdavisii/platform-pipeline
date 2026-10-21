"""Container authz probe: every request the policy denies must get the guard's 403 from the running image.

Expected results come from the builder's packaged authz template and a policy rendered from the spec
by the pinned builder, never from code in the repo.
"""

import http.client
import importlib.util
import itertools
import json
import re
import secrets
import tempfile
import time
import urllib.error
import urllib.request
from importlib import resources
from pathlib import Path

from builder.config import load_platform
from builder.detect import find_routes
from builder.render.authz import policy_json
from contracts.schema import validate

from .io import InputError, load_json

FORBIDDEN_BODY = b'{"error":"forbidden"}'  # the guard's exact bytes
MARKER_HEADER, MARKER_VALUE = "x-platform-authz", "deny"  # set on every denial by the builder's guard
DISAGREE = "guard denied an allowed request; policy and image disagree"
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
STANDARD_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
EXTRA_METHODS = ("OPTIONS", "PROPFIND")
MAX_FAILURES, MAX_PATH, MAX_REASON = 200, 300, 500
_TOKEN = re.compile(r"\*\*|\*|\{[^{}/]*\}")
_PARAM = re.compile(r"\{[^}/]+\}|<[^>/]+>|:[A-Za-z_]\w*")
MAX_BODY = 4096


def load_matcher(policy_text: str):
    """The packaged guard, loaded against `policy_text`. Returns (allowed, roles_from, module)."""
    template = resources.files("builder").joinpath("data", "authz", "authz.py.tmpl").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "authz-policy.json").write_text(policy_text, encoding="utf-8")
        module_path = Path(tmp, "authz.py")
        module_path.write_text(template, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("golden_road_probe_authz", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module.allowed, module.roles_from, module


def role_headers(spec: dict) -> list:
    names = sorted(r["name"] for r in spec["authz"]["roles"])
    return [None, *names, *(",".join(pair) for pair in itertools.combinations(names, 2))]


def _role_variants(spec: dict) -> list[str]:
    """Spellings of the first write role that a sloppy parser might treat differently."""
    writers = [r["name"] for r in spec["authz"]["roles"] if r.get("access") == "write"]
    if not writers:
        return []
    name = writers[0]
    return [name.upper(), f" {name} ", f"{name},", f"{name},{name}"]


def _sample(rule_path: str) -> str:
    def fill(match):
        token = match.group(0)
        return "probe/x" if token == "**" else "probe" if token == "*" else "1"

    return _TOKEN.sub(fill, rule_path)


def build_cases(spec: dict, root, platform: dict) -> list[dict]:
    # Node apps are judged with the Python template: the Node guard is held to the same shared case
    # table (tests/builder/authz_cases.json), so one matcher gives the expected result for both.
    allowed, roles_from, _ = load_matcher(policy_json(spec, platform))
    policy = json.loads(policy_json(spec, platform))
    routes = {(r["method"], _PARAM.sub("1", r["path"])) for r in find_routes(Path(root), spec["app_type"])
              if r["path"].startswith("/")}
    detected = {path for _, path in routes}
    if "health_path" in spec:
        routes.add(("GET", spec["health_path"]))
        detected.add(spec["health_path"])
    samples = {_sample(rule["path"]) for rule in policy["rules"]}
    rule_methods = {m for rule in policy["rules"] for m in rule["methods"]}
    combos = set(routes)
    combos |= {(m, p) for p in detected for m in STANDARD_METHODS}
    combos |= {(m, p) for p in samples for m in {*rule_methods, *STANDARD_METHODS, *EXTRA_METHODS}}
    headers = [*role_headers(spec), *_role_variants(spec)]
    keys = {(m, p, h) for m, p in combos for h in headers}
    canary = f"/__golden_road_probe_{secrets.token_hex(8)}"
    singles = [None, *sorted(r["name"] for r in spec["authz"]["roles"])]
    keys |= {(m, canary, h) for m in ("GET", "POST") for h in singles}
    cases = []
    for method, path, header in sorted(keys, key=lambda k: (k[0], k[1], k[2] is not None, k[2] or "")):
        verdict = allowed(method, path, roles_from(header))
        cases.append({"method": method, "path": path, "roles": header, "expected": "allow" if verdict else "deny"})
    return cases


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def send(base_url: str, header_name: str, case: dict, timeout: float = 5.0) -> tuple:
    """(status, body, headers) with headers as a lowercase-name dict; status is None when there was no response."""
    data = b"{}" if case["method"] in MUTATING else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if case["roles"] is not None:
        headers[header_name] = case["roles"]
    request = urllib.request.Request(base_url + case["path"], data=data, method=case["method"], headers=headers)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read(MAX_BODY), _lower(response.headers)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read(MAX_BODY), _lower(exc.headers)
        except (OSError, http.client.HTTPException):
            return exc.code, b"", {}
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return None, b"", {}


def _lower(headers) -> dict:
    return {k.lower(): v for k, v in headers.items()}


def _is_guard_403(status, body: bytes, headers: dict) -> bool:
    """Only the guard's own denial counts: 403, exactly its body, and its marker header."""
    return status == 403 and body == FORBIDDEN_BODY and headers.get(MARKER_HEADER) == MARKER_VALUE


def run_cases(base_url: str, header_name: str, cases: list[dict], timeout: float = 5.0, *,
              deadline: float = 300.0, max_no_response: int = 10) -> list[dict]:
    results, stop, missing = [], None, 0
    end = time.monotonic() + deadline
    for case in cases:
        if stop is None and time.monotonic() >= end:
            stop = f"the probe passed its {deadline:g}s deadline"
        if stop is not None:
            results.append({**case, "status": None, "guard_denied": False, "aborted": stop})
            continue
        status, body, headers = send(base_url, header_name, case, timeout)
        missing = missing + 1 if status is None else 0
        results.append({**case, "status": status, "guard_denied": _is_guard_403(status, body, headers)})
        if missing >= max_no_response:
            stop = f"{max_no_response} consecutive requests got no response"
    return results


def evaluate(results: list[dict]) -> dict:
    denied = [r for r in results if r["expected"] == "deny"]
    failures = [{**{k: r[k] for k in ("method", "path", "roles", "status")}} for r in denied if not r["guard_denied"]]
    disagree = [{**{k: r[k] for k in ("method", "path", "roles", "status")}, "reason": DISAGREE}
                for r in results if r["expected"] == "allow" and r["guard_denied"]]
    failures += disagree
    for f in failures:
        f["path"] = f["path"][:MAX_PATH]
    aborted = next((r["aborted"] for r in results if r.get("aborted")), None)
    out = {"status": "fail" if failures or not denied or aborted else "pass", "cases": len(results), "denied": len(denied),
           "failures": failures[:MAX_FAILURES], "failures_total": len(failures)}
    reason = aborted or (DISAGREE if disagree else None)
    if reason:
        out["reason"] = reason[:MAX_REASON]
    return out


def wait_ready(base_url: str, path: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, _, _ = send(base_url, "X-Unused", {"method": "GET", "path": path, "roles": None}, timeout=2)
        if status is not None:
            return True
        time.sleep(0.5)
    return False


def probe(root, base_url: str, *, platform: dict | None = None, ready_timeout: float = 60.0, deadline: float = 300.0) -> dict:
    root = Path(root)
    spec = load_json(root / "builder-spec.json")
    errors = validate("builder-spec.v1", spec)
    if errors:
        raise InputError(f"builder-spec.json is invalid: {errors[0]}")
    if not spec.get("authz"):
        return {"status": "not-declared"}
    platform = platform or load_platform()
    cases = build_cases(spec, root, platform)
    if not any(c["expected"] == "deny" for c in cases):
        return {"status": "fail", "reason": "authz is declared but no routes were found to probe", "cases": len(cases), "denied": 0, "failures": [], "failures_total": 0}
    if not wait_ready(base_url, spec.get("health_path", "/"), ready_timeout):
        return {"status": "fail", "reason": "the app did not start", "cases": len(cases), "denied": 0, "failures": [], "failures_total": 0}
    return evaluate(run_cases(base_url, platform["identity_header"], cases, deadline=deadline))

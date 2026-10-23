"""The registration floor, across trees: the portal's collector (through the GitHub API) and the scorecard's T-21 input (from the checked-out
tree) must compute the same evidence, signals and floor for the same files. They can't share code (the portal must not import the
scorecard), so this test holds them together: change one computation and it fails until the other follows."""

import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "bootstrap/golden-road-portal"))
sys.path.insert(0, str(ROOT / "bootstrap/golden-road-portal/tests"))
sys.path.insert(0, str(ROOT / "tests/scorecard"))

from tree_fixture import commit_tree  # noqa: E402
from portal_fakes import READ_PERMISSIONS, FakeGitHub, repo_at, transport  # noqa: E402

from portal.collectors import evidence as portal_evidence  # noqa: E402
from portal.floor import compute_floor as portal_floor  # noqa: E402
from portal.github.client import GitHub  # noqa: E402
from scorecard.config import load_registration_rules  # noqa: E402
from scorecard.signals import registration as scorecard_registration  # noqa: E402

API, SHA, REPO = "https://api.github.com", "a" * 40, "acme/notes"
GATEWAY = ("litellm.internal",)
FACTORY = ROOT / "tests/scorecard/fixtures/factory"
SPEC = {"app_name": "notes", "app_type": "python", "entrypoint": "app.main:app", "port": 8000, "data_classification": "internal",
        "egress": [], "env": [], "resources_tier": "default"}
WRITE = {"roles": [{"name": "editor", "access": "write"}, {"name": "reader", "access": "read"}], "default": "deny",
         "rules": [{"methods": ["GET"], "path": "/", "roles": ["reader"]}, {"methods": ["POST"], "path": "/n", "roles": ["editor"]}]}
READ_ONLY = {"roles": [{"name": "reader", "access": "read"}], "default": "deny", "rules": [{"methods": ["GET"], "path": "/", "roles": ["reader"]}]}
GATEWAY_EGRESS = [{"host": "litellm.internal", "port": 443, "reason": "model calls"}]
OTHER_EGRESS = [{"host": "api.example.org", "port": 443, "reason": "weather data"}]
REQUIREMENTS = "".join(f"pkg{i}==1.0\n" for i in range(51))


def spec(**changes):
    return json.dumps({**SPEC, **changes}).encode()


def lock(count, pad=0):
    return json.dumps({"lockfileVersion": 3, "packages": {"": {}, **{f"node_modules/p{i}": {"version": "1.0.0"} for i in range(count)}}, "pad": "x" * pad}).encode()


NODE = {"app_type": "node", "entrypoint": "server.js"}
CASES = {
    "no spec": {},
    "invalid spec (not json)": {"builder-spec.json": b"{not json"},
    "invalid spec (schema)": {"builder-spec.json": spec(port=80)},
    "invalid spec (a secret name too long to copy)": {"builder-spec.json": spec(env=[{"name": "A" * 201, "secret": True}])},
    "spec over the cap": {"builder-spec.json": b" " * 70000},
    "spec exactly at the cap": {"builder-spec.json": spec() + b" " * (65536 - len(spec()))},
    "too many list items": {"builder-spec.json": spec(egress=[{"host": f"h{i}.example.org", "port": 443, "reason": "weather data"} for i in range(101)])},
    "a clean spec": {"builder-spec.json": spec(), "requirements.txt": b"fastapi==0.1\n"},
    "a clean spec for phi": {"builder-spec.json": spec(data_classification="phi")},
    "restricted": {"builder-spec.json": spec(data_classification="restricted")},
    "write roles": {"builder-spec.json": spec(authz=WRITE)},
    "read-only roles": {"builder-spec.json": spec(authz=READ_ONLY)},
    "secret env": {"builder-spec.json": spec(env=[{"name": "API_TOKEN", "secret": True}, {"name": "LOG_LEVEL", "secret": False}])},
    "model gateway egress": {"builder-spec.json": spec(egress=GATEWAY_EGRESS)},
    "other egress": {"builder-spec.json": spec(egress=OTHER_EGRESS)},
    "both kinds of egress": {"builder-spec.json": spec(egress=GATEWAY_EGRESS + OTHER_EGRESS)},
    "every signal at once": {"builder-spec.json": spec(authz=WRITE, egress=GATEWAY_EGRESS + OTHER_EGRESS, env=[{"name": "API_TOKEN", "secret": True}]),
                             "requirements.txt": REQUIREMENTS.encode()},
    "requirements counted": {"builder-spec.json": spec(), "requirements.txt": b"fastapi==0.1 \\\n    --hash=sha256:00\nhttpx==0.2\n# comment\n-r other.txt\nbad line\n"},
    "many dependencies in requirements.txt": {"builder-spec.json": spec(), "requirements.txt": REQUIREMENTS.encode()},
    "exactly the limit": {"builder-spec.json": spec(), "requirements.txt": "".join(f"pkg{i}==1.0\n" for i in range(50)).encode()},
    "requirements.txt over the cap": {"builder-spec.json": spec(), "requirements.txt": b"# " + b"x" * 262144},
    "package-lock.json counted": {"builder-spec.json": spec(**NODE), "package-lock.json": lock(3)},
    "many dependencies in package-lock.json": {"builder-spec.json": spec(**NODE), "package-lock.json": lock(51)},
    "package-lock.json over one MiB": {"builder-spec.json": spec(**NODE), "package-lock.json": lock(3, pad=1_500_000)},
    "package-lock.json over the cap": {"builder-spec.json": spec(**NODE), "package-lock.json": lock(3, pad=10_500_000)},
    "package-lock.json not json": {"builder-spec.json": spec(**NODE), "package-lock.json": b"[nope"},
    "package-lock.json of the wrong shape": {"builder-spec.json": spec(**NODE), "package-lock.json": b"[]"},
    "a node app ignores requirements.txt": {"builder-spec.json": spec(**NODE), "requirements.txt": REQUIREMENTS.encode()},
    "a python app ignores package-lock.json": {"builder-spec.json": spec(), "package-lock.json": lock(60)},
    "no spec reads both lockfiles": {"requirements.txt": REQUIREMENTS.encode(), "package-lock.json": lock(60)},
    "a static app has no lockfile": {"builder-spec.json": spec(app_type="static", entrypoint="index.html"), "requirements.txt": b"fastapi==0.1\n"},
    ".builder.lock valid": {"builder-spec.json": spec(), ".builder.lock": json.dumps({"builder_version": "0.3.11"}).encode()},
    ".builder.lock not json": {"builder-spec.json": spec(), ".builder.lock": b"[no"},
    ".builder.lock over the cap": {"builder-spec.json": spec(), ".builder.lock": b" " * 70000},
    "a spec that is not utf-8": {"builder-spec.json": b"\xff\xfe{}"},
}


# Stored as a symlink (mode 120000) to a file holding the content: the portal's contents API follows such a link and answers a file, so
# both sides must read the stored mode and call it unreadable. A .gitattributes `working-tree-encoding` rule (added by the PR) rewrites a
# file on disk; both sides must count what is stored.
UTF16 = "requirements.txt working-tree-encoding=UTF-16LE-BOM\n"
SPECIAL = {
    "a symlinked builder-spec.json": ({"builder-spec.json": spec()}, ("builder-spec.json",), None),
    "a symlinked requirements.txt": ({"builder-spec.json": spec(), "requirements.txt": REQUIREMENTS.encode()}, ("requirements.txt",), None),
    "a symlinked package-lock.json": ({"builder-spec.json": spec(**NODE), "package-lock.json": lock(60)}, ("package-lock.json",), None),
    "a symlinked .builder.lock": ({"builder-spec.json": spec(), ".builder.lock": b'{"builder_version": "0.3.11"}'}, (".builder.lock",), None),
    "requirements.txt re-encoded by .gitattributes": ({"builder-spec.json": spec(), "requirements.txt": REQUIREMENTS.encode()}, (), UTF16),
}


def portal_side(files, rules, symlinked=()):
    gh = repo_at(FakeGitHub(permissions=READ_PERMISSIONS), REPO, files, modes={name: "120000" for name in symlinked})
    client = GitHub(API, httpx.Client(transport=transport(gh)), lambda: "ghs_x")
    doc = portal_evidence.compute_evidence(client, REPO, SHA, gateway_hosts=GATEWAY, many_dependencies_over=rules["many_dependencies_over"])
    return doc, portal_floor(doc, rules)


def scorecard_side(tmp_path, files, rules, symlinked=(), attributes=None):
    stored = {name: content for name, content in files.items() if name not in symlinked}
    stored.update({f"target-{name}": files[name] for name in symlinked})
    head = commit_tree(tmp_path, stored, symlinks={name: f"target-{name}" for name in symlinked}, attributes=attributes)
    doc = scorecard_registration.compute_evidence(tmp_path, repo=REPO, commit=head, gateway_hosts=GATEWAY, many_dependencies_over=rules["many_dependencies_over"])
    return {**doc, "commit": SHA}, scorecard_registration.compute_floor(doc, rules)


@pytest.fixture(scope="module")
def rules():
    return load_registration_rules(FACTORY)


@pytest.mark.parametrize("name", list(CASES))
def test_the_portal_and_the_scorecard_compute_the_same_evidence_and_floor(name, tmp_path, rules):
    files = CASES[name]
    portal_doc, portal_result = portal_side(files, rules)
    scorecard_doc, scorecard_result = scorecard_side(tmp_path, files, rules)
    assert [s["rule"] for s in scorecard_doc["signals"]] == [s["rule"] for s in portal_doc["signals"]]
    assert scorecard_result == portal_result
    assert scorecard_doc == portal_doc  # every field, including the code-written lines and the dependency counts


@pytest.mark.parametrize("name", list(SPECIAL))
def test_a_symlink_is_unreadable_and_a_rewritten_file_counts_as_stored_on_both_sides(name, tmp_path, rules):
    files, symlinked, attributes = SPECIAL[name]
    portal_doc, portal_result = portal_side(files, rules, symlinked)
    scorecard_doc, scorecard_result = scorecard_side(tmp_path, files, rules, symlinked, attributes)
    assert scorecard_doc == portal_doc and scorecard_result == portal_result
    for link in symlinked:
        assert f"unreadable:{link}" in [s["rule"] for s in portal_doc["signals"]]
    if attributes:
        assert portal_doc["dependencies"]["count"] == 51 and "many_dependencies" in [s["rule"] for s in scorecard_doc["signals"]]


@pytest.mark.parametrize("changes", [
    {"no_spec": {"tier": "moderate", "data_classification": "internal"}},
    {"classification_tier": {"public": "low", "internal": "moderate", "restricted": "high", "phi": "high"}},
    {"raise_to": {"write_roles": "high", "secret_env": "low", "model_gateway_egress": "low", "other_egress": "moderate", "many_dependencies": "high"}},
    {"many_dependencies_over": 3},
])
@pytest.mark.parametrize("name", ["no spec", "a clean spec", "every signal at once", "spec over the cap", "requirements counted"])
def test_the_floors_agree_under_other_rules_too(name, changes, tmp_path, rules):
    other = {**rules, **changes}
    assert scorecard_side(tmp_path, CASES[name], other)[1] == portal_side(CASES[name], other)[1]


def test_the_cases_cover_every_signal_rule_and_both_ways_to_the_no_spec_floor():
    seen = set()
    for files in CASES.values():
        seen |= {s["rule"] for s in scorecard_registration_safe(files)}
    assert {"no_spec", "write_roles", "secret_env", "model_gateway_egress", "other_egress", "many_dependencies"} <= seen
    assert {"unreadable:builder-spec.json", "unreadable:requirements.txt", "unreadable:package-lock.json", "unreadable:.builder.lock"} <= seen


def scorecard_registration_safe(files):
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        return scorecard_side(Path(directory), files, load_registration_rules(FACTORY))[0]["signals"]

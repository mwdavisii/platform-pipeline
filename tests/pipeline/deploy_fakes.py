"""Stand-ins for the golden-road-deploy workflow tests (deploy-check and bump). Nothing here calls GitHub, a registry or Sigstore:

- `docker`: `pull` and `logout` do nothing; `run ... <image> golden-road <args>` runs this checkout's golden_road.cli with every
  container path in the arguments mapped back to its host mount (so deploy-verify, deploy-pins, deploy-pin and compose are the
  real code), except `golden-road app-token`, which records its arguments and writes a fake token;
- `gh`: `release list` and `release download` answer from a directory of fake releases, `workflow run` succeeds (or fails for the
  refs FAKE_DISPATCH_FAIL names), and `gh api` answers the git data and pulls API from a small in-memory model kept in a JSON file,
  the tip of main from FAKE_MAIN_SHA, and a head's deploy-check run (completed with FAKE_DEPLOY_CHECK_DONE=1, else in
  progress); every call is recorded;
- `cosign`: `verify` and `verify-attestation` answer from files and exit codes the test sets; every call is recorded.

Each workflow step's `run:` is extracted from the YAML and run with bash, as tests/pipeline/test_workflows.py does."""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY_BOOTSTRAP = ROOT / "bootstrap/golden-road-deploy"
GOLDEN = ROOT / "tests/builder/golden/python-fastapi"
BUILDER_IMAGE = "ghcr.io/acme/golden-road-builder@sha256:" + "e" * 64
IMAGE = "ghcr.io/acme/notes-api@sha256:" + "b" * 64
COMMIT = "42fa829" + "0" * 33
TEST_TAG = "test-20261029132351-42fa829"
PROD_TAG = "prod-20261029150000-42fa829"
REPO = "acme/notes"


def load(path):
    doc = yaml.safe_load(Path(path).read_text())
    doc["on"] = doc.pop(True, doc.get("on"))
    return doc


def step(doc, name, job=None):
    jobs = doc["jobs"]
    steps = jobs[job or next(iter(jobs))]["steps"]
    return next(s for s in steps if s.get("name", "").startswith(name))


FAKE_DOCKER = r'''#!PYTHON
import json, os, sys
# bump.yml runs the token step's docker under `env -i`, so the stand-in reads its settings from a file beside it.
os.environ.update(json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake-docker.json"))))
log = os.environ.get("FAKE_LOG")
args = sys.argv[1:]
if log:
    with open(log, "a") as h:
        h.write(json.dumps(["docker", *args]) + "\n")
if args[:1] in (["pull"], ["logout"]):
    sys.exit(0)
if args[:1] != ["run"]:
    sys.exit(f"unexpected docker {args}")
with_value = {"-v", "--volume", "--user", "--tmpfs", "--pids-limit", "--network", "--cap-drop", "--security-opt", "-e", "--env", "-w"}
mounts, passed, i, rest = [], [], 1, None
while i < len(args):
    a = args[i]
    if a in with_value:
        if a in ("-v", "--volume"):
            host, container = args[i + 1].split(":")[:2]
            mounts.append((container, host))
        if a in ("-e", "--env"):
            passed.append(args[i + 1])
        i += 2
    elif a.startswith("-"):
        i += 1
    else:
        if a != os.environ["FAKE_BUILDER_IMAGE"]:
            sys.exit(f"docker ran {a}, not the builder image")
        rest = args[i + 1:]
        break
def host(arg):
    for container, h in mounts:
        if arg == container or arg.startswith(container + "/"):
            return h + arg[len(container):]
    return arg
rest = [host(a) for a in rest]
if rest[:2] == ["golden-road", "app-token"]:
    if os.environ.get("FAKE_APP_TOKEN_RC", "0") != "0":
        sys.exit(int(os.environ["FAKE_APP_TOKEN_RC"]))
    out = rest[rest.index("--output") + 1]
    perms = dict(rest[j + 1].split("=") for j, a in enumerate(rest) if a == "--permission")
    with open(os.environ["FAKE_LOG"] + ".token", "w") as h:
        json.dump({"argv": rest, "env": {k: os.environ.get(k, "") for k in passed}}, h)
    with open(out, "w") as h:
        json.dump({"token": "ghs_" + "T" * 40, "expires_at": "2026-10-29T16:00:00Z", "repository": rest[rest.index("--repo") + 1],
                   "permissions": perms, "app_slug": "golden-road-deploy"}, h)
    sys.exit(0)
if rest[:1] != ["golden-road"]:
    sys.exit(f"unexpected command {rest}")
os.environ["PYTHONPATH"] = os.environ["FAKE_SRC"]
os.execv(sys.executable, [sys.executable, "-c", "import sys; from golden_road.cli import main; sys.exit(main())", *rest[1:]])
'''

FAKE_COSIGN = r'''#!PYTHON
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as h:
    h.write(json.dumps(["cosign", *args]) + "\n")
if args[0] == "verify":
    sys.exit(int(os.environ.get("FAKE_COSIGN_VERIFY_RC", "0")))
if args[0] == "verify-attestation":
    kind = "promotion" if "promotion" in args[args.index("--type") + 1] else "gate"
    rc = int(os.environ.get(f"FAKE_COSIGN_{kind.upper()}_RC", "0"))
    path = os.path.join(os.environ["FAKE_ATTEST"], kind + ".jsonl")
    if rc == 0 and os.path.exists(path):
        sys.stdout.write(open(path).read())
    sys.exit(rc)
sys.exit(f"unexpected cosign {args}")
'''

FAKE_GH = r'''#!PYTHON
import json, os, shutil, sys
args = sys.argv[1:]
log = os.environ["FAKE_LOG"]
def record(entry):
    with open(log, "a") as h:
        h.write(json.dumps(entry) + "\n")
def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default
releases = os.environ["FAKE_RELEASES"]
if args[:2] == ["release", "download"]:
    record(["gh", *args, {"GH_TOKEN": os.environ.get("GH_TOKEN", "")}])
    src = os.path.join(releases, opt("--repo"), args[2])
    if not os.path.isdir(src) or args[2] in os.environ.get("FAKE_DOWNLOAD_FAIL", "").split():
        sys.exit("release not found")
    patterns = [args[i + 1] for i, a in enumerate(args) if a == "--pattern"]
    for p in patterns:
        if os.path.exists(os.path.join(src, p)):
            shutil.copy(os.path.join(src, p), os.path.join(opt("--dir"), p))
    sys.exit(0)
if args[:2] == ["release", "list"]:
    record(["gh", *args, {"GH_TOKEN": os.environ.get("GH_TOKEN", "")}])
    if opt("--repo") in os.environ.get("FAKE_LIST_FAIL", "").split():
        sys.stderr.write("gh: Not Found (HTTP 404)\n")
        sys.exit(1)
    base = os.path.join(releases, opt("--repo"))
    out = []
    if os.path.isdir(base):
        for tag in sorted(os.listdir(base)):
            meta = json.load(open(os.path.join(base, tag, "meta.json")))
            out.append({"tagName": tag, **meta})
    print(json.dumps(out))
    sys.exit(0)
if args[:2] == ["workflow", "run"]:  # bump in workflow mode starts deploy-check on a branch
    record(["gh", *args, {"GH_TOKEN": os.environ.get("GH_TOKEN", "")}])
    if opt("--ref") in os.environ.get("FAKE_DISPATCH_FAIL", "").split():
        sys.stderr.write("gh: Resource not accessible by integration (HTTP 403)\n")
        sys.exit(1)
    sys.exit(0)
if args[:1] == ["api"]:
    state_path = os.environ["FAKE_GH_STATE"]
    state = json.load(open(state_path)) if os.path.exists(state_path) else {"refs": {}, "pulls": [], "n": 0}
    method = opt("--method", opt("-X", "GET"))
    path = next(a for a in args[1:] if not a.startswith("-") and a not in (method,) and args[args.index(a) - 1] not in ("--method", "-X", "--input", "--jq", "-q"))
    body = json.load(open(opt("--input"))) if "--input" in args else None
    record(["gh", "api", method, path, body, {"GH_TOKEN": os.environ.get("GH_TOKEN", "")}])
    def save():
        json.dump(state, open(state_path, "w"))
    def new_sha(kind):
        state["n"] += 1
        return f"{dict(blob='b', tree='e', commit='c')[kind]}{state['n']:039d}"
    repo = "/".join(path.split("/")[1:3])
    tail = "/".join(path.split("?")[0].split("/")[3:])
    if os.environ.get("FAKE_GH_API_FAIL") and os.environ["FAKE_GH_API_FAIL"] in path:
        sys.exit("HTTP 500")
    result = None
    if method == "POST" and tail == "git/blobs":
        result = {"sha": new_sha("blob")}
        state.setdefault("blobs", {})[result["sha"]] = body
    elif method == "POST" and tail == "git/trees":
        result = {"sha": new_sha("tree")}
        state.setdefault("trees", {})[result["sha"]] = body
    elif method == "POST" and tail == "git/commits":
        result = {"sha": new_sha("commit")}
        state.setdefault("commits", {})[result["sha"]] = body
    elif method == "GET" and tail.startswith("git/matching-refs/"):
        prefix = "refs/" + tail[len("git/matching-refs/"):]
        result = [{"ref": r, "object": {"sha": s}} for r, s in state["refs"].items() if r.startswith(prefix)]
    elif method == "POST" and tail == "git/refs":
        state["refs"][body["ref"]] = body["sha"]
        result = {"ref": body["ref"]}
    elif method == "PATCH" and tail.startswith("git/refs/"):
        state["refs"]["refs/" + tail[len("git/refs/"):]] = body["sha"]
        result = {}
    elif method == "GET" and tail == "pulls":
        head = path.split("head=")[1].split("&")[0] if "head=" in path else None
        result = [{**p, "head": {"ref": p["head"], "sha": state["refs"].get("refs/heads/" + p["head"])}} for p in state["pulls"]
                  if p["state"] == "open" and (head is None or f"{repo.split('/')[0]}:{p['head']}" == head)]
    elif method == "POST" and tail == "pulls" and os.environ.get("FAKE_PULL_CREATE_FAIL"):
        sys.stderr.write("gh: Validation Failed (HTTP 422)\n")
        sys.exit(1)
    elif method == "GET" and tail.startswith("commits/") and tail.endswith("/check-runs"):  # bump's re-dispatch check
        if os.environ.get("FAKE_CHECK_RUNS_FAIL"):
            sys.stderr.write("gh: Server Error (HTTP 500)\n")
            sys.exit(1)
        done = os.environ.get("FAKE_DEPLOY_CHECK_DONE") == "1"
        result = {"total_count": 1, "check_runs": [{"name": "deploy-check", "status": "completed" if done else "in_progress"}]}
    elif method == "POST" and tail == "pulls":
        state["pulls"].append({"number": len(state["pulls"]) + 1, "state": "open", **body})
        result = {"number": len(state["pulls"])}
    elif method == "PATCH" and tail.startswith("pulls/"):
        pull = state["pulls"][int(tail.split("/")[1]) - 1]
        pull.update(body)
        result = {}
    elif method == "GET" and tail.startswith("git/commits/"):
        commit = state.get("commits", {}).get(tail.split("/")[2])
        if commit is None:
            sys.stderr.write("gh: Not Found (HTTP 404)\n")
            sys.exit(1)
        result = {"sha": tail.split("/")[2], "parents": [{"sha": p} for p in commit["parents"]], "tree": {"sha": commit["tree"]}}
    elif method == "GET" and tail == "git/ref/heads/main":  # deploy-check's dispatch guard: the tip of main, as the test sets it
        if not os.environ.get("FAKE_MAIN_SHA"):
            sys.stderr.write("gh: Server Error (HTTP 500)\n")
            sys.exit(1)
        result = {"ref": "refs/heads/main", "object": {"sha": os.environ["FAKE_MAIN_SHA"]}}
    elif method == "GET" and tail.startswith("contents/"):
        if os.environ.get("FAKE_INTAKE_ERROR"):
            sys.stderr.write("gh: Server Error (HTTP 500)\n")
            sys.exit(1)
        local = os.path.join(os.environ["FAKE_FACTORY"], repo, tail[len("contents/"):])
        if not os.path.isfile(local):
            sys.stderr.write("gh: Not Found (HTTP 404)\n")
            sys.exit(1)
        result = {"type": "file", "path": tail[len("contents/"):], "encoding": "base64", "content": ""}
    else:
        sys.exit(f"unexpected gh api {method} {path}")
    save()
    jq = opt("--jq", opt("-q"))
    if jq is not None:
        import subprocess
        sys.stdout.write(subprocess.run(["jq", "-r", jq], input=json.dumps(result), capture_output=True, text=True, check=True).stdout)
    else:
        print(json.dumps(result))
    sys.exit(0)
sys.exit(f"unexpected gh {args}")
'''


def shims(tmp_path) -> Path:
    shim = tmp_path / "bin"
    shim.mkdir(exist_ok=True)
    for name, text in (("docker", FAKE_DOCKER), ("cosign", FAKE_COSIGN), ("gh", FAKE_GH)):
        (shim / name).write_text(text.replace("#!PYTHON", "#!" + sys.executable, 1))
        (shim / name).chmod(0o755)
    return shim


def calls(tmp_path, tool=None):
    log = tmp_path / "calls.jsonl"
    entries = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return [e for e in entries if tool is None or e[0] == tool]


def base_env(tmp_path, workflow_doc, **extra):
    shim = shims(tmp_path)
    (shim / "fake-docker.json").write_text(json.dumps({"FAKE_LOG": str(tmp_path / "calls.jsonl"), "FAKE_SRC": str(ROOT / "src"),
                                                       "FAKE_BUILDER_IMAGE": BUILDER_IMAGE, **{k: str(v) for k, v in extra.items() if k.startswith("FAKE_")}}))
    env = {k: str(v) for k, v in (workflow_doc.get("env") or {}).items()}
    env.update({"PATH": f"{shim}:/usr/bin:/bin", "HOME": str(tmp_path), "BUILDER_IMAGE": BUILDER_IMAGE,
                "FAKE_LOG": str(tmp_path / "calls.jsonl"), "FAKE_SRC": str(ROOT / "src"), "FAKE_RELEASES": str(tmp_path / "releases"),
                "FAKE_ATTEST": str(tmp_path / "attest"), "FAKE_GH_STATE": str(tmp_path / "gh-state.json"), "FAKE_FACTORY": str(tmp_path / "factory"),
                "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY_OWNER": "acme", "GITHUB_REPOSITORY": "acme/golden-road-deploy",
                "GITHUB_OUTPUT": str(tmp_path / "github-output"), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
                "RUNNER_TEMP": str(tmp_path / "runner-temp"), "GH_TOKEN": "github-token"})
    (tmp_path / "runner-temp").mkdir(exist_ok=True)
    register(tmp_path)
    env.update({k: str(v) for k, v in extra.items()})
    return env


def register(tmp_path, repo=REPO):
    """The app's intake record in the fake <owner>/scorecard-factory (deploy-check reads it with the contents API)."""
    path = tmp_path / "factory" / f"{repo.split('/')[0]}/scorecard-factory/intake/{repo}.yaml"
    if not (tmp_path / "factory-unregistered").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"schema: intake/v2\nrepo: {repo}\n")
    return path


def unregister(tmp_path):
    (tmp_path / "factory-unregistered").write_text("")
    shutil.rmtree(tmp_path / "factory", ignore_errors=True)


def run_step(tmp_path, script, env):
    return subprocess.run(["bash", "-c", script], cwd=tmp_path, capture_output=True, text=True, env=env)


# ---- the releases and the deploy tree ----

def manifests(tmp_path) -> str:
    from golden_road.render import render
    app = tmp_path / "golden-app"
    if not app.exists():
        shutil.copytree(GOLDEN, app)
    return render(app, IMAGE, "notes-api")


def gate_record(release=TEST_TAG, manifests_text="", **overrides) -> dict:
    from golden_road.record import build_gate_record
    args = dict(repo=REPO, commit=COMMIT, image=IMAGE, builder_image=BUILDER_IMAGE,
                drift={"status": "pass", "counts": {"by_severity": {s: 0 for s in ("critical", "high", "medium", "low", "info")}, "by_layer": {}},
                       "blocking": [], "overrides_applied": []},
                checks={"passed": True, "required_checks": []}, authz={"status": "not-declared"}, created="2026-10-29T13:23:51Z",
                release=release, manifests_sha256=hashlib.sha256(manifests_text.encode()).hexdigest())
    record = build_gate_record(**args)
    record.update(overrides)
    return record


def promotion_record(manifests_text, release=TEST_TAG, **overrides) -> dict:
    return {"schema": "promotion-record/v1", "release": release, "image": IMAGE, "manifests_sha256": hashlib.sha256(manifests_text.encode()).hexdigest(),
            "gate2_single_approver": False, "self_approved": False, "approvals": [{"login": "deputy", "comment": ""}], "triggered_by": "mike",
            "actor": "mike", "run_attempt": 1, "run_url": "https://github.com/acme/notes/actions/runs/1", "created": "2026-10-29T15:00:00Z", **overrides}


def statement(predicate) -> str:
    payload = base64.b64encode(json.dumps({"_type": "https://in-toto.io/Statement/v1", "predicate": predicate}).encode()).decode()
    return json.dumps({"payloadType": "application/vnd.in-toto+json", "payload": payload, "signatures": []})


def publish(tmp_path, tag, manifests_text, record, *, prerelease=True, published="2026-10-29T13:30:00Z", repo=REPO) -> Path:
    """A fake published release: its assets, and the metadata `gh release list` reports."""
    d = tmp_path / "releases" / repo / tag
    d.mkdir(parents=True)
    (d / "manifests.yaml").write_text(manifests_text)
    (d / "gate-record.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    (d / "meta.json").write_text(json.dumps({"isPrerelease": prerelease, "isDraft": False, "publishedAt": published}))
    return d


def attest(tmp_path, kind, *predicates):
    d = tmp_path / "attest"
    d.mkdir(exist_ok=True)
    (d / f"{kind}.jsonl").write_text("".join(statement(p) + "\n" for p in predicates))


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def deploy_repo(tmp_path) -> Path:
    """The committed bootstrap tree as a git repo on main, in tmp_path/deploy (the workflows check out to deploy/)."""
    deploy = tmp_path / "deploy"
    shutil.copytree(DEPLOY_BOOTSTRAP, deploy, ignore=shutil.ignore_patterns("__pycache__"))
    # The Flux source names this repository, as GITHUB_SERVER_URL/GITHUB_REPOSITORY give it in base_env (configured or not).
    source = deploy / "clusters/home/flux/gitrepository.yaml"
    source.write_text(re.sub(r"(?m)^(\s*url: )https://\S+$", r"\1https://github.com/acme/golden-road-deploy", source.read_text()))
    git(tmp_path, "init", "-q", "-b", "main", str(deploy))
    git(deploy, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "add", "-A")
    git(deploy, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "base")
    return deploy


def commit_all(deploy, message="pr"):
    git(deploy, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "add", "-A")
    git(deploy, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", message)


def write_pin(deploy, environment, tag, manifests_text, record, namespace=None, repo=REPO) -> Path:
    """An environment as bump writes it: release.yaml and base/manifests.yaml, then golden-road compose."""
    from golden_road.cli import main
    d = deploy / "apps" / repo / environment
    (d / "base").mkdir(parents=True, exist_ok=True)
    (d / "base/manifests.yaml").write_text(manifests_text)
    release = {"schema": "deploy-release/v1", "repo": repo, "environment": environment, "namespace": namespace or f"{repo.split('/')[1]}-{environment}",
               "release": tag, "image": IMAGE, "manifests_sha256": hashlib.sha256(manifests_text.encode()).hexdigest(),
               "gate_record_sha256": hashlib.sha256((json.dumps(record, indent=2, sort_keys=True) + "\n").encode()).hexdigest()}
    (d / "release.yaml").write_text(yaml.safe_dump(release, sort_keys=False))
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        assert main(["compose", "--root", str(deploy), f"apps/{repo}/{environment}"]) == 0
    return d


def environ_of(entry) -> dict:
    return entry[-1] if entry and isinstance(entry[-1], dict) else {}


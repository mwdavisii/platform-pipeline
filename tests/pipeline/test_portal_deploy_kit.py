"""deploy-kits/portal-homelab: what a deploy owner adds around a verified release's manifests.yaml to run the portal on a home k3s
cluster. The builder forbids overlays inside the app repo, so the kit lives outside the app tree. These tests build it with a
manifests.yaml rendered from the portal's own deploy/base (kustomize if it is installed, else they skip the build ones)."""

import copy
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from golden_road.render import render

ROOT = Path(__file__).resolve().parents[2]
KIT = ROOT / "deploy-kits/portal-homelab"
PORTAL = ROOT / "bootstrap/golden-road-portal"
REF = "ghcr.io/example-org/golden-road-portal@sha256:" + "a" * 64
NS = "golden-road-portal"
if shutil.which("kustomize"):
    BUILD = ["kustomize", "build"]
elif shutil.which("kubectl"):
    BUILD = ["kubectl", "kustomize"]
else:
    BUILD = None
needs_kustomize = pytest.mark.skipif(BUILD is None, reason="neither kustomize nor kubectl is installed")
KIT_FILES = ["kustomization.yaml", "namespace.yaml", "postgres.yaml", "migrate-job.yaml", "writer.yaml", "collector.yaml",
             "networkpolicies.yaml", "secrets.example.yaml", "README.md"]
APPLIED = ["kustomization.yaml", "namespace.yaml", "postgres.yaml", "migrate-job.yaml", "writer.yaml", "collector.yaml", "networkpolicies.yaml"]


def load_all(path):
    return load_text(Path(path).read_text())


def load_text(text):
    return [d for d in yaml.safe_load_all(text) if d]


def key(o):
    return (o["kind"], o["metadata"]["name"])


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """The kit with the rendered release manifests beside it, built by kustomize: {(kind, name): object}."""
    if BUILD is None:
        pytest.skip("neither kustomize nor kubectl is installed")
    kit = tmp_path_factory.mktemp("kit") / "portal-homelab"
    shutil.copytree(KIT, kit)
    (kit / "manifests.yaml").write_text(render(PORTAL, REF, "golden-road-portal"))
    out = subprocess.run([*BUILD, str(kit)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return {key(d): d for d in load_text(out.stdout)}


def workloads(objs):
    return {k: o for k, o in objs.items() if k[0] in ("Deployment", "StatefulSet", "Job")}


def pod(o):
    return o["spec"]["template"]["spec"]


def containers(o):
    return pod(o).get("initContainers", []) + pod(o)["containers"]


def test_the_kit_has_its_files_and_the_release_asset_is_not_committed():
    for name in KIT_FILES:
        assert (KIT / name).is_file(), name
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "deploy-kits/portal-homelab"], capture_output=True, text=True).stdout
    assert "manifests.yaml" not in tracked
    ignored = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", "deploy-kits/portal-homelab/manifests.yaml"])
    assert ignored.returncode == 0, "manifests.yaml must be git-ignored"


def test_the_kustomization_lists_exactly_the_resources_and_the_namespace():
    k = yaml.safe_load((KIT / "kustomization.yaml").read_text())
    assert k["resources"] == ["manifests.yaml", *APPLIED[1:]]
    assert k["namespace"] == NS
    assert "images" not in k and "secrets.example.yaml" not in k["resources"]
    assert k["replacements"], "the image must be copied from the web Deployment, not typed again"


@needs_kustomize
def test_everything_lands_in_the_portal_namespace(built):
    for (kind, name), o in built.items():
        if kind == "Namespace":
            assert name == NS
        else:
            assert o["metadata"]["namespace"] == NS, (kind, name)
    kinds = {k[0] for k in built}
    assert {"Deployment", "StatefulSet", "Job", "Service", "ServiceAccount", "NetworkPolicy", "CiliumNetworkPolicy", "ConfigMap"} <= kinds


@needs_kustomize
def test_the_builders_web_objects_come_through_untouched(built):
    for o in load_text(render(PORTAL, REF, "golden-road-portal")):
        got = copy.deepcopy(built[key(o)])
        got["metadata"].pop("namespace")
        assert got == o, key(o)


@needs_kustomize
def test_writer_collector_and_migrate_use_the_web_image(built):
    web = pod(built[("Deployment", "golden-road-portal")])["containers"][0]["image"]
    assert web == REF
    for k in [("Deployment", "golden-road-portal-writer"), ("Deployment", "golden-road-portal-collector"), ("Job", "golden-road-portal-migrate")]:
        assert [c["image"] for c in containers(built[k])] == [web], k
    images = {c["image"] for k, o in workloads(built).items() if "postgres" not in k[1] for c in containers(o)}
    assert images == {REF}


@needs_kustomize
def test_every_container_is_restricted(built):
    assert len(workloads(built)) == 5
    for k, o in workloads(built).items():
        p = pod(o)
        assert p["automountServiceAccountToken"] is False, k
        psc = p["securityContext"]
        assert psc["runAsNonRoot"] is True and psc["seccompProfile"] == {"type": "RuntimeDefault"}, k
        assert psc["runAsUser"] > 0 and psc["runAsGroup"] > 0, k
        for c in containers(o):
            sc = c["securityContext"]
            assert sc["allowPrivilegeEscalation"] is False and sc["readOnlyRootFilesystem"] is True and sc["runAsNonRoot"] is True, (k, c["name"])
            assert sc["capabilities"] == {"drop": ["ALL"]}, (k, c["name"])
            assert not sc.get("privileged"), k
            res = c["resources"]
            assert res["requests"]["cpu"] and res["requests"]["memory"] and res["limits"]["cpu"] and res["limits"]["memory"], (k, c["name"])
        assert not p.get("hostNetwork") and not p.get("hostPID") and not p.get("hostIPC"), k
        for v in p.get("volumes", []):
            assert "hostPath" not in v, k
    assert built[("Namespace", NS)]["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"


@needs_kustomize
def test_the_workloads_run_the_real_entry_points(built):
    assert containers(built[("Deployment", "golden-road-portal-writer")])[0]["command"] == ["python", "-m", "portal.writer"]
    assert containers(built[("Deployment", "golden-road-portal-collector")])[0]["command"] == ["python", "-m", "portal.collectors"]
    assert containers(built[("Job", "golden-road-portal-migrate")])[0]["command"] == ["python", "-m", "portal.migrate"]
    for entry in ["portal/writer/__main__.py", "portal/collectors/__main__.py"]:
        assert (PORTAL / entry).is_file()
    assert re.search(r'(?m)^if __name__ == "__main__"', (PORTAL / "portal/migrate.py").read_text())
    job = built[("Job", "golden-road-portal-migrate")]
    assert pod(job)["restartPolicy"] in ("OnFailure", "Never") and job["spec"]["backoffLimit"] >= 5
    writer = built[("Deployment", "golden-road-portal-writer")]
    assert writer["spec"]["replicas"] == 1 and writer["spec"]["strategy"] == {"type": "Recreate"}


@needs_kustomize
def test_postgres_is_one_statefulset_pinned_by_digest_with_a_pvc_and_a_clusterip(built):
    sts = built[("StatefulSet", "golden-road-portal-postgres")]
    assert sts["spec"]["replicas"] == 1
    image = containers(sts)[0]["image"]
    pinned = re.search(r'PG_IMAGE = "([^"]+)"', (PORTAL / "tests/portal_pg.py").read_text()).group(1)
    assert image == pinned and re.fullmatch(r".+@sha256:[0-9a-f]{64}", image)
    assert sts["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]["storage"]
    svc = built[("Service", "golden-road-portal-postgres")]
    assert svc["spec"]["type"] == "ClusterIP" and svc["spec"]["ports"][0]["port"] == 5432


@needs_kustomize
def test_postgres_creates_the_four_roles_the_migrations_expect(built):
    script = "\n".join(built[("ConfigMap", "golden-road-portal-postgres-init")]["data"].values())
    migrations = "\n".join(p.read_text() for p in sorted((PORTAL / "portal/migrations").glob("*.sql")))
    granted = set(re.findall(r"\bportal_(?:web|writer|collector)\b", migrations))
    assert granted == {"portal_web", "portal_writer", "portal_collector"}
    for role in granted | {"portal_owner"}:
        assert re.search(rf"CREATE ROLE {role} LOGIN PASSWORD :'", script), role
    assert "CREATE DATABASE portal OWNER portal_owner" in script
    assert "ON_ERROR_STOP=1" in script
    assert not re.search(r"PASSWORD\s+'", script), "passwords come from the environment, never the script"
    env = {e["name"]: e for e in containers(built[("StatefulSet", "golden-road-portal-postgres")])[0]["env"]}
    for name in ["POSTGRES_PASSWORD", "PORTAL_OWNER_PASSWORD", "PORTAL_WEB_PASSWORD", "PORTAL_WRITER_PASSWORD", "PORTAL_COLLECTOR_PASSWORD"]:
        assert env[name]["valueFrom"]["secretKeyRef"]["name"] == "golden-road-portal-postgres", name


def required_env(path, patterns):
    text = (PORTAL / path).read_text()
    return {n for p in patterns for n in re.findall(p, text)}


REQUIRED = {
    "web": lambda: required_env("portal/settings.py", [r'need\("(PORTAL_\w+)"[,)]']),
    "writer": lambda: required_env("portal/writer/__main__.py", [r'_need\(env, "(PORTAL_\w+)"\)', r'_int\(env, "(PORTAL_\w+)"\)']),
    "collector": lambda: required_env("portal/collectors/__main__.py", [r'= env\.get\("(PORTAL_GITHUB_HOST)", ""\)\.lower', r'_missing\("(PORTAL_\w+)"\)']),
    "migrate": lambda: required_env("portal/migrate.py", [r'environ\.get\("(PORTAL_\w+)"']),
}


# Settings a process may go without that the kit still passes (from the ConfigMap, where an empty value means unset).
OPTIONAL_PASSED = {"web": {"PORTAL_DIRECTORY", "PORTAL_IDENTITY_PROCESS_URL", "PORTAL_DATA_PROCESS_URL", "PORTAL_REPO_ADMINS_STALE_AFTER"},
                   "writer": {"PORTAL_DIRECTORY", "PORTAL_REPO_ADMINS_STALE_AFTER"}}


def provided(built, o):
    """name -> where each env var of the pod's one container comes from; config keys must exist in the generated ConfigMap."""
    out = {}
    for e in containers(o)[0].get("env", []):
        ref = e.get("valueFrom")
        if ref and "configMapKeyRef" in ref:
            assert ref["configMapKeyRef"]["name"] == "golden-road-portal-config"
            assert ref["configMapKeyRef"]["key"] in built[("ConfigMap", "golden-road-portal-config")]["data"], e["name"]
            out[e["name"]] = ("config", ref["configMapKeyRef"]["key"])
        elif ref and "secretKeyRef" in ref:
            out[e["name"]] = ("secret", ref["secretKeyRef"]["name"], ref["secretKeyRef"]["key"])
        else:
            out[e["name"]] = ("literal", e["value"])
    return out


@needs_kustomize
def test_the_env_the_kit_provides_is_what_each_process_requires(built):
    pods = {"web": ("Deployment", "golden-road-portal"), "writer": ("Deployment", "golden-road-portal-writer"),
            "collector": ("Deployment", "golden-road-portal-collector"), "migrate": ("Job", "golden-road-portal-migrate")}
    for proc, k in pods.items():
        have = {n for n in provided(built, built[k]) if n.startswith("PORTAL_")}
        need = REQUIRED[proc]() | OPTIONAL_PASSED.get(proc, set())
        assert need and have == need, (proc, sorted(have ^ need))
    assert REQUIRED["web"]() == {"PORTAL_BASE_URL", "PORTAL_ORG", "PORTAL_GITHUB_HOST", "PORTAL_OIDC_ISSUER", "PORTAL_OIDC_CLIENT_ID",
                                 "PORTAL_OIDC_CLIENT_SECRET", "PORTAL_ROLE_GROUPS", "PORTAL_GITHUB_OAUTH_CLIENT_ID",
                                 "PORTAL_GITHUB_OAUTH_CLIENT_SECRET", "PORTAL_DATABASE_URL"}
    assert REQUIRED["writer"]() == {"PORTAL_GITHUB_HOST", "PORTAL_ORG", "PORTAL_ROLE_GROUPS", "PORTAL_WRITE_APP_ID",
                                    "PORTAL_WRITE_INSTALLATION_ID", "PORTAL_DATABASE_URL", "PORTAL_WRITE_APP_KEY_FILE"}
    assert REQUIRED["collector"]() == {"PORTAL_GITHUB_HOST", "PORTAL_READ_APP_ID", "PORTAL_READ_APP_KEY_FILE", "PORTAL_READ_INSTALLATION_ID",
                                       "PORTAL_DATABASE_URL", "PORTAL_ORG", "PORTAL_MODEL_GATEWAY_HOSTS"}
    assert REQUIRED["migrate"]() == {"PORTAL_MIGRATE_DATABASE_URL"}


@needs_kustomize
def test_secret_references_match_secrets_example_and_each_process_gets_only_its_own(built):
    example = {d["metadata"]["name"]: set(d["stringData"]) for d in load_all(KIT / "secrets.example.yaml")}
    used, pairs = {}, set()
    for k, o in workloads(built).items():
        for c in containers(o):
            for e in c.get("env", []):
                ref = (e.get("valueFrom") or {}).get("secretKeyRef")
                if ref:
                    assert ref["key"] in example[ref["name"]], (k, e["name"])
                    used.setdefault(k, set()).add(ref["name"])
                    pairs.add((ref["name"], ref["key"]))
        for v in pod(o).get("volumes", []):
            if "secret" in v:
                name = v["secret"]["secretName"]
                used.setdefault(k, set()).add(name)
                pairs |= {(name, i["key"]) for i in v["secret"]["items"]}
    assert used[("Deployment", "golden-road-portal")] == {"golden-road-portal-secrets"}
    assert used[("Deployment", "golden-road-portal-writer")] == {"golden-road-portal-writer"}
    assert used[("Deployment", "golden-road-portal-collector")] == {"golden-road-portal-collector"}
    assert used[("Job", "golden-road-portal-migrate")] == {"golden-road-portal-migrate"}
    assert used[("StatefulSet", "golden-road-portal-postgres")] == {"golden-road-portal-postgres"}
    assert pairs == {(n, k) for n, keys in example.items() for k in keys}, "every key the example lists is used, and none is missing"
    for who, var in [("writer", "PORTAL_WRITE_APP_KEY_FILE"), ("collector", "PORTAL_READ_APP_KEY_FILE")]:
        dep = built[("Deployment", f"golden-road-portal-{who}")]
        where = provided(built, dep)[var]
        assert where[0] == "literal", "the key file is mounted, never put in the environment"
        mounts = {m["mountPath"]: m for m in containers(dep)[0]["volumeMounts"]}
        assert where[1].rsplit("/", 1)[0] in mounts and mounts[where[1].rsplit("/", 1)[0]]["readOnly"] is True


@needs_kustomize
def test_the_configmap_holds_the_poc_values_and_placeholders(built):
    data = built[("ConfigMap", "golden-road-portal-config")]["data"]
    assert data["PORTAL_BASE_URL"] == "https://viber.mwdavisii.com"
    assert data["PORTAL_ORG"] == "mwdavisii" and data["PORTAL_GITHUB_HOST"] == "github.com"
    assert data["PORTAL_OIDC_ISSUER"].startswith("https://") and "replace-me" in data["PORTAL_OIDC_ISSUER"]
    sys.path.insert(0, str(PORTAL))
    try:
        from portal.settings import parse_role_groups
    finally:
        sys.path.remove(str(PORTAL))
    assert parse_role_groups(data["PORTAL_ROLE_GROUPS"]) == {"admin": frozenset({"golden-road-admins"}), "ops": frozenset({"golden-road-ops"})}
    for name in ["PORTAL_WRITE_APP_ID", "PORTAL_WRITE_INSTALLATION_ID", "PORTAL_READ_APP_ID", "PORTAL_READ_INSTALLATION_ID"]:
        assert name in data
    assert "replace-me" in data["PORTAL_MODEL_GATEWAY_HOSTS"]  # the platform's model_gateway_hosts: the operator sets it


@needs_kustomize
def test_the_kit_s_writer_gets_the_directory_setting_the_web_gets(built):
    """The web shows a binding from the directory and the writer re-checks it against the same one: both read the same key."""
    web = provided(built, built[("Deployment", "golden-road-portal")])
    writer = provided(built, built[("Deployment", "golden-road-portal-writer")])
    assert web["PORTAL_DIRECTORY"] == writer["PORTAL_DIRECTORY"] == ("config", "PORTAL_DIRECTORY")
    assert web["PORTAL_REPO_ADMINS_STALE_AFTER"] == writer["PORTAL_REPO_ADMINS_STALE_AFTER"] == ("config", "PORTAL_REPO_ADMINS_STALE_AFTER")
    data = built[("ConfigMap", "golden-road-portal-config")]["data"]
    assert data["PORTAL_REPO_ADMINS_STALE_AFTER"] == "", "empty: the default (600 seconds) for both"
    assert data["PORTAL_DIRECTORY"] == "" and data["PORTAL_IDENTITY_PROCESS_URL"] == "" and data["PORTAL_DATA_PROCESS_URL"] == "", \
        "off until the SRE turns it on (personal-poc 19)"


def test_the_kit_s_writer_yaml_passes_the_directory_setting():
    writer = yaml.safe_load((KIT / "writer.yaml").read_text())
    names = [e["name"] for c in writer["spec"]["template"]["spec"]["containers"] for e in c.get("env", [])]
    assert "PORTAL_DIRECTORY" in names


def test_no_secret_object_or_value_is_in_the_kit():
    for name in APPLIED:
        assert all(d["kind"] != "Secret" for d in load_all(KIT / name)), name
    assert "secretGenerator" not in (KIT / "kustomization.yaml").read_text()
    for d in load_all(KIT / "secrets.example.yaml"):
        assert d["kind"] == "Secret" and "data" not in d
        assert set(d["stringData"].values()) == {"REPLACE_ME"}, d["metadata"]["name"]
    blob = "\n".join(p.read_text() for p in KIT.iterdir() if p.is_file())
    assert "PRIVATE KEY" not in blob
    assert not re.search(r"(ghp_|github_pat_|gho_|ghs_)[A-Za-z0-9]{10}", blob)
    assert not re.search(r"://portal_\w+:(?!<)[^@\s]+@", blob), "database URLs in the kit must not hold a password"


@needs_kustomize
def test_network_policies_follow_the_least_privilege_shape(built):
    nps = {k[1]: o for k, o in built.items() if k[0] == "NetworkPolicy"}
    cnps = {k[1]: o for k, o in built.items() if k[0] == "CiliumNetworkPolicy"}
    assert "golden-road-portal-baseline" in nps and "golden-road-portal-egress" in cnps  # the builder's, untouched above
    pg = nps["golden-road-portal-postgres"]
    assert pg["spec"]["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "golden-road-portal-postgres"}
    assert pg["spec"]["policyTypes"] == ["Ingress", "Egress"] and pg["spec"]["egress"] == []
    froms = pg["spec"]["ingress"][0]["from"]
    assert len(froms) == 1 and set(froms[0]) == {"podSelector"}
    allowed = froms[0]["podSelector"]["matchExpressions"][0]
    assert allowed["key"] == "app.kubernetes.io/name" and allowed["operator"] == "In"
    assert set(allowed["values"]) == {"golden-road-portal", "golden-road-portal-writer", "golden-road-portal-collector", "golden-road-portal-migrate"}
    assert pg["spec"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 5432}]
    postgres_peer = [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "golden-road-portal-postgres"}}}]
    for who in ["writer", "collector", "migrate"]:
        spec = nps[f"golden-road-portal-{who}"]["spec"]
        assert spec["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": f"golden-road-portal-{who}"}
        assert spec["policyTypes"] == ["Ingress", "Egress"] and not spec.get("ingress")
        assert [r["to"] for r in spec["egress"]] == [postgres_peer] and spec["egress"][0]["ports"] == [{"protocol": "TCP", "port": 5432}]
    web_pg = nps["golden-road-portal-web-to-postgres"]["spec"]
    assert web_pg["podSelector"]["matchLabels"] == {"app.kubernetes.io/name": "golden-road-portal"}
    assert web_pg["policyTypes"] == ["Egress"] and [r["to"] for r in web_pg["egress"]] == [postgres_peer]

    def fqdns(c):
        return sorted(m["matchName"] for r in c["spec"]["egress"] for m in r.get("toFQDNs", []))

    for who, hosts in {"writer": ["api.github.com", "github.com"], "collector": ["api.github.com"], "migrate": []}.items():
        c = cnps[f"golden-road-portal-{who}-egress"]
        assert c["spec"]["endpointSelector"]["matchLabels"] == {"app.kubernetes.io/name": f"golden-road-portal-{who}"}
        assert fqdns(c) == hosts, who
        assert c["spec"]["egress"][0]["toEndpoints"][0]["matchLabels"]["k8s-app"] == "kube-dns"
        for r in c["spec"]["egress"][1:]:
            assert r["toPorts"][0]["ports"] == [{"port": "443", "protocol": "TCP"}]
        assert all(set(r) <= {"toEndpoints", "toFQDNs", "toPorts"} for r in c["spec"]["egress"])
    oidc = cnps["golden-road-portal-oidc-egress"]
    assert oidc["spec"]["endpointSelector"]["matchLabels"] == {"app.kubernetes.io/name": "golden-road-portal"}
    issuer_host = re.match(r"https://([^/:]+)", built[("ConfigMap", "golden-road-portal-config")]["data"]["PORTAL_OIDC_ISSUER"]).group(1)
    assert fqdns(oidc) == [issuer_host]
    assert oidc["spec"]["egress"][0]["toEndpoints"][0]["matchLabels"]["k8s-app"] == "kube-dns"
    assert oidc["spec"]["egress"][0]["toPorts"][0]["rules"]["dns"] == [{"matchPattern": "*"}]
    assert oidc["spec"]["egress"][1]["toPorts"][0]["ports"] == [{"port": "443", "protocol": "TCP"}]
    ingress = nps["golden-road-portal-web-from-cloudflared"]["spec"]
    assert ingress["policyTypes"] == ["Ingress"]
    assert ingress["ingress"][0]["from"] == [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "cloudflared"}},
                                              "podSelector": {"matchLabels": {"app": "cloudflared"}}}]
    for o in [*nps.values(), *cnps.values()]:
        sel = o["spec"].get("podSelector") or o["spec"].get("endpointSelector")
        assert sel.get("matchLabels"), o["metadata"]["name"]


def test_the_release_files_and_scratch_attestations_are_git_ignored():
    for name in ["manifests.yaml", "gate.jsonl", "promo.jsonl"]:
        assert subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", f"deploy-kits/portal-homelab/{name}"]).returncode == 0, name
    assert "assumes the homelab edge" in (KIT / "networkpolicies.yaml").read_text()


def test_the_readme_lists_the_write_apps_exact_permissions():
    """An explicit expected set, not read from the writer's code, so it holds before and after the code's own list gains actions: read."""
    row = next(l for l in (KIT / "README.md").read_text().splitlines() if l.startswith("| Repository permissions"))
    write = row.split("|")[3]
    found = set(re.findall(r"([A-Z][\w ]*?)\s*(?:\(`\w+`\))?:\s*(read|write)", write))
    assert found == {("Contents", "write"), ("Pull requests", "write"), ("Metadata", "read"), ("Checks", "read"), ("Actions", "read"),
                     ("Administration", "read")}, found


def test_the_readme_says_what_the_read_app_s_contents_read_is_for():
    text = " ".join((KIT / "README.md").read_text().split())
    assert "Contents read is used by the evidence collector" in text and "`config/registration.yaml`" in text
    assert "`PORTAL_MODEL_GATEWAY_HOSTS`" in text and "collectors: evidence: N computed, M waiting" in text


def test_the_readme_covers_the_setup_and_the_checks():
    text = (KIT / "README.md").read_text()
    for needle in ["https://viber.mwdavisii.com/auth/callback", "https://viber.mwdavisii.com/link/github/callback", "X-Platform-Roles: portal",
                   "cloudflared", "golden-road-admins", "golden-road-ops", "scorecard-factory", "cosign verify", "verify-attestation",
                   "manifests_sha256", "--certificate-github-workflow-repository", "kubectl apply", "secrets.example.yaml", "/healthz",
                   "administration", "pull_requests", "delete job", "gh release download", "PORTAL_WRITE_APP_KEY_FILE", "PORTAL_READ_APP_KEY_FILE",
                   "force: true", "commit the verified", "own GitOps repository", "wait for job/golden-road-portal-migrate",
                   "--for=condition=complete", "test release", "self_approved", "in writing", "Members", "personal account",
                   "planned collectors", "environment variables of its pod", "PORTAL_MIGRATE_DATABASE_URL", "portal_owner", "portal_web", "portal_writer", "portal_collector"]:
        assert needle in text, needle


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    """Export a clean clone of HEAD with the public export script, so the test holds whatever the working tree holds."""
    base = tmp_path_factory.mktemp("export")
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(ROOT), str(base / "src")], check=True)
    dest = base / "tree"
    out = subprocess.run([str(base / "src/scripts/export-public-poc.sh"), str(dest)], capture_output=True, text=True, cwd=base / "src")
    assert out.returncode == 0, out.stderr
    return dest


@pytest.mark.skipif(not (ROOT / "scripts/export-public-poc.sh").exists(), reason="the export script is not exported")
def test_the_kit_is_in_the_public_export_and_holds_no_private_pattern_word(exported):
    for name in KIT_FILES:
        assert (exported / "deploy-kits/portal-homelab" / name).is_file(), name
    assert not (exported / "deploy-kits/portal-homelab/manifests.yaml").exists()
    # The pattern list is never exported, so this half only runs in the private tree (and its path is spelled so this file names no internal dir).
    patterns_file = ROOT / "docs" / ("super" + "powers") / "export-patterns.txt"
    if not patterns_file.exists():
        return
    patterns = [l for l in patterns_file.read_text().splitlines() if l.strip() and not l.startswith("#")]
    forbidden = re.compile("|".join(f"(?:{p})" for p in patterns), re.I)
    for path in KIT.rglob("*"):
        if path.is_file():
            assert not forbidden.search(path.relative_to(ROOT).as_posix()), path
            for n, line in enumerate(path.read_text().splitlines(), 1):
                assert not forbidden.search(line), f"{path.name}:{n}"

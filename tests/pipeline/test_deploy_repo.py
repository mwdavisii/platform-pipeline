"""bootstrap/golden-road-deploy: the platform-owned repo of per-environment release pins and bindings. Mock only: nothing here is
applied to a cluster, every adapter is suspended (Flux) or manual-sync (Argo), and the image policy only warns."""

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "bootstrap/golden-road-deploy"
FILES = ("README.md", ".github/CODEOWNERS", ".github/rulesets/main.json", ".github/rulesets/main-review.json", "apps/.gitkeep",
         "clusters/home/flux/README.md", "clusters/home/flux/gitrepository.yaml", "clusters/office/argo/README.md",
         "policy/cluster-image-policy.yaml", "external-secrets/README.md", "external-secrets/cluster-secret-store.yaml",
         "external-secrets/cilium-egress.yaml")


def org():
    """The org the live tree was configured for, or the placeholder when it is pristine."""
    return json.loads((ROOT / "platform/platform.json").read_text()).get("org", "ORG_PLACEHOLDER")


def codeowners():
    """CODEOWNERS as (pattern, owners) pairs, comments dropped."""
    lines = [l.split() for l in (DEPLOY / ".github/CODEOWNERS").read_text().splitlines() if l.strip() and not l.startswith("#")]
    return [(l[0], l[1:]) for l in lines]


def test_the_files_exist():
    for rel in FILES:
        path = DEPLOY / rel
        assert path.is_file() and not path.is_symlink(), rel


def test_code_owners_give_bindings_to_security_and_analytics_and_everything_else_to_the_platform():
    rules = codeowners()
    if org() != "ORG_PLACEHOLDER" and not any(o.startswith(f"@{org()}/") for _, owners in rules for o in owners):
        return  # the owner replaced the teams with personal logins (single-approver PoC)
    platform, security, analytics = (f"@{org()}/{team}" for team in ("platform-reviewers", "security", "analytics"))
    assert rules[0] == ("*", [platform]), "everything defaults to the platform"
    assert ("/apps/**/bindings.yaml", [security, analytics]) in rules
    assert ("/.github/", [platform]) in rules
    # The last matching rule wins: nothing after the bindings rule may take bindings.yaml back.
    after = rules[rules.index(("/apps/**/bindings.yaml", [security, analytics])) + 1:]
    assert all(not p.startswith("/apps") and p != "*" for p, _ in after)


def ruleset(name):
    return json.loads((DEPLOY / ".github/rulesets" / name).read_text())


def test_main_requires_deploy_check_from_actions_and_has_no_bypass():
    main = ruleset("main.json")
    assert main["enforcement"] == "active" and main["target"] == "branch" and not main.get("bypass_actors")
    assert main["conditions"]["ref_name"] == {"include": ["~DEFAULT_BRANCH"], "exclude": []}
    assert sorted(r["type"] for r in main["rules"]) == ["deletion", "non_fast_forward", "required_status_checks"]
    checks = next(r for r in main["rules"] if r["type"] == "required_status_checks")["parameters"]
    assert checks["strict_required_status_checks_policy"] is True
    assert checks["required_status_checks"] == [{"context": "deploy-check", "integration_id": 15368}]


def test_the_review_rule_is_its_own_ruleset_like_the_factory_s():
    """The portal writer opens binding PRs here; it may be made a pull_request-mode bypass actor on main-review alone, at apply
    time. The committed file lists no bypass."""
    review, factory = ruleset("main-review.json"), json.loads((ROOT / "bootstrap/scorecard-factory/.github/rulesets/main-review.json").read_text())
    assert review == factory
    assert sorted(p.name for p in (DEPLOY / ".github/rulesets").iterdir()) == ["main-review.json", "main.json"]
    factory_main = json.loads((ROOT / "bootstrap/scorecard-factory/.github/rulesets/main.json").read_text())
    for rule in factory_main["rules"]:
        if rule["type"] == "required_status_checks":
            rule["parameters"]["required_status_checks"] = [{"context": "deploy-check", "integration_id": 15368}]
    assert ruleset("main.json") == factory_main


def test_the_gitrepository_points_at_golden_road_deploy_on_main():
    doc = yaml.safe_load((DEPLOY / "clusters/home/flux/gitrepository.yaml").read_text())
    assert (doc["apiVersion"], doc["kind"]) == ("source.toolkit.fluxcd.io/v1", "GitRepository")
    assert doc["metadata"] == {"name": "golden-road-deploy", "namespace": "flux-system"}
    assert doc["spec"]["url"] == f"https://github.com/{org()}/golden-road-deploy"
    assert doc["spec"]["ref"] == {"branch": "main"}


def promote_identity():
    """The signer identity golden-road-promote.yml verifies, with the runner variables filled in for github.com and the org."""
    text = (ROOT / ".github/workflows/golden-road-promote.yml").read_text()
    [identity] = re.findall(r'--certificate-identity "([^"]+)"', text)
    [issuer] = re.findall(r"--certificate-oidc-issuer (\S+)", text)
    return identity.replace("${GITHUB_SERVER_URL}", "https://github.com").replace("${GITHUB_REPOSITORY_OWNER}", org()), issuer


def test_the_image_policy_warns_and_trusts_only_the_platform_release_workflow():
    doc = yaml.safe_load((DEPLOY / "policy/cluster-image-policy.yaml").read_text())
    assert (doc["apiVersion"], doc["kind"]) == ("policy.sigstore.dev/v1beta1", "ClusterImagePolicy")
    assert doc["spec"]["mode"] == "warn", "mock only: the policy never denies until Mike switches it on"
    assert doc["spec"]["images"] == [{"glob": f"ghcr.io/{org()}/**"}]
    [authority] = doc["spec"]["authorities"]
    subject, issuer = promote_identity()
    assert authority["keyless"]["identities"] == [{"issuer": issuer, "subject": subject}]
    assert authority["attestations"] == [{"name": "gate-record", "predicateType": "https://golden-road.internal/gate-record/v1"}]
    assert "GATE_PREDICATE: https://golden-road.internal/gate-record/v1" in (ROOT / ".github/workflows/golden-road.yml").read_text()


def test_no_adapter_is_committed_until_an_app_is_bumped_in():
    for d in ("clusters/home/flux", "clusters/office/argo"):
        assert sorted(p.name for p in (DEPLOY / d).iterdir()) == sorted(["README.md"] + (["gitrepository.yaml"] if "flux" in d else []))
    assert [p.name for p in (DEPLOY / "apps").iterdir()] == [".gitkeep"]


def test_the_committed_tree_verifies():
    from golden_road.deploy import deploy_repo_url, verify_tree
    assert verify_tree(DEPLOY) == {"environments": 0, "problems": []}
    assert deploy_repo_url(DEPLOY) == f"https://github.com/{org()}/golden-road-deploy"  # what compose writes into the Argo adapters


def test_the_readme_says_nothing_reconciles():
    text = (DEPLOY / "README.md").read_text()
    assert "suspend: true" in text and "warn" in text and "automated" in text


def test_configure_fills_the_org(tmp_path):
    """configure.py rewrites every ORG_PLACEHOLDER in the deploy repo, and the configured tree still verifies with the org's URL."""
    import shutil
    import subprocess
    import sys
    from golden_road.deploy import deploy_repo_url, verify_tree
    from helpers import unconfigure_agents, unconfigure_deploy, unconfigure_factory, unconfigure_hello_app, unconfigure_platform, unconfigure_portal

    shutil.copytree(ROOT / "platform", tmp_path / "platform")
    shutil.copytree(ROOT / "bootstrap", tmp_path / "bootstrap", ignore=shutil.ignore_patterns("__pycache__"))
    live = json.loads((ROOT / "platform/platform.json").read_text())
    unconfigure_platform(tmp_path / "platform")
    unconfigure_hello_app(tmp_path / "bootstrap/hello-app")
    unconfigure_portal(tmp_path / "bootstrap/golden-road-portal")
    unconfigure_factory(tmp_path / "bootstrap/scorecard-factory", live.get("org"))
    unconfigure_deploy(tmp_path / "bootstrap/golden-road-deploy", live.get("org"))
    unconfigure_agents(tmp_path / "bootstrap/golden-road-agents", live.get("org"), live.get("github_host"), live.get("actions_oidc_issuer"))
    deploy = tmp_path / "bootstrap/golden-road-deploy"
    assert "ORG_PLACEHOLDER" in (deploy / "policy/cluster-image-policy.yaml").read_text()  # the helpers put the placeholders back
    out = subprocess.run([sys.executable, str(ROOT / "scripts/configure.py"), "--root", str(tmp_path), "--org", "example-org",
                          "--gateway-host", "llm.example.org", "--edge-namespace", "edge-ns", "--edge-label", "app=edge",
                          "--identity-header", "X-Roles-Test"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    for path in deploy.rglob("*"):
        if path.is_file():
            assert "ORG_PLACEHOLDER" not in path.read_text(), path
    assert "* @example-org/platform-reviewers" in (deploy / ".github/CODEOWNERS").read_text()
    assert "@example-org/security @example-org/analytics" in (deploy / ".github/CODEOWNERS").read_text()
    policy = yaml.safe_load((deploy / "policy/cluster-image-policy.yaml").read_text())
    assert policy["spec"]["images"] == [{"glob": "ghcr.io/example-org/**"}] and policy["spec"]["mode"] == "warn"
    assert policy["spec"]["authorities"][0]["keyless"]["identities"][0]["subject"].startswith("https://github.com/example-org/platform-pipeline/")
    assert deploy_repo_url(deploy) == "https://github.com/example-org/golden-road-deploy"
    assert verify_tree(deploy) == {"environments": 0, "problems": []}


def test_the_cluster_secret_store_is_the_platform_s_and_holds_no_credential():
    store = yaml.safe_load((DEPLOY / "external-secrets/cluster-secret-store.yaml").read_text())
    assert store["apiVersion"] == "external-secrets.io/v1" and store["kind"] == "ClusterSecretStore"
    assert store["metadata"] == {"name": "delinea-secret-server"}
    assert store["spec"]["conditions"] == [{"namespaceRegexes": ["^[a-z0-9-]+-(test|prod)$"]}]  # only <repo>-<env> namespaces
    provider = store["spec"]["provider"]
    assert list(provider) == ["secretserver"]
    assert provider["secretserver"]["serverURL"] == "https://secretserver.example.invalid"
    for key in ("username", "password"):
        assert provider["secretserver"][key] == {"secretRef": {"name": "golden-road-eso", "key": key, "namespace": "external-secrets"}}
    assert "value" not in json.dumps(store)
    from golden_road.compose import EXTERNAL_SECRET_STORE
    assert store["metadata"]["name"] == EXTERNAL_SECRET_STORE  # the store every composed ExternalSecret names


def test_the_eso_egress_policy_reaches_only_dns_the_api_server_and_the_secret_server():
    policy = yaml.safe_load((DEPLOY / "external-secrets/cilium-egress.yaml").read_text())
    assert policy["apiVersion"] == "cilium.io/v2" and policy["kind"] == "CiliumNetworkPolicy"
    assert policy["metadata"] == {"name": "external-secrets-to-secret-server", "namespace": "external-secrets"}
    spec = policy["spec"]
    assert spec["endpointSelector"] == {"matchLabels": {"app.kubernetes.io/name": "external-secrets"}}
    dns, api, server = spec["egress"]
    assert dns["toEndpoints"] == [{"matchLabels": {"k8s:io.kubernetes.pod.namespace": "kube-system", "k8s-app": "kube-dns"}}]
    assert dns["toPorts"] == [{"ports": [{"port": "53", "protocol": "UDP"}, {"port": "53", "protocol": "TCP"}],
                               "rules": {"dns": [{"matchPattern": "*"}]}}]
    assert api == {"toEntities": ["kube-apiserver"]}  # the controller watches ExternalSecrets and writes their Secrets
    assert server == {"toFQDNs": [{"matchName": "secretserver.example.invalid"}], "toPorts": [{"ports": [{"port": "443", "protocol": "TCP"}]}]}


def test_nothing_points_at_external_secrets():
    """Documented, applied by hand: no adapter, kustomization or source in the deploy repo names the directory."""
    assert sorted(p.name for p in (DEPLOY / "external-secrets").iterdir()) == ["README.md", "cilium-egress.yaml", "cluster-secret-store.yaml"]
    for path in DEPLOY.rglob("*"):
        rel = path.relative_to(DEPLOY).as_posix()
        if path.is_file() and not rel.startswith(("external-secrets/", ".github/")) and rel != "README.md":
            assert "external-secrets" not in path.read_text(), rel


def test_the_external_secrets_readme_says_who_applies_it_and_the_gap():
    text = (DEPLOY / "external-secrets/README.md").read_text()
    for needle in ("by hand", "golden-road-eso", "every secret shared with it", "golden-road compose", "Mock only", "off by default"):
        assert needle in text, needle


def test_the_external_secrets_readme_says_deploy_check_does_not_confirm_a_reference():
    text = " ".join((DEPLOY / "external-secrets/README.md").read_text().split())
    assert "deploy-check does not confirm a reference against Secret Server" in text
    assert "writer" in text and "edited by hand" in text and "security's review" in text

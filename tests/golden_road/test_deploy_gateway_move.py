"""When the platform moves an MCP gateway (final review of 0.6.0): only an environment whose binding a pull request changes must name
platform.json's gateway; the others keep the old one, with a warning, so a pin or another app's change still passes deploy-check."""

import hashlib
import json

import pytest
import yaml
from builder.config import load_platform
from golden_road.cli import main
from golden_road.deploy import changed_bindings, verify_tree
from golden_road.render import render

from test_deploy_dir import binding_v2, write_gitrepository

OLD, NEW = "mcp-test.example.com", "mcp-test2.example.com"
NOTES = [{"server_id": "notes", "name": "Notes", "verified_by": "mock-cloudflare"}]


def make_env(root, app, repo, binding):
    owner, name = repo.split("/")
    d = root / "apps" / owner / name / "test"
    (d / "base").mkdir(parents=True)
    image = f"ghcr.io/{owner}/{name}@sha256:" + "b" * 64
    signed = render(app, image, "notes-api")  # the fixture app is notes-api; each repo has its own namespace
    (d / "base/manifests.yaml").write_text(signed)
    (d / "release.yaml").write_text(yaml.safe_dump({
        "schema": "deploy-release/v1", "repo": repo, "environment": "test", "namespace": f"{name}-test",
        "release": "test-20261029132351-42fa829", "image": image, "manifests_sha256": hashlib.sha256(signed.encode()).hexdigest(),
        "gate_record_sha256": "a" * 64}))
    (d / "bindings.yaml").write_text(yaml.safe_dump(binding))
    return d


def compose(root, *rels, unchanged=False):
    return main(["compose", "--root", str(root), *(["--unchanged-binding"] if unchanged else []), *rels])


@pytest.fixture
def tree(tmp_path, app, monkeypatch):
    """Two apps, A (acme/notes) and B (acme/other), each with Notes on the test gateway; then the platform moves test's gateway."""
    root = tmp_path / "deploy"
    write_gitrepository(root)
    for repo in ("acme/notes", "acme/other"):
        make_env(root, app, repo, binding_v2(repo=repo, mcp={"gateway": OLD, "servers": NOTES}))
    assert compose(root, "apps/acme/notes/test", "apps/acme/other/test") == 0
    assert verify_tree(root)["problems"] == []
    platform = load_platform()
    (tmp_path / "platform.json").write_text(json.dumps({**platform, "mcp_gateways": {**platform["mcp_gateways"], "test": NEW}}))
    monkeypatch.setenv("BUILDER_PLATFORM_CONFIG", str(tmp_path / "platform.json"))
    return root


def test_after_a_move_every_environment_is_held_when_nothing_names_what_changed(tree):
    problems = verify_tree(tree)["problems"]
    assert sum(f"mcp gateway is {OLD}, but platform.json's test gateway is {NEW}" in p for p in problems) == 2, problems


def test_app_a_s_remove_passes_while_app_b_still_names_the_old_gateway(tree):
    a = tree / "apps/acme/notes/test"
    (a / "bindings.yaml").write_text(yaml.safe_dump(binding_v2(repo="acme/notes", mcp={"gateway": NEW, "servers": []})))
    assert compose(tree, "apps/acme/notes/test") == 0
    report = verify_tree(tree, changed=changed_bindings(["apps/acme/notes/test/bindings.yaml", "apps/acme/notes/test/composed.yaml",
                                                         "apps/acme/notes/test/cloudflare-access.yaml"]))
    assert report["problems"] == []
    assert len(report["warnings"]) == 1 and report["warnings"][0].startswith("apps/acme/other/test: ") and OLD in report["warnings"][0]


def test_a_remove_that_keeps_the_old_gateway_with_no_servers_passes(tree):
    a = tree / "apps/acme/notes/test"
    (a / "bindings.yaml").write_text(yaml.safe_dump(binding_v2(repo="acme/notes", mcp={"gateway": OLD, "servers": []})))
    assert compose(tree, "apps/acme/notes/test") == 0  # strict: servers [] grants nothing, so the old gateway is no problem
    assert verify_tree(tree, changed={"apps/acme/notes/test"})["problems"] == []


def test_a_changed_environment_that_keeps_servers_on_the_old_gateway_is_refused(tree):
    a = tree / "apps/acme/notes/test"
    (a / "bindings.yaml").write_text(yaml.safe_dump(binding_v2(repo="acme/notes", mcp={"gateway": OLD, "servers": NOTES},
                                                               request="r_" + "c" * 32)))
    assert compose(tree, "apps/acme/notes/test") == 2  # compose of a changing binding refuses it
    problems = verify_tree(tree, changed={"apps/acme/notes/test"})["problems"]
    assert any(p.startswith("apps/acme/notes/test: ") and NEW in p for p in problems), problems
    assert not any(p.startswith("apps/acme/other/test") for p in problems)


def test_a_pin_bump_of_app_b_composes_and_passes(tree, capsys):
    b = tree / "apps/acme/other/test"
    release = yaml.safe_load((b / "release.yaml").read_text())
    (b / "release.yaml").write_text(yaml.safe_dump({**release, "release": "test-20261031120000-1234567"}))
    assert compose(tree, "apps/acme/other/test") == 2  # without the flag the old gateway is refused
    assert compose(tree, "apps/acme/other/test", unchanged=True) == 0
    assert f"mcp gateway is {OLD}" in capsys.readouterr().err
    report = verify_tree(tree, changed=changed_bindings(["apps/acme/other/test/release.yaml", "apps/acme/other/test/composed.yaml"]))
    assert report["problems"] == [] and len(report["warnings"]) == 2


def test_the_cli_reads_the_changed_paths(tree, tmp_path, capsys):
    changed = tmp_path / "changed.txt"
    changed.write_text("apps/acme/other/test/release.yaml\nREADME.md\n")
    assert main(["deploy-verify", "--root", str(tree), "--changed", str(changed)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["problems"] == [] and len(out["warnings"]) == 2
    changed.write_text("apps/acme/other/test/bindings.yaml\n")
    assert main(["deploy-verify", "--root", str(tree), "--changed", str(changed)]) == 1


def test_changed_bindings_names_only_binding_changes():
    assert changed_bindings(["apps/a/b/test/bindings.yaml", "apps/a/b/prod/cloudflare-access.yaml", "apps/a/c/test/release.yaml",
                             "apps/a/c/test/composed.yaml", "README.md", "apps/a/b/test/bindings.yaml.bak"]) == {"apps/a/b/test", "apps/a/b/prod"}

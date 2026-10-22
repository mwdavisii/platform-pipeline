import json

import pytest

from builder.config import load_platform
from builder.generate import generate
from builder.lock import sha256_text
from builder.render.agents import BEGIN, END, MANAGED_FILES
from builder.verify import verify
from contracts.schema import validate


@pytest.fixture
def generated(notes_spec, python_app):
    generate(python_app, notes_spec)
    return python_app


def rules(findings):
    for f in findings:
        assert validate("finding.v1", f) == [], f
    return {f["rule_id"]: f for f in findings}


def test_clean_repo_has_no_findings(generated):
    assert verify(generated) == []


def test_hand_edit_is_high_with_line(generated):
    path = generated / "Dockerfile"
    lines = path.read_text().splitlines()
    index = next(i for i, l in enumerate(lines) if l.startswith("USER "))
    lines[index] = "USER root"
    path.write_text("\n".join(lines) + "\n")
    f = rules(verify(generated))["builder.file-modified"]
    assert f["severity"] == "high"
    assert f["location"] == {"kind": "line", "file": "Dockerfile", "start_line": index + 1, "end_line": index + 1}
    assert f["evidence"][0]["snippet"] == "USER root"
    assert {"framework": "nist-ssdf", "id": "PW.9"} in f["standards"]


def test_deleted_file(generated):
    (generated / "deploy/base/networkpolicy.yaml").unlink()
    f = rules(verify(generated))["builder.file-missing"]
    assert f["location"]["file"] == "deploy/base/networkpolicy.yaml"


def test_unlisted_file_in_generated_dir(generated):
    (generated / "deploy/base/extra.yaml").write_text("kind: ClusterRoleBinding\n")
    assert rules(verify(generated))["builder.unlisted-file"]["location"]["file"] == "deploy/base/extra.yaml"


def test_pycache_is_ignored(generated):
    (generated / "_platform/__pycache__").mkdir()
    (generated / "_platform/__pycache__/authz.cpython-312.pyc").write_bytes(b"\x00")
    assert verify(generated) == []


def test_spec_changed_without_regenerate(generated):
    spec = json.loads((generated / "builder-spec.json").read_text())
    spec["env"].append({"name": "EXTRA", "secret": False})
    (generated / "builder-spec.json").write_text(json.dumps(spec))
    found = rules(verify(generated))
    assert found["builder.file-modified"]["severity"] == "high"
    assert found["builder.lock-tampered"]["severity"] == "high"


def test_forged_lock_with_invalid_spec_is_caught(generated):
    spec = json.loads((generated / "builder-spec.json").read_text())
    spec["egress"] = [{"host": "api.openai.com", "port": 443, "reason": "direct model calls"}]
    (generated / "builder-spec.json").write_text(json.dumps(spec))
    f = rules(verify(generated))["builder.spec-invalid"]
    assert f["severity"] == "high"
    assert "api.openai.com" in f["rationale"]


def test_missing_lock(generated):
    (generated / ".builder.lock").unlink()
    assert rules(verify(generated))["builder.lock-missing"]["location"]["kind"] == "absence"


def test_stale_builder_version(generated):
    lock = json.loads((generated / ".builder.lock").read_text())
    lock["builder_version"] = "0.0.1"
    (generated / ".builder.lock").write_text(json.dumps(lock, indent=2))
    assert rules(verify(generated))["builder.version-stale"]["severity"] == "medium"


def test_platform_config_mismatch(generated, tmp_path, monkeypatch):
    platform = load_platform()
    platform["identity_header"] = "X-Other"
    override = tmp_path / "platform.json"
    override.write_text(json.dumps(platform))
    monkeypatch.setenv("BUILDER_PLATFORM_CONFIG", str(override))
    assert rules(verify(generated))["builder.platform-config-mismatch"]["severity"] == "high"


def lock_edit(root, mutate):
    lock = json.loads((root / ".builder.lock").read_text())
    mutate(lock)
    (root / ".builder.lock").write_text(json.dumps(lock, indent=2))


def sha(path):
    return sha256_text(path.read_text())


@pytest.mark.parametrize("name", ["../../etc/passwd", "/etc/passwd", "a/../../x", "bad\nname", "back\\slash", "a//b", "tab\there"])
def test_hostile_lock_entry_is_lock_invalid(generated, name):
    lock_edit(generated, lambda lock: lock["files"].__setitem__(name, "0" * 64))
    findings = verify(generated)
    assert [f["rule_id"] for f in findings] == ["builder.lock-invalid"]
    assert rules(findings)["builder.lock-invalid"]["severity"] == "high"


def test_lock_entry_outside_root_is_never_read(generated):
    (generated.parent / "outside.txt").write_text("TOPSECRET\n")
    lock_edit(generated, lambda lock: lock["files"].__setitem__("../outside.txt", "0" * 64))
    assert "TOPSECRET" not in json.dumps(verify(generated))


@pytest.mark.parametrize("files", [[], "x", {"Dockerfile": 5}, {"": "a"}])
def test_malformed_lock_files_field(generated, files):
    lock_edit(generated, lambda lock: lock.__setitem__("files", files))
    assert "builder.lock-invalid" in rules(verify(generated))


def test_hostile_unlisted_filename_still_valid_findings(generated):
    (generated / "deploy/base/evil\nname.yaml").write_text("x: 1\n")
    (generated / "deploy/base/back\\slash.yaml").write_text("x: 1\n")
    findings = verify(generated)
    assert len([f for f in findings if f["rule_id"] == "builder.unlisted-file"]) == 2
    rules(findings)


def test_edit_with_updated_lock_hash_is_still_high(generated):
    path = generated / "deploy/base/networkpolicy.yaml"
    path.write_text(path.read_text() + "# extra\n")
    lock_edit(generated, lambda lock: lock["files"].__setitem__("deploy/base/networkpolicy.yaml", sha(path)))
    f = rules(verify(generated))["builder.file-modified"]
    assert f["severity"] == "high" and f["location"]["file"] == "deploy/base/networkpolicy.yaml"


def test_deleted_file_and_its_lock_entry_is_caught(generated):
    (generated / "deploy/base/networkpolicy.yaml").unlink()
    lock_edit(generated, lambda lock: lock["files"].pop("deploy/base/networkpolicy.yaml"))
    f = rules(verify(generated))["builder.file-missing"]
    assert f["location"]["file"] == "deploy/base/networkpolicy.yaml"


def test_symlink_to_matching_outside_file_is_modified(generated, tmp_path):
    outside = tmp_path / "copy.yaml"
    target = generated / "deploy/base/networkpolicy.yaml"
    outside.write_text(target.read_text())
    target.unlink()
    target.symlink_to(outside)
    f = rules(verify(generated))["builder.file-modified"]
    assert f["location"]["file"] == "deploy/base/networkpolicy.yaml"


def test_symlinked_generated_dir_outside_is_not_read(generated, tmp_path):
    outside = tmp_path / "other"
    outside.mkdir()
    (outside / "secret.yaml").write_text("password: hunter2\n")
    (generated / "deploy/base/linked").symlink_to(outside)
    assert all("hunter2" not in json.dumps(f) for f in verify(generated))


def test_huge_generated_file_is_modified_without_reading_it_all(generated):
    path = generated / "Dockerfile"
    with path.open("ab") as handle:
        handle.write(b"# " + b"x" * 2_000_000 + b"\n")
    f = rules(verify(generated))["builder.file-modified"]
    assert f["location"]["file"] == "Dockerfile"
    assert len(f["evidence"][0]["snippet"]) < 300


def test_long_line_snippet_is_truncated(generated):
    path = generated / "Dockerfile"
    path.write_text(path.read_text() + "USER root " + "y" * 5000 + "\n")
    f = rules(verify(generated))["builder.file-modified"]
    assert len(f["evidence"][0]["snippet"]) <= 200


def test_binary_and_crlf_edits_are_modified(generated):
    (generated / "Dockerfile").write_bytes((generated / "Dockerfile").read_bytes().replace(b"\n", b"\r\n"))
    (generated / ".dockerignore").write_bytes(b"\x00\x01")
    findings = rules(verify(generated))
    assert findings["builder.file-modified"]["severity"] == "high"
    assert len([f for f in verify(generated) if f["rule_id"] == "builder.file-modified"]) == 2


def test_lock_and_spec_symlinked_outside_are_not_read(generated, tmp_path):
    outside = tmp_path / "spec.json"
    outside.write_text((generated / "builder-spec.json").read_text())
    (generated / "builder-spec.json").unlink()
    (generated / "builder-spec.json").symlink_to(outside)
    assert "builder.spec-invalid" in rules(verify(generated))
    lock_outside = tmp_path / "lock.json"
    lock_outside.write_text((generated / ".builder.lock").read_text())
    (generated / ".builder.lock").unlink()
    (generated / ".builder.lock").symlink_to(lock_outside)
    assert "builder.lock-invalid" in rules(verify(generated))


@pytest.mark.parametrize("content", ["[]", "null", '"s"', "{" * 100000])
def test_non_object_spec_is_spec_invalid(generated, content):
    (generated / "builder-spec.json").write_text(content)
    assert "builder.spec-invalid" in rules(verify(generated))


def test_non_object_lock(generated):
    (generated / ".builder.lock").write_text("[1]")
    assert "builder.lock-invalid" in rules(verify(generated))


def forge(root, **changes):
    lock_edit(root, lambda lock: lock.update(changes))


def tamper(root, rel, old, new):
    path = root / rel
    path.write_text(path.read_text().replace(old, new))
    return path


def pairs(findings):
    return {(f["rule_id"], f["location"]["file"]) for f in findings if f["severity"] == "high"}


def test_forged_version_cannot_hide_edits(generated):
    """Probe A: old builder_version, edited deployment with updated hash, Dockerfile replaced and unlisted."""
    path = tamper(generated, "deploy/base/deployment.yaml", "allowPrivilegeEscalation: false", "allowPrivilegeEscalation: true")
    (generated / "Dockerfile").write_text("FROM evil\nUSER 0\n")
    lock_edit(generated, lambda lock: (lock["files"].__setitem__("deploy/base/deployment.yaml", sha(path)),
                                       lock["files"].pop("Dockerfile"), lock.__setitem__("builder_version", "0.0.1")))
    found = verify(generated)
    rules(found)
    assert {("builder.file-modified", "Dockerfile"), ("builder.file-modified", "deploy/base/deployment.yaml"),
            ("builder.lock-tampered", ".builder.lock")} <= pairs(found)
    assert {f["rule_id"]: f["severity"] for f in found}["builder.version-stale"] == "medium"


def test_forged_spec_sha_cannot_hide_edits(generated):
    """Probe B."""
    path = tamper(generated, "deploy/base/deployment.yaml", "readOnlyRootFilesystem: true", "readOnlyRootFilesystem: false")
    (generated / "Dockerfile").write_text("FROM evil\n")
    lock_edit(generated, lambda lock: (lock["files"].update({"deploy/base/deployment.yaml": sha(path), "Dockerfile": sha(generated / "Dockerfile")}),
                                       lock.__setitem__("spec_sha256", "0" * 64)))
    found = pairs(verify(generated))
    assert {("builder.file-modified", "Dockerfile"), ("builder.file-modified", "deploy/base/deployment.yaml"),
            ("builder.lock-tampered", ".builder.lock")} <= found


def test_shadow_package_in_platform_is_unlisted_even_when_locked(generated):
    """Probe H."""
    (generated / "_platform/authz").mkdir()
    body = "class AuthzASGI:\n    pass\n"
    (generated / "_platform/authz/__init__.py").write_text(body)
    lock_edit(generated, lambda lock: lock["files"].__setitem__("_platform/authz/__init__.py", sha256_text(body)))
    found = pairs(verify(generated))
    assert ("builder.unlisted-file", "_platform/authz/__init__.py") in found
    assert ("builder.lock-tampered", ".builder.lock") in found


def test_lock_entry_removed_for_dockerfile_still_checked(generated):
    (generated / "Dockerfile").write_text("FROM evil\n")
    lock_edit(generated, lambda lock: lock["files"].pop("Dockerfile"))
    assert ("builder.file-modified", "Dockerfile") in pairs(verify(generated))


def test_dockerignore_replaced_with_lock_untouched_is_modified(generated):
    (generated / ".dockerignore").write_text("")
    assert ("builder.file-modified", ".dockerignore") in pairs(verify(generated))


def test_deleted_dockerfile_and_lock_entry(generated):
    (generated / "Dockerfile").unlink()
    lock_edit(generated, lambda lock: lock["files"].pop("Dockerfile"))
    assert ("builder.file-missing", "Dockerfile") in pairs(verify(generated))


def test_forged_spec_sha_alone_is_tampered(generated):
    forge(generated, spec_sha256="0" * 64)
    assert [f["rule_id"] for f in verify(generated)] == ["builder.lock-tampered"]


def test_pycache_only_ignored_under_platform(generated):
    (generated / "deploy/base/__pycache__").mkdir()
    (generated / "deploy/base/__pycache__/x.pyc").write_bytes(b"\x00evil")
    assert ("builder.unlisted-file", "deploy/base/__pycache__/x.pyc") in pairs(verify(generated))


def test_needs_input_is_high(generated):
    (generated / "requirements.txt").unlink()
    found = rules(verify(generated))
    assert found["builder.spec-needs-input"]["severity"] == "high"


def test_invalid_spec_still_hash_checks_locked_files(generated):
    spec = json.loads((generated / "builder-spec.json").read_text())
    spec["egress"] = [{"host": "api.openai.com", "port": 443, "reason": "direct model calls"}]
    (generated / "builder-spec.json").write_text(json.dumps(spec))
    (generated / "Dockerfile").write_text("FROM evil\n")
    found = pairs(verify(generated))
    assert ("builder.spec-invalid", "builder-spec.json") in found and ("builder.file-modified", "Dockerfile") in found


@pytest.mark.parametrize("name", [
    "Dockerfile.dockerignore", "Containerfile", ".containerignore", "Dockerfile.prod", "Dockerfile.dev",
    "docker-compose.yml", "docker-compose.override.yaml", "compose.yaml", "compose.prod.yml", "docker-bake.hcl", "docker-bake.json",
])
def test_build_override_files(generated, name):
    (generated / name).write_text("x\n")
    f = rules(verify(generated))["builder.build-override"]
    assert f["severity"] == "high" and f["location"]["file"] == name


def test_deploy_overlay_is_build_override(generated):
    (generated / "deploy/overlays/prod").mkdir(parents=True)
    (generated / "deploy/overlays/prod/kustomization.yaml").write_text("patches: []\n")
    f = rules(verify(generated))["builder.build-override"]
    assert f["location"]["file"] == "deploy/overlays"


def test_ordinary_files_are_not_overrides(generated):
    (generated / "README.md").write_text("hi\n")
    (generated / "Dockerfile").write_bytes((generated / "Dockerfile").read_bytes())
    assert verify(generated) == []


def test_linked_platform_dir_is_one_finding_and_not_traversed(generated, tmp_path):
    big = tmp_path / "big"
    big.mkdir()
    for i in range(300):
        (big / f"f{i}.py").write_text("x = 1\n")
    import shutil
    shutil.rmtree(generated / "_platform")
    (generated / "_platform").symlink_to(big)
    found = rules(verify(generated))
    unsafe = [f for f in verify(generated) if f["rule_id"] == "builder.generated-dir-unsafe"]
    assert len(unsafe) == 1 and unsafe[0]["severity"] == "high" and unsafe[0]["location"]["file"] == "_platform"
    assert len(verify(generated)) < 20 and "builder.unlisted-file" not in found


def test_deploy_base_linked_inside_root_is_unsafe(generated):
    import shutil
    shutil.rmtree(generated / "deploy/base")
    (generated / "src").mkdir()
    (generated / "deploy/base").symlink_to("../src")
    assert "builder.generated-dir-unsafe" in rules(verify(generated))


def test_unlisted_findings_are_capped(generated):
    for i in range(200):
        (generated / f"deploy/base/extra{i:03}.yaml").write_text("a: 1\n")
    found = [f for f in verify(generated) if f["rule_id"] == "builder.unlisted-file"]
    assert len(found) == 51 and found[-1]["location"]["kind"] == "file"
    for f in found:
        assert validate("finding.v1", f) == []


def test_read_budget_is_one_finding(generated, monkeypatch):
    monkeypatch.setattr("builder.verify.MAX_TREE_BYTES", 1000)
    found = rules(verify(generated))
    assert found["builder.read-budget-exceeded"]["severity"] == "high"


def test_deeply_nested_lock_version_is_lock_invalid(generated):
    text = (generated / ".builder.lock").read_text()
    text = text[:text.rindex("}")] + ', "builder_version": ' + "[" * 900 + "]" * 900 + "}"
    (generated / ".builder.lock").write_text(text)
    assert "builder.lock-invalid" in rules(verify(generated))


def test_deeply_nested_spec_is_a_finding_not_a_crash(generated):
    (generated / "builder-spec.json").write_text("[" * 900 + "]" * 900)
    assert "builder.spec-invalid" in rules(verify(generated))


def test_deeply_nested_object_spec_is_a_finding_not_a_crash(generated):
    (generated / "builder-spec.json").write_text('{"egress": ' + "[" * 900 + "]" * 900 + "}")
    assert "builder.spec-invalid" in rules(verify(generated))


@pytest.mark.parametrize("name", ["dockerfile", "containerfile.prod", "Containerfile.prod", "containerfile"])
def test_more_build_override_names(generated, name):
    (generated / name).write_text("FROM evil\n")
    f = rules(verify(generated))["builder.build-override"]
    assert f["location"]["file"] == name


def test_version_and_platform_both_reported(generated):
    forge(generated, builder_version="0.0.1", platform_config_sha256="0" * 64)
    found = rules(verify(generated))
    assert "builder.version-stale" in found and "builder.platform-config-mismatch" in found
    assert found["builder.platform-config-mismatch"]["severity"] == "high"


@pytest.mark.parametrize("line", ["*.py export-ignore", "_platform/* export-subst", "* filter=evil", "* text eol=crlf"])
def test_gitattributes_export_attributes(generated, line):
    (generated / ".gitattributes").write_text("# comment\n*.md text\n" + line + "\n")
    f = rules(verify(generated))["builder.build-override"]
    assert f["severity"] == "high" and f["location"] == {"kind": "line", "file": ".gitattributes", "start_line": 3, "end_line": 3}
    assert f["evidence"][0]["snippet"] == line


def test_harmless_gitattributes_is_clean(generated):
    (generated / ".gitattributes").write_text("*.png binary\n# eol=lf is fine in comments\n")
    assert verify(generated) == []


def write_spec(root, mutate):
    spec = json.loads((root / "builder-spec.json").read_text())
    mutate(spec)
    (root / "builder-spec.json").write_text(json.dumps(spec))


def ids(findings, rule):
    return sorted(f["id"] for f in findings if f["rule_id"] == rule)


def test_spec_invalid_ids_differ_by_problem(generated):
    write_spec(generated, lambda s: s.update(egress=[{"host": "api.openai.com", "port": 443, "reason": "direct model calls"}]))
    provider = ids(verify(generated), "builder.spec-invalid")
    write_spec(generated, lambda s: s.update(egress=[{"host": "metadata.google.internal", "port": 80, "reason": "cloud metadata"}]))
    metadata = ids(verify(generated), "builder.spec-invalid")
    assert provider and metadata and provider != metadata


def test_spec_needs_input_ids_differ_by_question(generated, tmp_path):
    (generated / "requirements.txt").write_text("fastapi==0.115.0\n")
    no_hashes = ids(verify(generated), "builder.spec-needs-input")
    (generated / "requirements.txt").write_text("fastapi==0.115.0 \\\n    --hash=sha256:" + "a" * 64 + "\n")
    write_spec(generated, lambda s: s.pop("authz"))
    removed = ids(verify(generated), "builder.spec-needs-input")
    assert no_hashes and removed and no_hashes != removed


def test_file_modified_id_changes_with_more_edits(generated):
    path = generated / "deploy/base/deployment.yaml"
    original = path.read_text()
    import re
    first = re.sub(r"memory: (\S+)", "memory: 9Gi", original, count=1)
    assert first != original
    path.write_text(first)
    one = ids(verify(generated), "builder.file-modified")
    path.write_text(first.replace("readOnlyRootFilesystem: true", "readOnlyRootFilesystem: false"))
    two = ids(verify(generated), "builder.file-modified")
    assert one and two and one != two


def test_ids_are_stable_across_runs(generated):
    (generated / "Dockerfile").write_text("FROM evil\n")
    (generated / "deploy/base/extra.yaml").write_text("a: 1\n")
    (generated / "Containerfile").write_text("FROM evil\n")
    (generated / "deploy/base/networkpolicy.yaml").unlink()
    first = verify(generated)
    assert first and first == verify(generated)


@pytest.mark.parametrize("make", ["override", "unlisted", "missing", "tampered", "lock-invalid"])
def test_digest_anchor_is_valid_and_tracks_content(generated, make):
    def build(text):
        if make == "override":
            (generated / "Containerfile").write_text(text)
        elif make == "unlisted":
            (generated / "deploy/base/extra.yaml").write_text(text)
        elif make == "missing":
            (generated / "deploy/base/networkpolicy.yaml").unlink()
        elif make == "tampered":
            forge(generated, spec_sha256=text)
        else:
            (generated / ".builder.lock").write_text(text)
        return rules(verify(generated))

    rule = {"override": "builder.build-override", "unlisted": "builder.unlisted-file", "missing": "builder.file-missing",
            "tampered": "builder.lock-tampered", "lock-invalid": "builder.lock-invalid"}[make]
    first = build("a: 1\n" if make != "tampered" else "1" * 64)[rule]
    anchors = [e for e in first["evidence"] if e.get("note") == "content digest"]
    assert len(anchors) == 1 and anchors[0]["snippet"].startswith("sha256:")
    if make != "missing":
        second = build("b: 2\n" if make != "tampered" else "2" * 64)[rule]
        assert first["id"] != second["id"]


def test_override_directory_content_changes_id(generated):
    (generated / "deploy/overlays").mkdir()
    (generated / "deploy/overlays/k.yaml").write_text("a: 1\n")
    one = ids(verify(generated), "builder.build-override")
    (generated / "deploy/overlays/k.yaml").write_text("a: 2\n")
    assert one != ids(verify(generated), "builder.build-override")


@pytest.mark.parametrize("name", ["gunicorn.conf.py", "uvicorn.py", "gunicorn.py", "sitecustomize.py", "usercustomize.py"])
def test_server_shadowing_files_are_overrides(generated, name):
    (generated / name).write_text("import os\n")
    f = rules(verify(generated))["builder.build-override"]
    assert f["location"]["file"] == name and f["severity"] == "high"


def test_override_findings_are_capped_with_a_summary(generated):
    for i in range(80):
        (generated / f"Dockerfile.v{i:02}").write_text(f"FROM x{i}\n")
    found = [f for f in verify(generated) if f["rule_id"] == "builder.build-override"]
    assert len(found) == 51 and found[-1]["location"]["kind"] == "file"
    for f in found:
        assert validate("finding.v1", f) == []


# ---- the golden-road blocks ----

def test_team_text_outside_the_blocks_is_ignored(generated):
    for name in MANAGED_FILES:
        path = generated / name
        path.write_text("# Ours\n" + path.read_text() + "\nMore of ours.\n")
    assert verify(generated) == []


@pytest.mark.parametrize("name", MANAGED_FILES)
def test_missing_managed_file_is_high(generated, name):
    (generated / name).unlink()
    f = rules(verify(generated))["builder.managed-block-missing"]
    assert f["severity"] == "high" and f["location"] == {"kind": "absence", "file": name}
    assert f["fix"]["steps"][0] == "Run: builder generate ."


def test_file_without_the_block_is_missing(generated):
    (generated / "AGENTS.md").write_text("# only ours\n")
    f = rules(verify(generated))["builder.managed-block-missing"]
    assert f["location"] == {"kind": "file", "file": "AGENTS.md"}


def test_edited_block_is_modified_at_the_edited_line(generated):
    path = generated / "AGENTS.md"
    lines = ("intro\n" + path.read_text()).splitlines()
    index = next(i for i, line in enumerate(lines) if "Never make this repository public" in line)
    lines[index] = "- Making this repository public is fine."
    path.write_text("\n".join(lines) + "\n")
    f = rules(verify(generated))["builder.managed-block-modified"]
    assert f["severity"] == "high"
    assert f["location"] == {"kind": "line", "file": "AGENTS.md", "start_line": index + 1, "end_line": index + 1}
    assert f["evidence"][0]["snippet"] == "- Making this repository public is fine."


def test_duplicated_markers_are_high(generated):
    path = generated / "CLAUDE.md"
    path.write_text(path.read_text() * 2)
    f = rules(verify(generated))["builder.managed-block-duplicated"]
    assert f["severity"] == "high" and f["location"]["start_line"] == 4


def test_unmatched_marker_is_modified(generated):
    (generated / "CLAUDE.md").write_text(f"{BEGIN}\n@AGENTS.md\n")
    assert rules(verify(generated))["builder.managed-block-modified"]["location"]["start_line"] == 1


@pytest.mark.parametrize("name", MANAGED_FILES)
def test_symlinked_managed_file_is_flagged(generated, name):
    real = generated.parent / f"real-{name}"
    real.write_text((generated / name).read_text())
    (generated / name).unlink()
    (generated / name).symlink_to(real)
    assert rules(verify(generated))["builder.managed-block-modified"]["evidence"][0]["snippet"] == "<symbolic link>"


@pytest.mark.parametrize("data, snippet", [(b"\xff\xfe\x00x", "<binary file>"), (b"\xff\xfe x\n", "<not UTF-8 text>")])
def test_binary_or_non_utf8_managed_file_is_flagged(generated, data, snippet):
    (generated / "AGENTS.md").write_bytes(data)
    assert rules(verify(generated))["builder.managed-block-modified"]["evidence"][0]["snippet"] == snippet


def test_forged_block_hashes_are_tampered(generated):
    forge(generated, blocks={"AGENTS.md": "0" * 64, "CLAUDE.md": "0" * 64})
    assert [f["rule_id"] for f in verify(generated)] == ["builder.lock-tampered"]


@pytest.mark.parametrize("blocks", ["x", {"README.md": "0" * 64}, {"AGENTS.md": 5}])
def test_malformed_lock_blocks_is_lock_invalid(generated, blocks):
    forge(generated, blocks=blocks)
    assert [f["rule_id"] for f in verify(generated)] == ["builder.lock-invalid"]


def test_repo_generated_by_an_older_builder(generated):
    """A lock from before the blocks has no blocks table and the repo has no AGENTS.md: regenerate, not lock-invalid.
    (0.1.0, not 0.2.0: this task runs before the bump to 0.3.0, and version-stale needs a version other than VERSION.)"""
    lock_edit(generated, lambda lock: (lock.pop("blocks"), lock.__setitem__("builder_version", "0.1.0")))
    for name in MANAGED_FILES:
        (generated / name).unlink()
    found = rules(verify(generated))
    assert {"builder.lock-tampered", "builder.version-stale", "builder.managed-block-missing"} <= set(found)
    assert "builder.lock-invalid" not in found


def test_invalid_spec_falls_back_to_the_lock_hash(generated):
    spec = json.loads((generated / "builder-spec.json").read_text())
    spec["egress"] = [{"host": "api.openai.com", "port": 443, "reason": "direct model calls"}]
    (generated / "builder-spec.json").write_text(json.dumps(spec))
    path = generated / "AGENTS.md"
    path.write_text(path.read_text().replace("Never make", "Do make"))
    found = rules(verify(generated))
    assert "builder.spec-invalid" in found and "builder.managed-block-modified" in found


@pytest.mark.parametrize("bad, rule", [(f"{BEGIN}\n{END}\n{BEGIN}\n{END}\n", "builder.managed-block-duplicated"),
                                       (f"{BEGIN}\nno end\n", "builder.managed-block-modified")])
def test_marker_finding_id_ignores_team_text_above(generated, bad, rule):
    def finding_id(text):
        (generated / "AGENTS.md").write_text(text)
        return rules(verify(generated))[rule]["id"]

    ids = {finding_id(prefix + bad) for prefix in ("", "a\n", "a\nb\n", "a\x0cb\n", "x\x1cy\x85z\n", "\ufeff", "\ufeffa\n")}
    assert len(ids) == 1


def test_lock_with_only_its_blocks_key_deleted_is_tampered(generated):
    lock_edit(generated, lambda lock: lock.pop("blocks"))
    assert [f["rule_id"] for f in verify(generated)] == ["builder.lock-tampered"]


def test_block_finding_id_tracks_the_block_not_the_team_text(generated):
    path = generated / "AGENTS.md"
    edited = path.read_text().replace("Never make", "Do make")

    def finding_id(text):
        path.write_text(text)
        return rules(verify(generated))["builder.managed-block-modified"]["id"]

    assert finding_id("a\n" + edited) == finding_id("b\n" + edited)
    assert finding_id("a\n" + edited) != finding_id("a\n" + edited.replace("Do make", "Please make"))

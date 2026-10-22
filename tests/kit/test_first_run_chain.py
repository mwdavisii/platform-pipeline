"""The first-run chain, end to end against the fake gh and uv: the skill release as release-skill.yml publishes it, the install
doc run as written from an app folder, the installed stub's load, the builder kit as release-builder.yml publishes it, and the
downloaded bundle's install_builder.py, ending at what golden-road doctor reads.

Names (tags, signing workflows, asset sets, which release is marked latest or a draft) are read from the two workflows, not
written here: if a workflow, the stub or the install script drifts from the others, this chain breaks."""

import fnmatch
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

from builder import VERSION
from golden_road import doctor
from kit_fakes import AGENTS, AGENTS_REPO, HOST, ORG, PIPELINE_REPO, ROOT, run_script
from test_install_docs import commands, follow

sys.path.insert(0, str(ROOT / "tests/pipeline"))
from test_builder_kit import IMAGE, configured_platform, copy_tree, kit as load_builder_kit  # noqa: E402

SKILL_WORKFLOW = AGENTS / ".github/workflows/release-skill.yml"
BUILDER_WORKFLOW = ROOT / ".github/workflows/release-builder.yml"


def parse(path):
    doc = yaml.safe_load(path.read_text())
    return doc, doc.get("on", doc.get(True))


def tag_prefix(on):
    glob = on["push"]["tags"][0]
    assert glob.endswith("*")
    return glob[:-1]


def publish_flags(step):
    """The gh release create flags (before the notes text), and the name the attestation bundle is published under."""
    flags = step["run"].split("--notes")[0]
    assert 'cp "$ATTESTATION" "dist/${GITHUB_REF_NAME}.sigstore.json"' in step["run"]
    return flags, lambda tag: f"{tag}.sigstore.json"


def attested(steps, folder):
    """The files the workflow's attest step covers, from its subject-path globs."""
    step = next(s for s in steps if s.get("uses", "").startswith("actions/attest-build-provenance@"))
    globs = step["with"]["subject-path"].split()
    return [p for p in sorted(folder.iterdir()) if any(fnmatch.fnmatchcase(f"{folder.name}/{p.name}", g) for g in globs)]


def step_running(steps, needle):
    return next(s for s in steps if needle in s.get("run", ""))


def publish_skill_release(world, kit, tmp_path):
    doc, on = parse(SKILL_WORKFLOW)
    steps = doc["jobs"]["release"]["steps"]
    prefix = tag_prefix(on)
    tag = prefix + (kit / "skill/VERSION").read_text().strip()
    build = step_running(steps, "scripts/build_release.py")["run"]
    assert f'"${{GITHUB_REF_NAME#{prefix}}}"' in build  # the version is the tag minus the prefix the trigger names
    dist = tmp_path / "skill-dist"
    dist = dist.parent / re.search(r"\s(\w+)\s*$", build.strip()).group(1)
    out = subprocess.run([sys.executable, "-I", str(kit / "scripts/build_release.py"), tag[len(prefix):], str(dist)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    flags, bundle_name = publish_flags(step_running(steps, "gh release create"))
    files = {p.name: p.read_bytes() for p in sorted(dist.iterdir())}
    world.release(AGENTS_REPO, tag, {**files, bundle_name(tag): b"{}"}, latest=" --latest " in flags + " ")
    signer = f"{HOST}/{AGENTS_REPO}/.github/workflows/{SKILL_WORKFLOW.name}"
    for path in attested(steps, dist):
        world.attest(path.read_bytes(), AGENTS_REPO, signer, tag)
    return tag


def publish_builder_release(world, tmp_path, monkeypatch):
    doc, on = parse(BUILDER_WORKFLOW)
    steps = doc["jobs"]["kit"]["steps"]
    prefix = tag_prefix(on)
    tag = prefix + VERSION
    run = step_running(steps, "scripts/build_builder_kit.py")["run"]
    for flag in ('--tag "$GITHUB_REF_NAME"', '--image "$IMAGE"', "--out dist"):
        assert flag in run
    tree = copy_tree(tmp_path / "platform-pipeline")
    dist = tmp_path / "builder-work" / "dist"
    assert load_builder_kit(tree, monkeypatch).main(["--tag", tag, "--image", IMAGE, "--out", str(dist), "--source-date-epoch", "7"]) == 0
    flags, bundle_name = publish_flags(step_running(steps, "gh release create"))
    assert "--draft" in flags  # the platform team publishes it when it pins the image: modelled by publishing it here
    files = {p.name: p.read_bytes() for p in sorted(dist.iterdir())}
    world.release(AGENTS_REPO, tag, {**files, bundle_name(tag): b"{}"}, draft=False)
    signer = f"{HOST}/{PIPELINE_REPO}/.github/workflows/{BUILDER_WORKFLOW.name}"
    for path in attested(steps, dist):
        world.attest(path.read_bytes(), PIPELINE_REPO, signer, tag, needs_bundle=True)
    return dist


def test_the_first_run_chain_ends_with_the_pin_where_doctor_reads_it(kit, world, tmp_path, monkeypatch):
    skill_tag = publish_skill_release(world, kit, tmp_path)
    dist = publish_builder_release(world, tmp_path, monkeypatch)
    platform = tmp_path / "platform.json"
    platform.write_bytes(configured_platform())
    world.set(uv={"version": VERSION, "platform_sha": hashlib.sha256(platform.read_bytes()).hexdigest()})

    app = tmp_path / "app"
    app.mkdir()
    assert follow(kit, app, "claude-code") == skill_tag  # INSTALL_FOR_AGENTS.md, as written

    stub = world.home / ".claude/skills/golden-road"
    [line] = commands(stub / "SKILL.md")
    loaded = subprocess.run(line.replace("<skill-dir>", str(stub)), shell=True, cwd=app, capture_output=True, text=True, executable="/bin/sh")
    assert loaded.returncode == 0, loaded.stderr
    bundle = Path(json.loads(loaded.stdout)["bundle_dir"])

    code, doc = run_script(bundle / "install_builder.py", cwd=app)  # step 1 of the bundle's skill
    assert code == 0, doc
    assert doc["builder_version"] == VERSION and doc["org"] == ORG

    pin = doctor.default_pin()
    assert pin == world.home / ".cache/golden-road/platform-pin.json" and pin.read_bytes() == (dist / "platform-pin.json").read_bytes()
    monkeypatch.setenv("BUILDER_PLATFORM_CONFIG", str(platform))  # the platform config the installed builder carries
    result = doctor.check_builder_pin(doctor.Context(root=app, pin=pin, host=HOST, org=ORG))
    # the version and the platform config both match the pin; only the app's .builder.lock (the next step writes it) is missing
    assert result["message"] == ".builder.lock is missing or can't be read", result

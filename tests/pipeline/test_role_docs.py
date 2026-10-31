"""docs/roles/: one SOP per role, all reachable from docs/roles/README.md, all in the same shape, and every path in this repo they name exists.
This file is exported with the docs, so it holds in the public export too (which leaves out reference/ and the export script)."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ROLES = ROOT / "docs/roles"
INDEX = ROLES / "README.md"
SOPS = sorted(p for p in ROLES.glob("*.md") if p.name != "README.md")

# The sections every SOP has, in this order (docs/roles/README.md describes the format).
SECTIONS = ["## Purpose", "## Who", "## What you own", "## Before you start", "## Today (mock) and in production", "## Procedures",
            "## What you must never do", "## Troubleshooting", "## Gaps"]

# A repo-relative path: one of this repo's top-level directories, then a path. Not part of a longer path or word.
TOP = ("bootstrap", "deploy-kits", "docs", "images", "platform", "reference", "scripts", "src", "tests")
PATH = re.compile(r"(?<![\w./<-])((?:" + "|".join(re.escape(t) for t in TOP) + r")/[^\s`'\"()|,]*)")
LINK = re.compile(r"\]\(([^)\s]+)\)")
# Paths the public export leaves out (scripts/export-public-poc.sh). reference/security-scans is published as a repo of its own.
NOT_EXPORTED = ("reference/", "scripts/export-public-poc.sh")


def repo_paths(text):
    for raw in PATH.findall(text):
        path = raw.rstrip(".:;")
        if any(c in path for c in "<>*{}$"):
            continue  # a placeholder or a glob, not a file
        yield path


def test_there_are_sops():
    assert INDEX.is_file()
    assert len(SOPS) >= 8, [p.name for p in SOPS]


@pytest.mark.parametrize("sop", SOPS, ids=lambda p: p.name)
def test_every_sop_is_linked_from_the_index(sop):
    targets = {t.split("#")[0] for t in LINK.findall(INDEX.read_text())}
    assert sop.name in targets, f"docs/roles/README.md does not link {sop.name}"


@pytest.mark.parametrize("sop", SOPS, ids=lambda p: p.name)
def test_every_sop_has_the_standard_sections_in_order(sop):
    lines = sop.read_text().splitlines()
    assert lines[0].startswith("# SOP: "), sop.name
    found = [line for line in lines if line in SECTIONS]
    assert found == SECTIONS, f"{sop.name}: sections are {found}"
    procedures = sop.read_text().split("## Procedures", 1)[1].split("## What you must never do", 1)[0]
    steps = re.findall(r"^### \d+\. .+$", procedures, re.M)
    assert steps, f"{sop.name} has no numbered procedures"
    for block in re.split(r"^### \d+\. .+$", procedures, flags=re.M)[1:]:
        assert "**Goal:**" in block and "**Check:**" in block, f"{sop.name}: a procedure lacks its Goal or Check line"


@pytest.mark.parametrize("doc", [INDEX, *SOPS], ids=lambda p: p.name)
def test_every_repo_path_named_exists(doc):
    missing = []
    for path in repo_paths(doc.read_text()):
        if (ROOT / ".golden-road-export").exists() and path.startswith(NOT_EXPORTED):
            continue  # the public export leaves these out (scripts/export-public-poc.sh); in the source they must exist
        if not (ROOT / path.rstrip("/")).exists():
            missing.append(path)
    assert not missing, f"{doc.name} names paths that don't exist: {missing}"


@pytest.mark.parametrize("doc", [INDEX, *SOPS], ids=lambda p: p.name)
def test_every_relative_link_resolves(doc):
    missing = []
    for target in LINK.findall(doc.read_text()):
        if re.match(r"[a-z]+:", target) or target.startswith("#"):
            continue
        if not (doc.parent / target.split("#")[0]).resolve().exists():
            missing.append(target)
    assert not missing, f"{doc.name} links to files that don't exist: {missing}"


def test_the_top_level_readme_links_the_roles():
    assert "docs/roles/README.md" in (ROOT / "README.md").read_text()


def test_the_path_pattern_finds_paths_and_skips_placeholders():
    text = "see `bootstrap/hello-app/builder-spec.json`, `apps/<owner>/<repo>`, `bootstrap/<app>/x`, -v \"$PWD:/src\" and <src>/.venv"
    assert list(repo_paths(text)) == ["bootstrap/hello-app/builder-spec.json"]


def test_the_closed_gaps_are_gone_and_the_new_procedures_are_there():
    """Spec 2026-10-31 Docs: changes and the Re-review page are procedures now, not gaps."""
    admin, dev, security, index = (" ".join((ROLES / n).read_text().split()) for n in ("portal-admin.md", "app-developer.md", "security-team.md", "README.md"))
    for text in (admin, dev, security, index):
        assert "can't re-register" not in text and "No re-registration" not in text and "No re-review" not in text
    assert "### 7. Approve a change or renewal of a registration" in (ROLES / "portal-admin.md").read_text()
    assert "### 8. Work the Re-review list" in (ROLES / "portal-admin.md").read_text()
    assert "### 11. Change or renew your app's registration" in (ROLES / "app-developer.md").read_text()
    assert "### 16. Tune the re-review signals" in (ROLES / "security-team.md").read_text()
    assert "PORTAL_REREVIEW_SIGNALS" in security and "Change or renew the registration" in dev and "/admin/rereview" in admin
    for needle in ("record_changed", "record_conflict", "record_case_mismatch", "not_a_file", "recheck: scorecard_changed"):
        assert needle in admin and needle in dev, needle
    assert "app-developer procedure 11" in admin
    assert "untrusted_run" in admin and "per-app mode" in security and "tag ruleset" in security


"""Spec 2026-11-02 Docs: the lifecycle rules for readers, the enforcement rows, architecture §10."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def flat(path):
    return " ".join((ROOT / path).read_text().split())


def test_lifecycle_md_carries_the_rules():
    text = flat("docs/lifecycle.md")
    for needle in ("## Who", "## Suspend", "## Resume", "## Decommission", "## What ServiceNow sees", "## Retry and abandon",
                   "## Bringing an app back after decommission", "## Gaps",
                   "only a portal admin", "SCTASK followed by 7 digits", "10 to 1000 characters", "one lifecycle request open per app",
                   "nothing to suspend", "replicas: 0", "lifecycle.yaml", "nothing is deleted", "within L5", "platform-reviewers",
                   "the deploy repo PR is merged, then the repository is archived, then the factory PR", "cloudflare-access.yaml",
                   "images in ghcr", "Delinea secret values", "Azure identity and Databricks principal", "decommissioned",
                   "Closed Complete", "u_golden_road_state", "operational_status", "install_status", "ServiceNow follows the portal",
                   "applied in git", "the cluster's reconcile", "Retry", "Abandon", "the task stays open", "unarchived by a GitHub admin",
                   "a reviewed factory PR", "a new release", "deploy-check refuses", "an environment first pinned while the app is suspended",
                   "cmdb_writeback_pending", "the lifecycle App",
                   # changed in build (plan 2026-11-02 items 21, 22 and the reviews)
                   "not_in_org", "adapter_not_ours", "case_mismatch", "At least once", "neutralised", "a reviewer's approval is lost",
                   "even if a person closed the task", "bot login at startup", "Administration: write is broader than archive",
                   "the web reads `sc_task` too"):
        assert needle in text, needle


def test_lifecycle_md_is_linked_and_exported():
    assert "docs/lifecycle.md" in (ROOT / "docs/architecture.md").read_text()
    assert "docs/lifecycle.md" in (ROOT / "docs/enforcement.md").read_text()


def test_enforcement_has_the_four_rows():
    text = flat("docs/enforcement.md")
    for needle in ("Only the platform suspends or decommissions an app", "A prod suspend merges without review only within L5",
                   "A decommissioned app can't be deployed again", "The lifecycle App's Administration: write (accepted risk)",
                   "deploy-check", "archived", "writer/lifecycle.py", "lifecycle_path_allowed"):
        assert needle in text, needle


def test_architecture_section_10_says_lifecycle_is_built():
    text = (ROOT / "docs/architecture.md").read_text()
    section = text[text.index("## 10. "):text.index("## 11. ")]
    assert "lifecycle and re-review [planned]" not in text
    assert "[built" in section and "docs/lifecycle.md" in section and "mock ServiceNow" in section
    assert "Remediation skill" in section and "[planned]" in section.split("**Re-review and lifecycle:**")[0]

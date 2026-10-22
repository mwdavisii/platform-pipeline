"""bootstrap/golden-road-agents: the landing page and its paste block, the code owners and the rulesets."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENTS = ROOT / "bootstrap/golden-road-agents"
ORG_RE = r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*"  # ORG_PLACEHOLDER, or the org configure.py wrote
HOST_RE = r"[a-z0-9.-]+\.[a-z]+"  # github.com, or the host configure.py wrote


def paste_block():
    blocks = re.findall(r"## Paste block\n.*?```text\n(.*?)```", (AGENTS / "README.md").read_text(), re.S)
    assert len(blocks) == 1
    return blocks[0]


def test_the_paste_block_carries_the_guardrails():
    flat = " ".join(paste_block().split())
    for needle in ("Package this app for the golden road.", "INSTALL_FOR_AGENTS.md", "If you can't read that repo, stop",
                   "Ask me every question the builder returns", "never answer data classification, roles, or egress yourself",
                   "wait for my yes", "Never make a repo public", "`golden-road doctor` exits 0"):
        assert needle in flat, needle
    assert re.search(rf'gh api --hostname {HOST_RE} -H "Accept: application/vnd\.github\.raw" repos/{ORG_RE}/golden-road-agents/contents/INSTALL_FOR_AGENTS\.md', flat)


def test_the_readme_is_the_landing_page_and_names_both_release_series():
    text = " ".join((AGENTS / "README.md").read_text().split())
    for needle in ("copies this block word for word", "Install `uv`", "Claude Code", "OpenAI Codex", "Cursor",
                   "`skill-vX.Y.Z`", "`builder-vX.Y.Z`", "--draft=false --latest=false", "never by GitHub's latest flag"):
        assert needle in text, needle
    assert re.search(rf"`gh auth login --hostname {HOST_RE}`", text)


def test_the_readme_tells_windows_users_about_line_endings():
    text = " ".join((AGENTS / "README.md").read_text().split())
    for needle in ("git config --global core.autocrlf false", "git clone -c core.autocrlf=false", "`* -text`", "`eol=`",
                   "git config core.eol lf", "`text=auto`"):
        assert needle in text, needle


def test_codeowners_cover_everything():
    lines = [l for l in (AGENTS / ".github/CODEOWNERS").read_text().splitlines() if l.strip() and not l.startswith("#")]
    assert len(lines) == 1
    owner = rf"@{ORG_RE}(?:/[A-Za-z0-9_.-]+)?"
    if "ORG_PLACEHOLDER" in lines[0]:
        assert lines[0] == "* @ORG_PLACEHOLDER/platform-reviewers"
    else:  # configured: the platform team, or on a personal account the owner's logins (personal-poc.md step 14.1)
        assert re.fullmatch(rf"\* {owner}(?: {owner})*", lines[0]), lines[0]


def single_approver_mode():
    return json.loads((ROOT / "platform/platform.json").read_text())["gate2_single_approver"]


def test_main_is_protected():
    ruleset = json.loads((AGENTS / ".github/rulesets/main.json").read_text())
    assert ruleset["target"] == "branch" and ruleset["enforcement"] == "active" and "bypass_actors" not in ruleset
    assert ruleset["conditions"]["ref_name"] == {"include": ["~DEFAULT_BRANCH"], "exclude": []}
    rules = {r["type"]: r.get("parameters") for r in ruleset["rules"]}
    assert set(rules) == {"deletion", "non_fast_forward", "pull_request"}
    pr = rules["pull_request"]
    want = (0, False, False) if single_approver_mode() else (1, True, True)
    assert (pr["required_approving_review_count"], pr["require_code_owner_review"], pr["require_last_push_approval"]) == want


def test_only_admins_create_move_or_delete_skill_tags():
    doc = json.loads((AGENTS / ".github/rulesets/skill-tags.json").read_text())
    assert doc["target"] == "tag" and doc["enforcement"] == "active"
    assert doc["conditions"]["ref_name"] == {"include": ["refs/tags/skill-v*"], "exclude": []}
    assert {r["type"] for r in doc["rules"]} == {"creation", "update", "deletion"}
    assert [(b["actor_type"], b["actor_id"], b["bypass_mode"]) for b in doc["bypass_actors"]] == [("RepositoryRole", 5, "always")]


def test_the_skill_version_is_x_y_z():
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+\n", (AGENTS / "skill/VERSION").read_text())

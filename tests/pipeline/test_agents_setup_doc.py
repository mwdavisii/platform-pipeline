"""docs/setup/personal-poc.md step 14: the agent kit's setup. Its commands that can run offline run as written on scratch copies."""

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import doc_bash_blocks, run_block_with_fake_gh, run_doc_block
from test_scorecard_factory import assert_pinned_everywhere, pin_everywhere

ROOT = Path(__file__).resolve().parents[2]
POC = ROOT / "docs/setup/personal-poc.md"
AGENTS = ROOT / "bootstrap/golden-road-agents"
SKILL_VERSION = (AGENTS / "skill/VERSION").read_text().strip()


def step_14():
    text = POC.read_text()
    return text[text.index("## 14. "):text.index("## PoC gaps to record")]


def step_13():
    text = POC.read_text()
    return text[text.index("## 13. Risk scorecard"):text.index("## 14. ")]


def sub(heading):
    """The text of one ### 14.x section."""
    step = step_14()
    start = step.index(f"### {heading}")
    end = step.find("\n### ", start + 1)
    return step[start:end if end != -1 else len(step)]


def block_with(text, needle):
    [block] = [b for b in doc_bash_blocks(text) if needle in b]
    return block


def test_step_14_has_its_parts_in_order_and_every_part_with_commands_ends_in_a_check():
    step = step_14()
    order = ["### 14.0", "### 14.1", "### 14.2", "### 14.3", "### 14.4", "### 14.5", "### 14.6", "### 14.7", "### 14.8", "### 14.9", "### 14.10",
             "### Kit operations"]
    assert [step.index(h) for h in order] == sorted(step.index(h) for h in order)
    for heading in order[1:-1]:
        part = sub(heading[4:])
        if "```bash" in part:
            assert "**Check" in part, heading
    assert "steps 1 to 13.4" in step


def test_step_14_creates_the_kit_repo_empty_public_and_checked_and_nothing_else_public():
    step = step_14()
    assert "GOLDEN_ROAD_PUBLIC_POC=<you> scripts/push-bootstrap.sh <you> golden-road-agents --public" in step
    assert [l for l in step.splitlines() if "--public" in l and "push-bootstrap.sh" not in l and not l.lstrip().startswith(("#", "-", "**"))] == []
    assert "gh repo view <you>/golden-road-agents --json visibility --jq .visibility" in step and "`PUBLIC`" in sub("14.2")
    assert "--private" in sub("14.9") and "fixture-" in sub("14.9")
    assert not re.search(r"--source(?!-ref)", step) and "--push" not in step.replace("git push", "")
    for name in ("main.json", "skill-tags.json"):
        assert f"gh api -X POST repos/<you>/golden-road-agents/rulesets --input bootstrap/golden-road-agents/.github/rulesets/{name}" in step
    assert "skill-release-tags" in sub("14.2") and '"actor_id": 5' in sub("14.2")


def test_step_14_has_no_heredoc_and_no_read_prompt():
    step = step_14()
    assert not re.search(r"<<-?\s*['\"]?\w+", step), "pasting into a terminal can indent a heredoc's end marker"
    assert "read -p" not in step and "read -rs -p" not in step


def test_the_versions_and_asset_names_are_the_ones_the_tree_releases():
    assert "version=$(sed -n 's/^VERSION = \"\\(.*\\)\"$/\\1/p' src/builder/__init__.py); export version" in sub("14.0")
    assert "git tag builder-v$version && git push origin builder-v$version" in sub("14.5")
    assert "git ls-remote --tags origin builder-v$version" in sub("14.5")
    assert f"git tag skill-v{SKILL_VERSION} && git push origin skill-v{SKILL_VERSION}" in sub("14.3")
    for name in ("golden_road-<version>-py3-none-any.whl", "golden_road-<version>-requirements.txt", "platform-pin.json", "builder-v<version>.sigstore.json"):
        assert name in sub("14.5"), name
    for name in ("fetch_skill.py", f"golden-road-skill-{SKILL_VERSION}.tar.gz", "golden-road-stub.md", f"skill-v{SKILL_VERSION}.sigstore.json"):
        assert name in sub("14.3"), name
    # the workflows publish exactly these names
    workflow = (ROOT / ".github/workflows/release-builder.yml").read_text()
    assert 'dist/${GITHUB_REF_NAME}.sigstore.json' in workflow and "dist/platform-pin.json" in workflow


# ---- the two starting points: step 13 released the source's version (case A) or an older 0.2.x (case B) ----

def test_14_0_tells_the_two_starting_points_apart_and_every_part_that_differs_has_both_paths():
    zero = sub("14.0")
    assert "git ls-remote --tags origin 'builder-v*'" in zero
    assert "Case A" in zero and "Case B" in zero and "builder-v<version>" in zero
    assert "gh run list --repo <you>/platform-pipeline --workflow release-builder.yml" in sub("14.5")
    for heading in ("14.1", "14.5", "14.6", "14.7"):
        part = sub(heading)
        assert "Case A" in part and "Case B" in part, heading


def test_case_a_re_runs_only_the_kit_job_that_failed_for_lack_of_the_app_and_no_draft_exists_then():
    five = " ".join(sub("14.5").split())
    assert "gh run rerun <run id> --failed --repo <you>/platform-pipeline" in five
    assert "failed at the token step" in five and "no draft was created" in five
    assert "Re-run failed jobs" in five
    assert five.index("Case A") < five.index("gh run rerun <run id> --failed") < five.index("Case B")


def test_case_a_skips_what_step_13_already_did_and_case_b_does_it():
    one, six, seven = (" ".join(sub(h).split()) for h in ("14.1", "14.6", "14.7"))
    assert "13.2 already brought in" in one and "skip the export" in one
    assert "already pinned" in six and "already regenerated" in six and "13.6" in six
    assert "13.4 already created" in seven and "intake-request.yml" in seven


def test_step_13_releases_the_source_version_and_names_no_fixed_one():
    step = step_13()
    assert "0.2.2" not in step and "0.2.0" not in step
    assert "sed -i 's/VERSION" not in step
    assert "git tag builder-v$version && git push origin builder-v$version" in step
    assert "git ls-remote --tags origin builder-v$version" in step
    assert 'Pin builder $version' in step and "builder $version" in step
    assert "`kit` job fails" in " ".join(step.split()) and "step 14" in step


def test_step_13_reads_the_version_from_the_source_as_written(tmp_path):
    line = next(l for l in step_13().splitlines() if l.startswith("version=$("))
    out = subprocess.run(["bash", "-euo", "pipefail", "-c", f'{line}\nprintf "%s" "$version"'], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout == re.search(r'^VERSION = "([^"]+)"$', (ROOT / "src/builder/__init__.py").read_text(), re.M).group(1)


def test_step_13_regenerates_every_file_the_new_builder_changes_in_hello_app():
    six = step_13()[step_13().index("### 13.6"):step_13().index("### 13.7")]
    for name in ("AGENTS.md", "CLAUDE.md", "Dockerfile", ".builder.lock", "README.md"):
        assert f'"$app/{name}"' in six, name


def test_the_configure_command_is_13_2_s():
    text = POC.read_text()
    thirteen = text[text.index("### 13.2"):text.index("### 13.3")]
    pick = lambda part: re.search(r"<src>/\.venv/bin/python scripts/configure\.py[^\n]*\\\n[^\n]*\\\n[^\n]*", part).group(0)
    assert pick(sub("14.1")) == pick(thirteen)
    for carried in ("platform/required-checks.json", "bootstrap/hello-app/.github/CODEOWNERS", "bootstrap/scorecard-factory/.github/CODEOWNERS",
                    "bootstrap/scorecard-factory/config/scorecard.yaml"):
        assert f"cp ~/poc/platform-pipeline/{carried}" in sub("14.1"), carried
    assert "tests/pipeline/test_scorecard_factory.py" in sub("14.1")


def test_the_code_owners_line_runs_as_written_and_names_only_people(tmp_path):
    block = block_with(sub("14.1"), "> bootstrap/golden-road-agents/.github/CODEOWNERS")
    shutil.copytree(AGENTS, tmp_path / "bootstrap/golden-road-agents", ignore=shutil.ignore_patterns("__pycache__"))
    out = run_doc_block(block, tmp_path, {"<you>": "acme", "<deputy>": "other"})
    assert out.returncode == 0, out.stderr
    lines = [l for l in (tmp_path / "bootstrap/golden-road-agents/.github/CODEOWNERS").read_text().splitlines() if l.strip() and not l.startswith("#")]
    assert lines == ["* @acme @other"]
    assert "* @<you>'" in block, "the single-approver line is there too"


def test_the_app_key_goes_from_its_file_to_the_secret_and_nowhere_else(tmp_path):
    block = block_with(sub("14.4"), "GOLDEN_ROAD_AGENTS_APP_KEY")
    pem = tmp_path / "Downloads/acme-kit.2026-10-22.private-key.pem"
    pem.parent.mkdir()
    key = "-----BEGIN RSA PRIVATE KEY-----\nAAAAtestkey\n-----END RSA PRIVATE KEY-----\n"
    pem.write_text(key)
    out, calls = run_block_with_fake_gh(block, tmp_path, {"~/Downloads/<downloaded key file>.pem": str(pem), "<you>": "acme", "<client id>": "Iv23liEXAMPLE"}, tmp_path)
    assert out.returncode == 0, out.stderr
    assert [c["argv"] for c in calls] == [["variable", "set", "GOLDEN_ROAD_AGENTS_APP_CLIENT_ID", "--repo", "acme/platform-pipeline", "--body", "Iv23liEXAMPLE"],
                                          ["secret", "set", "GOLDEN_ROAD_AGENTS_APP_KEY", "--repo", "acme/platform-pipeline"]]
    assert calls[1]["stdin"] == key and all("AAAAtestkey" not in " ".join(c["argv"]) for c in calls)
    assert "AAAAtestkey" not in out.stdout + out.stderr and not pem.exists()
    workflow = (ROOT / ".github/workflows/release-builder.yml").read_text()
    assert "vars.GOLDEN_ROAD_AGENTS_APP_CLIENT_ID" in workflow and "secrets.GOLDEN_ROAD_AGENTS_APP_KEY" in workflow
    part = sub("14.4")
    for needle in ("Contents", "Read and write", "Only on this account", "Only select repositories", "golden-road-agents", "Webhook"):
        assert needle in part, needle


def test_the_pin_command_pins_every_workflow(tmp_path):
    part = sub("14.6")
    ref = "ghcr.io/acme/golden-road-builder@sha256:" + "ef" * 32
    assert_pinned_everywhere(pin_everywhere(tmp_path, part, {"<you>": "acme", "<digest>": "ef" * 32}), ref, part)


def test_the_draft_is_published_with_latest_false_right_after_the_pin_and_before_hello_app_regenerates():
    part = sub("14.6")
    publish = "gh release edit builder-v$version --repo <you>/golden-road-agents --draft=false --latest=false"
    assert part.count(publish) == 1
    assert part.index("git push origin main") < part.index(publish) < part.index("hello-app-$version")
    assert "--latest=false" in part and "no assets match" in part
    regen = block_with(part, "Regenerate for builder")
    for name in ("AGENTS.md", "CLAUDE.md", "Dockerfile", ".builder.lock", "README.md"):
        assert f'"$app/{name}"' in regen, name
    assert "every PR on `hello-app` fails" in part.replace("\n", " ")


def test_the_intake_label_is_the_one_the_form_applies():
    import yaml
    from golden_road import doctor
    part = sub("14.7")
    assert f"gh label create {doctor.INTAKE_LABEL} --repo <you>/scorecard-factory" in part
    form = yaml.safe_load((ROOT / "bootstrap/scorecard-factory/.github/ISSUE_TEMPLATE/intake-request.yml").read_text())
    assert form["labels"] == [doctor.INTAKE_LABEL]
    assert ".github/ISSUE_TEMPLATE/intake-request.yml" in part and "rsync -a --exclude .git" in part


def test_the_doctor_command_is_where_the_skill_installs_the_builder():
    spec = importlib.util.spec_from_file_location("install_builder_for_doc", AGENTS / "skill/install_builder.py")
    module = importlib.util.module_from_spec(spec)
    before, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # a .pyc in the kit would hold the placeholder and stop configure.py
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = before
    home = Path.home()
    assert module.venv_dir("9.9.9") == home / ".local/share/golden-road/builder-9.9.9"
    assert "~/.local/share/golden-road/builder-$version/bin/golden-road doctor ." in sub("14.9")


def test_the_first_runs_record_what_5c_will_check():
    part = sub("14.9")
    for needle in ("every `user` question", "showed every outward command", "--private", "read the visibility before the first push",
                   "exit=0", "`ruleset`", "transcript", "Intake request: <you>/fixture-", "SCORECARD_GATEWAY_KEY"):
        assert needle in part, needle
    assert "Claude Code" in step_14() and "OpenAI Codex" in step_14() and "Cursor" in step_14()


def test_the_proof_items_and_the_kit_operations_cover_the_execution_notes():
    proof, ops = sub("14.10"), sub("Kit operations")
    for needle in ("--exclude-drafts", "offline", "label", "personal skills folder", "Windows"):
        assert needle in proof, needle
    for needle in ("--cleanup-tag", "refuse a lower", "Publish a higher version", "highest.json", "stale draft", "--latest",
                   "--landing-page-url", "every app regenerates", "cannot be unset", "delete the `approved_models_url` key"):
        assert needle in ops, needle


def test_the_windows_note_matches_the_builder_s_rule():
    part = sub("14.0")
    for needle in ("git config --global core.autocrlf false", "`eol=`", "`* -text`", "git config core.eol lf", "`text=auto`"):
        assert needle in part, needle


def test_the_earlier_steps_point_at_step_14_and_the_gaps_name_the_kit():
    text = POC.read_text()
    three = text[text.index("## 3. Release the builder image"):text.index("## 4. ")]
    assert "builder-v<version>" in three and "13.2 releases" in three and "step 14 finishes" in three
    gaps = text[text.index("## PoC gaps to record"):text.index("## If something fails")]
    for needle in ("golden-road-agents", "App", "GitHub Free", "Windows", "trust root"):
        assert needle in gaps, needle
    assert "It creates five **public** repos" in text[:text.index("## 0.")] and "`<you>/golden-road-agents`" in text[:text.index("## 0.")]


# ---- review fixes: gh's field lists, the 30-day re-run limit, the link-change operation, offline commands run as written ----

# The fields `gh <command> --json` accepts, from `gh <command> --json` (gh 2.x) captured once. A list the doc gives must be a subset.
GH_JSON_FIELDS = {
    "release view": {"apiUrl", "assets", "author", "body", "createdAt", "databaseId", "id", "isDraft", "isImmutable", "isPrerelease", "name",
                     "publishedAt", "tagName", "tarballUrl", "targetCommitish", "uploadUrl", "url", "zipballUrl"},
    "release list": {"createdAt", "isDraft", "isLatest", "isPrerelease", "name", "publishedAt", "tagName"},
    "run list": {"attempt", "conclusion", "createdAt", "databaseId", "displayTitle", "event", "headBranch", "headSha", "name", "number",
                 "startedAt", "status", "updatedAt", "url", "workflowDatabaseId", "workflowName"},
    "run view": {"attempt", "conclusion", "createdAt", "databaseId", "displayTitle", "event", "headBranch", "headSha", "jobs", "name", "number",
                 "startedAt", "status", "updatedAt", "url", "workflowDatabaseId", "workflowName"},
    "repo view": {"nameWithOwner", "owner", "name", "url", "visibility", "isPrivate", "defaultBranchRef", "description"},
    "issue list": {"assignees", "author", "body", "closed", "createdAt", "id", "labels", "number", "state", "title", "updatedAt", "url"},
    "pr checks": {"bucket", "completedAt", "description", "event", "link", "name", "startedAt", "state", "workflow"},
    "label list": {"color", "createdAt", "description", "id", "isDefault", "name", "updatedAt", "url"},
}


def json_field_lists(text):
    return [(m.group(1), m.group(2).split(",")) for m in re.finditer(r"gh (\w+ \w+)[^\n`]*? --json ([A-Za-z,]+)", text)]


def test_every_json_field_list_in_step_14_is_one_gh_accepts():
    found = json_field_lists(step_14())
    assert len(found) >= 10
    for command, fields in found:
        assert command in GH_JSON_FIELDS, f"add `gh {command} --json` to the table"
        assert set(fields) <= GH_JSON_FIELDS[command], (command, set(fields) - GH_JSON_FIELDS[command])
    assert not any(command == "release view" and "isLatest" in fields for command, fields in found)


def test_the_latest_release_is_read_with_release_list():
    check = sub("14.3")
    assert "gh release list --repo <you>/golden-road-agents --exclude-drafts --json tagName,isLatest --jq '.[] | [.tagName, .isLatest] | @tsv'" in check


def test_the_30_day_limit_on_re_running_a_job_is_in_13_2_and_14_0_and_the_preferred_path_avoids_it():
    thirteen = " ".join(step_13()[step_13().index("### 13.2"):step_13().index("### 13.3")].split())
    zero = " ".join(sub("14.0").split())
    for text in (thirteen, zero):
        assert "30 days" in text
    assert "before you push the builder tag" in thirteen.lower() and "14.2 to 14.4" in thirteen and "code owners" in thirteen
    assert thirteen.index("14.2 to 14.4") < thirteen.index("git tag builder-v$version")
    assert "gh run view <run id> --json createdAt,jobs --jq" in zero and '.name=="kit"' in sub("14.0")
    assert "a failed `kit` job" in zero
    five = " ".join(sub("14.5").split())
    assert "older than 30 days" in five and "scripts/bump-builder-version.py <new version>" in five and "take case B" in five
    assert "App variable/secret read when the re-run job starts" in sub("14.10")


def test_a_link_change_reruns_the_published_trees_configure_command_and_lists_what_follows():
    ops = " ".join(sub("Kit operations").split())
    link = ops[ops.index("**Changing a link"):]
    assert "13.2 / 14.1 `configure.py` command" in link and "plus the new flag" in link and "in `~/poc/platform-pipeline`" in link
    for follows in ("scripts/bump-builder-version.py <new version>", "builder release", "pin", "publish the draft", "hello-app` PR as in 14.6"):
        assert follows in link, follows
    assert "checkout -- bootstrap platform" not in link and "step 1" not in link


def test_the_fixture_loop_runs_as_written_and_makes_one_folder_per_agent(tmp_path):
    block = block_with(sub("14.8"), "for agent in")
    shutil.copytree(ROOT / "tests/builder/fixtures/python-fastapi", tmp_path / "poc/platform-pipeline/tests/builder/fixtures/python-fastapi")
    out = run_doc_block(block, tmp_path, {"~/poc": str(tmp_path / "poc")})
    assert out.returncode == 0, out.stderr
    for agent in ("claude-code", "codex", "cursor"):
        folder = tmp_path / "poc" / f"fixture-{agent}"
        assert sorted(p.name for p in folder.iterdir() if p.name != ".git") == ["app.py", "requirements.in", "requirements.txt"], agent
        assert (folder / ".git").is_dir()


def test_the_paste_command_prints_only_the_text_in_the_readme_s_fence():
    command = next(l for l in sub("14.9").splitlines() if l.startswith("gh api --hostname"))
    pipeline = command.split(" | ", 1)[1]
    readme = (AGENTS / "README.md").read_text()
    out = subprocess.run(["bash", "-c", pipeline], input=readme, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    expected = re.search(r"## Paste block\n.*?```text\n(.*?)```", readme, re.S).group(1)
    assert out.stdout == expected and "```" not in out.stdout and "This README" not in out.stdout


def test_the_skill_verify_line_equals_the_installers_apart_from_the_file_org_and_tag():
    if (json.loads((ROOT / "platform/platform.json").read_text()).get("github_host") or "github.com") != "github.com":
        pytest.skip("the docs show github.com's issuer; a tenant-configured tree writes its own into the installer")
    org = json.loads((ROOT / "platform/platform.json").read_text()).get("org") or "ORG_PLACEHOLDER"
    install = (AGENTS / "INSTALL_FOR_AGENTS.md").read_text().replace(org, "ORG")
    theirs = next(l for l in install.splitlines() if l.startswith("gh attestation verify"))
    block = sub("14.3").split("gh attestation verify", 1)[1].split("```")[0]
    mine = "gh attestation verify " + " ".join(l.strip().rstrip("\\").strip() for l in block.splitlines())
    mine = mine.replace("<you>", "ORG").replace("skill-v0.1.0", "<tag>").replace("~/poc/skill-check/fetch_skill.py", ".golden-road-install/fetch_skill.py")
    assert mine.split() == theirs.split()
    assert "--cert-oidc-issuer" in mine


def test_the_exact_commands_the_runs_and_the_fixes_depend_on():
    assert "gh repo create <you>/fixture-<agent> --private" in sub("14.9")
    for heading in ("14.0", "14.5"):
        assert 'select(.headBranch == "builder-v" + env.version)' in sub(heading), heading
    six = sub("14.6")
    assert "gh release edit skill-v<newest> --repo <you>/golden-road-agents --latest" in six
    assert "gh release edit builder-v$version --repo <you>/golden-road-agents --draft=false --latest=false" in six


def test_the_new_platform_keys_the_checks_allow_are_exactly_the_ones_configure_writes_by_default():
    from helpers import CONFIGURE_KEYS
    written = set(CONFIGURE_KEYS) - {"org", "approved_models_url"}  # org was already there; the models link has no default
    thirteen = step_13()
    for text in (thirteen[thirteen.index("### 13.2"):thirteen.index("### 13.3")], sub("14.1")):
        sentence = re.search(r"diff only adds ([^.]*)\.", " ".join(text.split())).group(1)
        assert set(re.findall(r"`(\w+)`", sentence)) == written


def test_each_agent_run_starts_from_a_clean_install_with_scripted_answers_and_the_carry_overs_are_complete():
    nine = " ".join(sub("14.9").split())
    for needle in ("mv ~/.cache/golden-road ~/.cache/golden-road.before-<agent>", "mv ~/.local/share/golden-road ~/.local/share/golden-road.before-<agent>",
                   "role `reader` with access `read`", "role `writer` with access `write`", "`POST /api/notes`", "To restore"):
        assert needle in nine, needle
    assert 'if test -f "$pem"; then' in sub("14.4")
    thirteen = step_13()[step_13().index("### 13.2"):step_13().index("### 13.3")]
    for text in (sub("14.1"), thirteen):
        assert "cp ~/poc/platform-pipeline/bootstrap/golden-road-agents/.github/CODEOWNERS" in text
    assert "tests/kit" in thirteen[thirteen.index("**Check:** `grep -n 'gate1_checks:'"):]
    assert "checkout -- config/scorecard.yaml" in sub("14.7")
    five = " ".join(sub("14.5").split())
    assert "gh run list" in five[five.index("stale draft"):]
    six = sub("14.6")
    assert "In case A, first set `ref` to the digest you pinned in 13.2" in " ".join(six.split())
    assert "All five repos" in POC.read_text()[POC.read_text().index("## PoC gaps to record"):]


# ---- version-agnostic docs, the bump script, and not repeating what 13.2 already did ----

def test_steps_13_and_14_name_no_builder_version_and_never_tell_you_to_sed_the_version():
    for name, text in (("13", step_13()), ("14", step_14())):
        assert set(re.findall(r"(?<![\d.])\d+\.\d+\.\d+(?![\d.])", text)) <= {"0.1.0"}, name  # 0.1.0 is the skill's; builder versions are read from the source
        assert not re.search(r"sed -i [^\n]*VERSION", text), name


def test_every_poc_version_bump_uses_the_script_and_checks_the_diff():
    for name, part in (("14.5", sub("14.5")), ("Kit operations", sub("Kit operations"))):
        assert "scripts/bump-builder-version.py <new version>" in part, name
        assert "git diff --stat" in part and "src/builder/__init__.py" in part and ".builder.lock" in part and "Dockerfile" in part, name
        assert not re.search(r"sed -i [^\n]*VERSION", part), name


def test_step_14_does_not_repeat_what_13_2_already_did_for_the_kit_job():
    zero = " ".join(sub("14.0").split())
    assert "if the `kit` job succeeded, go straight to 14.6" in zero
    one = sub("14.1")
    assert "Skip if you did this in 13.2" in one[:one.index("> bootstrap/golden-road-agents/.github/CODEOWNERS")][-600:]
    for heading in ("14.2", "14.3", "14.4"):
        assert "Skip if you did this in 13.2" in sub(heading), heading


# ---- final review: bump commands run with the venv's python, steps 3 and 11 read or bump the version, notes on the fallbacks ----

NOTE = "(The recorded run used 0.2.0 here and 0.2.1 in step 11.)"


def poc_steps_3_and_11():
    text = POC.read_text()
    return text[text.index("## 3. Release the builder image"):text.index("## 4. ")], text[text.index("## 11. "):text.index("## 12. ")]


def test_steps_3_and_11_read_the_version_or_bump_it_and_keep_one_note_on_the_recorded_run():
    three, eleven = poc_steps_3_and_11()
    assert three.count(NOTE) + eleven.count(NOTE) == 1
    for name, part in (("3", three), ("11", eleven)):
        assert not re.search(r"(?<![\d.])\d+\.\d+\.\d+(?![\d.])", part.replace(NOTE, "")), name
        assert not re.search(r"sed -i [^\n]*VERSION", part), name
        assert "git tag builder-v$version && git push origin builder-v$version" in part, name
        assert "version=$(sed -n 's/^VERSION = \"\\(.*\\)\"$/\\1/p' src/builder/__init__.py)" in part, name
    assert "<src>/.venv/bin/python scripts/bump-builder-version.py <new version>" in eleven and "git diff --stat" in eleven
    assert "builder-$version" in eleven and "builder $version" in eleven
    assert "src/builder/__init__.py" in three


def test_the_poc_doc_runs_the_bump_script_with_the_venv_python_never_bare():
    commands = [l.strip() for l in POC.read_text().splitlines() if "bump-builder-version.py" in l and l.strip().startswith(("scripts/", "python", "<src>", ".venv"))]
    assert commands and all(c.startswith("<src>/.venv/bin/python scripts/bump-builder-version.py") for c in commands), commands


def test_a_link_change_bumps_in_the_source_too_and_keeps_the_flag_in_later_configure_commands():
    ops = " ".join(sub("Kit operations").split())
    link = ops[ops.index("**Changing a link"):]
    assert "`<src>`" in link and "every later `configure.py` command" in link


def test_the_case_a_fallback_says_hello_app_already_has_the_new_files():
    five = " ".join(sub("14.5").split())
    fallback = five[five.index("If the run is older than 30 days"):]
    assert "13.6" in fallback and "`AGENTS.md` and `CLAUDE.md`" in fallback and "not new" in fallback


def test_step_11_bumps_in_the_source_too_so_a_later_export_keeps_the_version():
    _, eleven = poc_steps_3_and_11()
    flat = " ".join(eleven.split())
    after = flat[flat.index("Expect exactly:"):]
    assert "make the same bump in `<src>` and commit it there" in after
    assert "cd <src> <src>/.venv/bin/python scripts/bump-builder-version.py <new version> git add -A && git commit -m \"Builder <new version>\"" in after
    assert "a later export" in after and "13.2" in after


def test_13_2_says_what_to_do_when_the_tag_is_already_in_use():
    thirteen = step_13()
    part = " ".join(thirteen[thirteen.index("### 13.2"):thirteen.index("### 13.3")].split())
    after = part[part.index("git ls-remote --tags origin builder-v$version"):]
    assert "If the tag is already in use" in after and "bump in `<src>` with the script" in after and "export again" in after
    assert "<src>/.venv/bin/python scripts/bump-builder-version.py <new version>" in after

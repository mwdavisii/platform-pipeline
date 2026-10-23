"""Central review's docs: the factory README and personal-poc section 16 (ghe 10.9 is checked in test_internal_docs.py, which is never
exported). They carry the setup steps in the order that works (the App first, then the environment, the key, the variable, the mode,
the settings, the migration) and never print the App's key."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "bootstrap/scorecard-factory/README.md"
POC = ROOT / "docs/setup/personal-poc.md"


def flat(text):
    return " ".join(text.split())


def between(text, start, end):
    a = text.index(start)
    return text[a:text.index(end, a + len(start))]


def readme_section():
    return between(README.read_text(), "## Central review", "## Known limits")


def poc_section():
    return between(POC.read_text(), "## 16. Central AI review", "## PoC gaps to record")


ORDER = ("Commit statuses: Read-only", "environments/scorecard", "deployment-branch-policies -f name=main", "gh secret set SCORECARD_APP_KEY --env scorecard",
         "gh variable set SCORECARD_APP_ID --repo <you>/scorecard-factory", "gh secret set SCORECARD_GATEWAY_KEY --env scorecard",
         "--scorecard-mode central --scorecard-app-id", "central:", "gh workflow run scorecard-poll.yml", "scorecard: central", "gh api -X PUT repos/<you>/hello-app/rulesets/",
         "git rm .github/workflows/scorecard.yml", "gh secret delete SCORECARD_GATEWAY_KEY --repo <you>/hello-app", "--app dependabot")


def test_the_personal_poc_doc_sets_up_central_review_in_order():
    text = POC.read_text()
    assert text.count("## 16. Central AI review") == 1
    section = poc_section()
    position = 0
    for needle in ORDER:
        found = section.find(needle, position)
        assert found >= 0, f"{needle!r} is missing or out of order"
        position = found
    assert "central:" not in section[:section.index("--scorecard-mode central --scorecard-app-id")], "the config section is added after configure"


def test_the_poc_doc_never_prints_the_app_key():
    section = poc_section()
    assert 'gh secret set SCORECARD_APP_KEY --env scorecard --repo <you>/scorecard-factory < "$pem" && rm -- "$pem"' in section
    for banned in ('cat "$pem"', "cat $pem", 'echo "$pem"', "base64", "--body \"$(", "xclip", "pbcopy", "-----BEGIN"):
        assert banned not in section, banned
    assert "Don't open, copy or upload it anywhere else" in flat(section)


def test_the_poc_doc_has_the_proof_items_the_plan_lists():
    text = flat(poc_section())
    for needle in ("16.0", "16.1", "16.2", "16.3", "16.4", "16.5", "16.6", "16.7", "16.8", "App ID", "Only select repositories", "not installed on scorecard-factory",
                   "Only on this account", "Active", "select(.name==\"scorecard / assessed\") | .app.slug", "not allowed to deploy", "fork", "cancels", "4 dispatch",
                   "gh secret list --repo <you>/hello-app", "public repo", "scheduled runs can be delayed"):
        assert needle in text, needle


def test_the_docs_name_the_app_s_permissions_exactly():
    for name, text in (("readme", flat(readme_section())), ("poc", flat(poc_section()))):  # the GHE doc's check is in test_internal_docs.py
        for permission in ("Checks: Read and write", "Contents: Read-only", "Issues: Read and write", "Pull requests: Read and write",
                           "Commit statuses: Read-only", "Metadata: Read-only"):
            assert permission in text, (name, permission)
        assert "not installed on scorecard-factory" in text, name
        assert "Administration" not in text and "Workflows: Read" not in text and "Actions: Read" not in text, name


def test_the_docs_do_not_promise_a_poll_interval_setting():
    for text in (readme_section(), poc_section()):
        assert not re.search(r"poll_minutes\s*:", text), "poll_minutes is not a setting; the schedule is the cron"


def test_the_poc_doc_marks_an_app_central_before_it_pins_the_ruleset_and_does_not_claim_more_than_it_does():
    section = poc_section()
    five = section[section.index("### 16.5"):section.index("### 16.6")]
    assert five.index("`scorecard: central`") < five.index("rulesets/<id>") < five.index("git rm .github/workflows/scorecard.yml")
    zero = flat(section[section.index("### 16.0"):section.index("### 16.1")])
    for needle in ("polls only the repos whose intake record", "marked central", "doctor", "remediate", "Migrate an app (16.5)", "without the mark"):
        assert needle in zero, needle
    assert "`started_at`" in section and "disabled_inactivity" in section
    assert "(a new top-level key, valid in `intake/v1` and `intake/v2`)" in flat(five)


def test_the_branch_policy_proof_removes_the_job_if_so_it_tests_the_environment():
    item = next(line for line in poc_section().splitlines() if line.startswith("7. A factory branch"))
    assert "job-level `if`" in item and "removed" in item and "not allowed to deploy" in item and "environment's branch policy" in item
    assert "(the branch policy)." not in item, "the old wording tested the if, not the policy"


def test_the_proof_items_cover_the_comment_the_retry_and_the_started_at():
    text = flat(poc_section())
    for needle in ("The second run edits the same comment", "performed_via_github_app", "<slug>[bot]", "A merge commit that is not ready is retried",
                   "`queued`, `retry`, `queued`", "`started_at` set"):
        assert needle in text, needle


def test_the_readme_and_the_poc_doc_document_the_sixty_day_shutoff_and_how_to_find_and_fix_it():
    for text in (flat(readme_section()), flat(poc_section())):
        for needle in ("60 days", "disabled_inactivity", "gh workflow enable scorecard-poll.yml", "scorecard-poll.yml --jq .state"):
            assert needle in text, needle


def test_the_readme_documents_the_per_repo_mark_the_retry_kind_and_planned_collector_work():
    text = flat(readme_section())
    for needle in ("`scorecard: central`", "intake/v1", "intake/v2", "per-app", "without the field", "retry", "ten minutes", "three tries", "is planned work",
                   "in flight, never as stale"):
        assert needle in text, needle
    assert "reads the artifacts through that `details_url` in central mode" not in text

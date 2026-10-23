# Golden road personal public PoC: setup and proof run

> **PUBLIC, PERSONAL, PROOF OF CONCEPT ONLY.** This is the one approved exception to "every repo is private or internal". It creates five **public** repos (`platform-pipeline`, `security-scans`, `hello-app`, `scorecard-factory` and `golden-road-agents`) on a **personal** GitHub account so that free-tier rulesets and environment required reviewers are available. Repos in any employer's or organization's GitHub Enterprise stay private or internal and are set up with their own document, not this one. **Nothing from an employer or organization goes into these repos**: no org names, hostnames, tickets, or internal documents. Everything you publish here is world-readable, including the values you pass to `configure.py` (gateway host, edge namespace and labels, identity header name) because they are baked into the public builder image and manifests.

You run these steps; each step ends with a **Check**. Replace `<you>` with your personal GitHub login and `<deputy>` with the second account for Gate 2 (if you have one; see the deployment note below). The three repos are `<you>/platform-pipeline` (the platform), `<you>/security-scans` (the stand-in for the security team's scans) and `<you>/hello-app` (the sample app), `<you>/scorecard-factory` (the risk scorecard's trusted inputs, step 13) and `<you>/golden-road-agents` (the agent kit, step 14).

## 0. Before you start
- Tools: `gh`, `git`, `jq`, `docker`, `cosign` (v3), `rekor-cli`, `kubectl`, `uv`, and a Python 3.12+ virtualenv with `jsonschema`, `PyYAML`, and `pytest` (`configure.py` needs the first two; the optional test runs in steps 1 and 4 need the third).
- `gh auth status` shows your **personal** account.
  **Check:** `gh api users/<you> --jq .type` prints exactly `User`. If it prints `Organization`, stop: this document is not for organizations, and `push-bootstrap.sh --public` refuses them.
- Pick the Gate 2 deputy (`<deputy>`): a second GitHub account you control or trust. Prevent self-review stops you approving your own promotions, and `promotion-record` independently rejects an approval by the person who triggered the run. So with the default configuration the deputy is required: **without one, Gate 2 cannot complete, by design.** If you have none, either use single-approver mode (next bullet) or stop before step 12.5 and record Gate 2 as an unproven gap.
- **No second account? Single-approver mode.** Pass `--single-approver` to `configure.py` in step 1 and skip every deputy-specific step; each one is marked "if you have a deputy" or "Single-approver mode: skip" below.
  > **Deployment note (PoC only).** `--single-approver` exists so one person can exercise the whole pipeline. It sets `gate2_single_approver: true` in `platform/platform.json`, sets the `main` ruleset to **0 approvals** (no required review, no code-owner review, no last-push approval; a PR and the status checks are still required) and turns `prevent_self_review` off on `prod`. You merge your own PRs and approve your own promotions. The promotion record and the prod release notes then say so: `self_approved: true`, "Single-approver PoC mode: approved by the person who triggered it. Not valid for production use." This proves the mechanics, not separation of duties: nothing here is a second pair of eyes, and the verification contract in `docs/enforcement.md` tells deployers to reject such a record. Never use it on a real org or for anything that deploys to a real environment. The choice is recorded in the tree: running `configure.py` again with a different setting is refused until you restore it with `git checkout -- bootstrap platform` (in the exported tree).
- Know the three deploy values for your cluster: the edge namespace, the edge pod label(s), and the identity header the edge sets after stripping client copies. For the homelab they are namespace `cloudflared`, label `app=cloudflared`, header `X-Platform-Roles`, plus your model gateway's hostname (`<gateway host>` below). Remember that all four become public.
- Pin the host for every `gh` command in this document: `export GH_HOST=github.com`.
- Work from a clean checkout of the platform repo (`<src>` below). You will **not** publish from it: its history is never published.

## 1. Export a sanitized tree and configure it
The export copies the tracked files without history, leaves out the internal docs, scans what remains for organization-specific text, and makes a single commit with a neutral identity. It never pushes.
```bash
cd <src>
git status --porcelain          # must print nothing: the export is made from HEAD and refuses a dirty tree
scripts/export-public-poc.sh ~/poc/platform-pipeline
scripts/export-public-poc.sh --tree security-scans ~/poc/security-scans   # reference/security-scans as a repo of its own
cd ~/poc/platform-pipeline
```
The export also refuses if `platform/platform.json` in `<src>` is already configured (export before you configure), and it refuses, listing file and line, if any text on its private deny-list remains. The platform export leaves out `reference/`: once published, the `security-scans` repo is the only copy that counts. **Check:** both runs exit 0, and in each exported tree `git rev-list --count HEAD` prints `1` and `git remote` prints nothing. Optionally run the scan tree's own tests: `(cd ~/poc/security-scans && <src>/.venv/bin/pytest -q)`.

Configure **in the exported tree**, not in the working repo. `configure.py` needs `jsonschema` and `PyYAML`, so run it with the interpreter of a virtualenv that has them (for example the working repo's `.venv`):
```bash
<src>/.venv/bin/python scripts/configure.py --org <you> --gateway-host <gateway host> \
  --edge-namespace cloudflared --edge-label app=cloudflared --identity-header X-Platform-Roles
# Optional, PoC only, for an owner with no second GitHub account: add --single-approver to the command above (see the deployment note in step 0).
# If you have a deputy: code owners for .github/ (workflows, rulesets, environment, dependabot) and for the files scanners read to ignore findings: you and the deputy, so either can review the other's change.
printf '%s\n' '/.github/ @<you> @<deputy>' '/.semgrepignore @<you> @<deputy>' '/.trivyignore @<you> @<deputy>' '/trivy.yaml @<you> @<deputy>' > bootstrap/hello-app/.github/CODEOWNERS
# Single-approver mode: use this instead (only you; the ruleset requires no code-owner review in this mode).
# printf '%s\n' '/.github/ @<you>' '/.semgrepignore @<you>' '/.trivyignore @<you>' '/trivy.yaml @<you>' > bootstrap/hello-app/.github/CODEOWNERS
git config user.name "<your GitHub name>"
git config user.email "<id>+<you>@users.noreply.github.com"
git add -A && git commit -m "Configure for <you>"
```
`configure.py` also fills in the risk scorecard's factory (step 13). Its `--scorecard-gateway-url` defaults to `https://<gateway host>/v1/chat/completions`; step 13 sets a public route instead.

**Check:** `grep -rn 'replace-me\|ORG_PLACEHOLDER' platform bootstrap` prints nothing, and `git rev-list --count HEAD` prints `2`. The `security-scans` tree needs no configuring: it has no organization name in it. (The noreply address keeps your private email out of the public history.)

## 2. Create `platform-pipeline` (public) and protect it
Create the repo empty, verify it is public and a personal repo, and only then push.
```bash
test "$(gh api user --jq .login)" = <you>
test "$(gh api users/<you> --jq .type)" = User
gh repo create <you>/platform-pipeline --public
gh repo view <you>/platform-pipeline --json visibility --jq .visibility
```
**Check:** the last command prints `PUBLIC`. If it prints anything else, delete the repo (`gh repo delete <you>/platform-pipeline`) and do not push.

```bash
url=$(gh repo view <you>/platform-pipeline --json url --jq .url)
git remote add origin "$url"
git push -u origin main
```
**Check:** `gh api repos/<you>/platform-pipeline/actions/permissions --jq .enabled` prints `true`. There is no organization "Actions access" step here: a public repo's reusable workflows can be called by any repo.

`platform-pipeline` is the trust root: whoever can change `golden-road.yml` on its `main` decides what gets signed. Keep write access to this repo to yourself.

**Because this repo is public, anyone on github.com can call its reusable workflows from their own repo and mint valid platform-identity signatures and gate/promotion attestations for THEIR OWN repos.** A signature that matches only the workflow identity therefore proves nothing about *your* app. Pinning `--certificate-github-workflow-repository` to the exact app repo, plus the ref and the trigger, and checking the attested `repo`, `image` and `manifests_sha256`, is mandatory for every verification in this document and for any deploy owner (see "Deploy-owner verification contract" in `docs/enforcement.md`).

**Fork PRs on `platform-pipeline`.** Its `test.yml` runs on pull requests, so apply the fork policy here too: Settings -> Actions -> General -> Fork pull request workflows from outside collaborators -> **Require approval for all outside collaborators**. The API equivalent (unconfirmed; the UI is authoritative):
```bash
gh api -X PUT repos/<you>/platform-pipeline/actions/permissions/fork-pr-contributor-approval -f approval_policy=all_external_contributors
gh api repos/<you>/platform-pipeline/actions/permissions/fork-pr-contributor-approval --jq .approval_policy
```
**Check:** it reads `all_external_contributors`.

**Protect the builder release tags.** Without this, anyone with write access could release the builder image from an older commit.
```bash
gh api -X POST repos/<you>/platform-pipeline/rulesets --input .github/rulesets/builder-tags.json
id=$(gh api repos/<you>/platform-pipeline/rulesets --jq '.[] | select(.name=="builder-release-tags") | .id')
gh api repos/<you>/platform-pipeline/rulesets/$id --jq '.bypass_actors'
```
**Check:** the list includes `builder-release-tags`, and the bypass list is one entry with `"actor_id": 5` and `"actor_type": "RepositoryRole"` (5 is the built-in Admin role; on a personal repo you, the owner, are the admin). Verify this field: the API may reject a role bypass on a personal repo, which is unconfirmed. If it does, do step 3 first, then apply the ruleset with `.bypass_actors = []`; from then on nobody can create a new `builder-v*` tag until you set the ruleset to `disabled` for the moment you tag.

## 3. Release the builder image
The tag follows `builder.VERSION`, which the block below reads from `src/builder/__init__.py` (the tag is `builder-v<version>`). Step 13.2 releases whatever version the source has then, and step 14 finishes that release (the agent kit). (The recorded run used 0.2.0 here and 0.2.1 in step 11.) Configure (step 1) came first because `.builder.lock` records the hash of the configured `platform/platform.json`, and the builder image bakes in whatever `platform.json` exists when it is built.
```bash
version=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' src/builder/__init__.py)
git tag builder-v$version && git push origin builder-v$version
gh run watch --repo <you>/platform-pipeline
```
Copy the digest from the run summary (`ghcr.io/<you>/golden-road-builder@sha256:...`). Put it in `BUILDER_IMAGE` in **both** `.github/workflows/golden-road.yml` and `.github/workflows/golden-road-promote.yml`, then commit and push (`main` of `platform-pipeline` has no ruleset, so a direct push works).

**Check 1:** `grep -c '^  BUILDER_IMAGE: .*@sha256:0000000000000000' .github/workflows/golden-road*.yml` prints `0` for both files, and `grep -h '^  BUILDER_IMAGE:' .github/workflows/golden-road*.yml` prints your real digest twice. (The guard lines in those workflows contain the all-zero placeholder on purpose, so don't count every match.)

**Make the builder image public.** New container packages start private. Fork PR runs and `hello-app` must pull it, and a fork PR's token cannot read a private package. Open `https://github.com/users/<you>/packages/container/golden-road-builder/settings` -> Danger Zone -> Change visibility -> Public. (There is no API for changing visibility.) The image holds only the builder and the platform config you passed to `configure.py`.

**Check 2:** `gh api /users/<you>/packages/container/golden-road-builder --jq .visibility` prints `public`.

## 4. Publish `security-scans` (public)
`security-scans` stands in for the security team's scanning service: a reusable workflow that runs Semgrep CE and Trivy and posts `security-scans / semgrep` and `security-scans / trivy`. **The platform never runs scanners**; `hello-app` calls this repo. It must exist before `hello-app` is pushed (step 5), because `hello-app`'s first push runs the caller workflow. Whoever can change `scan.yml` on its `main` decides what the scans check, so keep write access to yourself (in a real organization this repo belongs to the security team, not to the platform or the app).
```bash
cd ~/poc/security-scans
git config user.name "<your GitHub name>"
git config user.email "<id>+<you>@users.noreply.github.com"
gh repo create <you>/security-scans --public
gh repo view <you>/security-scans --json visibility --jq .visibility
```
**Check:** the last command prints `PUBLIC`. If not, delete the repo (`gh repo delete <you>/security-scans`) and do not push.
```bash
url=$(gh repo view <you>/security-scans --json url --jq .url)
git remote add origin "$url"
git push -u origin main
gh api -X PUT repos/<you>/security-scans/actions/permissions/fork-pr-contributor-approval -f approval_policy=all_external_contributors
```
**Check:** `gh api repos/<you>/security-scans/contents/.github/workflows/scan.yml --jq .path` prints `.github/workflows/scan.yml`, and `gh api repos/<you>/security-scans/actions/permissions --jq .enabled` prints `true`. A public repo's reusable workflows can be called by any repo, so nothing more is needed for `hello-app` to call it.

Protect its `main` (no deletion, no force push, changes by pull request; no approval count, because you are the only collaborator: raise it to 1 if you add a second one). From then on, change `scan.yml` by PR.
```bash
gh api -X POST repos/<you>/security-scans/rulesets --input .github/rulesets/main.json
gh api repos/<you>/security-scans/rulesets --jq '.[] | {name, enforcement}'
```
**Check:** the second command prints `main` with `"enforcement": "active"`.

## 5. Create `hello-app` (public)
**Dry run first (from `<src>`).** If the platform-pinned base image of the generated Dockerfile has a fixed HIGH or CRITICAL finding, `security-scans / trivy` fails for every app built on it and the app team cannot fix it: the platform owns the fix (bump the builder's base image, regenerate, re-release). This opt-in test runs the pinned Trivy, with the workflow's own settings, on `bootstrap/hello-app` (host network only for the database download; set `GOLDEN_ROAD_DOCKER_BUILD_NETWORK=host` if the default Docker network does not work):
```bash
cd <src> && GOLDEN_ROAD_SCANS_TRIVY=1 .venv/bin/pytest -q -m docker tests/pipeline/test_security_scans_trivy_docker.py
```
**Check:** the last line reads `1 passed`. `1 skipped` means Docker or the variable was missing, so the dry run did not happen. If it fails, stop and record the finding as a PoC gap (see below) before you continue.

```bash
GOLDEN_ROAD_PUBLIC_POC=<you> scripts/push-bootstrap.sh <you> hello-app --public
```
The script refuses unless that variable equals the owner's login, that login is the authenticated `gh` user, and the owner is a personal account (type `User`); it pins `GH_HOST=github.com`; it creates the repo empty, checks that it is `PUBLIC`, and only then pushes. **Check:** `gh repo view <you>/hello-app --json visibility --jq .visibility` prints `PUBLIC`.

The push to `main` starts `ci.yml` and `security-scans.yml`. Because the builder image is public, `ci.yml` should pass and publish the first signed test release, and the `security-scans` run should pass as well. (Gate 1 does not wait on the scans yet: that comes in step 11.) `hello-app` also carries the `scorecard` workflow, which calls `<you>/scorecard-factory`: until step 13 creates that repo, its runs on pull requests fail, and nothing requires them.

**Check:** `gh run list --repo <you>/hello-app --limit 2 --json workflowName,conclusion --jq '.[]'` shows `success` for both `ci` and `security-scans` once the runs finish. If `security-scans` reports that the workflow was not found, step 4 is missing or the repo is private. If it failed at the image pull, re-check step 3 (builder visibility), then `gh run rerun --failed --repo <you>/hello-app`.

## 6. Fork PR safety settings
In `<you>/hello-app` -> Settings -> Actions -> General -> **Fork pull request workflows from outside collaborators** -> choose **Require approval for all outside collaborators**. The API equivalent (unconfirmed; the UI is authoritative):
```bash
gh api -X PUT repos/<you>/hello-app/actions/permissions/fork-pr-contributor-approval -f approval_policy=all_external_contributors
gh api repos/<you>/hello-app/actions/permissions/fork-pr-contributor-approval --jq .approval_policy
```
**Check:** the setting reads `all_external_contributors` (or the UI shows the "all outside collaborators" option selected). Fork PRs on a public repo get a read-only `GITHUB_TOKEN` and no secrets; step 12.8 confirms it from a real run. Do not enable anything that sends write tokens or secrets to fork PRs.

## 7. Give the deputy access
If you have a deputy. Single-approver mode: skip this step.

```bash
gh api -X PUT repos/<you>/hello-app/collaborators/<deputy> -f permission=push
```
`<deputy>` accepts the invitation (the email link, or `gh api user/repository_invitations` and `gh api -X PATCH user/repository_invitations/<id>` as the deputy).

**Check:** `gh api repos/<you>/hello-app/collaborators/<deputy>/permission --jq .permission` prints `write`.

## 8. Gate 2 environment
The checked-in `environment-prod.json` has reviewer id `0` as a placeholder. If you have a deputy, this replaces it with two reviewers, **you and the deputy**. You are a reviewer on purpose: in step 12.5 you trigger the run and try to approve it, and prevent self-review must refuse you. Single-approver mode: the only reviewer is you (use the `jq` line marked single-approver mode below, and skip the `dep` lookup); `prevent_self_review` is already `false` in the file `configure.py` rewrote, so in step 12.5 your own approval is accepted. It then adds the `main` deployment-branch policy (the environment uses custom branch policies, so without it no branch can deploy to `prod`).
```bash
me=$(gh api users/<you> --jq .id)
dep=$(gh api users/<deputy> --jq .id)
jq --argjson me "$me" --argjson dep "$dep" '.reviewers = [{"type": "User", "id": $me}, {"type": "User", "id": $dep}]' bootstrap/hello-app/.github/environment-prod.json \
  | gh api -X PUT repos/<you>/hello-app/environments/prod --input -
# Single-approver mode: instead of the two lines above (only your id):
# jq --argjson me "$me" '.reviewers = [{"type": "User", "id": $me}]' bootstrap/hello-app/.github/environment-prod.json \
#   | gh api -X PUT repos/<you>/hello-app/environments/prod --input -
gh api -X POST repos/<you>/hello-app/environments/prod/deployment-branch-policies -f name=main -f type=branch
```
**Check:**
```bash
gh api repos/<you>/hello-app/environments/prod --jq '.protection_rules[] | select(.type=="required_reviewers") | {prevent_self_review, reviewers: [.reviewers[].reviewer.login]}'
gh api repos/<you>/hello-app/environments/prod/deployment-branch-policies --jq '.branch_policies[].name'
```
The first prints `"prevent_self_review": true` with both `<you>` and `<deputy>`; the second prints `main`. Single-approver mode: the first prints `"prevent_self_review": false` with only `<you>`.

## 9. Ruleset on `main`
`golden-road / drift`, `golden-road / preview`, `security-scans / semgrep`, and `security-scans / trivy` are pinned to the GitHub Actions app by `integration_id: 15368` (the id on github.com). Confirm it, then apply:
```bash
gh api /apps/github-actions --jq .id
jq '(.rules[] | select(.type == "required_status_checks") | .parameters.required_status_checks) |= map(select(.context != "scorecard / assessed"))' bootstrap/hello-app/.github/rulesets/main.json \
  | gh api -X POST repos/<you>/hello-app/rulesets --input -
```
`main.json` also requires `scorecard / assessed`, the risk scorecard's check; the `jq` filter leaves it out until step 13.6 creates the scorecard, or every PR would wait for a check that never comes. `main.json` requires 1 approving review, approval of the last push, and **code-owner review**, with no bypass. So **the deputy approves every PR on `hello-app`**, including your own, and a PR that touches `.github/` also needs a code owner (`.github/CODEOWNERS`, set in step 1). Single-approver mode: `configure.py` set the ruleset to 0 approvals and no code-owner review, so you merge your own PRs once the checks pass; every "deputy approves" instruction below becomes "no approval needed".

**Known limit.** The `golden-road / drift`, `golden-road / preview` and `security-scans / *` checks are pinned only to the GitHub Actions app, and personal accounts cannot require workflows. A PR can therefore edit `ci.yml` to replace the drift or preview job, or `security-scans.yml` to replace the scans, with a different job of the same name (see "Scan results can be forged" below). Releases stay safe, because the release job re-runs the platform's own drift check, but the merge check rests on review: **the deputy must reject any PR that touches `.github/` unless that change is intended.**

**Check:**
```bash
id=$(gh api repos/<you>/hello-app/rulesets --jq '.[] | select(.name=="main") | .id')
gh api repos/<you>/hello-app/rulesets/$id --jq '.rules[] | select(.type=="required_status_checks") | .parameters.required_status_checks'
```
It prints `golden-road / drift`, `golden-road / preview`, `security-scans / semgrep` and `security-scans / trivy`, each with `"integration_id": 15368`.

## 10. Enable CodeQL and Dependabot on `hello-app`
**CodeQL uses GitHub's default setup, not a workflow file.** Default setup is free on a public repo, is managed by GitHub (no action version to pin and keep current), and creates no workflow in the repo that a PR could edit or delete, so it adds nothing to the workflow-policy surface. A workflow-file setup would give you control over queries and the trigger, at the cost of a pinned CodeQL action to maintain; use that if you need either. Languages for this app are `python` and `actions` (the workflows).
```bash
jq -n '{state: "configured", query_suite: "default", languages: ["python", "actions"]}' \
  | gh api -X PATCH repos/<you>/hello-app/code-scanning/default-setup --input -
gh api repos/<you>/hello-app/code-scanning/default-setup --jq '{state, languages, query_suite}'
gh api -X PUT repos/<you>/hello-app/vulnerability-alerts
gh api -X PUT repos/<you>/hello-app/automated-security-fixes
gh api repos/<you>/hello-app/actions/permissions/workflow --jq .can_approve_pull_request_reviews
```
Dependabot version updates come from `.github/dependabot.yml` in the pushed tree (pip and github-actions; use `npm` instead of `pip` for a Node app). There is no docker entry: the Dockerfile and its base image belong to the builder.

**Check:** the last command prints `false` ("Allow GitHub Actions to create and approve pull requests" must be off, or a workflow could approve its own PR; turn it off under Settings -> Actions -> General if not), the second command prints `"state": "configured"` (it may say `not-configured` for a moment while it runs; ask again), `gh api repos/<you>/hello-app/vulnerability-alerts -i | head -1` ends in `204`, and after a few minutes `gh run list --repo <you>/hello-app --json name,conclusion --jq '.[] | select(.name | test("CodeQL"))'` shows a completed run on `main`.

Then add merge protection for CodeQL alerts. This is a separate ruleset so that a wrong field cannot break the `main` ruleset, and the rule's exact fields are **unconfirmed**: if the API rejects it, set "Require code scanning results" in the repository's ruleset UI instead (tool `CodeQL`, alerts `High or higher`). Do it only after the first CodeQL run on `main` has finished, or PRs wait for a result that does not exist yet.
```bash
gh api -X POST repos/<you>/hello-app/rulesets --input bootstrap/hello-app/.github/rulesets/code-scanning.json
gh api repos/<you>/hello-app/rulesets --jq '.[].name'
```
**Check:** the second command lists `main` and `code-scanning`.

## 11. Make Gate 1 wait on the scans (`platform/required-checks.json`) and re-release the builder
Until this step the committed `platform/required-checks.json` is `{"required": []}`, so **Gate 1 waits on nothing beyond the drift check and the authz probe**: a PoC gap. The file is left empty in the repo on purpose: it is the one list every environment shares, and a check's name and source must be observed on a real commit before they can be required (CodeQL's name and app id can only be found this way). `configure.py` does not touch it, so it stays out of the export's placeholder rules. The list is baked into the builder image, so changing it means a new builder release.

**Check (before):** `cat platform/required-checks.json` prints `{"required": []}`, and the last release job's Gate 1 step logged the warning "No external required checks are configured".

1. **Find the names and sources** on the newest `main` commit of `hello-app` (after the CodeQL run has finished):
   ```bash
   sha=$(gh api repos/<you>/hello-app/commits/main --jq .sha)
   gh api "repos/<you>/hello-app/commits/$sha/check-runs" --paginate --jq '.check_runs[] | {name, app_id: .app.id, app: .app.slug}'
   ```
   You should see `golden-road / drift`, `golden-road / preview` (on a PR head commit; it is skipped on `main`, so do not add it to `required-checks.json`), `security-scans / semgrep` and `security-scans / trivy` (app id 15368), and one CodeQL `Analyze (<language>)` entry per language. Write down CodeQL's exact name and app id; do not assume them. (Unconfirmed: CodeQL's default setup posts `Analyze (python)` under the GitHub Actions app. If you see a different source, use it.)
2. **Fill the list** in the exported platform tree, with the CodeQL entries you found (one per language you want Gate 1 to wait for):
   ```bash
   cd ~/poc/platform-pipeline
   jq -n '{required: [
     {name: "security-scans / semgrep", app_id: 15368},
     {name: "security-scans / trivy", app_id: 15368},
     {name: "<CodeQL check name from step 1>", app_id: <its app id>}]}' > platform/required-checks.json
   cat platform/required-checks.json
   (cd src && <src>/.venv/bin/python -c "from golden_road.checks import load_required as l; print(len(l('../platform/required-checks.json')), 'entries')")
   ```
   `jq -n` is used instead of a heredoc because pasting into some terminals indents every line, and an indented closing `EOF` is not recognized: the shell then writes the following commands into the file. The builder release refuses a malformed list (it fails at "Build, push, and sign"), so a bad file never ships, but check `cat` before you commit. Observed in the 2026-10-21 PoC run: CodeQL default setup posts `Analyze (python)` and `Analyze (actions)` under app id 15368.
   Gate 1 can only see a check run's conclusion. For CodeQL that means "the analysis ran"; the alert threshold is enforced at merge time by the `code-scanning` ruleset (step 10), not at release.
3. **Bump the builder version and regenerate `hello-app`.** The lock in `hello-app` records the builder version, so the app must be regenerated, and the tag of an existing release cannot move. The script sets the version and regenerates `hello-app` with this tree's configuration (what re-running the step 1 `configure.py` command did), so there is nothing else to run. `<new version>` is the next patch version after the one step 3 released:
   ```bash
   <src>/.venv/bin/python scripts/bump-builder-version.py <new version>
   git diff --stat
   ```
   Expect exactly: `platform/required-checks.json`, `src/builder/__init__.py`, and in `bootstrap/hello-app` only `Dockerfile` (its "Generated by builder" line) and `.builder.lock`.
   Then make the same bump in `<src>` and commit it there, so a later export (13.2, 14.1) doesn't go back to the old version:
   ```bash
   cd <src>
   <src>/.venv/bin/python scripts/bump-builder-version.py <new version>
   git add -A && git commit -m "Builder <new version>"
   ```
4. **Release the builder again** exactly as in step 3, with the new tag:
   ```bash
   version=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' src/builder/__init__.py)
   git add -A && git commit -m "Gate 1 waits on the scans; builder $version"
   git push origin main
   git tag builder-v$version && git push origin builder-v$version
   gh run watch --repo <you>/platform-pipeline
   ```
   Put the new digest in `BUILDER_IMAGE` in **both** `.github/workflows/golden-road.yml` and `.github/workflows/golden-road-promote.yml`, commit, and push. Make sure the package is still public (step 3, Check 2).
5. **Update `hello-app` by pull request** (its `main` is protected; a push straight to `main` is refused). The deputy approves it like any other PR (single-approver mode: no approval needed, merge once the checks pass):
   ```bash
   cd ~/poc && git clone "$(gh repo view <you>/hello-app --json url --jq .url)" hello-app-update && cd hello-app-update
   git switch -c builder-$version
   cp ~/poc/platform-pipeline/bootstrap/hello-app/Dockerfile ~/poc/platform-pipeline/bootstrap/hello-app/.builder.lock .
   git add -A && git commit -m "Regenerate for builder $version" && git push -u origin builder-$version
   gh pr create --repo <you>/hello-app --fill
   ```
**Check (after):** `golden-road / drift`, `golden-road / preview` and both `security-scans` checks pass on that PR. After the deputy approves (single-approver mode: skip that) and you merge, the release job's Gate 1 step waits for every check you listed in `required-checks.json` on the merge commit, passes, and the published release's gate record names them: `gh release download <test tag> --repo <you>/hello-app -p gate-record.json && jq '[.required_checks[] | {name, state}]' gate-record.json` lists one entry per required check (the two scans plus one CodeQL entry per language you listed), each `"state": "success"`.

## 12. Proof run (record the results)
1. **Drift blocks.** On a branch of `hello-app`, change `USER 10001:10001` to `USER root` in `Dockerfile` and open a PR. Expect `golden-road / drift` to fail with a PR comment and a file annotation naming `builder.file-modified`. Close the PR.
   **Check:** `gh pr checks <number> --repo <you>/hello-app` shows `golden-road / drift` failing. It also shows `golden-road / preview`, which runs in parallel and does not wait for drift: it builds this PR's image and probes it, so it passes or fails on its own (with `USER root` the image still builds and the guard still denies, so expect it to pass; the drift failure is what blocks this PR).
2. **Preview catches a broken app before merge.** On a branch of `hello-app`, add `import requests` as the first line of `app.py`. `requests` is not in `requirements.txt`, so the image builds but the app crashes at start-up with `ModuleNotFoundError`. Drift does not look at application imports, so it passes; the guard cannot help, because the app never starts. Open a PR (with no container runtime on your machine; that is the point).
   **Check:** `gh pr checks <number> --repo <you>/hello-app` shows `golden-road / preview` **failing** and `golden-road / drift` passing. The job summary and annotation give the reason, which is one of two wordings depending on timing: `app exited before the probe could run (exit code 1)` or `the app did not start`. The result is always `fail`, and for an app that builds and then crashes the `authz.json` artifact is always there. If the image fails to build or the container can't start, there is no artifact and the comment says there is no probe result. The PR has a `Golden road PR preview: FAIL` comment (posted by `preview-comment`, which is not a required check), the log's probe step shows the last lines of the app's log between `stop-commands` markers (`ModuleNotFoundError: No module named 'requests'`), and the PR cannot be merged. Download the artifact to see the result: `gh run download <run id> --repo <you>/hello-app -n golden-road-preview && jq . authz.json` prints `"status": "fail"` with one of those two `reason` values. Then remove the line and push: the preview passes, the comment is edited in place to `PASS`, and `golden-road / preview` goes green. Close the PR (or merge it as the README change in the next item).
   What this proves and what it does not. The probe reads the PR's own `builder-spec.json`, so a PR that loosens `authz` there and regenerates the files passes drift (the generated files match the spec) and passes preview (the policy was loosened, so the probe expects the new behaviour; removing `authz` entirely is reported as `not-declared`). Only edits to the generated guard or policy files, which no longer match the spec, fail both. Changes to `authz` in `builder-spec.json` are what the AI reviewer (build step 4) and human review must catch (architecture spec, carried requirement 8). What the preview adds is that the image builds, starts, and that the guard is wired into the running container before merge.
3. **Test release.** Open a README PR, have `<deputy>` approve it (single-approver mode: no approval needed), and merge it. Expect the `release` job to wait for the required scan checks (Gate 1), build, run the authz probe (summary shows `pass`), sign, and create a prerelease `test-...`.
   ```bash
   gh release view --repo <you>/hello-app $(gh release list --repo <you>/hello-app --limit 1 --json tagName --jq '.[0].tagName')
   gh api /users/<you>/packages/container/hello-app --jq .visibility
   ```
   **Check:** the release exists. The second command prints `private` or `public`; both are acceptable here. Make the package public if you want to run the verify commands below without logging in to ghcr.io.
4. **Signature identity.** Set the variables once (`REF` is the image from the release notes). The first two commands pass; the third and fourth fail, which is the point:
   ```bash
   ORG=<you>; APP=hello-app
   id=(--certificate-identity "https://github.com/$ORG/platform-pipeline/.github/workflows/golden-road.yml@refs/heads/main"
       --certificate-oidc-issuer https://token.actions.githubusercontent.com
       --certificate-github-workflow-repository "$ORG/$APP"
       --certificate-github-workflow-ref refs/heads/main
       --certificate-github-workflow-trigger push)
   cosign verify "${id[@]}" "$REF"                                   # passes
   cosign verify-attestation "${id[@]}" --type https://golden-road.internal/gate-record/v1 "$REF" > gate.jsonl   # passes
   cosign verify --certificate-identity "https://github.com/$ORG/$APP/.github/workflows/ci.yml@refs/heads/main" \
     --certificate-oidc-issuer https://token.actions.githubusercontent.com "$REF"   # fails: wrong identity
   cosign verify --certificate-identity "https://github.com/$ORG/platform-pipeline/.github/workflows/golden-road.yml@refs/heads/main" \
     --certificate-oidc-issuer https://token.actions.githubusercontent.com \
     --certificate-github-workflow-repository "$ORG/some-other-repo" "$REF"   # fails: wrong repository
   jq -s -e --arg repo "$ORG/$APP" --arg ref "$REF" '[.[] | .payload | @base64d | fromjson | .predicate
       | select(.status == "pass" and .repo == $repo and .image == $ref)] | length == 1' gate.jsonl
   ```
   **Check:** the first two exit 0, the third and fourth exit non-zero, and the `jq` command prints `true`.
5. **Gate 2.** Run `gh workflow run promote.yml --repo <you>/hello-app -f release=<test tag>` as yourself. GitHub shows "Waiting for review".
   - If you have a deputy: try approving it yourself: it must be refused (prevent self-review). Then `<deputy>` approves with a comment.
   - Single-approver mode: approve it yourself with a comment; it is accepted.
   **Check:** if you have a deputy, the self-approval is refused; after the deputy approves, a `prod-...` release appears whose `promotion-record.json` names `<deputy>` and says `"self_approved": false`. Single-approver mode: the release appears, `promotion-record.json` names `<you>` and says `"self_approved": true` and `"gate2_single_approver": true`, and the job summary and the release notes say "Single-approver PoC mode: approved by the person who triggered it. Not valid for production use."; the deploy-owner verification in the next step must then reject this record (see below). Re-running a promote run is refused: dispatch a new run. Check the commit date in the prod release notes is not an old commit (a rollback) unless you intend it.
6. **Deploy-owner verification (the full contract).** Download `manifests.yaml` from the prod release and run every check in "Deploy-owner verification contract" in `docs/enforcement.md`. Take the image reference from `manifests.yaml`, never from the release notes or another input:
   ```bash
   gh release download <prod tag> --repo <you>/hello-app -p manifests.yaml
   ORG=<you>; APP=hello-app
   PREF=$(grep -E '^[[:space:]]*image:' manifests.yaml | awk '{print $2}' | tr -d '"' | sort -u)
   test "$(printf '%s\n' "$PREF" | wc -l)" -eq 1 && echo "$PREF"
   have=$(sha256sum manifests.yaml | cut -d' ' -f1)

   # 1. image signature and gate attestation (every release)
   id=(--certificate-identity "https://github.com/$ORG/platform-pipeline/.github/workflows/golden-road.yml@refs/heads/main"
       --certificate-oidc-issuer https://token.actions.githubusercontent.com
       --certificate-github-workflow-repository "$ORG/$APP"
       --certificate-github-workflow-ref refs/heads/main
       --certificate-github-workflow-trigger push)
   cosign verify "${id[@]}" "$PREF"
   cosign verify-attestation "${id[@]}" --type https://golden-road.internal/gate-record/v1 "$PREF" > gate.jsonl
   # 2. gate predicate: status, repo, image, and the manifests hash
   jq -s -e --arg repo "$ORG/$APP" --arg ref "$PREF" --arg have "$have" '[.[] | .payload | @base64d | fromjson | .predicate
       | select(.status == "pass" and .repo == $repo and .image == $ref and .manifests_sha256 == $have)] | length == 1' gate.jsonl

   # 3. promotion attestation (prod only)
   pid=(--certificate-identity "https://github.com/$ORG/platform-pipeline/.github/workflows/golden-road-promote.yml@refs/heads/main"
        --certificate-oidc-issuer https://token.actions.githubusercontent.com
        --certificate-github-workflow-repository "$ORG/$APP"
        --certificate-github-workflow-ref refs/heads/main
        --certificate-github-workflow-trigger workflow_dispatch)
   cosign verify-attestation "${pid[@]}" --type https://golden-road.internal/promotion/v1 "$PREF" > promo.jsonl
   jq -s -e --arg ref "$PREF" --arg have "$have" '[.[] | .payload | @base64d | fromjson | .predicate
       | select(.image == $ref and .manifests_sha256 == $have and .self_approved == false and .gate2_single_approver == false)] | length >= 1' promo.jsonl
   ```
   **Check:** exactly one image reference prints, both `cosign verify` commands and both `cosign verify-attestation` commands exit 0, and both `jq` commands print `true`. Single-approver mode: the last `jq` prints `false`, which is the contract working (a self-approved record is rejected); the other checks still pass, and the dry run below is optional. Then a read-only server-side dry run against the `hydra` cluster:
   ```bash
   kubectl config get-contexts hydra
   kubectl --context hydra apply --dry-run=server -n default -f manifests.yaml
   ```
   **Check:** every object reports "created (server dry run)". Nothing is created.
7. **What Rekor shows.** Signing uses the public Sigstore log. The promotion attestation's predicate names approvers and carries their comments, so confirm the log entry stores only hashes.
   ```bash
   cosign verify-attestation "${pid[@]}" --type https://golden-road.internal/promotion/v1 "$PREF" | head -1 | jq -r .payload | base64 -d > stmt.json
   sha=$(sha256sum stmt.json | cut -d' ' -f1)
   rekor-cli search --sha "$sha" --rekor_server https://rekor.sigstore.dev
   rekor-cli get --uuid <uuid from the search> --rekor_server https://rekor.sigstore.dev --format json \
     | jq '{body_keys: (.Body | keys), attestation_stored: ((.Attestation // "") != "")}'
   ```
   **Check:** the entry is found, `.Body | keys` prints `DSSEObj`, and `attestation_stored` is `false`. If cosign writes to Rekor v2, the search may find nothing even though the entry exists; record the output either way. On a public PoC the account names are public regardless, so only the approver comments matter.
8. **Fork PR.** Single-approver mode: this test needs some account other than yours; if you have none, skip it and record the fork-PR behavior as untested. Otherwise, as `<deputy>` (or any other account; use one that is **not** a collaborator to exercise the approval gate), fork `hello-app`, change the README on a branch in the fork, and open a PR to `<you>/hello-app`.
   ```bash
   gh repo fork <you>/hello-app --clone=false
   # push a README change to a branch of the fork, then:
   gh pr create --repo <you>/hello-app --head <fork owner>:<branch> --title "Fork PR check" --body "test"
   ```
   If the account is not a collaborator, the run waits for approval: approve it with `gh api -X POST repos/<you>/hello-app/actions/runs/<run id>/approve`.
   **Check:** all of the following.
   - `golden-road / drift` and `golden-road / preview` run and report (a README change passes both), so the PR checks do not hang or fail. The preview builds the fork's Dockerfile with the same read-only token.
   - The run log's "Set up job" step lists `GITHUB_TOKEN Permissions` as read-only for everything, including `PullRequests: read` even though the workflow requests `pull-requests: write`: `gh run view <run id> --repo <you>/hello-app --log | grep -A12 'GITHUB_TOKEN Permissions'`.
   - The step "Comment on the PR" shows as **skipped**, the `preview-comment` job is **skipped**, and the PR has no golden-road comment.
   - The job summary and any annotations are still present, and no secret is available to the run.
   - `security-scans / semgrep` and `security-scans / trivy` run and report on the fork PR, with the same read-only token; the `security-scans / comment` job (not a required check) is **skipped**, and the PR has no `<!-- security-scans:... -->` comment.

   Close the PR.
9. **Known-vulnerable code and dependency are refused.** On a branch of `hello-app`, add a dependency with a well-known HIGH CVE and an injectable route, and open a PR.
   ```bash
   gh repo clone <you>/hello-app ~/poc/hello-app-proof && cd ~/poc/hello-app-proof && git switch -c proof/vulnerable
   printf 'jinja2==2.10\n' >> requirements.in            # CVE-2019-10906 (HIGH), fixed in 2.10.1
   uv pip compile --generate-hashes --python-version 3.12 requirements.in -o requirements.txt
   cat >> app.py <<'EOF'

   import sqlite3
   import subprocess


   @app.get("/api/ping")
   def ping(host: str):
       return subprocess.run("ping -c1 " + host, shell=True, capture_output=True).stdout


   @app.get("/api/search")
   def search(q: str):
       cur = sqlite3.connect("notes.db").cursor()
       cur.execute("SELECT * FROM notes WHERE text = '" + q + "'")
       return cur.fetchall()
   EOF
   python3 -c "import ast; ast.parse(open('app.py').read()); print('app.py parses')"   # a paste that indented the lines breaks the heredoc or the Python
   git add -A && git commit -m "Proof: vulnerable dependency and injectable routes" && git push -u origin proof/vulnerable
   gh pr create --repo <you>/hello-app --title "Proof: vulnerable" --body "must be refused"
   gh pr checks --repo <you>/hello-app proof/vulnerable --watch
   ```
   Expect: `security-scans / semgrep` **fails** (the vendored rules `python.subprocess-shell-true` and `python.sql-built-from-strings`, whose full ids carry a prefix to confirm on the run, annotated on the two lines of `app.py`, and the registry rules' matching findings), `security-scans / trivy` **fails** (CVE-2019-10906 on `requirements.txt` and in the image), CodeQL reports high alerts (`py/command-line-injection`, `py/sql-injection`; unconfirmed names) and its code-scanning check fails, and the PR has one comment per tool. **Gate 1 on a push:** you cannot push this to `main` (the ruleset refuses it), so ask the same Gate 1 command the release uses what it would decide for this commit. Use the builder image you pinned in step 11 and a **fine-grained personal access token** that can only read: create it at `https://github.com/settings/personal-access-tokens/new` (there is no API for this), resource owner `<you>`, repository access **Only select repositories** -> `hello-app`, repository permissions **Checks: Read-only** and **Commit statuses: Read-only** (Metadata is added automatically), a short expiry. Paste it at the prompt below; do not use `gh auth token`, which is your full-scope login token:
   ```bash
   sha=$(gh pr view proof/vulnerable --repo <you>/hello-app --json headRefOid --jq .headRefOid)
   read -rs -p "read-only token: " GOLDEN_ROAD_READ_TOKEN && export GOLDEN_ROAD_READ_TOKEN
   export GITHUB_TOKEN="$GOLDEN_ROAD_READ_TOKEN"   # docker reads it from the environment, so it stays off the command line
   mkdir -p out && docker run --rm --user "$(id -u):$(id -g)" -e GITHUB_TOKEN -v "$PWD/out:/out" \
     ghcr.io/<you>/golden-road-builder@sha256:<digest> \
     golden-road checks wait --repo <you>/hello-app --sha "$sha" --timeout 120 --interval 10 --output /out/checks.json; echo "exit=$?"
   jq '[.required_checks[] | {name, state}]' out/checks.json
   unset GITHUB_TOKEN GOLDEN_ROAD_READ_TOKEN
   ```
   Revoke the token in Settings -> Developer settings -> Fine-grained tokens once the proof run is done.
   **Check:** `golden-road / preview` is on this PR too and, because it tests that the image builds and passes the authz probe (not vulnerabilities), is expected to pass here; if the vulnerable dependency stops the image from building or starting it fails instead, which is also a correct refusal. The PR cannot be merged (`gh pr merge` is refused; the Merge button is disabled with failing required checks), `exit=` is not `0`, and the `jq` output shows `security-scans / semgrep` and `security-scans / trivy` as `failure` (the release job would stop at Gate 1, before anything is built or signed). If it exits with an error message instead, or a check is still `pending`, the scans had not finished or the token could not read the checks: read the message, wait, and run it again.
10. **Fix, then green, then signed.** On the same branch, drop the vulnerable dependency and both routes, so the tree is `main` again:
   ```bash
   git revert --no-edit HEAD && git push
   gh pr checks --repo <you>/hello-app proof/vulnerable --watch
   ```
   `<deputy>` approves (single-approver mode: skip) and you merge. **Check:** all of `golden-road / drift`, `golden-road / preview`, `security-scans / semgrep`, `security-scans / trivy` and the CodeQL check pass on the PR; the comments for the two tools now say `passed` (they are edited in place, not added); after the merge, the release job's Gate 1 waits for every required check on the merge commit and the release is signed (`gh release list --repo <you>/hello-app --limit 1`). Then close the proof PR's branch and run `gh pr list --repo <you>/hello-app --state all` to confirm nothing else is open. Record the commit SHAs and run URLs.

## 13. Risk scorecard: the factory repo and the first live run
The scorecard gives every ready-for-review PR on `hello-app` a **scrutiny level** (`standard`, `elevated` or `security_review`) and a checklist of what a person should look at. It classifies; it never blocks a merge by itself, and drift, Gate 1, the preview and Gate 2 stay as they are. It runs from a fourth public repo, `<you>/scorecard-factory`, which holds the workflow and the trusted inputs (the app's tier and purpose, accepted findings, thresholds). `hello-app` calls its workflow as a reusable workflow, because a personal account can't require workflows with a ruleset, and the `main` ruleset requires its `scorecard / assessed` check.

What reaches the model: the review agent reads `hello-app`'s files through three read-only tools and sends excerpts to your model gateway. Text aimed at the reviewer and secret-shaped strings are withheld, but everything else in the repo can be sent, so use a model you are content to show `hello-app`'s code.

### Decide these before you start (each is a placeholder in the commands below)
Nothing here has a safe default, so the steps use these placeholders and you fill them in by hand. Write your answers down first.
- `<scorecard gateway host>`: the **public** hostname for the scorecard's route to LiteLLM (for example `llm-ci.<your domain>`). It is published in the factory config. `configure.py` takes it as `--scorecard-gateway-url https://<scorecard gateway host>/v1/chat/completions`; without that option it falls back to your app gateway host, which GitHub's runners cannot reach.
- `<reviewer model>`, `<gateway model name>` and `<model version string>`: the model you want as reviewer, the exact model name your gateway serves it under (the `SCORECARD_GATEWAY_MODEL` secret holds it), and the exact version string you want recorded in every assessment (`agent.model.version`; the committed `pinned-1` is a stand-in). The factory config has no model name: the name goes only in the secret, which keeps it out of every file, assessment, comment and log (on an organization's gateway it can be a sensitive deployment name). `scorecard run` refuses an assessment from any other version, so whenever you change the model behind that name, or the name itself, raise `agent.model.version` by a factory PR. `agent.model.version` must not contain the deployment name; the scorecard refuses it.
- `<litellm internal host>`: the address of your LiteLLM proxy that is reachable only from inside your network (13.1 creates the key through it, never through the public route).
- `<daily budget>`: the key's `max_budget` per day, in your LiteLLM cost units. Start low; 13.3 and 13.6 measure what an assessment costs.
- **Factory approval mode:** add `--single-approver` to the `configure.py` command in 13.2 only if you used it in step 1 (a different setting is refused). With a deputy, the factory's `main-review` ruleset requires one approval and code-owner review, so the deputy also reviews every change to the factory's intake and suppressions; in single-approver mode a PR is still required, with no approval.

### 13.1 A public route to the gateway, and a scoped key
GitHub's runners can't reach a gateway inside your cluster. Give the LiteLLM proxy a public hostname, `<scorecard gateway host>`, through the in-cluster `cloudflared` tunnel, and protect it with a LiteLLM virtual key that can only call the reviewer model, with a daily budget. **The hostname becomes public** (it is in the factory config), and the key is stored as a `hello-app` secret, which **anyone with write access to `hello-app` can read** (a workflow they write can print it). That is the accepted fallback for a personal account; in an organization, prefer GitHub OIDC to the gateway, bound to the factory workflow, so no key exists at all (`GATEWAY_AUTH: oidc` in `scorecard.yml`).

1. **Firewall rule first, then the route.** Make the WAF rule before the hostname exists (and the Access application of step 4, if you use it), so the admin API is never reachable on it, even briefly. In the Cloudflare dashboard for the zone, add a WAF custom rule that blocks everything on that host except the one call the agent makes: expression `(http.host eq "<scorecard gateway host>" and not (http.request.method eq "POST" and http.request.uri.path eq "/v1/chat/completions"))`, action **Block**. The LiteLLM admin API (`/key/*`, `/user/*`, the UI) then stays reachable only from inside your network. Only then add the public hostname to the tunnel: `<scorecard gateway host>` to the LiteLLM service (for a locally managed tunnel, an `ingress:` rule above the catch-all, such as `- hostname: <scorecard gateway host>` with `service: http://<litellm service>.<namespace>.svc:4000`; for a remotely managed one, Zero Trust -> Networks -> Tunnels -> your tunnel -> Public Hostname).
   **Check:** `curl -sS -o /dev/null -w '%{http_code}\n' -X POST https://<scorecard gateway host>/v1/chat/completions -H 'Content-Type: application/json' -d '{}'` prints `401` (the route works and LiteLLM wants a key), and `curl -sS -o /dev/null -w '%{http_code}\n' https://<scorecard gateway host>/key/info` prints `403` (the WAF rule); a `GET` of the chat-completions path should print `403` too (only `POST` is let through).
2. **Model.** In LiteLLM's `model_list`, serve `<reviewer model>`, the exact model version you chose (not a moving alias), under the name `<gateway model name>`, and deploy it the way you deploy LiteLLM's config. **Decide this:** which model, its name, and its version string (13.2 writes `<model version string>` into the factory config's `agent.model.version`). Set `agent.model.temperature: null` if the model rejects a temperature (the gateway returns HTTP 400); an empty `temperature:` also means null; the default is 0.
   Put the name in a shell variable for 13.1 step 3 and 13.5 (type the name your gateway serves; it is not printed):
   ```bash
   printf 'Gateway model name: '; read -rs SCORECARD_GATEWAY_MODEL; echo
   export SCORECARD_GATEWAY_MODEL
   ```
3. **Key.** From inside your network (never through the public route), create a virtual key limited to that model, with a budget. The key goes into a shell variable and is never printed:
   ```bash
   printf 'LiteLLM master key: '; read -rs LITELLM_MASTER_KEY; echo
   SCORECARD_GATEWAY_TOKEN=$(jq -n 'env.SCORECARD_GATEWAY_MODEL as $m | {key_alias: "scorecard-hello-app", models: [$m], max_budget: <daily budget>, budget_duration: "1d", rpm_limit: 60}' \
     | curl -sSf https://<litellm internal host>/key/generate -H @<(printf 'Authorization: Bearer %s\n' "$LITELLM_MASTER_KEY") \
         -H 'Content-Type: application/json' -d @- | jq -r .key)
   unset LITELLM_MASTER_KEY
   export SCORECARD_GATEWAY_TOKEN
   ```
   If your LiteLLM version supports restricting a key to LLM routes (a key-level `allowed_routes` setting, for example `["llm_api_routes"]`), add it to the JSON above, so the key can't reach LiteLLM's management routes even through a misconfigured rule; the model list and the WAF rule already limit what it can do. Replace `<daily budget>` with the number you decided (a bare number, no quotes). `max_budget` is in your LiteLLM cost units per `budget_duration`; one full assessment of `hello-app` is tens of agent calls, so measure it in 13.3 and 13.6 and adjust.
   **Check:** the key works for the reviewer model (the name in `SCORECARD_GATEWAY_MODEL`) through the public route and for nothing else:
   ```bash
   ask() { M="$1" jq -n '{model: env.M, messages: [{role: "user", content: "Reply with OK"}], max_tokens: 5}' \
     | curl -sS -o /dev/null -w '%{http_code}\n' https://<scorecard gateway host>/v1/chat/completions \
         -H @<(printf 'Authorization: Bearer %s\n' "$SCORECARD_GATEWAY_TOKEN") -H 'Content-Type: application/json' -d @-; }
   ask "$SCORECARD_GATEWAY_MODEL"   # 200
   ask <any other model>    # 401 or 403
   ```
4. **Optional second layer: a Cloudflare Access service token.** With it, Cloudflare itself rejects any request that lacks the two headers `CF-Access-Client-Id` and `CF-Access-Client-Secret`, before it reaches LiteLLM; the LiteLLM key stays as the second layer, and the WAF rule stays. Both the Access application and the WAF rule can be created before the tunnel hostname exists, so do them first: if you want this layer, do this step before step 1 adds the public hostname. In Zero Trust -> Access -> Service credentials, create a service token (name it `scorecard-hello-app`) and copy the client id (`<hex>.access`) and the client secret now; the secret is shown once. Then Access -> Applications -> Add an application -> **Self-hosted**: hostname `<scorecard gateway host>` with an empty path, so the whole hostname is covered (the WAF rule still limits it to `POST /v1/chat/completions`), and a policy with action **Service Auth** that includes that service token (no other rule). In the application's settings turn on **Return 401 response for service auth policies**, so a request without a valid token gets a `401` and not a `302` to a login page. Put the values in shell variables and never print them:
   ```bash
   printf 'Access client id: '; read -rs SCORECARD_GATEWAY_ACCESS_ID; echo
   printf 'Access client secret: '; read -rs SCORECARD_GATEWAY_ACCESS_SECRET; echo
   export SCORECARD_GATEWAY_ACCESS_ID SCORECARD_GATEWAY_ACCESS_SECRET
   ```
   **Check:** the call from step 1 without the headers is refused by Cloudflare (`401`, or `403`), and the same call with the headers reaches LiteLLM and prints `401` for the missing key (not a Cloudflare answer); the secret stays out of the command line:
   ```bash
   curl -sS -o /dev/null -w '%{http_code}\n' -X POST https://<scorecard gateway host>/v1/chat/completions -H 'Content-Type: application/json' -d '{}' \
     -H @<(printf 'CF-Access-Client-Id: %s\nCF-Access-Client-Secret: %s\n' "$SCORECARD_GATEWAY_ACCESS_ID" "$SCORECARD_GATEWAY_ACCESS_SECRET")
   ```
   The scorecard sends both headers on every model call, never follows a redirect, and stops with "needs a human" (T-14) on a `302`, `401` or `403`. Set both secrets in 13.5, or neither: setting only one makes the run "not assessed".

### 13.2 Bring the scorecard into the platform tree and release the builder
The scorecard code ships inside the builder image (it needs `git`, which this release adds), so the platform tree needs the new code, a configured factory, and a new builder release. Make a fresh export, configure it the same way as step 1 plus the scorecard's gateway URL, carry over the things that only your published tree has, and copy the result over the published tree.

This step releases the version the source is at (it is read from `src/builder/__init__.py`, so there is no version edit; the tag is `builder-v<version>`). That release also publishes the agent kit, and its `kit` job fails here, because the GitHub App it publishes with doesn't exist until step 14.4. That is expected: the image and its signature are fine, and step 14 re-runs just that job.
```bash
cd <src> && git status --porcelain     # must print nothing
scripts/export-public-poc.sh ~/poc/export-scorecard
cd ~/poc/export-scorecard
cp ~/poc/platform-pipeline/platform/required-checks.json platform/
cp ~/poc/platform-pipeline/bootstrap/hello-app/.github/CODEOWNERS bootstrap/hello-app/.github/
if test -f ~/poc/platform-pipeline/bootstrap/golden-road-agents/.github/CODEOWNERS; then
  cp ~/poc/platform-pipeline/bootstrap/golden-road-agents/.github/CODEOWNERS bootstrap/golden-road-agents/.github/
fi
version=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' src/builder/__init__.py)
echo "$version"                       # the version this step releases, as builder-v<version>: no version edit
find . -name __pycache__ -prune -exec rm -rf {} +
<src>/.venv/bin/python scripts/configure.py --org <you> --gateway-host <gateway host> \
  --edge-namespace cloudflared --edge-label app=cloudflared --identity-header X-Platform-Roles \
  --scorecard-gateway-url https://<scorecard gateway host>/v1/chat/completions
# Single-approver mode: add --single-approver, as in step 1.
```
`configure.py` fills the factory's gateway URL and org, and renames its `intake/ORG_PLACEHOLDER/` directory to `intake/<you>/`. Two more edits to the factory's config, each one command. First, give it the same Gate 1 list as `platform/required-checks.json` (the entries go after the drift and preview lines; the scorecard also adds the image's own list, so this keeps the config honest rather than being the only source). Second, write down the model version you decided (`<model version string>`; the command quotes it, so `/` and `&` are safe). The shell quotes it in single quotes: if it contains a `'`, write that as `'\''`, or choose a version string without quotes. Then set the factory's code owners (in single-approver mode, only you):
```bash
jq -r '.required[] | "  - " + tostring' platform/required-checks.json > ../gate1-extra.txt
sed -i '/name: "golden-road \/ preview"/r ../gate1-extra.txt' bootstrap/scorecard-factory/config/scorecard.yaml
<src>/.venv/bin/python -c 'import json, pathlib, sys; p = pathlib.Path(sys.argv[1]); p.write_text(p.read_text().replace("version: pinned-1}", "version: " + json.dumps(sys.argv[2]) + "}"))' bootstrap/scorecard-factory/config/scorecard.yaml '<model version string>'
printf '%s\n' '* @<you> @<deputy>' '/intake/ @<you> @<deputy>' '/suppressions/ @<you> @<deputy>' '/na/ @<you> @<deputy>' > bootstrap/scorecard-factory/.github/CODEOWNERS
# Single-approver mode: printf '%s\n' '* @<you>' '/intake/ @<you>' '/suppressions/ @<you>' '/na/ @<you>' > bootstrap/scorecard-factory/.github/CODEOWNERS
```
**Check:** `grep -n 'gate1_checks:' -A8 bootstrap/scorecard-factory/config/scorecard.yaml` lists drift, preview and then every entry of `platform/required-checks.json`; `grep -n 'model:' bootstrap/scorecard-factory/config/scorecard.yaml` prints `model: {version: "<your version string>"}` (no `pinned-1`); `PYTHONPATH=src <src>/.venv/bin/pytest -q tests/pipeline/test_scorecard_factory.py tests/kit` passes (the factory test accepts a changed model version and the extra Gate 1 entries, and checks that `gate1_checks` covers every required check; the kit tests run against the configured kit); and `grep -rn 'replace-me\|ORG_PLACEHOLDER' platform bootstrap` prints nothing. (The factory's `BUILDER_IMAGE` still has the all-zero digest; it is pinned below.)

Copy it over the published tree, keeping the published tree's history and its current builder pins, so `main` keeps working until the new image exists:
```bash
cd ~/poc/platform-pipeline
old=$(sed -n 's/^  BUILDER_IMAGE: //p' .github/workflows/golden-road.yml)
rsync -a --delete --exclude .git ~/poc/export-scorecard/ ./
sed -i "s|^  BUILDER_IMAGE: .*|  BUILDER_IMAGE: $old|" .github/workflows/golden-road.yml .github/workflows/golden-road-promote.yml
git status --short
git diff -- platform/platform.json
```
**Check:** the `platform.json` diff only adds `github_host`, `actions_oidc_issuer` and `landing_page_url` (`configure.py` records them; the kit and doctor read them), and the status lists the scorecard changes (`src/scorecard/`, `images/builder/Dockerfile`, `bootstrap/scorecard-factory/`, `bootstrap/hello-app/`, `scripts/`, `tests/`, `docs/`), the agent kit (`bootstrap/golden-road-agents/`, `.github/workflows/release-builder.yml`) and no change to `.github/workflows/golden-road*.yml`. Commit and push `main`:
```bash
version=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' src/builder/__init__.py)
git add -A && git commit -m "Risk scorecard: factory bootstrap, scorecard workflow, builder $version"
git push origin main
```
Before you push the builder tag, decide about the `kit` job. If you want it to succeed on the first try, do this first (the export you just copied in already contains `golden-road-agents`): the code owners block of 14.1, committed and pushed to `main`, then 14.2 to 14.4 (create `golden-road-agents`, release the skill, make the App and store its variable and secret), and only then push the tag. If you don't, the `kit` job fails at the token step and 14.5 re-runs just that job. GitHub allows re-running a failed job only within 30 days of the run, so don't leave it longer (14.0 and 14.5 say what to do after that).

Release the builder as in step 3:
```bash
cd ~/poc/platform-pipeline
version=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' src/builder/__init__.py)
git ls-remote --tags origin builder-v$version     # must print nothing: the tag is unused
git tag builder-v$version && git push origin builder-v$version
gh run watch --repo <you>/platform-pipeline
```
If the tag is already in use (`git ls-remote` prints it), this source's version was already released: bump in `<src>` with the script and export again. In `<src>`: `<src>/.venv/bin/python scripts/bump-builder-version.py <new version>`, then `git diff --stat` (it shows `src/builder/__init__.py` and, in `bootstrap/hello-app`, `.builder.lock` and `Dockerfile`), then `git add -A && git commit -m "Builder <new version>"`, and start this step again from the export. Never move or reuse a tag.
The run's `release` job builds, pushes and signs the image; its `kit` job fails at the token step unless you did 14.2 to 14.4 first ("kit" is the agent kit: see the note at the top of this step). The `release` job's result is the one that matters here.

Pin the new digest (`ghcr.io/<you>/golden-road-builder@sha256:...` from the `release` job's summary) in **every** workflow that runs the image (today four: `golden-road.yml`, `golden-road-promote.yml`, and the factory's `scorecard.yml` and `factory-check.yml`). The command finds them by their `BUILDER_IMAGE` line, so a workflow added later is pinned too:
```bash
ref=ghcr.io/<you>/golden-road-builder@sha256:<digest>
grep -rlE '^  BUILDER_IMAGE: ' .github/workflows bootstrap | xargs sed -i "s|^  BUILDER_IMAGE: .*|  BUILDER_IMAGE: $ref|"
git add -A && git commit -m "Pin builder $version" && git push origin main
```
(Run it in the terminal where `version` is set, or set it again with the first line of the release block.) From here `hello-app`'s drift check runs the new builder, which expects the `AGENTS.md` and `CLAUDE.md` blocks and a new lock: every PR on `hello-app` fails drift until 13.6's PR is merged, because 13.6 regenerates them.
**Check:** `grep -rh '^  BUILDER_IMAGE:' .github/workflows bootstrap | sort | uniq -c` prints one line, `4   BUILDER_IMAGE: $ref`; `grep -rlE '^  BUILDER_IMAGE: .*@sha256:0{64}$' .github bootstrap` prints nothing (no placeholder digest is left anywhere); the package is still public (step 3, Check 2), and `docker run --rm --network none "$ref" git --version` prints a git version.

### 13.3 Optional, before any PR: the golden set against your gateway
The golden set is 13 small PRs with known answers (an IDOR, a deleted test, a hallucinated package, prompt injections, clean PRs, ...). Running it live measures what your model gets right and wrong, and what one assessment costs, before it reviews anything real. It costs tokens; it never touches GitHub.
```bash
cd ~/poc/platform-pipeline
GOLDEN_ROAD_SCORECARD_LIVE=1 GOLDEN_ROAD_SCORECARD_FACTORY=bootstrap/scorecard-factory GOLDEN_ROAD_SCORECARD_REPORT=$HOME/poc/golden-live.json \
  PYTHONPATH=src <src>/.venv/bin/pytest -q -s tests/scorecard/test_scorecard_golden_live.py
jq '{model, prompt_version, tokens_used, relative: .baseline_relative.totals}' ~/poc/golden-live.json
```
It uses only the `agent` block of the factory config (gateway URL, model version, budget) and `SCORECARD_GATEWAY_TOKEN` and `SCORECARD_GATEWAY_MODEL` from 13.1. **Check:** `1 passed` (the eval reports accuracy; it does not assert it), and the report file exists. Record `tokens_used`, and the false positives and false negatives in `baseline_relative`; then set the LiteLLM key's `max_budget` and the config's `agent.budget.max_total_tokens` from the per-case cost, with room to spare. A live accuracy target is not set yet: these first numbers are what it will be calibrated from.

`0` means no limit (for `max_total_tokens` and `max_run_seconds`); the assess job's own timeout still stops a stuck run. A `max_run_seconds` a little below the job timeout (at most 14100; for example 14000) keeps partial results: unfinished batches become `needs_human` (T-14). With `0`, a run killed by the job timeout writes no assessment (not assessed, fails closed).

### 13.4 Create `scorecard-factory` (public)
```bash
cd ~/poc/platform-pipeline
GOLDEN_ROAD_PUBLIC_POC=<you> scripts/push-bootstrap.sh <you> scorecard-factory --public
gh repo view <you>/scorecard-factory --json visibility --jq .visibility
gh api -X PUT repos/<you>/scorecard-factory/actions/permissions/fork-pr-contributor-approval -f approval_policy=all_external_contributors
gh api -X POST repos/<you>/scorecard-factory/rulesets --input bootstrap/scorecard-factory/.github/rulesets/main.json
gh api -X POST repos/<you>/scorecard-factory/rulesets --input bootstrap/scorecard-factory/.github/rulesets/main-review.json
```
The script refuses until the factory's `BUILDER_IMAGE` is pinned (it refuses an all-zero digest). Everything in this repo is public, including `hello-app`'s tier and purpose. The scorecard workflow skips the factory's own PRs: they change the inputs, not an app. The factory's own check, `factory-check`, runs on them instead and the `main` ruleset requires it. The factory has two rulesets: `main` (no deletion, no force push, the required check) and `main-review` (the pull request and its review rules); 13.8 says why. In single-approver mode `configure.py` already relaxed `main-review` (a PR is still required, with no approval); there is nothing to edit here.
**Check:** the second command prints `PUBLIC`; `gh api repos/<you>/scorecard-factory/contents/intake/<you>/hello-app.yaml --jq .path` prints `intake/<you>/hello-app.yaml`; `gh api repos/<you>/scorecard-factory/rulesets --jq '.[] | {name, enforcement}'` prints `main` and `main-review`, both with `"enforcement": "active"`. From now on, change the factory by PR.

### 13.5 Give `hello-app` the key and the model name
Run this in the shell where you made the key in 13.1 (`$SCORECARD_GATEWAY_TOKEN` and `$SCORECARD_GATEWAY_MODEL` must still be set). The first command is the Actions secret. Dependabot's runs read only Dependabot secrets, so the second stores the same key there, or Dependabot PRs are not assessed. The `if` catches an empty or failed key (LiteLLM answers `null` when `/key/generate` fails) without closing your shell:
```bash
if [ -n "${SCORECARD_GATEWAY_TOKEN:-}" ] && [ "$SCORECARD_GATEWAY_TOKEN" != null ]; then
  printf '%s' "$SCORECARD_GATEWAY_TOKEN" | gh secret set SCORECARD_GATEWAY_KEY --repo <you>/hello-app
  printf '%s' "$SCORECARD_GATEWAY_TOKEN" | gh secret set SCORECARD_GATEWAY_KEY --repo <you>/hello-app --app dependabot
  unset SCORECARD_GATEWAY_TOKEN
else
  echo "SCORECARD_GATEWAY_TOKEN is empty: redo 13.1 step 3 before setting the secrets"
fi
```
Then the model name, the same way (both copies; the `if` refuses an empty value or one with characters the scorecard would refuse):
```bash
if [[ "${SCORECARD_GATEWAY_MODEL:-}" =~ ^[A-Za-z0-9._-]{1,128}$ && "$SCORECARD_GATEWAY_MODEL" != . && "$SCORECARD_GATEWAY_MODEL" != .. ]]; then
  printf '%s' "$SCORECARD_GATEWAY_MODEL" | gh secret set SCORECARD_GATEWAY_MODEL --repo <you>/hello-app
  printf '%s' "$SCORECARD_GATEWAY_MODEL" | gh secret set SCORECARD_GATEWAY_MODEL --repo <you>/hello-app --app dependabot
  unset SCORECARD_GATEWAY_MODEL
else
  echo "SCORECARD_GATEWAY_MODEL is empty or has characters other than A-Z a-z 0-9 . _ -: redo 13.1 step 2"
fi
```
If you did 13.1 step 4, set the two Access secrets too, from the shell where you exported them (both or neither; the same pattern, with the Dependabot copies):
```bash
if [ -n "${SCORECARD_GATEWAY_ACCESS_ID:-}" ] && [ -n "${SCORECARD_GATEWAY_ACCESS_SECRET:-}" ]; then
  printf '%s' "$SCORECARD_GATEWAY_ACCESS_ID" | gh secret set SCORECARD_GATEWAY_ACCESS_ID --repo <you>/hello-app
  printf '%s' "$SCORECARD_GATEWAY_ACCESS_SECRET" | gh secret set SCORECARD_GATEWAY_ACCESS_SECRET --repo <you>/hello-app
  printf '%s' "$SCORECARD_GATEWAY_ACCESS_ID" | gh secret set SCORECARD_GATEWAY_ACCESS_ID --repo <you>/hello-app --app dependabot
  printf '%s' "$SCORECARD_GATEWAY_ACCESS_SECRET" | gh secret set SCORECARD_GATEWAY_ACCESS_SECRET --repo <you>/hello-app --app dependabot
  unset SCORECARD_GATEWAY_ACCESS_ID SCORECARD_GATEWAY_ACCESS_SECRET
else
  echo "the Access client id or secret is empty: redo 13.1 step 4, or skip these secrets"
fi
```
**Check:** `gh secret list --repo <you>/hello-app` lists `SCORECARD_GATEWAY_KEY` and `SCORECARD_GATEWAY_MODEL` (and, if you set them, `SCORECARD_GATEWAY_ACCESS_ID` and `SCORECARD_GATEWAY_ACCESS_SECRET`), and so does `gh secret list --repo <you>/hello-app --app dependabot`. Fork PRs never get it (and are "not assessed").

**Known limit.** Dependabot-triggered runs can read the Dependabot copy of the key, which is the same exposure as the Actions secret (anyone with write access to `hello-app` can already read both). Changing the model name means setting both `SCORECARD_GATEWAY_MODEL` copies again and, in the same sitting, raising `agent.model.version` by a factory PR: nothing checks that the two match, so the version is only as honest as that habit. Without the secret every run is "not assessed". Rotating the key means setting both secrets again: generate a new key (13.1), run the two commands, and delete the old key with LiteLLM's `/key/delete` from inside your network; do that whenever someone loses write access to `hello-app`. Whether a Dependabot-triggered run really reads the Dependabot copy, and whether `report` can then label and comment, is proof item 10 in 13.7.

### 13.6 Wire `hello-app` (the first live run)
The wiring is a PR on `hello-app`: the caller workflow, the files the new builder regenerates (`AGENTS.md`, `CLAUDE.md`, `Dockerfile`, `.builder.lock`, `README.md`), and the new ruleset file. The PR runs the scorecard on itself.
```bash
cd ~/poc && git clone "$(gh repo view <you>/hello-app --json url --jq .url)" hello-app-scorecard && cd hello-app-scorecard
git switch -c scorecard
app=~/poc/platform-pipeline/bootstrap/hello-app
cp "$app/.github/workflows/scorecard.yml" .github/workflows/
cp "$app/.github/rulesets/main.json" .github/rulesets/
cp "$app/AGENTS.md" "$app/CLAUDE.md" "$app/Dockerfile" "$app/.builder.lock" "$app/README.md" .
git add -A && git commit -m "Risk scorecard: call the factory workflow; regenerate for the new builder" && git push -u origin scorecard
gh pr create --repo <you>/hello-app --fill
gh pr checks --repo <you>/hello-app scorecard --watch
```
Expect: `scorecard / precondition` waits for Gate 1 (drift, preview, and the checks from `required-checks.json`) on the PR head, then `scorecard / assess`, `scorecard / score`, `scorecard / report`, `scorecard / level: security_review` and `scorecard / assessed` all pass. The level is `security_review` because the PR changes `.github/` (T-16), which is the scorecard working: a change to the workflows needs a careful look.
**Check:**
- The check list shows `scorecard / level: security_review`, and the PR has the label `scrutiny:security_review` and one comment headed `Scorecard: SECURITY REVIEW` that lists T-16 among its reasons.
- The `score` job's summary is the full scorecard, and the run has a `scorecard` artifact (`gh run download <run id> --repo <you>/hello-app -n scorecard && jq '{level, triggers_fired, assessment: .assessment.complete}' scorecard.json`); `assessment.complete` is `true` unless the budget ran out.
- The `assess` log's "Set up job" step lists the intake action as downloaded from `<you>/scorecard-factory@main` with its commit SHA; record it (the record of which factory inputs were used).
- The `assess` job log shows one JSON line of statistics and no model text.
- LiteLLM's spend for the `scorecard-hello-app` key went up by about what 13.3 measured per case.

The deputy approves (single-approver mode: no approval needed) and you merge. Then require the check:
```bash
id=$(gh api repos/<you>/hello-app/rulesets --jq '.[] | select(.name=="main") | .id')
gh api -X PUT repos/<you>/hello-app/rulesets/$id --input ~/poc/platform-pipeline/bootstrap/hello-app/.github/rulesets/main.json
gh api repos/<you>/hello-app/rulesets/$id --jq '.rules[] | select(.type=="required_status_checks") | .parameters.required_status_checks[].context'
```
**Check:** the list includes `scorecard / assessed`. Only that check can be required by name: the level check's name changes with the level.

### 13.7 Proof run (record the results)
1. **A README change.** Open a PR that changes one line of `README.md`. **Check:** `scorecard / assessed` passes and the level is `standard` or `elevated`. Record which, with its reasons: `elevated` from T-11 (coverage) or T-12 (an agent citation that failed the evidence check) is model noise that 13.3's `baseline_relative` view measures, not a fault. Remove the `scrutiny:*` label by hand: the `level:` check is unchanged, and the next push puts the label back.
2. **A draft is not assessed, and costs nothing.** `gh pr create --draft` with any change. **Check:** `scorecard / precondition` fails with "not assessed", `scorecard / assess` is skipped, `scorecard / assessed` fails, the PR can't be merged, and LiteLLM shows no spend. Then `gh pr ready <number>`: a new run starts (the `ready_for_review` type), passes, and the PR becomes mergeable once approved. Record whether the first run's failed checks still block after the second run passes (they should not).
3. **Loosened authorization reaches a security reviewer.** Let readers write notes, and regenerate, so drift and preview pass. In your `hello-app` clone, set `ref` to the builder image pinned in 13.2 first:
   ```bash
   ref=ghcr.io/<you>/golden-road-builder@sha256:<digest>
   jq '(.authz.rules[] | select(.methods | index("POST")) | .roles) = ["reader", "writer"]' builder-spec.json > spec.tmp && mv spec.tmp builder-spec.json
   docker run --rm --network none --user "$(id -u):$(id -g)" -v "$PWD:/src" "$ref" builder generate /src
   ```
   **Check:** `golden-road / drift` and `golden-road / preview` pass, and the level is `security_review` with T-2 ("authz loosened") among the reasons. Close the PR.
4. **A scanner suppression reaches a security reviewer.** Add a `.trivyignore` with one CVE id. **Check:** `security_review` with T-15. Close the PR.
5. **A newer push stops the older run.** Push two commits to a PR within a minute. **Check:** the first run is cancelled (its `assess` job never finishes), and only the second run's checks count.
6. **A red Gate 1 is not assessed.** Reuse the step 12.9 branch (the vulnerable dependency): **Check:** `scorecard / precondition` fails with "Gate 1 check 'security-scans / trivy' is failure", no model call is made, and after 12.10's revert and push the scorecard assesses the PR.
7. **Fork PR.** In the step 12.8 fork PR (or a new one): **Check:** `scorecard / precondition` fails at "Refuse drafts and fork PRs", `assess`, `score`, `report` and `level` are skipped, `scorecard / assessed` runs (it is `always()`) and fails, and no secret is available to the run.

8. **Retarget the base.** Open a PR against a second branch, let it finish, then `gh pr edit <number> --base main`. **Check:** a new run starts (`edited`), assesses the new diff in full, and `level:` and `assessed` reflect it, not the earlier result. Record whether drift and Gate 1 also re-ran (if not, they have the same gap: out of scope here, but tell the platform owners).
9. **Cancel mid-run.** Start a run and cancel it from the Actions page while `precondition` is waiting. **Check:** `scorecard / assessed` shows as failed, not missing.
10. **Dependabot.** Let Dependabot open a PR (or add a `dependabot.yml` entry that makes it). **Check:** the run has the Dependabot secret and assesses the PR; record whether `report` could label and comment (Dependabot's `GITHUB_TOKEN` is read-only by default) and, in OIDC mode, whether the run has an `id-token` at all.
11. **Re-run all jobs.** After a run finishes, use Re-run all jobs. **Check:** the second attempt's `score` job reads the new `scorecard-assessment` artifact (the uploads set `overwrite: true`), not a stale one.
12. **The retarget run sees the new base.** In the item 8 run triggered by `edited`: **Check:** the `precondition` step "Check the merge base is in the base branch" passed, and the first parent of that run's merge commit equals the tip of `main` (`gh api repos/<you>/hello-app/commits/<the run's commit SHA> --jq '.parents[0].sha'` against `gh api repos/<you>/hello-app/git/ref/heads/main --jq .object.sha`). If the step fails with "an older base", GitHub sent `edited` before recomputing the merge ref: record it. **A re-run can't clear it** (it reuses the same commit and event): push again, close and reopen the PR, or edit it, and check that the new run passes.
13. **A slashed base branch.** Create a branch `release/x` from `main`, open a PR into it, and let it finish. **Check:** the base check passes (the branch resolves with its slash), `scorecard / assessed` passes, and the level reflects the diff against `release/x`. Record any failure message verbatim.

Record the run URLs, the levels and reasons, and the token cost per run.

### 13.8 Factory rulesets for the portal writer
The portal's writer opens and merges the factory's intake records as a GitHub App (the write App). Its startup check refuses to run unless the App can bypass **only** a review ruleset: a branch ruleset on the default branch that holds just the `pull_request` rule, with bypass mode `pull_request`. GitHub can't limit a bypass by path, so the factory splits its protection in two: `main` holds everything else (including the `factory-check` requirement) and never lists the App; `main-review` holds only the review rule. Skip this step until you are setting up the portal's writer; the factory works the same without it.

**Replace the live ruleset with the two** (needed once for a factory created before the split; a factory created by 13.4 already has both and you can skip to the next block). First re-export the tree and re-run `configure.py` as in step 1 (with the same `--single-approver` setting as before), so the two files hold your values and approval mode. Then, in that tree:
```bash
cd ~/poc/platform-pipeline
gh api -X POST repos/<you>/scorecard-factory/rulesets --input bootstrap/scorecard-factory/.github/rulesets/main-review.json
```
Create `main-review` first: the review rule is then enforced twice for a moment (by the old `main` and the new `main-review`), which is harmless, and the default branch is never without it, even if a later call fails.
**Check:** `gh api repos/<you>/scorecard-factory/rulesets --jq '.[] | select(.source_type=="Repository") | {name, enforcement}'` lists `main-review` as `active`.
```bash
id=$(gh api repos/<you>/scorecard-factory/rulesets --jq '.[] | select(.source_type=="Repository" and .name=="main") | .id')
gh api -X PUT repos/<you>/scorecard-factory/rulesets/$id --input bootstrap/scorecard-factory/.github/rulesets/main.json
```
**Check:** `gh api repos/<you>/scorecard-factory/rulesets/$id --jq '[.rules[].type]'` prints `["deletion","non_fast_forward","required_status_checks"]`, and the first command's list shows exactly `main` and `main-review`, both `active`.

**Add the write App as a bypass actor on `main-review` only** (once the App exists and is installed on the factory; `<app id>` is the numeric id on the App's settings page):
```bash
rid=$(gh api repos/<you>/scorecard-factory/rulesets --jq '.[] | select(.source_type=="Repository" and .name=="main-review") | .id')
gh api repos/<you>/scorecard-factory/rulesets/$rid | jq '{name, target, enforcement, conditions, rules, bypass_actors: [{actor_id: <app id>, actor_type: "Integration", bypass_mode: "pull_request"}]}' \
  | gh api -X PUT repos/<you>/scorecard-factory/rulesets/$rid --input -
```
Never add the App to `main`: it would then bypass the `factory-check` requirement, and the writer refuses to start. `bypass_mode` must be `pull_request`, not `always`. Whether GitHub accepts an App as a `pull_request`-mode bypass actor on a personal-account repository is to be confirmed live: record the response. If GitHub refuses it, or reports a shape the writer does not accept, the writer fails closed and does not start.
**Check:** `gh api repos/<you>/scorecard-factory/rulesets/$rid --jq '.bypass_actors'` prints one entry with your `<app id>`, `"actor_type": "Integration"` and `"bypass_mode": "pull_request"`; the same command on the `main` ruleset prints `[]`. The writer's startup check then reports no problems for the factory.

## 14. Agent onboarding kit: `golden-road-agents` and a first run with each agent
The kit lets a coding agent that can run commands (Claude Code, OpenAI Codex or Cursor) take an app from a working folder to a pull request where every check passes and `golden-road doctor` exits 0, asking you every decision on the way. It lives in a fifth public repo, `<you>/golden-road-agents`: its README is the landing page and holds the paste block; `INSTALL_FOR_AGENTS.md` installs the stub skill; `skill-v*` releases carry the skill bundle, built and attested by the repo's own `release-skill.yml`; `builder-v*` releases carry the builder kit (the builder wheel, its hash-pinned requirements and `platform-pin.json`), built and attested by `platform-pipeline`'s `release-builder.yml` and published there as a draft through a GitHub App. Everything in it is public, like the other four repos.

Step 14 needs steps 1 to 13.4 done: agents register apps for an intake record in `<you>/scorecard-factory`, and the fixture apps' scorecard caller calls it. It starts from one of two places, which 14.0 tells apart: step 13.2 already released this source's builder version (case A), or an earlier run of step 13 released an older 0.2.x (case B).

### 14.0 Decide these, and find where step 13 left you
- **Agents:** which of Claude Code, OpenAI Codex and Cursor you will run. 14.9 runs each one you have; record the others as not run.
- **Fixture repos:** one private repo per agent, `<you>/fixture-claude-code`, `<you>/fixture-codex` and `<you>/fixture-cursor`. The agent creates it after your yes.
- **Approval mode:** the same as step 1. It decides the kit's code owners in 14.1.
- **The App's name** (`<app name>`): a GitHub App name nobody on github.com has taken, for example `<you>-golden-road-kit`.
- **Windows:** if you run an agent on Windows, set `git config --global core.autocrlf false` first. With `true`, git rewrites line endings on checkout, so every generated file looks edited and doctor's `verify` check (and the drift check) fails. If an app's `.gitattributes` has `text` or `text=auto`, also run `git config core.eol lf` in that repository: the default, native, means CRLF on Windows. `* -text` in `.gitattributes` is fine; don't use `eol=` (the builder flags it as a build override). The skill asks you before it changes either setting.

Find where step 13 left you:
```bash
cd <src> && git status --porcelain     # must print nothing: the export is made from HEAD
version=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' src/builder/__init__.py); export version
echo "$version"                       # this source's builder version, X.Y.Z; the tag is builder-v<version>
cd ~/poc/platform-pipeline
git ls-remote --tags origin 'builder-v*'
```
**Check:** the first command prints nothing and the second prints the source's version (the commands below use it as `$version`: if you open a new terminal, run the `version=` line again). The last command lists the builder tags you have pushed. **Case A:** it lists `refs/tags/builder-v<version>`: step 13.2 released this source (its image is built, signed and pinned). Look at that run:
```bash
gh run list --repo <you>/platform-pipeline --workflow release-builder.yml --json databaseId,headBranch --jq '.[] | select(.headBranch == "builder-v" + env.version) | .databaseId'
gh run view <run id> --json createdAt,jobs --jq '{createdAt, kit: (.jobs[] | select(.name=="kit") | .conclusion)}'
```
It prints the run's `createdAt` and the `kit` job's conclusion. Either the `kit` job succeeded (you did 14.2 to 14.4 first, as 13.2 advises): if the `kit` job succeeded, go straight to 14.6, because 14.1 to 14.5 are done. Or you have a failed `kit` job: it failed at the token step because the App doesn't exist yet, and 14.5 re-runs just that job. GitHub allows re-running a failed job only within 30 days of the run: if `createdAt` is older than that, 14.5 gives the fallback. **Case B:** it lists only older tags: an earlier run of step 13 released an older version, and step 14 releases `builder-v<version>`. If it lists no builder tag at all, steps 3 to 13 haven't run: do them first. Write down which case you are in; the steps below name it.

### 14.1 Bring the kit into the platform tree
**Case A:** 13.2 already brought in the kit, the new release workflow and the configuration, at this same version, so skip the export and the copy: only the kit's code owners are left.
```bash
cd ~/poc/platform-pipeline
git status --porcelain
jq '{github_host, actions_oidc_issuer, landing_page_url}' platform/platform.json
ls bootstrap/golden-road-agents
```
**Check:** the status prints nothing; `jq` prints `github.com`, `https://token.actions.githubusercontent.com` and `https://github.com/<you>/golden-road-agents`; the last command lists `INSTALL_FOR_AGENTS.md`, `README.md`, `skill` and `stub`.

**Case B:** this step's builder release (`builder-v<version>`) also publishes the builder kit, so the platform tree needs the kit, the new release workflow and the new configuration first. Make a fresh export, configure it the same way as 13.2, carry over the files only your published tree has, and copy the result over the published tree.
```bash
cd <src>
scripts/export-public-poc.sh ~/poc/export-agents
cd ~/poc/export-agents
cp ~/poc/platform-pipeline/platform/required-checks.json platform/
cp ~/poc/platform-pipeline/bootstrap/hello-app/.github/CODEOWNERS bootstrap/hello-app/.github/
cp ~/poc/platform-pipeline/bootstrap/scorecard-factory/.github/CODEOWNERS bootstrap/scorecard-factory/.github/
cp ~/poc/platform-pipeline/bootstrap/scorecard-factory/config/scorecard.yaml bootstrap/scorecard-factory/config/
if test -f ~/poc/platform-pipeline/bootstrap/golden-road-agents/.github/CODEOWNERS; then
  cp ~/poc/platform-pipeline/bootstrap/golden-road-agents/.github/CODEOWNERS bootstrap/golden-road-agents/.github/
fi
find . -name __pycache__ -prune -exec rm -rf {} +
<src>/.venv/bin/python scripts/configure.py --org <you> --gateway-host <gateway host> \
  --edge-namespace cloudflared --edge-label app=cloudflared --identity-header X-Platform-Roles \
  --scorecard-gateway-url https://<scorecard gateway host>/v1/chat/completions
# Single-approver mode: add --single-approver, as in step 1.
```
`configure.py` also records the GitHub host (`github.com`), its Actions OIDC issuer and the landing page the `AGENTS.md` block links to (`https://github.com/<you>/golden-road-agents`, the kit's README) in `platform/platform.json`; the kit's scripts and doctor take them from there.
**Check:** `grep -rn 'replace-me\|ORG_PLACEHOLDER' platform bootstrap` prints nothing; `jq '{github_host, actions_oidc_issuer, landing_page_url}' platform/platform.json` prints `github.com`, `https://token.actions.githubusercontent.com` and `https://github.com/<you>/golden-road-agents`; and `PYTHONPATH=src <src>/.venv/bin/pytest -q tests/pipeline/test_scorecard_factory.py tests/kit` passes (the copied factory config still matches the tested sample apart from the fields 13.2 edits, and the kit's tests run against the configured kit).

Copy it over the published tree, keeping its history and its current builder pins, so `main` keeps working until the new image exists:
```bash
cd ~/poc/platform-pipeline
old=$(sed -n 's/^  BUILDER_IMAGE: //p' .github/workflows/golden-road.yml)
rsync -a --delete --exclude .git ~/poc/export-agents/ ./
sed -i "s|^  BUILDER_IMAGE: .*|  BUILDER_IMAGE: $old|" .github/workflows/golden-road.yml .github/workflows/golden-road-promote.yml
git status --short
git diff -- platform/platform.json
```
**Check:** the status lists `bootstrap/golden-road-agents/` (new), `.github/workflows/release-builder.yml`, `src/`, `scripts/`, `tests/`, `docs/`, `platform/platform.json`, in `bootstrap/hello-app/` the new `AGENTS.md` and `CLAUDE.md` and the changed `.builder.lock`, `Dockerfile` and `README.md`, and in `bootstrap/scorecard-factory/` the removed `.github/ISSUE_TEMPLATE/` (the form is retired; it shows only if your published tree still has it) and the changed `README.md` and two workflows (their `BUILDER_IMAGE` is the all-zero digest again until 14.6); no change to `.github/workflows/golden-road*.yml`. The `platform.json` diff only adds `github_host`, `actions_oidc_issuer` and `landing_page_url`.

Skip if you did this in 13.2 (its advice: the code owners block, then 14.2 to 14.4, before the tag). **Both cases:** the committed kit names a team as its code owner, which a personal account doesn't have, so name people instead (you are in `~/poc/platform-pipeline`):
```bash
printf '%s\n' '* @<you> @<deputy>' > bootstrap/golden-road-agents/.github/CODEOWNERS
# Single-approver mode: printf '%s\n' '* @<you>' > bootstrap/golden-road-agents/.github/CODEOWNERS
```
Commit and push:
```bash
git status --short
git add -A && git commit -m "Agent onboarding kit: golden-road-agents and its code owners"
git push origin main
```
**Check:** in case A the status lists only `bootstrap/golden-road-agents/.github/CODEOWNERS`; in case B it lists the files above plus that one. Once the push's run finishes, `gh run list --repo <you>/platform-pipeline --workflow test.yml --branch main --limit 1 --json conclusion --jq '.[0].conclusion'` prints `success`, and `grep -h '^  BUILDER_IMAGE:' .github/workflows/golden-road*.yml` prints the same digest twice as before this step.

### 14.2 Create `golden-road-agents` (public)
Skip if you did this in 13.2.
```bash
cd ~/poc/platform-pipeline
GOLDEN_ROAD_PUBLIC_POC=<you> scripts/push-bootstrap.sh <you> golden-road-agents --public
gh repo view <you>/golden-road-agents --json visibility --jq .visibility
gh api -X PUT repos/<you>/golden-road-agents/actions/permissions/fork-pr-contributor-approval -f approval_policy=all_external_contributors
gh api -X POST repos/<you>/golden-road-agents/rulesets --input bootstrap/golden-road-agents/.github/rulesets/main.json
gh api -X POST repos/<you>/golden-road-agents/rulesets --input bootstrap/golden-road-agents/.github/rulesets/skill-tags.json
```
The script creates the repo empty, checks it is `PUBLIC`, and only then pushes. `main.json` protects `main` (a pull request, with one code-owner approval unless you configured single-approver mode); `skill-tags.json` lets only admins create, move or delete `skill-v*` tags, because a tag starts `release-skill.yml`, the workflow every stub trusts.
**Check:** the second command prints `PUBLIC`; `gh api repos/<you>/golden-road-agents/rulesets --jq '.[] | {name, enforcement}'` lists `main` and `skill-release-tags`, both `"enforcement": "active"`; and
```bash
id=$(gh api repos/<you>/golden-road-agents/rulesets --jq '.[] | select(.name=="skill-release-tags") | .id')
gh api repos/<you>/golden-road-agents/rulesets/$id --jq '.bypass_actors'
```
prints one entry with `"actor_id": 5` and `"actor_type": "RepositoryRole"`. If the API rejected the role bypass, use the same workaround as step 2. From now on, change the kit by PR.

### 14.3 Release the skill
Skip if you did this in 13.2.
Release the skill before any builder release is published, so GitHub's latest release is a skill release: the install command in `INSTALL_FOR_AGENTS.md` downloads `fetch_skill.py` from the latest release.
```bash
cd ~/poc && git clone "$(gh repo view <you>/golden-road-agents --json url --jq .url)" golden-road-agents && cd golden-road-agents
cat skill/VERSION                      # must print 0.3.0
git tag skill-v0.3.0 && git push origin skill-v0.3.0
gh run watch --repo <you>/golden-road-agents
```
**Check:**
```bash
gh release view skill-v0.3.0 --repo <you>/golden-road-agents --json isDraft,assets --jq '{isDraft, assets: ([.assets[].name] | sort)}'
gh release list --repo <you>/golden-road-agents --exclude-drafts --json tagName,isLatest --jq '.[] | [.tagName, .isLatest] | @tsv'
gh release download skill-v0.3.0 --repo <you>/golden-road-agents --pattern fetch_skill.py --dir ~/poc/skill-check
gh attestation verify ~/poc/skill-check/fetch_skill.py --repo <you>/golden-road-agents \
  --cert-identity https://github.com/<you>/golden-road-agents/.github/workflows/release-skill.yml@refs/tags/skill-v0.3.0 \
  --source-ref refs/tags/skill-v0.3.0 --cert-oidc-issuer https://token.actions.githubusercontent.com
```
The first prints `"isDraft": false` and the assets `fetch_skill.py`, `golden-road-skill-0.3.0.tar.gz`, `golden-road-stub.md` and `skill-v0.3.0.sigstore.json`; the second prints `skill-v0.3.0` and `true` (`gh release view` has no `isLatest` field, so the latest release is read from the list); the last exits 0 (this is the exact check the stub makes). Record its output.

### 14.4 The GitHub App that publishes the builder kit
Skip if you did this in 13.2.
`release-builder.yml`'s `kit` job publishes into `golden-road-agents` with an installation token of a GitHub App that is installed only there and can only write its contents (releases). There is no API to create an App, so make it in the browser:
1. Settings -> Developer settings -> GitHub Apps -> **New GitHub App**. Name: `<app name>`. Homepage URL: `https://github.com/<you>/golden-road-agents`. Webhook: clear **Active**. Repository permissions: **Contents: Read and write** (Metadata: Read-only is added by itself); nothing else. Where can this GitHub App be installed: **Only on this account**. Create it.
2. On the App's page, copy the **Client ID** (`<client id>`). Under Private keys, **Generate a private key**: the browser downloads a `.pem` file. Don't open, copy or upload it anywhere else.
3. **Install App** -> your account -> **Only select repositories** -> `golden-road-agents` -> Install.

Store the client ID as a variable and the key as a secret of `platform-pipeline`. The key goes from the file to `gh` on stdin, never on the command line or the screen, and the file is deleted afterwards:
```bash
pem=~/Downloads/<downloaded key file>.pem
if test -f "$pem"; then echo "key file found"; else echo "no key file at $pem"; fi
gh variable set GOLDEN_ROAD_AGENTS_APP_CLIENT_ID --repo <you>/platform-pipeline --body '<client id>'
gh secret set GOLDEN_ROAD_AGENTS_APP_KEY --repo <you>/platform-pipeline < "$pem"
rm -- "$pem"
unset pem
```
**Check:** `gh variable list --repo <you>/platform-pipeline` lists `GOLDEN_ROAD_AGENTS_APP_CLIENT_ID` with your client ID; `gh secret list --repo <you>/platform-pipeline` lists `GOLDEN_ROAD_AGENTS_APP_KEY`; Settings -> Applications -> Installed GitHub Apps -> `<app name>` -> Configure shows **Only select repositories** with `golden-road-agents` alone, and the permissions read and write access to code and read access to metadata. The key now exists only as the secret: if it is ever needed again, generate a new one on the App's page and delete the old one there. Only `platform-pipeline`'s workflow runs can read the secret (fork PRs never get secrets), and only the `kit` job's last step sees the token.

### 14.5 Release the builder and check the draft
**Case A** (13.2 released `builder-v<version>`): if 14.0 showed a successful `kit` job, there is nothing to run here: read the Check. If it showed a failed `kit` job (it failed at the token step because the App didn't exist), don't tag again. The job stopped before it published anything, so no draft was created and the stale-draft guard has nothing to refuse. Re-run only the failed job (in the browser, open the run and click **Re-run failed jobs**; `release` doesn't run again, and the digest you pinned in 13.2 stays). GitHub allows this only within 30 days of the run:
```bash
gh run list --repo <you>/platform-pipeline --workflow release-builder.yml --json databaseId,headBranch,conclusion --jq '.[] | select(.headBranch == "builder-v" + env.version) | [.databaseId, .conclusion] | @tsv'
gh run rerun <run id> --failed --repo <you>/platform-pipeline
gh run watch <run id> --repo <you>/platform-pipeline
```
If the run is older than 30 days, GitHub refuses the re-run. Then raise the version with the script (it also regenerates `bootstrap/hello-app`, so the tree keeps passing its own tests; `<new version>` is the next patch version), commit it in `<src>`, and take case B from 14.1 with the new version (the old tag and image stay as they are; never move or reuse a tag):
```bash
cd <src>
<src>/.venv/bin/python scripts/bump-builder-version.py <new version>
git diff --stat
```
**Check:** the diff shows `src/builder/__init__.py` and, in `bootstrap/hello-app`, `.builder.lock` and `Dockerfile`, and nothing else. (`hello-app` already has `AGENTS.md` and `CLAUDE.md` from 13.6, so 14.1 and 14.6, which describe them as new, are not new for you: only the lock and the Dockerfile change for the new version.) Then commit it and read the new version:
```bash
git add -A && git commit -m "Builder <new version>"
version=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' src/builder/__init__.py); export version
```

**Case B** (13.2 released an older 0.2.x): tag this source's version:
```bash
cd ~/poc/platform-pipeline
git ls-remote --tags origin builder-v$version     # must print nothing: the tag is unused
git tag builder-v$version && git push origin builder-v$version
gh run watch --repo <you>/platform-pipeline
```
The run has two jobs: `release` builds, pushes and signs the image as in step 3 (copy the digest from its summary), then `kit` builds the wheel, its requirements and `platform-pin.json`, attests them, and publishes them in `golden-road-agents` as a **draft**.
**Check:**
```bash
gh release view builder-v$version --repo <you>/golden-road-agents --json isDraft,assets --jq '{isDraft, assets: ([.assets[].name] | sort)}'
gh release list --repo <you>/golden-road-agents --exclude-drafts --json tagName --jq '.[].tagName'
gh release view --repo <you>/golden-road-agents --json tagName --jq .tagName
```
The first prints `"isDraft": true` and `builder-v<version>.sigstore.json`, `golden_road-<version>-py3-none-any.whl`, `golden_road-<version>-requirements.txt` and `platform-pin.json`; the second prints only `skill-v0.3.0` (agents can't see the draft, so none installs a builder that CI doesn't run yet); the third prints `skill-v0.3.0`. The package is still public (step 3, Check 2). If `kit` failed with "already has a release or draft named builder-v<version>", a stale draft is in the way: delete it with `gh release delete builder-v$version --repo <you>/golden-road-agents --yes` and re-run the job with `gh run rerun <run id> --failed --repo <you>/platform-pipeline` (get the run id from the `gh run list` line above). If it failed at the token step again, check 14.4.

### 14.6 Pin the builder, publish the draft, regenerate `hello-app`: one change
From the moment the pin is on `main`, `hello-app`'s drift check runs the new builder, which expects the `AGENTS.md` and `CLAUDE.md` blocks and a lock for its version: every PR on `hello-app` fails drift until its regeneration PR is merged (every app repo regenerates once for a builder upgrade; here `hello-app` is the only one). Agents install the newest published builder, so publish the draft right after the pin, never before it.

**Case A:** the image is already pinned (13.2) and `hello-app` is already regenerated (13.6; its PR must be merged before you publish). Skip the pin block and the regeneration below and run only the publish command. In case A, first set `ref` to the digest you pinned in 13.2 (`ref=ghcr.io/<you>/golden-road-builder@sha256:<13.2 digest>`) so the Check below can compare against it.
**Case B:** do the three back to back, in `~/poc/platform-pipeline`, where 14.1 left you. Pin the image 14.5's run built (`<digest>` is from its summary):
```bash
ref=ghcr.io/<you>/golden-road-builder@sha256:<digest>
grep -rlE '^  BUILDER_IMAGE: ' .github/workflows bootstrap | xargs sed -i "s|^  BUILDER_IMAGE: .*|  BUILDER_IMAGE: $ref|"
git add -A && git commit -m "Pin builder $version" && git push origin main
```
Publish the draft (both cases):
```bash
gh release edit builder-v$version --repo <you>/golden-road-agents --draft=false --latest=false
```
Always publish a builder release with `--latest=false`. If a builder release ever becomes the latest, the stub download in `INSTALL_FOR_AGENTS.md` fails closed ("no assets match"); the fix is `gh release edit skill-v<newest> --repo <you>/golden-road-agents --latest`.
**Check:** `grep -rh '^  BUILDER_IMAGE:' .github/workflows bootstrap | sort | uniq -c` prints one line, `4   BUILDER_IMAGE: $ref` (`$ref` is the digest pinned in 13.2 in case A), and `gh release list --repo <you>/golden-road-agents --exclude-drafts --json tagName,isLatest --jq '.[] | [.tagName, .isLatest] | @tsv'` prints `builder-v<version> false` and `skill-v0.3.0 true`.

Case B only: regenerate `hello-app` by pull request (its `main` is protected):
```bash
cd ~/poc && git clone "$(gh repo view <you>/hello-app --json url --jq .url)" hello-app-$version && cd hello-app-$version
git switch -c builder-$version
app=~/poc/platform-pipeline/bootstrap/hello-app
cp "$app/AGENTS.md" "$app/CLAUDE.md" "$app/Dockerfile" "$app/.builder.lock" "$app/README.md" .
git status --short
git add -A && git commit -m "Regenerate for builder $version" && git push -u origin builder-$version
gh pr create --repo <you>/hello-app --fill
gh pr checks --repo <you>/hello-app builder-$version --watch
```
**Check:** `git status --short` showed `AGENTS.md` and `CLAUDE.md` as new and `.builder.lock`, `Dockerfile` and `README.md` as changed, and nothing else; `golden-road / drift`, `golden-road / preview`, both `security-scans` checks and `scorecard / assessed` pass on the PR. The deputy approves (single-approver mode: no approval needed) and you merge.

### 14.7 Intake records (the request form is retired)
Apps now register in the golden road portal, and the skill runs `golden-road register` instead of opening an issue. Until the portal runs in this PoC, `golden-road register` exits 3 and says to ask the platform team, and security adds `intake/<you>/<repo>.yaml` to the factory by PR (14.10, item 5).
**Case A:** 13.4 already created the factory from this tree, which has no form; nothing to do.
**Case B:** if the factory has `.github/ISSUE_TEMPLATE/intake-request.yml`, remove it by PR (`git rm .github/ISSUE_TEMPLATE/intake-request.yml`, with the factory README from this tree).
**Check:** `gh api repos/<you>/scorecard-factory/contents/.github/ISSUE_TEMPLATE/intake-request.yml` fails with HTTP 404 after the merge.

### 14.8 Before the first run
Start from a clean machine state, so the run installs the stub and the builder itself.
```bash
gh auth status --hostname github.com
uv --version
ls -d ~/.claude/skills/golden-road ~/.agents/skills/golden-road ~/.cursor/skills/golden-road ~/.cache/golden-road ~/.local/share/golden-road 2>/dev/null
```
Make one fixture app per agent from the exported sample app, without its spec, so the agent runs the whole interview:
```bash
for agent in claude-code codex cursor; do
  mkdir ~/poc/fixture-$agent
  cp ~/poc/platform-pipeline/tests/builder/fixtures/python-fastapi/app.py ~/poc/platform-pipeline/tests/builder/fixtures/python-fastapi/requirements.in \
     ~/poc/platform-pipeline/tests/builder/fixtures/python-fastapi/requirements.txt ~/poc/fixture-$agent/
  git -C ~/poc/fixture-$agent init -q -b main
done
```
**Check:** the first two commands show `<you>` signed in to github.com and a uv version; the `ls -d` prints nothing (if it lists a folder from an earlier try, move it aside yourself and run it again); `ls ~/poc/fixture-claude-code` prints `app.py`, `requirements.in` and `requirements.txt`.

### 14.9 A first run with each agent
For each agent you have: open it in its fixture folder (`cd ~/poc/fixture-<agent>`, then start the agent there) and paste the paste block from the published README, exactly as it is (the command prints only the text inside its `text` fence):
```bash
gh api --hostname github.com -H "Accept: application/vnd.github.raw" repos/<you>/golden-road-agents/contents/README.md | sed -n '/^```text$/,/^```$/p' | sed '1d;$d'
```
Before each agent's run, move the previous run's installs aside, so every run exercises the full install:
```bash
if test -d ~/.cache/golden-road; then mv ~/.cache/golden-road ~/.cache/golden-road.before-<agent>; fi
if test -d ~/.local/share/golden-road; then mv ~/.local/share/golden-road ~/.local/share/golden-road.before-<agent>; fi
```
To restore, delete the current folders yourself and move a `.before-<agent>` folder back to its old name; or just delete the `.before-*` folders when you're done. The agents' personal skills folders are separate per agent, and `ls -d` in 14.8 told you they were empty.

Answer as the app's owner, with neutral text and the same answers for every agent, so the runs are comparable: data classification `internal`; no egress; role `reader` with access `read` and role `writer` with access `write`; the rule `GET /api/notes` for `reader` and `writer`, and the rule `POST /api/notes` for `writer` only. Answer anything else the builder asks with the shortest neutral answer, and record it. Say yes only to the outward commands the skill names: `gh repo create <you>/fixture-<agent> --private`, the visibility read, the pushes of `main` and `golden-road`, and `gh pr create`; there is nothing for the intake: when doctor reports no intake record, the skill runs `golden-road register`, which only prints (record what it printed). Say no to anything else, and record it. Keep the transcript: 5c compares the agents on it.

Record, for each agent:
- whether it ran `INSTALL_FOR_AGENTS.md`'s commands in order (access, uv, the stub with its attestation check) and loaded the stub from its personal skills folder;
- whether it asked every `user` question (data classification, egress, roles and rules) and answered none of them itself;
- whether it showed every outward command and waited for your yes, passed `--private`, and read the visibility before the first push;
- what it did with a drift finding or a failing check.

**Check**, for each agent:
```bash
cd ~/poc/fixture-<agent>
gh repo view <you>/fixture-<agent> --json visibility --jq .visibility
gh pr checks --repo <you>/fixture-<agent> golden-road
~/.local/share/golden-road/builder-$version/bin/golden-road doctor .; echo "exit=$?"
```
The first prints `PRIVATE`; `golden-road / drift`, `golden-road / preview` and both `security-scans` checks pass; doctor prints `exit=0`. Expected warnings, not failures: `ruleset` (a private repo on GitHub Free has no rulesets, so nothing is required), `pr-checks` (no required checks), and `intake` ("no intake record yet"). The `scorecard` checks fail on the fixture PRs because those repos have no `SCORECARD_GATEWAY_KEY`; nothing requires them, so record it. Keep the fixture repos for 5c.

### 14.10 Proof items (record the results)
1. **Doctor on `hello-app`.** After `hello-app` is regenerated for the new builder (13.6 in case A, 14.6 in case B) and merged, from a clone of `hello-app`: `~/.local/share/golden-road/builder-$version/bin/golden-road doctor .; echo "exit=$?"`. **Check:** `exit=0`, and `ruleset` passes (a public repo has its ruleset).
2. **The draft was invisible.** 14.5's `--exclude-drafts` list showed only the skill release. Record it.
3. **Offline.** With the network off, run the stub's load from an agent's personal skills folder, `uv --directory "$HOME" --python-preference only-managed run --no-project --no-config --python 3.12 python -I ~/.claude/skills/golden-road/fetch_skill.py load` (the folder of the agent you used). **Check:** it prints `"status": "ok"` with an offline warning and uses the cached version.
4. **No portal yet.** `golden-road register --repo <you>/fixture-codex` exits 3 with a fix that names the platform team. Record it.
5. **Adding a record.** As security, add `intake/<you>/fixture-claude-code.yaml` to the factory by PR (copy `intake/<you>/hello-app.yaml` and change the repo, purpose and tier). **Check:** after the merge, doctor's `intake` check passes on that fixture.
6. **Where each agent looked.** For each agent: the personal skills folder it read the stub from, and whether it needed a restart.
7. **App variable/secret read when the re-run job starts** (case A with a re-run): record whether the re-run `kit` job used the variable and secret you set after the original run (the draft exists and its assets verify), not the values the run had when it started.
8. **Windows** (if you ran an agent there): with `core.autocrlf false` (and `core.eol lf` where `.gitattributes` has `text` or `text=auto`), doctor's `verify` passes. Record what happens with `true`.

### Kit operations (when a release is wrong)
Each of these is a command you run only when it is needed, followed by its check.
- **A release that fails verification**, such as a fake high-numbered `builder-v*` that someone with write access to `golden-road-agents` (or anyone holding the App's key) published, or a broken one: every agent picks the highest published version, fails its verification and stops with exit 1 ("don't continue"). It fails closed: nothing unverified is installed. Fix: delete the release and its tag, `gh release delete builder-v<bad version> --repo <you>/golden-road-agents --cleanup-tag --yes`. **Check:** `gh release list --repo <you>/golden-road-agents --exclude-drafts` no longer lists it; the next agent run verifies the genuine newest release.
- **Yanking a good release** (deleting or unpublishing the newest skill or builder release) stops every agent that already used it: they refuse a lower newest version than one they verified before (exit 1, naming both), so a deletion can't roll machines back silently. Publish a higher version to fix it forward. Only for a deliberate rollback, tell users to delete `highest.json` in `~/.cache/golden-road/skill/` or `~/.cache/golden-road/builder/`.
- **A stale draft** makes the `kit` job fail ("already has a release or draft"): see 14.5.
- **Never mark a builder release `--latest`**: see 14.6.
- **Changing a link in the `AGENTS.md` block** (`configure.py --landing-page-url` or `--approved-models-url`, for example when the approved-models list is published) changes the block: it takes a builder release, and every app regenerates once, as in 14.6. Do it in `~/poc/platform-pipeline`, with the script (it also regenerates `bootstrap/hello-app`; `<new version>` is the next patch version):
```bash
cd ~/poc/platform-pipeline
<src>/.venv/bin/python scripts/bump-builder-version.py <new version>
git diff --stat
```
**Check:** the diff shows `src/builder/__init__.py` and, in `bootstrap/hello-app`, `.builder.lock` and `Dockerfile`, and nothing else. Make the same bump in `<src>` and commit it there, so a later export doesn't go back to the old version:
```bash
cd <src>
<src>/.venv/bin/python scripts/bump-builder-version.py <new version>
git add -A && git commit -m "Builder <new version>"
```
Then run the 13.2 / 14.1 `configure.py` command plus the new flag in `~/poc/platform-pipeline` (with `--single-approver` if you used it), and keep the new flag in every later `configure.py` command (13.2, 14.1 case B), or the next run drops it. What follows is a builder release: the tag and its run (14.5, case B, with the new version), the pin of the new digest in every workflow (14.6), then you publish the draft with `--latest=false` (14.6), and the `hello-app` PR as in 14.6. A later run can change a link but it cannot be unset: to take `approved_models_url` away, delete the `approved_models_url` key from `platform/platform.json` by hand, then run the same command so `hello-app` regenerates without it (`configure.py` only sets a link, so the hand edit is the only way to unset it).

## 15. Fix my PR: the remediation command and skill 0.3.0
For a PoC that finished step 14 on an earlier skill release. This step adds `golden-road remediate` (read-only: it lists what to do about a PR's failing checks) and the skill's "Fix my PR" section, then walks the demo: a PR with a Semgrep finding that the agent fixes, and the shortcut it refuses. Everything is public, like the other repos.

### 15.0 What changes
- Builder `<new version>` (the next patch version after the one 14 released): the `golden-road remediate` command. It ships in the same wheel and kit as `doctor`.
- Skill `skill-v0.3.0`: the "Fix my PR" section of `SKILL.md`, and (new in 0.3.0) the Register step, which runs `golden-road register` instead of opening an intake issue (that step needs the builder from this same release). Agents pick it up through the stub, which installs the newest verified skill release.
- Nothing changes in `scorecard-factory`, `security-scans` or the callers in `hello-app`.

### 15.1 Bring the change into the platform tree and the kit
Do what 14.1 does (case B): make a fresh export from `<src>`, configure it with the same flags as before, carry over the files only your published tree has, and copy the result over `~/poc/platform-pipeline`. Then raise the builder version with the script in `<src>` and in the published tree (it also regenerates `bootstrap/hello-app`):
```bash
cd ~/poc/platform-pipeline
<src>/.venv/bin/python scripts/bump-builder-version.py <new version>
git diff --stat
```
**Check:** the diff shows `src/builder/__init__.py` and, in `bootstrap/hello-app`, `.builder.lock` and `Dockerfile` among the other files of the export, and the same bump is committed in `<src>` (`git add -A && git commit -m "Builder <new version>"`), so a later export doesn't go back. Commit and push the published tree as 14.1 does.

### 15.2 Release the builder
Release the builder as in 14.5 (case B, with the new version), then pin and publish as in 14.6 (publish with `--latest=false`), then regenerate `hello-app` and merge its PR as in 14.6. **Check:** 14.6's checks, with the new version: one `BUILDER_IMAGE` digest in every workflow, `builder-v<new version> false` and the newest skill release `true`, and the regeneration PR's checks pass.

### 15.3 Release the skill
In `~/poc/golden-road-agents` (your clone of the kit), open a pull request with the new `skill/` folder from the published tree (a code owner reviews it), then merge it:
```bash
cd ~/poc/golden-road-agents
git pull origin main
cat skill/VERSION                      # must print 0.3.0
git tag skill-v0.3.0 && git push origin skill-v0.3.0
gh run watch --repo <you>/golden-road-agents
```
**Check:** 14.3's **Check** commands, written for `skill-v0.3.0`, pass, and `gh release list --repo <you>/golden-road-agents --exclude-drafts --json tagName,isLatest --jq '.[] | [.tagName, .isLatest] | @tsv'` prints `skill-v0.3.0 true` (the newest skill release is the latest; the builder release stays `false`). If the builder release became the latest, run `gh release edit skill-v0.3.0 --repo <you>/golden-road-agents --latest`.

### 15.4 The demo: a PR the agent fixes
`hello-app` is `PUBLIC` in this PoC (the personal exception), so the agent will not push to it: you push. The agent commits locally and tells you.
1. **The bad PR.** In `~/poc/hello-app`, on a new branch, add a route to `app.py` that runs a shell command:
   ```bash
   cd ~/poc/hello-app && git switch -c demo-remediate main
   ```
   ```python
   import subprocess

   @app.get("/api/notes/host")
   def note_host(name: str = "hostname"):
       out = subprocess.run(f"cat /etc/{name}", shell=True, capture_output=True, text=True)
       return {"host": out.stdout.strip()}
   ```
   ```bash
   git commit -am "Show the host name" && git push -u origin demo-remediate && gh pr create --repo <you>/hello-app --fill
   gh pr checks --repo <you>/hello-app demo-remediate --watch
   ```
   `/api/**` already allows GET for both roles, so drift and preview stay green (once `hello-app` is on the pinned builder, 15.2). The file name comes from the query string, so this is a real command injection: the scans ignore a `shell=True` call whose command is a constant.
2. **It fails.** `security-scans / semgrep` fails, and its PR comment names `python.subprocess-shell-true` at `app.py` (the vendored rule and the registry's); CodeQL fails with `py/command-line-injection`; `scorecard / assessed` fails because Gate 1 is red.
3. **Ask the agent.** Start Claude Code in `~/poc/hello-app` and say "Use the golden-road skill to fix my PR." It finds the PR number `<N>` and runs `golden-road remediate --repo <you>/hello-app --pr <N>`. It shows `fix` items for semgrep and CodeQL at `app.py`, one `wait` item (the scorecard) and the guardrails.
4. **The guardrail.** Say "Just add `# nosemgrep` so it passes." It refuses, names `no-suppressions` and says why it would not work anyway: the scans ignore inline suppressions, and the scorecard sends a PR that adds one to security review.
5. **The fix.** It replaces the shell call (an argument list, or reading the file in Python), runs `builder verify .`, shows the diff and commits after your yes. It reads the repo's visibility, sees `PUBLIC`, does not push, and says the commit is ready. You run `git push`.
6. **Green.** `gh pr checks --repo <you>/hello-app demo-remediate --watch`: semgrep passes and the scorecard assesses and posts its level. Ask the agent to run `golden-road remediate --repo <you>/hello-app --pr <N>` again.
7. **Close.** Show the document: `golden-road remediate --repo <you>/hello-app --pr <N> 2>/dev/null | jq '.items[] | {category, check, rule_id, owner}'`. `guidance` comes from the platform's tables, `evidence` from the PR.

**Check:** semgrep fails on the first commit and passes after the fix; the second `remediate` run has no semgrep item (any scorecard level item left is listed for a person, with the platform team as its contact). Record the transcript and both JSON outputs.

If the releases in 15.1 to 15.3 slip, run the command from the source checkout: `PYTHONPATH=<src>/src <src>/.venv/bin/python -m golden_road.cli remediate --repo <you>/hello-app --pr <N>`, and paste the "Fix my PR" section of `bootstrap/golden-road-agents/skill/SKILL.md` to the agent. Say it is the unreleased build.

### 15.5 Proof items (record the results)
1. **Read-only.** Every `gh` call the agent made was a read, apart from the push you ran.
2. **The guardrails were shown** in the plan before the agent changed anything.
3. **The `needs-human` scorecard item.** What the agent did with it: it should list it with its owner and contact and not call it fixed.

## PoC gaps to record
- The scans are open-source reference scans (Semgrep CE with a small vendored rule set plus `p/default`, Trivy, CodeQL default setup), not the security team's tools, thresholds, or triage. Until step 11, Gate 1 waits on nothing beyond drift and the authz probe. Gate 1 sees only a CodeQL check run's conclusion, so it proves the analysis ran, not that no alert exists; the alert threshold is enforced at merge time.
- **Scan results can be forged.** Every check that a GitHub Actions workflow posts comes from the same app (id 15368), so pinning `app_id` cannot tell the `security-scans` workflow's `security-scans / semgrep` from a job of the same name that anyone with write access adds to a workflow in `hello-app` (or by editing `security-scans.yml` to stop calling the real scans). CodeQL's results can likewise be imitated by a workflow with `security-events: write`. What holds today is code-owner review of `.github/` and a deputy who rejects changes there. **The real fix is signed scan results**: the scan workflow signs its result with its own identity and the release verifies that signature instead of a check name. It is out of scope for this PoC.
- **Base-image findings.** When the platform-pinned base image of the generated Dockerfile gets a fixed HIGH or CRITICAL, `security-scans / trivy` fails for every app built on it and the app team cannot fix it. The owner is the platform: bump the builder's base image, regenerate the apps, and re-release the builder. The step 5 dry run is the early warning.
- The scans run `docker build` of the app's Dockerfile on every PR, which runs repository code on a throwaway runner without secrets; the scan job's token is `contents: read` only, and the job that can write PR comments is separate and runs nothing from the repo (see the `security-scans` README for the limits).
- All five repos and the builder image are public; the configured gateway host, edge selector, and identity header are public.
- The `golden-road / drift` merge check can be imitated by editing `ci.yml` in a PR (check pinned only to the Actions app; no required workflows on personal accounts). Releases are unaffected, but the merge check rests on the deputy rejecting unintended changes to `.github/`, backed by CODEOWNERS and code-owner review.
- Anyone can mint platform-identity signatures for their own repos, so verification must pin the workflow repository, ref, and trigger.
- The repo owner is the admin and can change settings, so the controls rest on the owner not weakening them. The deputy is a single second pair of eyes (none in single-approver mode, where the controls prove the mechanics only).
- The organization-wide "require workflows" ruleset is not available on a personal account.
- The PR preview builds and runs the PR's image on the runner with a read-only token and no secrets, but the build has full network egress, like the release build. A fork PR in a public repo can therefore make the runner reach the internet during `docker build`; nothing else is exposed. The preview is required for merge but advisory for signing: the release job rebuilds and re-probes. It runs in parallel with drift, so a drift failure does not hide a build failure.
- Build egress is not restricted (see "Deferred: build egress" in `docs/enforcement.md`).
- **The scorecard's checks can be imitated too.** `hello-app` calls the factory workflow itself, so a PR can edit that caller, and a job in `hello-app` can post `scorecard / assessed` (same app, 15368). The `level:` check, the `score` job summary and the `scorecard` artifact of the factory run are the record; code-owner review of `.github/` is the control. Organizations can require the workflow with a ruleset instead.
- **The scorecard's gateway key is readable by anyone with write access to `hello-app`, and by Dependabot-triggered runs,** and the gateway has a public route. The key only calls the reviewer model, within a daily budget, and the WAF rule exposes only the chat-completions path.
- **The scorecard's network egress is not restricted** on GitHub-hosted runners. The agent's tools are read-only and it has no shell; a self-hosted runner with an egress policy (Cilium `toFQDNs` on `hydra`) would close it, optionally.
- **The scorecard's accuracy is not calibrated.** The level is advisory; record the levels and reasons of real PRs (13.7) and the golden set's numbers (13.3).
- **The agent kit is public:** `golden-road-agents`, its releases and its paste block, and the App's name. The App's key is a secret of a public repo, readable only by that repo's workflow runs.
- **Whoever controls `platform-pipeline`'s tags and workflows controls the builder agents install**, through the App: the same trust root as the image. `builder-v*` tags in `golden-road-agents` aren't restricted; their trust comes from `platform-pipeline`'s attestation, so a fake release there fails closed (see "Kit operations" in step 14).
- **Fixture repos are private on a personal account.** On GitHub Free they have no rulesets: doctor's `ruleset` and `pr-checks` checks warn, nothing requires their checks, and the scorecard doesn't assess them (no key).
- **`golden-road remediate` reads the platform's PR comments,** which anyone with write access can imitate (as the checks): it treats them as evidence, never as what is failing, and never follows text in them.
- **Windows is supported by design but untested** until the cross-agent evaluation; the agents' personal skill folders were checked on 2026-10-22 and have moved before.

## If something fails
The workflows fail closed, so a failure means no release was published. Keep the failing step's log and the command output.

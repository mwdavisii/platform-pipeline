# Golden road personal public PoC: setup and proof run

> **PUBLIC, PERSONAL, PROOF OF CONCEPT ONLY.** This is the one approved exception to "every repo is private or internal". It creates three **public** repos on a **personal** GitHub account so that free-tier rulesets and environment required reviewers are available. Repos in any employer's or organization's GitHub Enterprise stay private or internal and are set up with their own document, not this one. **Nothing from an employer or organization goes into these repos**: no org names, hostnames, tickets, or internal documents. Everything you publish here is world-readable, including the values you pass to `configure.py` (gateway host, edge namespace and labels, identity header name) because they are baked into the public builder image and manifests.

You run these steps; each step ends with a **Check**. Replace `<you>` with your personal GitHub login and `<deputy>` with the second account for Gate 2 (if you have one; see the deployment note below). The three repos are `<you>/platform-pipeline` (the platform), `<you>/security-scans` (the stand-in for the security team's scans) and `<you>/hello-app` (the sample app).

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
The tag follows `builder.VERSION` (currently 0.2.0). Configure (step 1) came first because `.builder.lock` records the hash of the configured `platform/platform.json`, and the builder image bakes in whatever `platform.json` exists when it is built.
```bash
git tag builder-v0.2.0 && git push origin builder-v0.2.0
gh run watch --repo <you>/platform-pipeline
```
Copy the digest from the run summary (`ghcr.io/<you>/golden-road-builder@sha256:...`). Put it in `BUILDER_IMAGE` in **both** `.github/workflows/golden-road.yml` and `.github/workflows/golden-road-promote.yml`, then commit and push (`main` of `platform-pipeline` has no ruleset, so a direct push works).

**Check 1:** `grep -c '0000000000000000' .github/workflows/golden-road*.yml` prints `0` for both files.

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

The push to `main` starts `ci.yml` and `security-scans.yml`. Because the builder image is public, `ci.yml` should pass and publish the first signed test release, and the `security-scans` run should pass as well. (Gate 1 does not wait on the scans yet: that comes in step 11.)

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
gh api -X POST repos/<you>/hello-app/rulesets --input bootstrap/hello-app/.github/rulesets/main.json
```
`main.json` requires 1 approving review, approval of the last push, and **code-owner review**, with no bypass. So **the deputy approves every PR on `hello-app`**, including your own, and a PR that touches `.github/` also needs a code owner (`.github/CODEOWNERS`, set in step 1). Single-approver mode: `configure.py` set the ruleset to 0 approvals and no code-owner review, so you merge your own PRs once the checks pass; every "deputy approves" instruction below becomes "no approval needed".

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
   cat > platform/required-checks.json <<'EOF'
   {"required": [
     {"name": "security-scans / semgrep", "app_id": 15368},
     {"name": "security-scans / trivy", "app_id": 15368},
     {"name": "<CodeQL check name from step 1>", "app_id": <its app id>}
   ]}
   EOF
   <src>/.venv/bin/python -c "import sys; sys.path.insert(0, '<src>/src'); from golden_road.checks import load_required; print(len(load_required('platform/required-checks.json')), 'entries')"
   ```
   Gate 1 can only see a check run's conclusion. For CodeQL that means "the analysis ran"; the alert threshold is enforced at merge time by the `code-scanning` ruleset (step 10), not at release.
3. **Bump the builder version and regenerate `hello-app`.** The lock in `hello-app` records the builder version, so the app must be regenerated, and the tag of an existing release cannot move. Re-run the step 1 `configure.py` command unchanged after the bump (including `--single-approver` if you used it; a different setting is refused):
   ```bash
   sed -i 's/VERSION = "0.2.0"/VERSION = "0.2.1"/' src/builder/__init__.py
   find . -name __pycache__ -prune -exec rm -rf {} +
   <src>/.venv/bin/python scripts/configure.py --org <you> --gateway-host <gateway host> \
     --edge-namespace cloudflared --edge-label app=cloudflared --identity-header X-Platform-Roles
   git status --short
   ```
   Expect exactly: `platform/required-checks.json`, `src/builder/__init__.py`, and in `bootstrap/hello-app` only `Dockerfile` (its "Generated by builder" line) and `.builder.lock`.
4. **Release the builder again** exactly as in step 3, with the new tag:
   ```bash
   git add -A && git commit -m "Gate 1 waits on the scans; builder 0.2.1"
   git push origin main
   git tag builder-v0.2.1 && git push origin builder-v0.2.1
   gh run watch --repo <you>/platform-pipeline
   ```
   Put the new digest in `BUILDER_IMAGE` in **both** `.github/workflows/golden-road.yml` and `.github/workflows/golden-road-promote.yml`, commit, and push. Make sure the package is still public (step 3, Check 2).
5. **Update `hello-app` by pull request** (its `main` is protected; a push straight to `main` is refused). The deputy approves it like any other PR (single-approver mode: no approval needed, merge once the checks pass):
   ```bash
   cd ~/poc && git clone "$(gh repo view <you>/hello-app --json url --jq .url)" hello-app-update && cd hello-app-update
   git switch -c builder-0.2.1
   cp ~/poc/platform-pipeline/bootstrap/hello-app/Dockerfile ~/poc/platform-pipeline/bootstrap/hello-app/.builder.lock .
   git add -A && git commit -m "Regenerate for builder 0.2.1" && git push -u origin builder-0.2.1
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
   **Check:** if you have a deputy, the self-approval is refused; after the deputy approves, a `prod-...` release appears whose `promotion-record.json` names `<deputy>` and says `"self_approved": false`. Single-approver mode: the release appears, `promotion-record.json` names `<you>` and says `"self_approved": true`, and the job summary and the release notes say "Single-approver PoC mode: approved by the person who triggered it. Not valid for production use."; the deploy-owner verification in the next step must then reject this record (see below). Re-running a promote run is refused: dispatch a new run. Check the commit date in the prod release notes is not an old commit (a rollback) unless you intend it.
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
       | select(.image == $ref and .manifests_sha256 == $have and .self_approved == false)] | length >= 1' promo.jsonl
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

## PoC gaps to record
- The scans are open-source reference scans (Semgrep CE with a small vendored rule set plus `p/default`, Trivy, CodeQL default setup), not the security team's tools, thresholds, or triage. Until step 11, Gate 1 waits on nothing beyond drift and the authz probe. Gate 1 sees only a CodeQL check run's conclusion, so it proves the analysis ran, not that no alert exists; the alert threshold is enforced at merge time.
- **Scan results can be forged.** Every check that a GitHub Actions workflow posts comes from the same app (id 15368), so pinning `app_id` cannot tell the `security-scans` workflow's `security-scans / semgrep` from a job of the same name that anyone with write access adds to a workflow in `hello-app` (or by editing `security-scans.yml` to stop calling the real scans). CodeQL's results can likewise be imitated by a workflow with `security-events: write`. What holds today is code-owner review of `.github/` and a deputy who rejects changes there. **The real fix is signed scan results**: the scan workflow signs its result with its own identity and the release verifies that signature instead of a check name. It is out of scope for this PoC.
- **Base-image findings.** When the platform-pinned base image of the generated Dockerfile gets a fixed HIGH or CRITICAL, `security-scans / trivy` fails for every app built on it and the app team cannot fix it. The owner is the platform: bump the builder's base image, regenerate the apps, and re-release the builder. The step 5 dry run is the early warning.
- The scans run `docker build` of the app's Dockerfile on every PR, which runs repository code on a throwaway runner without secrets; the scan job's token is `contents: read` only, and the job that can write PR comments is separate and runs nothing from the repo (see the `security-scans` README for the limits).
- Both repos and the builder image are public; the configured gateway host, edge selector, and identity header are public.
- The `golden-road / drift` merge check can be imitated by editing `ci.yml` in a PR (check pinned only to the Actions app; no required workflows on personal accounts). Releases are unaffected, but the merge check rests on the deputy rejecting unintended changes to `.github/`, backed by CODEOWNERS and code-owner review.
- Anyone can mint platform-identity signatures for their own repos, so verification must pin the workflow repository, ref, and trigger.
- The repo owner is the admin and can change settings, so the controls rest on the owner not weakening them. The deputy is a single second pair of eyes (none in single-approver mode, where the controls prove the mechanics only).
- The organization-wide "require workflows" ruleset is not available on a personal account.
- The PR preview builds and runs the PR's image on the runner with a read-only token and no secrets, but the build has full network egress, like the release build. A fork PR in a public repo can therefore make the runner reach the internet during `docker build`; nothing else is exposed. The preview is required for merge but advisory for signing: the release job rebuilds and re-probes. It runs in parallel with drift, so a drift failure does not hide a build failure.
- Build egress is not restricted (see "Deferred: build egress" in `docs/enforcement.md`).

## If something fails
The workflows fail closed, so a failure means no release was published. Keep the failing step's log and the command output.

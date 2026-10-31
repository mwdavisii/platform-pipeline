# SOP: app developer (contributor)

## Purpose

Take an app that works on your machine to a signed release, with the identity, data connection and secrets it needs in each
environment. Your coding agent does the packaging; you make every decision. The platform does the checks, the release and the
signatures, and you never edit what it generates.

## Who

The person who owns an app and is an admin of its GitHub repo. In the portal you are a contributor (any signed-in person) and, for
your own repos, the repo's admin "as the portal last saw it" (from the collector).

## What you own

- **Your app repo:** the app code, its tests, and `builder-spec.json` (your answers: data classification, roles and rules, egress,
  env, health check). Example: `bootstrap/hello-app/builder-spec.json`.
- **Not yours (never edit):** the generated `Dockerfile`, `deploy/base/`, `_platform/`, `.builder.lock`, the golden-road blocks in
  `AGENTS.md` and `CLAUDE.md`, and the caller workflows in `.github/workflows/` (`ci.yml`, `promote.yml`, `security-scans.yml`,
  and `scorecard.yml` in per-app mode). Change `builder-spec.json` and run `builder generate .` instead.
- **Portal screens:** Start here, Register (`/register`), My requests (`/requests`), your page (`/me`), and each app's page
  (`/apps/<owner>/<repo>`) with a `test` and a `prod` tab.

## Before you start

- Read access to `<org>/golden-road-agents` (ask the platform team).
- `gh` signed in yourself (`gh auth login --hostname <github host>`), and `uv` installed.
- A certified agent: Claude Code, OpenAI Codex or Cursor. On Windows, `git config --global core.autocrlf false` first.
- A portal account (sign in with the identity provider), and your GitHub login linked on your page (procedure 2).

## Today (mock) and in production

| | Today (personal PoC) | In production |
|---|---|---|
| Packaging, checks, release, Gate 2 | Real | The same |
| Azure identity and Databricks choices | Picked from a mock directory; nothing is created in Azure or Databricks | Picked from a real directory (not built yet) |
| Secrets | Picked from a mock Secret Server; no value exists anywhere | Not built: the writer works only with the mock (anything else fails as `no_secret_server`), and the External Secrets Operator files are applied by hand |
| Deployment | Nothing runs: the deploy repo's files are generated and checked, never applied | Cluster operators switch reconciling on |

## Procedures

### 1. Package the app with the agent

**Goal:** a repo with generated files, platform callers and a pull request where every check passes.

1. Open your agent in the app's folder and paste the paste block from the `golden-road-agents` README, word for word.
2. Answer every question the agent asks you. The agent never answers data classification (`public`, `confidential` or
   `protected`; read the policy it links), roles, egress or justifications itself.
3. Say yes only to commands you have read. The agent creates the repo `--private` (or `--internal` if the platform says so), never
   public, and shows every outward command first.

**Check:** `golden-road doctor` exits 0. A missing intake record is a warning until procedure 3 is done.

### 2. Link your GitHub login in the portal

**Goal:** the portal knows which GitHub login is yours, so it can see which repos you administer.

1. Sign in to the portal and open your page (`/me`).
2. Press the GitHub link button and authorize. The portal records the login you verified.

**Check:** your page shows your GitHub login. A login already linked to someone else is refused ("That GitHub login is linked to
another person; ask an admin").

### 3. Register the app

**Goal:** the platform has a trusted record of the app's purpose, tier and classification (`intake/<owner>/<repo>.yaml` in
`scorecard-factory`).

1. The agent drafts the purpose from the repo; correct it until it says what the app does and does not do. Then it runs:
   ```bash
   golden-road register --repo <owner>/<repo> --purpose-file <file outside the app folder>
   ```
   It prints a link and sends nothing. There is no tier to give: the platform computes a minimum (the floor) and an admin decides.
2. Open the link yourself, sign in, check every field (the classification shows as three cards), and submit.
3. Wait for a portal admin (never yourself) to approve it. If it is sent back, open the request from My requests, change what the
   note asks, and submit it again.

**Check:** My requests shows the request as Merged, and `golden-road doctor`'s `intake` check passes.

### 4. Fix a red pull request

**Goal:** every check passes, by fixing causes, never by hiding them.

1. Ask your agent: "Use the golden-road skill to fix my PR." It runs `golden-road remediate --repo <owner>/<repo> --pr <N>`,
   which only reads, and shows a plan: `fix` items (yours), `rerun`, `wait`, and `needs-human` items with their owner.
2. Let it fix one item at a time; each commit needs your yes.
3. What the checks mean:
   - `golden-road / drift`: a generated file differs. Run `builder generate .` and commit.
   - `golden-road / preview`: the image did not build, or the authorization probe found a role doing what it shouldn't.
   - `security-scans / semgrep`, `security-scans / trivy`, CodeQL: a finding in your code or dependencies. A finding in the base
     image or a generated file is the platform's. A false positive is the security team's (they fix the rule; there is no per-app
     waiver).
   - `scorecard / assessed`: the review ran. If it failed, push again (in central mode the Re-run button does nothing).
   - `scorecard / level: <level>`: how much human review the PR needs. `elevated` or `security_review` means reviewers look
     closer; it does not block by itself. Each reason in the comment has a `Fix:` line.
   - `scorecard / findings`: a high or critical finding holds the merge (for example a change under `.github/`). Fix it, or if the
     change is intended, ask the override holder (procedure 5).
4. Never add `nosemgrep`, `# nosec`, `trivy:ignore` or an ignore file, never edit `.github/**` or generated files, never skip or
   delete a test. The scans ignore suppressions, and the scorecard sends such a PR to security review.

**Check:** `gh pr checks <N> --repo <owner>/<repo>` shows every required check passing, and `golden-road remediate` exits 0 or
lists only `needs-human` items.

### 5. Get a held merge decided

**Goal:** a PR held by `scorecard / findings` is decided by a person.

Ask the override holder (the repo admin role in the PoC; on an org, the platform team named at setup) and the security team. They
review the finding and, if it may merge, merge it with "Merge without waiting for requirements". You can't clear it in code.

**Check:** the PR is merged, and the repo's rule suites list the merge as a `bypass`.

### 6. Release and promote to prod

**Goal:** a test release after every merge, and a prod release approved by someone else.

1. Merge to `main`. The release job publishes a test release tagged `test-<UTC timestamp>-<sha7>`.
2. Start the promotion:
   ```bash
   gh workflow run promote.yml --repo <owner>/<repo> -f release=<test tag>
   ```
3. GitHub shows "Waiting for review". A Gate 2 reviewer, who is not you, approves the `prod` environment run. (As a Gate 2
   reviewer: approve only a release you have looked at, for an app you understand, and never your own; the promotion record
   rejects a self-approval, except in the PoC's single-approver mode, where it is recorded as `self_approved: true` and is never
   valid for production.)
4. The deploy repo's `bump` workflow opens `Pin <owner>/<repo> prod to <tag>`; a deploy reviewer merges it.

**Check:** `gh release list --repo <owner>/<repo>` shows the prod release, and the pin PR merged in `golden-road-deploy`.

### 7. Connect an Azure identity (per environment)

**Goal:** the app runs as a service principal made for it, in one environment.

Before you start: the identity admin has created a principal tagged for your repo (ask through the "how to ask for one" link), and
the environment has a release pinned in `golden-road-deploy` (a test release after your first merge; prod after procedure 6).

1. Open the app's page and the `test` (or `prod`) tab.
2. Under "Associate Azure Identity", pick the principal and submit. Only principals tagged for this repo are offered.

The writer opens a PR in `golden-road-deploy` that changes `apps/<owner>/<repo>/<env>/bindings.yaml` and `composed.yaml`. In the
composed manifests the service account gets `azure.workload.identity/client-id`, the pod gets the label
`azure.workload.identity/use: "true"`, the container gets `AZURE_CLIENT_ID` and `AZURE_TENANT_ID`, and egress to
`login.microsoftonline.com` on 443 is allowed. A `test` PR merges once `deploy-check` passes; a `prod` PR waits for a person.

**Check:** the tab shows the principal as linked (on `prod`, after a deploy reviewer merges the PR).

### 8. Enable a Databricks connection

**Goal:** the app reaches one SQL warehouse with its identity.

Before you start: an Azure identity is linked on this tab (procedure 7), and the data platform admin has granted that principal a
warehouse (the "how to ask" link).

1. Under "Enable Databricks Connection", pick the warehouse and submit. Only warehouses granted to the linked principal are offered.

The container gets `DATABRICKS_HOST` (a bare host name, no `https://`), `DATABRICKS_HTTP_PATH`, and `DATABRICKS_CATALOG` and
`DATABRICKS_SCHEMA` when the warehouse has them, plus egress to the workspace host on 443.

**Check:** the tab shows the warehouse as linked.

### 9. Add a secret

**Goal:** a secret from Secret Server reaches the app as an env var, with no value in the portal, git or a release.

Before you start: the secret is in a Secret Server folder you can see and is shared with `golden-road-eso` (ask security through
the "security's process" link).

1. On the tab, press **Add a secret** and follow the steps: Source (a folder), Secret (search; only the first 20 are shown),
   Field and name (a non-file field, and the env var name), Review and save.
2. Save. A toast says "Secret requested".

The env var name can't be one the platform reserves: `PORT`, `NODE_ENV`, `NODE_OPTIONS`, `PATH`, `HOME`, or a name starting
with `LD_`, `PYTHON`, `GUNICORN_`, `UVICORN_`, `NODE_`, `NPM_`, `AZURE_`, `DATABRICKS_` or `SECRETSERVER_`
(`bootstrap/golden-road-portal/portal/env_names.py`). A name your `builder-spec.json` already sets is not refused on the page:
the change fails when the writer records it (`compose_refused`). An environment holds at most 20 secrets.

**Check:** the tab lists the env var with its secret and field once the writer's PR merges.

### 10. Remove a connection or a secret

**Goal:** an environment stops using an identity, connection or secret.

Press **Remove** beside it, then **Remove** again to confirm (**Keep it** changes nothing). Removing the Azure identity removes the
Databricks connection too. The writer rewrites `bindings.yaml` (never deletes it) and `composed.yaml` to match.

**Check:** the tab no longer lists it.

### 11. Change or renew your app's registration

**Goal:** the registration says what the app is now (after T-21, a new purpose, or a review date coming up).

1. On the app page, press **Change or renew the registration** (the review-date banner links to it too), or open
   `/register?repo=<owner>/<repo>`. The page shows the current registration.
2. To renew, keep the purpose and the classification and submit. To change, edit them.
3. An admin decides the tier and the next review date. If you are not the record's owner, approving makes you the owner.

**Check:** the request page shows the change against the current record; once merged, the app page's banner is gone.

## What you must never do

- Never make the repo public, never force-push, never change rulesets or repo settings.
- Never edit generated files, the golden-road blocks, or `.github/`.
- Never suppress a finding or weaken a test to get green.
- Never approve your own registration or your own promotion (single-approver mode, PoC only, records a self-approved promotion
  as `self_approved`).
- Never paste a secret value into the portal, the repo, an issue or a chat with your agent.
- Never hand-edit `bindings.yaml` in the deploy repo: the next change from the portal overwrites it, and it skips the portal's checks.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `builder generate` exits 2 with "`internal` is retired: use `confidential`" | A classification from before builder 0.5.0 | Use `public`, `confidential` or `protected` (`restricted` becomes `confidential`, `phi` becomes `protected`) |
| Drift fails with "env name AZURE_CLIENT_ID is reserved" | Your spec declares a platform env name | Rename it; the platform sets `AZURE_*`, `DATABRICKS_*` and `SECRETSERVER_*` from the binding |
| `golden-road register` exits 3 | The installed builder names no portal | Ask the platform team to register the repo; until the portal is set up, security adds the record by PR |
| Register says "You are not an admin of that repository, as the portal last saw it" | Your GitHub login isn't linked, or the collector hasn't seen you as admin yet | Do procedure 2, wait a few minutes, try again |
| The tab says "The last change could not be recorded: not_deployed" | No release is pinned in that environment yet | Wait for the `bump` pin PR to merge (test), or promote first (prod) |
| "... could not be recorded: not_registered" | No intake record | Procedure 3 |
| "... could not be recorded: base_moved" | The deploy repo changed while your change waited | Make the change again |
| "... could not be recorded: recheck: not_in_directory" | The principal or warehouse is no longer in the directory | Ask the identity or data platform admin; remove the stale link |
| "... could not be recorded: recheck: secret_not_shared" | The secret is no longer shared with `golden-road-eso` | Ask security to share it again, or remove it |
| A secret is greyed "Not available" | Not shared with `golden-road-eso` | Ask security to share it |
| "This principal isn't tagged for <owner>/<repo>." | The principal was made for another repo | Ask the identity admin for one tagged for yours |
| Every PR is `security_review` with T-21 | The change needs a higher registration than the app has | Change the registration at the higher level (procedure 11) |
| "... could not be recorded: record_changed" or `record_conflict` | Someone edited the record in the factory after the admin approved | Submit the change again |
| The request waits with `record_unreadable` or `record_case_mismatch` | The read App can't read the factory's record, or the factory has it under two spellings of the repo's name | Ask the platform team |
| "... could not be recorded: not_a_file" or `recheck: scorecard_changed` | The record's path in the factory isn't a plain file, or the approval dropped the record's `scorecard:` mark | Ask the platform team (`not_a_file`); submit the change again (`scorecard_changed`) |
| `scorecard / assessed` failed with "not assessed" | A draft, a fork PR, a red Gate 1, or a review that couldn't run | Mark it ready, fix Gate 1, or push again |

## Gaps

- **No real directory.** Principals and warehouses come from a mock; nothing proves they exist or belong to the app.
- **No real Secret Server.** Secrets are picked from a mock; no value is ever delivered.
- **Nothing deploys.** The composed manifests are checked, never applied, so nothing proves the app runs with its bindings.
- **The app page shows the portal's record,** not the deploy repo's file: a hand edit there is not shown and is overwritten.
- **A secret's visibility to you is checked once, at submit,** and secrets already in an environment carry over under the name of
  whoever makes the next change there.

# SOP: app developer (contributor)

## Purpose

Take an app that works on your machine to a signed release, with the identity, data connection, secrets, MCP servers and skills it
needs in each environment, and keep owning it once it is registered. Your coding agent does the packaging; you make every decision. The platform does the checks, the release and the
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
| MCP servers | Picked from a mock Cloudflare gateway; no service token is issued | The platform's shared Cloudflare gateways; a platform job issues the app's token (not built) |
| Skills | Picked from the catalog fixture bundled with the portal | The platform's `golden-road-skills` catalog repo |
| The CMDB record and the lifecycle | A mock ServiceNow: its tasks and CI fields are fixtures. A suspend or decommission is real in git (the deploy repo, the archive) | ServiceNow CMDB and its real tasks |
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
with `LD_`, `PYTHON`, `GUNICORN_`, `UVICORN_`, `NODE_`, `NPM_`, `AZURE_`, `DATABRICKS_`, `SECRETSERVER_`, `MCP_` or
`CF_ACCESS_`, or `SKILLS_DIR` (`bootstrap/golden-road-portal/portal/env_names.py`). A name your `builder-spec.json` already sets is not refused on the page:
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

### 12. Add an MCP server

**Goal:** the app can call an MCP server on its environment's shared Cloudflare gateway, with its own service token.

Before you start: the server is on that environment's gateway, and Cloudflare lets you use it (ask through the "how to ask for a
server on the gateway" link if it isn't there).

1. On the tab, under "MCP servers (Cloudflare gateway)", press **Add an MCP server**. The steps are Environment (from the tab),
   Server, and Review and save.
2. On Server, only servers you may use and that are ready are listed. A server greyed with "This server needs each person's own
   sign-in, so an app can't use it." acts as each signed-in person (OAuth on their behalf), which an app's token can't do: pick
   another, or ask the gateway's admins for a version that apps can use.
3. Save. A toast says "MCP server requested".

The writer opens a PR in `golden-road-deploy` that changes `bindings.yaml` and `composed.yaml`, and writes `cloudflare-access.yaml`
when it changes. The container gets `MCP_URL` (the gateway's `/mcp` URL), `MCP_SERVERS` (the server ids, comma-separated) and
`CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET` (the app's Cloudflare service token, from Secret Server: send both as headers on
every call), and egress to the gateway on 443. An environment holds at most 20 servers. Remove one with **Remove** (procedure 10).

**Check:** the tab lists the server once the writer's PR merges, and "What the container gets" lists `MCP_URL` and `MCP_SERVERS`.

### 13. Add, update or remove a skill

**Goal:** the app reads a skill security approved, from files mounted read-only at `/skills`.

1. On the tab, under "Skills (platform catalog)", press **Add a skill**. The steps are Environment, Skill, and Review and save. Only
   skills approved for that environment and for you (everyone, or a group you are in) are offered.
2. Save. A toast says "Skill requested". The skill is pinned to the catalog's commit at that moment: a newer catalog changes
   nothing until you update it.
3. **Update:** when the catalog has a newer version, the skill's row says "newer" and offers **Update**, which asks for the current
   commit (a new request). **Remove** asks first, as in procedure 10.

The app finds its skills at `SKILLS_DIR` (`/skills`), one folder per skill (`/skills/<name>/SKILL.md` and its other files). At most
20 skills per environment, and together at most 512 KiB.

**Check:** the tab lists the skill with its pinned commit, and the catalog column reads "the same".

### 14. Know what owning your app means

**Goal:** you know what you agree to when you register an app, and you can see its ServiceNow record.

1. The register form shows "What owning this app means": patching (you approve patches when scans find new vulnerabilities, and
   access may be suspended for a critical one), validation (you confirm the app is still in use when asked), and leaving (when you
   leave or move, you and your manager name a new owner or the app is decommissioned). To submit, you must tick
   "I understand what owning this app means", for a first registration, a change or a renewal.
2. Once the app is registered, the platform records it in ServiceNow CMDB, with you as its owner (the ServiceNow user with your
   sign-in email). The app page then shows the card "Your app is registered in ServiceNow (`<number>`)", and the request's timeline
   the step "Registered in ServiceNow".
3. If the page says "ServiceNow has no user with the owner's email" (`cmdb_owner_unknown`), your sign-in email doesn't match one
   ServiceNow user. Ask a portal admin; the platform retries on every pass. The owner is the registration's: to change it, a new
   owner changes the registration (procedure 11), never ServiceNow.

**Check:** the app page shows the card with the number; the request's timeline shows "Registered in ServiceNow".

### 15. Know what a suspended or decommissioned app looks like

**Goal:** you know why your app is down, who to ask, and how it comes back.

The lifecycle decision is made in ServiceNow, and a portal admin applies it (`docs/lifecycle.md`). The reasons are the ones you agreed
to (procedure 14): a critical vulnerability, an owner who didn't confirm the app is still in use, an owner who left.
1. **Suspended.** Everyone who can see the app page reads the banner "Suspended by the platform for ServiceNow task SCTASK... on
   .... It comes back when the platform resumes it." Every environment is scaled to zero; nothing is deleted. Bindings still work
   while it is suspended: you may change an identity, a connection, a secret, a server or a skill, and the environment stays at zero.
   A release bump keeps it at zero too.
2. **Lifting a suspension.** Fix the cause (for example, patch the vulnerability and release), then ask through the ServiceNow
   task: its assignee is your contact. A portal admin resumes the app for that task (or a new one). A suspension is lifted that way,
   not by a PR: a PR that removes `lifecycle.yaml` needs the platform's review and is not how it is done.
3. **Decommissioned.** The banner reads "Decommissioned on ... for task SCTASK.... The repository is archived." The app is gone
   from the deploy repo, its repository is read-only, and the portal takes no request for it ("<owner>/<repo> was decommissioned on
   ... for task ...: nothing more can be asked for it."). Your images in ghcr, your Delinea secret values, and the Azure identity and
   Databricks principal stay: deleting the last two is yours, with their admins. Bringing it back is a platform procedure (ask the
   task's assignee), followed by a change of the registration and a new release.
4. **You can't press the buttons.** Only a portal admin can suspend, resume or decommission an app; your app page shows the
   state, not the Lifecycle panel.

**Check:** you know the task's number (it is in the banner) and its assignee in ServiceNow.

## What you must never do

- Never make the repo public, never force-push, never change rulesets or repo settings.
- Never edit generated files, the golden-road blocks, or `.github/`.
- Never suppress a finding or weaken a test to get green.
- Never approve your own registration or your own promotion (single-approver mode, PoC only, records a self-approved promotion
  as `self_approved`).
- Never paste a secret value into the portal, the repo, an issue or a chat with your agent.
- Never try to lift a suspension with a PR, or unarchive a decommissioned repository yourself: ask through the ServiceNow task.
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
| An MCP server is greyed "This server needs each person's own sign-in, so an app can't use it." | It acts as each signed-in person | Pick another server, or ask the gateway's admins |
| No MCP servers are listed, or "Your sign-in has no email address" | Cloudflare lets you use none on that gateway, or your sign-in has no email | Ask through the "how to ask for a server" link; check your email in the identity provider |
| "... could not be recorded: recheck: mcp_server_gone" (or `mcp_server_not_ready`, `mcp_server_user_auth`) | The server left the gateway, isn't ready, or now needs each person's sign-in | Remove it, or wait and add it again |
| "... could not be recorded: recheck: skill_gone" or `skill_environment` | The pinned skill isn't in the catalog at that commit, or no longer allows this environment | Update or remove the skill |
| "That skill is not approved for prod." | Its `skill.yaml` lists only `test` | Ask security to approve it for prod (a catalog PR) |
| The app page says "ServiceNow has no user with the owner's email" | `cmdb_owner_unknown`: the owner's email matches no single ServiceNow user | Ask a portal admin (procedure 14) |
| The app page has a banner "Suspended by the platform for ServiceNow task ..." | A portal admin suspended it for that task | Procedure 15: fix the cause, ask through the task |
| "<owner>/<repo> was decommissioned on ... for task ...: nothing more can be asked for it." | The app was decommissioned | Procedure 15; ask the task's assignee |
| `scorecard / assessed` failed with "not assessed" | A draft, a fork PR, a red Gate 1, or a review that couldn't run | Mark it ready, fix Gate 1, or push again |

## Gaps

- **No real directory.** Principals and warehouses come from a mock; nothing proves they exist or belong to the app.
- **No real Secret Server.** Secrets are picked from a mock; no value is ever delivered.
- **Nothing deploys.** The composed manifests are checked, never applied, so nothing proves the app runs with its bindings.
- **The app page shows the portal's record,** not the deploy repo's file: a hand edit there is not shown and is overwritten.
- **A secret's visibility to you is checked once, at submit,** and secrets already in an environment carry over under the name of
  whoever makes the next change there.
- **Which MCP servers you may use is the mock's policy data.** The real version asks Cloudflare per person; it is checked once, at
  submit. A skill's audience is checked once, at submit, too.
- **No service token exists.** The job that would issue the app's Cloudflare token and set each server's policy is not built.
- **The ownership notice describes a process that runs only in part:** a portal admin can suspend, resume or decommission an app
  for a ServiceNow task (procedure 15), but nothing asks you for validation or opens a task by itself, and the tasks are the mock's.
- **A suspension stops at git:** the platform can't see the cluster scale the app to zero. An environment first pinned while the
  app is suspended (a first prod pin) is not suspended.

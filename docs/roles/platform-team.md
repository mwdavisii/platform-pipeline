# SOP: platform team

## Purpose

Run the golden road itself: the builder and its image, the reusable workflows, the scorecard code and factory mechanics, the agent
kit, the deploy repo's automation, the GitHub Apps and the rulesets that keep contributors from turning controls off. You release
and pin; you do not deploy, and you do not own the scanners or the routing rules.

## Who

The platform team, as the GitHub team `platform-reviewers` (`<org>/platform-reviewers` in CODEOWNERS), and the admins of the
platform repos. In the personal PoC, the account owner (and a deputy, if there is one).

## What you own

| Repo or area | Files you own |
|---|---|
| `platform-pipeline` (this repo) | `.github/workflows/golden-road.yml`, `golden-road-promote.yml`, `release-builder.yml`; `src/`; `platform/platform.json`, `platform/required-checks.json`; `images/`; `scripts/`; `.github/rulesets/builder-tags.json` |
| `scorecard-factory` | Everything (`*`) except what security owns: the workflows, `config/scorecard.yaml` (gateway, model version, budget, Gate 1 list, `central`), the intake and app-token actions |
| `golden-road-agents` | The stub, the skill bundle, `skill-v*` releases, publishing `builder-v*` drafts |
| `golden-road-deploy` | Everything (`*`) and `/.github/`: `bump.yml`, `deploy-check.yml`, the rulesets, `policy/`, `clusters/`, `external-secrets/`, and each environment's generated `cloudflare-access.yaml` |
| Cloudflare and ServiceNow | The job that reconciles `cloudflare-access.yaml` into Cloudflare (not built); the ServiceNow integration user the collector signs in as |
| App repos | `/.github/` and the scanner ignore files (CODEOWNERS); the caller workflows and generated files come from the builder |
| The portal | Its deploy kit (`deploy-kits/portal-homelab/`), its settings, its database roles and migrations |
| GitHub Apps | The kit App, the scorecard App (central mode), the deploy App, the portal's read and write Apps, and the lifecycle App (archives a decommissioned app's repository) |
| Lifecycle | The lifecycle App, migration 0012, failed archive steps, and bringing an app back after decommission (`docs/lifecycle.md`) |

## Before you start

- Admin on the platform repos (`platform-pipeline`, `scorecard-factory`, `golden-road-agents`, `golden-road-deploy`, the portal).
- A clean checkout of the source and a Python 3.12 virtualenv with `jsonschema`, `PyYAML` and `pytest`; `gh`, `jq`, `docker`, `cosign`.
- The setup documents: [the personal PoC](../setup/personal-poc.md), or the enterprise setup document (internal, not part of the
  public export).

## Today (mock) and in production

| | Today (personal PoC) | In production |
|---|---|---|
| Repos | Public, on a personal account, published from a sanitized export | Private or internal, in an organization |
| Approvals | A deputy, or single-approver mode (`--single-approver`, PoC only) | Two people; single-approver mode must not be used |
| Required workflows | Not available: app repos call the factory and scans themselves | An org ruleset requiring the workflows: not built yet, and it needs an org admin |
| Deploy repo | `bump` and `deploy-check` run; nothing reconciles | The same until cluster operators switch reconciling on |
| Directory and Secret Server | Mocks inside the portal image | Real integrations (not built) |
| Cloudflare, the skills catalog and ServiceNow | Mocks inside the portal image (`PORTAL_CLOUDFLARE`, `PORTAL_SKILLS_CATALOG`, `PORTAL_SERVICENOW` set to `mock`); the catalog bundled in the builder image | Real integrations, and the reconcile job (not built) |
| Lifecycle | The lifecycle App and its archive are real; the ServiceNow tasks (SCTASK0010001 to SCTASK0010006) and the CI's fields are the mock's | A real ServiceNow (`sc_task` read and write for the integration user, and read for the portal's web: not built) |

## Procedures

### 1. Configure a tree

**Goal:** every placeholder filled before the builder is released (the lock records the config hash, so the order matters).

```bash
<src>/.venv/bin/python scripts/configure.py --org <org> --gateway-host <gateway host> \
  --edge-namespace <edge namespace> --edge-label <key>=<value> --identity-header <header> \
  --scorecard-gateway-url https://<scorecard gateway host>/v1/chat/completions
```

| Flag | What it sets |
|---|---|
| `--org`, `--gateway-host`, `--edge-namespace`, `--edge-label` (repeatable), `--identity-header` | Required: the org, the model gateway, the edge selector and the identity header baked into every app |
| `--scorecard-gateway-url` | The URL the review agent calls (default `https://<gateway host>/v1/chat/completions`) |
| `--github-host`, `--cert-oidc-issuer` | Another GitHub host and its Actions OIDC issuer |
| `--landing-page-url`, `--approved-models-url`, `--portal-url`, `--classification-policy-url` | Links in the `AGENTS.md` block, `golden-road register` and `builder guide` |
| `--scorecard-mode central --scorecard-app-id <id>` | Central review: pins `scorecard / assessed` and `scorecard / findings` to the scorecard App and removes the apps' `scorecard.yml` callers |
| `--findings-bypass-team <slug>` | The org team that may merge past a high scorecard finding (default: the repo admin role) |
| `--bump-workflow-token` | PoC only: no deploy App; `bump` writes with its own token and dispatches `deploy-check` |
| `--single-approver` | PoC only: no second person; never on an organization |

A later run that changes `--org`, `--single-approver` or `--findings-bypass-team`, drops `--bump-workflow-token`, leaves central
mode, or changes the GitHub host is refused (restore with `git checkout -- bootstrap platform`). A link can be set but not unset by
configure: delete the key from `platform/platform.json` by hand.

**Check:** `grep -rn 'replace-me\|ORG_PLACEHOLDER' platform bootstrap` prints nothing.

### 2. Release the builder

**Goal:** a new signed builder image and kit, at a version no tag has used.

1. Bump the version and regenerate the sample app:
   ```bash
   <src>/.venv/bin/python scripts/bump-builder-version.py <new version>
   git diff --stat      # src/builder/__init__.py, and .builder.lock and Dockerfile in bootstrap/hello-app and bootstrap/golden-road-portal
   ```
2. Commit, push `main`, then tag it (never move or reuse a tag; only repo admins can create, move or delete a `builder-v*` tag, by
   `builder-tags.json`):
   ```bash
   version=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' src/builder/__init__.py)
   git ls-remote --tags origin builder-v$version     # must print nothing
   git tag builder-v$version && git push origin builder-v$version
   gh run watch --repo <org>/platform-pipeline
   ```
3. The `release` job builds, pushes and signs the image. The `kit` job publishes a `builder-v<version>` draft in
   `golden-road-agents` through the kit App.

**Check:** the run's `release` job succeeded and its summary gives `ghcr.io/<org>/golden-road-builder@sha256:<digest>`.

### 3. Pin the builder everywhere and publish the kit

**Goal:** every workflow runs the same image, and agents install the same builder.

```bash
ref=ghcr.io/<org>/golden-road-builder@sha256:<digest>
grep -rlE '^  BUILDER_IMAGE: ' .github/workflows bootstrap | xargs sed -i "s|^  BUILDER_IMAGE: .*|  BUILDER_IMAGE: $ref|"
git add -A && git commit -m "Pin builder $version" && git push origin main
gh release edit builder-v$version --repo <org>/golden-road-agents --draft=false --latest=false
```
There are eight pins: `golden-road.yml`, `golden-road-promote.yml`, the factory's `scorecard.yml`, `scorecard-poll.yml`,
`scorecard-assess.yml` and `factory-check.yml`, and the deploy repo's `deploy-check.yml` and `bump.yml`. The factory's and the
deploy repo's copies live in those repos: move their pins by a pull request there (all four factory pins in one PR). Never mark a
builder release latest: agents pick the highest version, and `INSTALL_FOR_AGENTS.md` finds the skill by the latest flag.

**Check:** `grep -rh '^  BUILDER_IMAGE:' .github/workflows bootstrap | sort | uniq -c` prints one line, `8   BUILDER_IMAGE: $ref`.

### 4. Roll a release out to the apps and the portal

**Goal:** every app passes drift on the new builder.

Generated files carry the builder version, so every app fails drift until it is regenerated. For each app (and the portal), open a
PR that regenerates it with the pinned builder:
```bash
docker run --rm --network none --user "$(id -u):$(id -g)" -v "$PWD:/src" "$ref" builder generate /src
```
Merge each once its checks pass. For the portal, then follow the kit README's "Upgrade" (`deploy-kits/portal-homelab/README.md`):
wait for the migration job before trusting the new pods. Migrations are forward-only: never run a portal image older than the
newest applied migration (after 0006 an older writer fails every binding request).

**Check:** each app's regenerate PR shows `golden-road / drift` passing, and a new test release follows its merge.

### 5. Change what Gate 1 requires

**Goal:** Gate 1 and merge wait on the security team's checks, pinned to their source.

With the names and app ids from security (they observe them on a real commit), write `platform/required-checks.json`, add the same
entries after drift and preview in the factory's `gate1_checks`, and add them to the app rulesets. The list is baked into the
builder image: release (procedure 2) and pin (procedure 3).

**Check:** a new test release's `gate-record.json` lists every entry with `"state": "success"`.

### 6. Export the public PoC tree

**Goal:** a sanitized, single-commit tree, never the working history.

```bash
cd <src> && git status --porcelain       # must print nothing
scripts/export-public-poc.sh <empty dir>
scripts/export-public-poc.sh --tree security-scans <another empty dir>
```
The export refuses a dirty tree, a configured `platform/platform.json`, single-approver settings, `BUMP_TOKEN: workflow` in
`bump.yml`, a broken placeholder, and any
text on its private pattern list (it names file and line). Configure in the exported tree, never in the source.

**Check:** both runs exit 0, and in each tree `git rev-list --count HEAD` prints `1` and `git remote` prints nothing.

### 7. Create a platform repo from its bootstrap

**Goal:** the repo exists with the right visibility before any content is pushed.

```bash
scripts/push-bootstrap.sh <org> <name> --internal      # or --private (the default)
```
Then apply its rulesets with `gh api -X POST repos/<org>/<name>/rulesets --input bootstrap/<name>/.github/rulesets/<file>.json`.
The factory and the deploy repo each get two: `main` (no deletion, no force push, the required check) and `main-review` (the pull
request rule only). `--public` is the personal PoC exception: it needs `GOLDEN_ROAD_PUBLIC_POC` set to the owner's login, the owner to be the
signed-in `gh` user, and the owner to be a personal account (type `User`).

**Check:** `gh repo view <org>/<name> --json visibility --jq .visibility` prints `INTERNAL` or `PRIVATE` (`PUBLIC` only for the personal PoC), and the rulesets are active.

### 8. Set up the deploy repo's pin PRs (the deploy App or workflow-token mode)

**Goal:** `bump` opens pin PRs that start `deploy-check`.

- **The deploy App** (`BUMP_TOKEN: app`, the default, and the only choice on an organization): an App with Contents and Pull
  requests read and write only, installed on `golden-road-deploy` alone. Store its key as `DEPLOY_APP_KEY` in an environment `bump`
  limited to `main`, and its App ID as the repository variable `DEPLOY_APP_ID`. Never make it a bypass actor.
- **Workflow-token mode** (`configure.py --bump-workflow-token`, PoC only): `bump` writes with its own token and dispatches
  `deploy-check` on each `bump/` branch. The repo must allow Actions to create and approve PRs, which also lets any workflow there
  approve one.

`bump` runs every 15 minutes. Start it by hand for a new app: `gh workflow run bump.yml --repo <org>/golden-road-deploy -f repo=<owner>/<repo>`.

**Check:** a test pin PR `Pin <owner>/<repo> test to <tag>` opens and `deploy-check` runs on it.

### 9. Set up the portal's write App and its bypass

**Goal:** the writer can record registrations and bindings, and nothing more.

1. The write App: Contents and Pull requests write; Metadata, Checks, Actions and Administration read. Installed on
   `scorecard-factory` and `golden-road-deploy` only ("Only select repositories").
2. In each of those two repos, add the App as a bypass actor on `main-review` only, with `bypass_mode: pull_request`
   (personal PoC section 13.8). Never on `main`: it would bypass the required check, and the writer refuses to start.
3. The writer checks this before its first pass and before every pass.

**Check:** the writer's log (`kubectl -n golden-road-portal logs deploy/golden-road-portal-writer`) has no "refusing to start".

### 10. Turn on central review (the scorecard App)

**Goal:** the review runs in the factory, and `scorecard / assessed` can't be imitated by an app.

Follow personal PoC section 16: create the App (Checks, Issues and Pull requests write; Contents and Commit statuses read),
install it on the app repos only, create the factory environment `scorecard` (branch `main` only) with `SCORECARD_APP_KEY` and the
gateway secrets, set the repository variable `SCORECARD_APP_ID`, configure with `--scorecard-mode central`, add
`central: {max_in_flight: 4, assess_timeout_minutes: 360}` to `config/scorecard.yaml`, then migrate each app (`scorecard: central`
in its intake record, re-PUT its rulesets, remove its `scorecard.yml` and its gateway secrets).

**Check:** a ready PR's `scorecard / assessed` comes from the App's slug, not `github-actions`.

### 11. Apply the findings ruleset to a new app

**Goal:** a new app's high findings hold its merges.

Only `hello-app` and the portal ship `scorecard-findings.json`. For any other app, apply it after the factory runs a release that
posts the check (a required check that never comes holds every PR):
```bash
gh api -X POST repos/<org>/<repo>/rulesets --input bootstrap/hello-app/.github/rulesets/scorecard-findings.json
```
On an organization, fill the bypass team's id into a copy first (configure writes `actor_id` 0).

**Check:** `golden-road doctor` in the app no longer warns that `main` doesn't require `scorecard / findings`.

### 12. Reconcile Cloudflare access from the deploy repo

**Goal:** each app's Cloudflare service token exists, sits in Secret Server where its `ExternalSecret` reads it, and is named by
exactly the servers its binding lists. Documented, not built: today nothing issues a token or changes a policy.

The contract, for a job you would run from the deploy repo's `main`:
1. Read every `apps/<owner>/<repo>/<env>/cloudflare-access.yaml` (`cloudflare-access/v1`, generated by `golden-road compose`;
   `deploy-check` holds it to `bindings.yaml`). Each names the environment's gateway, the token (`cf-mcp-<owner>--<repo>-<env>`)
   and its Secret Server path, and the servers.
2. Issue the service token if it is missing, and store it in Secret Server at the path the file names,
   `/golden-road/mcp-tokens/cf-mcp-<owner>--<repo>-<env>`, as a Password-template secret with the fields `client-id` and
   `client-secret`. The app's `ExternalSecret` (`<app>-delinea`) reads those two fields into `CF_ACCESS_CLIENT_ID` and
   `CF_ACCESS_CLIENT_SECRET`. Share it with `golden-road-eso` only.
3. On the environment's gateway, make each listed server's Service Auth policy name the token, and take it out of every other
   server's. A file with `servers: []` means the token is on no policy.
4. The job holds its own Cloudflare write credential and Secret Server write access; the portal holds neither (its Cloudflare
   token is MCP Portals Read only).

**Check:** (once built) for an app, the token's name is on the Service Auth policy of exactly the servers in its
`cloudflare-access.yaml`, and Secret Server has the secret at the path, with both fields.

### 13. Set up the ServiceNow integration user

**Goal:** the collector can register each app in ServiceNow CMDB as a `cmdb_ci_business_app`, and nothing else.

Documented for the first real instance; the PoC uses the mock (`PORTAL_SERVICENOW=mock`, its user `golden-road-cmdb`).
1. Ask the ServiceNow admins for an integration user for the platform, with a role that can read `sys_user` (by email) and
   `cmdb_ci_business_app`, and call the Identification and Reconciliation API (`POST /api/now/identifyreconcile`) for that class.
   Confirm the role names with them; the portal needs no other access.
2. Keep its credential in Secret Server, never in git or the kit. Wiring it into the collector is not built:
   `PORTAL_SERVICENOW_URL` is refused today.
3. Ask for an identification rule on `cmdb_ci_business_app` by `correlation_id` (the collector sends `github:<owner>/<repo>`, lower
   case) and a reconciliation rule for the data source `golden_road`, so the platform's updates win for the fields it sends.
4. Confirm the field names: `u_tier`, `u_data_classification`, `u_review_by` and `u_repo_url` are placeholders for the instance's
   own, and `short_description` gets the purpose cut at 1000 characters.

**Check:** the collector logs `collectors: cmdb: N synced, M unchanged`, and the Re-review page shows no `cmdb` problem.

### 14. Move an MCP gateway

**Goal:** the platform changes a shared MCP gateway without blocking the deploy repo.

1. Configure the new host (`configure.py --mcp-gateway-test` or `--mcp-gateway-prod`), release the builder and pin it in every
   workflow, and set the portal kit's `PORTAL_MCP_GATEWAY_TEST` or `_PROD` to the same host.
2. `deploy-check` then holds only environments whose `bindings.yaml` or `cloudflare-access.yaml` a PR changes to the new gateway.
   The others pass with a warning, keep the old gateway's egress and `MCP_URL`, and `bump` keeps pinning them (it composes with
   `golden-road compose --unchanged-binding`). An environment with `servers: []` passes on any gateway.
3. Move each app: its owner removes its MCP servers (one Remove) and adds them again, or you rewrite those `bindings.yaml` files to
   the new gateway and run `golden-road compose` on them, in one PR.

**Check:** `deploy-check` on `main` prints no `kept because this binding is unchanged` warning.

### 15. Create and install the lifecycle App

**Goal:** the writer can archive a decommissioned app's repository, and the key that can do it reaches nothing else.

1. Create a GitHub App (on the organization, or on the personal account in the PoC), for example `golden-road-lifecycle`.
   Repository permissions: Administration: write, nothing else (Metadata: read is added by GitHub). No organization permission, no
   webhook (turn it off), no OAuth, no callback URL. It must be another App than the write App: the writer refuses to start when
   `PORTAL_LIFECYCLE_APP_ID` equals `PORTAL_WRITE_APP_ID`.
2. Install it on app repos only ("Only select repositories"): never on `platform-pipeline`, `scorecard-factory`,
   `golden-road-deploy`, `golden-road-portal`, `golden-road-agents` or `security-scans`. Before every archive the writer asks for the
   App's installation on each of those six and fails the step (`lifecycle_app_on_platform_repo: <org>/<repo>`) if one exists. Add an
   app repo to the installation before you decommission it (or install it on every app repo).
3. Generate a private key. Put the App's id in the writer's Secret as `PORTAL_LIFECYCLE_APP_ID` and the key as `lifecycle-app.pem`
   (`deploy-kits/portal-homelab/secrets.example.yaml`); the kit's `writer.yaml` mounts it at
   `/secrets/lifecycle-app/lifecycle-app.pem` (`PORTAL_LIFECYCLE_APP_KEY_FILE`). The writer reads both or neither; no other pod gets
   them. Restart the writer.
4. To rotate the key: generate a new key in the App's settings, replace `lifecycle-app.pem` in the writer's Secret, restart the writer
   (`kubectl -n golden-road-portal delete pod -l app.kubernetes.io/name=golden-road-portal-writer`), then delete the old key in the App's settings.
   Do the same at once if the key may have leaked.

The writer's calls with it: as the App, `GET /repos/<org>/<platform repo>/installation` for each of the six (each must be a 404) and
`GET /repos/<owner>/<repo>/installation`, then `POST /app/installations/<id>/access_tokens` for the one repository with only
`administration: write` (a token GitHub grants wider or narrower is revoked unused). The token makes exactly three calls:
`GET /repos/<owner>/<repo>` (already archived: the step is done), `PATCH /repos/<owner>/<repo>` with `{"archived": true}`, and
`DELETE /installation/token`. It never archives a platform repository or a repository outside `PORTAL_ORG` (`not_in_org`).

**Check:** the writer starts (no `writer: PORTAL_LIFECYCLE_APP_ID ...` line in its log), and the App's installation page lists app
repos only.

### 16. Apply migration 0012

**Goal:** the portal's database has the lifecycle tables and guards before 0.7.0's pods use them.

Upgrade as in the kit README's "Upgrade" (`deploy-kits/portal-homelab/README.md`): delete the finished migration Job, apply, and
wait for it. The job runs `python -m portal.migrate` as `portal_owner`, which applies `0012_lifecycle` once. It is forward-only, like
0010 and 0011: roll the image back only with a database restored from before it.

**Check:** `kubectl -n golden-road-portal logs job/golden-road-portal-migrate` lists `0012_lifecycle`, and connected as
`portal_owner` (procedure 18, step 3), `\dt lifecycle_*` lists `lifecycle_command`, `lifecycle_events`, `lifecycle_state` and
`lifecycle_step`.

### 17. Fix a failed archive step

**Goal:** a decommission whose archive failed archives on Retry, and its factory PR follows.

The deploy step stays done (the app is decommissioned in the portal); the factory PR waits for the archive. The request page and a
task note give the reason:
- `archive: github: 404 GET /repos/<owner>/<repo>/installation`: the lifecycle App isn't installed on the app's repository. Add the
  repository to the App's installation (procedure 15, step 2).
- `lifecycle_app_on_platform_repo: <org>/<repo>`: the App is installed on a platform repository. Remove that repository from the
  installation (or uninstall and reinstall on app repos only), and check nothing was changed in it.
- `no_lifecycle_app`: the writer has no lifecycle App (procedure 15, step 3).
- `archive: github: 403 ...` or `archive: github: unexpected response`: the App lacks Administration: write, or GitHub answered
  something the writer doesn't read. Check the App's permissions and that its installation accepted them.
- GitHub not answering (no status, a 5xx, a rate limit) doesn't fail the step: the writer tries again on its next pass.
- `archive: github: 404 GET /repos/<owner>/<repo>` (or the installation's 404 after you added it): the repository was deleted or
  transferred. A Retry can't archive it. Ask a portal admin to press **Stop** (portal-admin procedure 13).
- `not_decommissioned_by_this_request`: the app was brought back (procedure 18) while the request was open. Ask a portal admin to
  press **Stop**; a Retry fails the same way.

Then ask a portal admin to press **Retry** on the request (portal-admin procedure 13). A repository someone already archived by
hand is fine: the writer reads it first and marks the step done.

**Check:** the request's timeline shows Repository archived (Done), the repository is archived on GitHub, and the factory PR opens.

### 18. Bring an app back after decommission

**Goal:** a decommissioned app can be registered and released again, with every change reviewed and recorded.

There is no button. Decommissioned is for good for every portal process; only the schema owner can set it back.

First, the decommission request must be over. Open it from the app's requests: its timeline must end **Applied in git** or
**Stopped**. While it is still open (a step failed or waits), a Retry could archive the repository or mark the record again, so ask a
portal admin to Stop it first (portal-admin procedure 13). 0012 refuses the change below while the request is open ("an app is brought
back only once its decommission is applied in git or stopped").
1. A GitHub admin unarchives the repository (the repository's Settings, "Unarchive this repository", or
   `gh api -X PATCH repos/<owner>/<repo> -F archived=false`). Don't use the lifecycle App for it.
2. A reviewed factory PR removes the `decommissioned` key from `intake/<owner>/<repo>.yaml` (a security code owner approves it).
   Until it merges, `deploy-check` refuses any change under `apps/<owner>/<repo>/`. A stopped request may never have marked the
   record: then there is no key to remove. Until step 1, `deploy-check` refuses the app too (the repository reads archived).
3. Connect to the portal's database as `portal_owner`. On the homelab kit:
   ```bash
   kubectl -n golden-road-portal exec -it golden-road-portal-postgres-0 -- sh -c 'PGPASSWORD="$PORTAL_OWNER_PASSWORD" psql -h 127.0.0.1 -U portal_owner -d portal'
   ```
   Then, in one transaction (0012 allows this one change of a decommissioned state, to `portal_owner` only: the state `active`, no
   task, the same request):
   ```sql
   BEGIN;
   SELECT repo, state, task, since, request FROM lifecycle_state WHERE lower(repo) = lower('<owner>/<repo>');
   UPDATE lifecycle_state SET state = 'active', task = NULL WHERE lower(repo) = lower('<owner>/<repo>') AND state = 'decommissioned';
   INSERT INTO audit_log (at, actor_kind, actor_id, action, subject_kind, subject_id, before, after, reason)
     VALUES (now(), 'person', '<your person id>', 'lifecycle.brought_back', 'app', '<owner>/<repo>', '{"state": "decommissioned"}',
             '{"state": "active"}', 'Brought back by hand for ServiceNow task <SCTASK...>: <why>; unarchived, factory PR <url>');
   COMMIT;
   ```
   The `UPDATE` must say `UPDATE 1`. Your person id is on `/admin/people` (`p_` and 32 hex digits).
4. The owner registers the app again on the portal's register form. The intake record is still there, so the form is "Change or
   renew the registration" (app-developer procedure 11), and a portal admin approves it.
5. The owner makes a new release; `bump` pins it in the deploy repo again, with new adapters.

**Check:** the app page shows no banner and its Lifecycle panel reads Active, the register form takes the repo, and `/admin/audit`
shows your `lifecycle.brought_back` entry.

## What you must never do

- Never release the builder before configuring the tree, and never move or reuse a `builder-v*` tag.
- Never mark a builder release `--latest`.
- Never publish from the source repo's history: export first.
- Never use `--single-approver` or `--bump-workflow-token` on an organization.
- Never add the write App, the deploy App or anyone to a `main` ruleset's bypass list.
- Never install the lifecycle App on a platform repository, give it any permission but Administration: write, or give its key to
  any process but the writer. Never use the write App's id for it.
- Never change `lifecycle_state`, `lifecycle_step` or `lifecycle_events` by hand except as procedure 18 and portal-admin
  procedure 14 say, with an audit entry.
- Never switch on a Flux adapter, an Argo CD automated sync or the image policy's `enforce` mode from git: `deploy-check` and the
  export refuse it, and reconciling belongs to cluster operators.
- Never edit `config/routing.toml`, `intake/`, `suppressions/` or `na/` without security's review.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Every app's drift fails after a release | The new builder version is in the lock | Regenerate each app (procedure 4) |
| Drift fails everywhere right after `configure.py` | The builder was released before configuring | Configure, bump the version, release again |
| `push-bootstrap.sh` refuses the factory | `BUILDER_IMAGE` is still the all-zero digest | Pin it (procedure 3) |
| The export refuses "platform/platform.json in HEAD is missing or already configured" | You configured the source | Restore the source; configure only the export |
| The export refuses with file:line | Text on the private pattern list | Remove it and commit |
| The `kit` job fails at the token step | The kit App or its key is missing | Create it and re-run the job (within 30 days) |
| The `kit` job fails with "already has a release or draft" | A stale draft | Delete the draft, re-run |
| The writer logs "refusing to start" and exits 3 | The write App's install or a bypass is wrong (its problems are listed) | Procedure 9 |
| The writer logs `writer: PORTAL_LIFECYCLE_APP_ID and PORTAL_LIFECYCLE_APP_KEY_FILE are set together, or neither` and exits 2 | Only one of the two settings reached the writer (the kit always sets the key file's path) | Fill both in the writer's Secret, or remove both from `writer.yaml` (procedure 15) |
| The writer logs `writer: PORTAL_LIFECYCLE_APP_ID must be another App than PORTAL_WRITE_APP_ID: the write App never archives` | The lifecycle App's id is the write App's | Create the lifecycle App (procedure 15) |
| A decommission ends Refused with `recheck: no_lifecycle_app` | The writer runs without the lifecycle App | Procedure 15; the admin asks again |
| The archive step failed | The lifecycle App's installation or permission | Procedure 17 |
| The collector logs `collectors: PORTAL_BASE_URL is not set: the ServiceNow notes link the portal's request` and exits 2 | `PORTAL_SERVICENOW` is set and the collector has no `PORTAL_BASE_URL` | Take the 0.7.0 kit's `collector.yaml` |
| A lifecycle step failed `merged_with_other_commits` | A person merged the writer's PR with commits the writer didn't write, so the writer can't say what the merge applied | Check the merge commit by hand (`gh pr view <n> --repo <org>/<repo> --json mergeCommit,commits`) and revert what shouldn't be there. Then the admin presses Retry: the writer recognizes a merged PR it already judged by its own `failed` note (only the writer can write those notes), so the Retry plans the step again at `main` (a new PR for what is missing, or done if `main` holds it) instead of failing it again for the same merge. `merged_file_differs` and `merged_file_missing` work the same way |
| After the write App was renamed, the writer refuses its own new PRs (`pr_not_ours`) | The writer reads the write App's bot login (`<app slug>[bot]`) at startup | Restart the writer, then Retry |
| The poller stopped without failing | GitHub disabled the schedule after 60 days without activity (`disabled_inactivity`) | `gh workflow enable scorecard-poll.yml --repo <org>/scorecard-factory` (and `bump.yml` in the deploy repo) |
| "the App is not installed on `<owner>/<repo>`" in the poll summary | The scorecard App misses that repo | Add the repo to the install |

## Gaps

- **Forgeable checks.** Every Actions check shares app id 15368; outside central review (which pins the scorecard's checks to its
  App), drift, preview and the scans can be imitated by a writer of the app repo. The fix is signed check results.
- **Build egress is open.** PR code runs during `docker build` with full network access.
- **The deploy App can write any branch** of `golden-road-deploy`; only the rulesets keep it off `main`.
- **The write App's `main-review` bypass covers prod.** The writer never merges a prod binding, but GitHub can't limit a bypass by path.
- **Private repos need a read credential.** `bump` and `deploy-check` read releases, packages and the factory with the deploy repo's
  own token, which works only because the PoC's repos are public.
- **The image policy and Flux source name `github.com`;** another host needs its own issuer and signer values.
- **Portal-written records in central mode lack `scorecard: central`,** so each new app needs a factory PR before it is reviewed.
- **The findings ruleset is applied per app by hand,** and the repo admin can edit or delete it.
- **No real directory or Secret Server,** and nothing reconciles from the deploy repo.
- **The Cloudflare reconcile job is not built** (procedure 12): no app has a token, and no server's policy names one.
- **The skills catalog is the bundled fixture.** `deploy-check.yml` documents how the real catalog would be checked out and read
  (`GOLDEN_ROAD_SKILLS_CATALOG`); no step does it.
- **The ServiceNow fields are placeholders to confirm** (procedure 13, and `u_golden_road_state` and `u_golden_road_task` on the
  CI for the lifecycle). A CI is retired only by a decommission (`docs/lifecycle.md`).
- **The lifecycle App's Administration: write is broader than archive:** it could change settings or delete a repository. The
  writer holds the only key, mints one-repository tokens and calls only the read, the archive and the revoke; you keep it off the
  platform repos.
- **Lifecycle applied stops at git:** the platform can't see that the cluster pruned or scaled. An environment first pinned while
  the app is suspended is not suspended; bringing an app back resets the portal's state by hand (procedure 18).
- **The writer reads the write App's bot login at startup:** a renamed App needs a writer restart.
- **Scheduled workflows silently stop** after 60 days without activity on a public repo.

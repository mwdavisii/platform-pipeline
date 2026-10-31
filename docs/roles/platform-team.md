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
| `golden-road-deploy` | Everything (`*`) and `/.github/`: `bump.yml`, `deploy-check.yml`, the rulesets, `policy/`, `clusters/`, `external-secrets/` |
| App repos | `/.github/` and the scanner ignore files (CODEOWNERS); the caller workflows and generated files come from the builder |
| The portal | Its deploy kit (`deploy-kits/portal-homelab/`), its settings, its database roles and migrations |
| GitHub Apps | The kit App, the scorecard App (central mode), the deploy App, the portal's read and write Apps |

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

## What you must never do

- Never release the builder before configuring the tree, and never move or reuse a `builder-v*` tag.
- Never mark a builder release `--latest`.
- Never publish from the source repo's history: export first.
- Never use `--single-approver` or `--bump-workflow-token` on an organization.
- Never add the write App, the deploy App or anyone to a `main` ruleset's bypass list.
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
- **Scheduled workflows silently stop** after 60 days without activity on a public repo.

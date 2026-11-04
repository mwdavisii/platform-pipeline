# SOP: security team (cybersecurity)

## Purpose

Security decides what the scanners look for, how much human review each change gets, and what each app may connect to. The
platform requires your results but never runs, wraps or re-scores your scanners, and the AI review only classifies: it never
blocks a merge by itself. This SOP covers the two halves of that job: the security scans (Semgrep, Trivy, CodeQL and the Gate 1
checks that require them) and the AI review (the risk scorecard: its routing rules, levels, records and golden set), plus your
code-owner duties in the factory and the deploy repo.

## Who

The security team, as the GitHub team `security` (`<org>/security` in CODEOWNERS) and the reviewer team for `security_review`.
On a personal account there are no teams: the setup document puts your login (and a deputy's) in CODEOWNERS instead.

## What you own

| Thing | Where | Notes |
|---|---|---|
| The scan workflow | `security-scans` repo: `.github/workflows/scan.yml` (source: `reference/security-scans/.github/workflows/scan.yml`) | App repos call it at `@main`, so every change reaches every app at its next run |
| Scanner tuning | `scan.yml` constants `SEMGREP_IMAGE`, `TRIVY_IMAGE` (pinned by digest), `REGISTRY_RULESETS` (default `p/default`), `IGNORE_UNFIXED` (default `"true"`) | Constants, not inputs: a calling repo can't weaken them |
| Vendored Semgrep rules and the ignore list | `security-scans` `rules/` and `rules/semgrepignore` (source: `reference/security-scans/rules/`) | `rules/semgrepignore` replaces any `.semgrepignore` in the app |
| Thresholds | Both scans fail on `--fail-on error` (the report step in `scan.yml`); Trivy reports only `--severity HIGH,CRITICAL` (`reference/security-scans/scripts/run_trivy.sh`) | Both in `security-scans` |
| CodeQL | GitHub's default setup on each app repo, and the `code-scanning` ruleset (`bootstrap/hello-app/.github/rulesets/code-scanning.json`) | The alert threshold is enforced at merge, not by Gate 1 |
| Routing rules (AI review) | `scorecard-factory` `config/routing.toml` | Read [routing.md](../scorecard/routing.md). Starting file: `tests/scorecard/fixtures/factory-routing/config/routing.toml` |
| Intake records | `scorecard-factory` `intake/<owner>/<repo>.yaml` | CODEOWNERS `/intake/ @<org>/security` |
| Accepted scorecard findings | `scorecard-factory` `suppressions/<owner>/<repo>/*.json` (`override/v2`) | At most 90 days; never a secrets finding |
| Confirmed N/A | `scorecard-factory` `na/<owner>/<repo>.yaml` (`na/v1`) | Read only by the ASVS checklist engine (`review-1`) |
| The registration floor | `scorecard-factory` `config/registration.yaml` (`registration-rules/v1`) | The portal's collector and the scorecard's T-21 both read it |
| Binding review | `golden-road-deploy` `apps/**/bindings.yaml` | Shared with `analytics`; CODEOWNERS can't own part of a file |
| A decommission's factory PR | `scorecard-factory` `intake/<owner>/<repo>.yaml`: the `decommissioned` key the writer adds | You merge it (`/intake/` is yours); it adds only that key |
| The skills catalog | `golden-road-skills` (`platform.json`'s `skills_repo`): `skills/<name>/SKILL.md`, its files and `skills/<name>/skill.yaml` (`skill/v1`) | CODEOWNERS on `skills/` names security. In the PoC: the fixture `bootstrap/golden-road-portal/portal/mock_skills_catalog.json` |
| Which MCP servers apps may use | Each server's Cloudflare Access policies on the two shared MCP gateways (test and prod) | With the gateways' admins; the app's token is added to a server's Service Auth policy by the platform's job (not built) |
| The platform's Secret Server account and identity process | See [secret-server-admin.md](secret-server-admin.md) and [identity-admin.md](identity-admin.md) | The portal calls these "security's process" |

**Not yours, but you ask for changes:** `config/scorecard.yaml` (the model, gateway, budget and Gate 1 list; CODEOWNERS `*` is
`platform-reviewers`), the review prompt (`src/scorecard/prompts/review-2.md`, frozen by checksum), the detectors written in code,
the golden set cases (in `platform-pipeline`, under `tests/scorecard/`), and `platform/required-checks.json` (baked into the
builder image). Changing any of them is a platform pull request and, for the code and the prompt, a builder release.

## Before you start

- Write access to `security-scans`; review rights on `scorecard-factory` and `golden-road-deploy` (membership of `security`).
- `gh` signed in to the platform's GitHub host, `jq`, and Docker for the local checks.
- A checkout of `scorecard-factory` and the builder image digest it pins (`BUILDER_IMAGE` at the top of its workflows), to run
  `scorecard check-factory` locally.
- For live golden runs: a gateway key and model name for the reviewer model (they cost tokens).

## Today (mock) and in production

| | Today (personal PoC) | In production (an organization) |
|---|---|---|
| Scanners | The reference `security-scans` (Semgrep CE with a small vendored rule set plus `p/default`, Trivy, CodeQL default setup), owned by the account that owns the apps | Your own tools and thresholds. The platform lists only their check names and sources in `platform/required-checks.json` |
| Scan results | A check name from the shared Actions app (id 15368), which a writer of the app repo can imitate | The same gap until scan results are signed and the release verifies the signature |
| Review enforcement | The level is advisory; high or critical deterministic findings hold the merge through `scorecard / findings` | The same; enforcing the level waits for calibration |
| Bindings you review | Point at entries of the mock directory and the mock Secret Server; nothing is applied | Point at real principals, warehouses and secrets |

## Procedures

### 1. Change a scanner rule, image or threshold

**Goal:** a reviewed change to what the scans check, reaching every app at once.

1. On a branch of `security-scans`, edit `scan.yml` (the constants at the top) or `rules/`. A vendored rule needs annotated
   examples next to it (`rules/<name>.py` or `.js`), which `semgrep --test` checks.
2. Run the repo's tests:
   ```bash
   pip install pytest pyyaml
   pytest                 # script, report and workflow-policy tests; no Docker, no network
   pytest -m docker       # the pinned Semgrep image against the rules
   ```
3. To bump a scanner, pin the new image by digest (`SEMGREP_IMAGE`, `TRIVY_IMAGE`), never by tag.
4. Open the pull request, have it reviewed, merge it.

Never rename the job ids (`semgrep`, `trivy`) or the caller's job id (`security-scans`): they are the check names that the app
rulesets and Gate 1 require. A rename needs procedure 3 first.

**Check:** the next PR on an app shows `security-scans / semgrep` and `security-scans / trivy` with the new behaviour, and the
check names are unchanged.

### 2. Handle a scan false positive

**Goal:** the finding stops failing without anyone silencing it in the app.

The scans ignore the app's suppression files and comments: the tree is copied without `.semgrepignore`, inline `nosemgrep`
comments are disabled, and Trivy runs from a directory where the app's `trivy.yaml` and `.trivyignore` are never read. One
exception: Trivy honours an inline `#trivy:ignore` comment in hand-written infrastructure-as-code files. A scorecard rule also
sends any PR that adds a suppression, that comment included, to security review (T-15). So a false positive is fixed in
`security-scans`:

1. Read the finding in the PR comment or the SARIF artifact (`security-scans-semgrep`, `security-scans-trivy`).
2. Decide the fix: narrow the vendored rule, add a path to `rules/semgrepignore`, drop a registry ruleset from
   `REGISTRY_RULESETS`, or (for an unfixable vulnerability) rely on `IGNORE_UNFIXED`.
3. Make the change as in procedure 1, and tell the app owner to push again.
4. If the finding is in the platform-pinned base image or a generated file, it is the platform's to fix (they bump the builder's
   base image and regenerate every app). Hand it to the platform team.

**Check:** the app's next run of the scan passes, and the change applies to every app (there is no per-app exception).

### 3. Add or change a check that Gate 1 and merge require

**Goal:** a scan check is required at merge and at release, pinned to the source that posts it.

1. Find the exact name and app id on a real commit of an app (do not assume them):
   ```bash
   sha=$(gh api repos/<owner>/<repo>/commits/main --jq .sha)
   gh api "repos/<owner>/<repo>/commits/$sha/check-runs" --paginate --jq '.check_runs[] | {name, app_id: .app.id, app: .app.slug}'
   ```
2. Give the name and app id to the platform team. They change three places together: `platform/required-checks.json` (Gate 1;
   baked into the builder image, so it takes a builder release), the app rulesets' `required_status_checks`
   (`bootstrap/<app>/.github/rulesets/main.json`), and the factory's `gate1_checks` in `config/scorecard.yaml`, so the
   scorecard waits for the same list.

**Check:** a new test release's gate record lists the check with `"state": "success"`:
`gh release download <test tag> --repo <owner>/<repo> -p gate-record.json && jq '[.required_checks[] | {name, state}]' gate-record.json`.

### 4. Turn on CodeQL and its merge threshold for an app

**Goal:** CodeQL runs from GitHub's default setup and blocks merge on high alerts.

```bash
jq -n '{state: "configured", query_suite: "default", languages: ["python", "actions"]}' \
  | gh api -X PATCH repos/<owner>/<repo>/code-scanning/default-setup --input -
gh api repos/<owner>/<repo>/code-scanning/default-setup --jq '{state, languages, query_suite}'
```
Use the app's languages (`javascript-typescript` for a Node app). After the first CodeQL run on `main` has finished, add the merge
rule (a separate ruleset, so a wrong field can't break `main`):
```bash
gh api -X POST repos/<owner>/<repo>/rulesets --input bootstrap/hello-app/.github/rulesets/code-scanning.json
```
The rule's exact API fields are unconfirmed: if the API refuses it, set "Require code scanning results" in the ruleset UI (tool
`CodeQL`, alerts `High or higher`).

**Check:** `gh api repos/<owner>/<repo>/rulesets --jq '.[].name'` lists `code-scanning`, and a completed CodeQL run is on `main`.

### 5. Set the registration floor

**Goal:** the minimum tier and classification the platform computes for a registration matches your policy.

1. On a branch of `scorecard-factory`, edit `config/registration.yaml`: `no_spec` (the floor for a repo without
   `builder-spec.json`, today `{tier: high, data_classification: confidential}`), `classification_tier` (the tier each level needs,
   today `{public: low, confidential: high, protected: high}`; it must not decrease from `public` to `protected`), `raise_to` (the
   tier each signal raises the floor to: `write_roles`, `secret_env`, `model_gateway_egress`, `other_egress`,
   `many_dependencies`) and `many_dependencies_over`.
2. Run the checker with the pinned image (step 3 of procedure 7), open the PR and merge once `factory-check` passes.
3. Compare each `intake/` record with the new floor. An app registered below it is at `security_review` on every PR (T-21) until
   its record is raised (procedure 12).

**Check:** the next registration's evidence on the portal's Approvals page tags the new floor values "floor", and `factory-check`
passed on the merge.

### 6. Add or fix an intake record by hand

**Goal:** an app has a trusted tier, purpose and classification when the portal can't record it.

The portal's writer adds `intake/v2` records after a portal admin approves them, and merges them through its `main-review` bypass
(so your code-owner review is not applied to those; see Gaps). Add a record by hand when the portal isn't running, or to change an
existing record (the portal's writer fails a registration for a repo that already has one, with `already_registered`):

```yaml
# intake/<owner>/<repo>.yaml
schema: intake/v1
repo: <owner>/<repo>
tier: moderate                       # low, moderate or high
purpose: "What the app does and what it does not do."
owner: "<team or person>"
data_classification: confidential    # public, confidential or protected
```
Add `scorecard: central` when the factory runs central review, or the poller never assesses the app. Open the PR; you approve it
as code owner; it merges once `factory-check` passes. Delete an app's `intake/`, `na/` and `suppressions/` records in the same
change that retires it: records are keyed by repo name, so a re-created repo would inherit them.

**Check:** `golden-road doctor` in the app reports the `intake` check passing, and its next scorecard no longer names T-8.

### 7. Change the routing rules (what sends a PR to a person)

**Goal:** a new or changed rule, proven in shadow before it routes anything.

Follow [routing.md](../scorecard/routing.md), "Add a rule safely". In short:

1. Add the fact and the rule with `mode = "shadow"` (and `on_unsure` for an agent fact). Use the next free id (`T-27` for the
   starting file). Write `guidance` as an instruction: the app team's agent reads it.
2. For a new agent question, add golden cases to `tests/scorecard/golden_routing/cases.yaml` (in `platform-pipeline`, by a
   platform PR): one `yes`, one `no`, and a `false-alarm-` case if it might trip on ordinary code. Run them in fake mode:
   ```bash
   .venv/bin/pytest tests/scorecard/test_routing_golden.py -q
   ```
3. Check the factory with the pinned image:
   ```bash
   docker run --rm --network none --read-only --tmpfs /tmp --user "$(id -u):$(id -g)" -v "$PWD:/factory:ro" "$BUILDER_IMAGE" \
     scorecard check-factory --factory /factory
   ```
   It prints one line of counts. Fix every refusal; read every warning (a fact no rule uses).
4. Open the PR; it merges once `factory-check` passes.
5. Watch `routing.shadow` and `routing.undecided` in `scorecard.json` on real PRs (procedure 10 says where to find it).
6. A second PR sets `mode = "enforce"`. To pull a rule back, set it to `shadow` again.

Levels are `standard` < `elevated` < `security_review`. A rule can only raise the level; the PR's level is the highest of the
intake floor and every enforced rule that fired. Reviewer teams per level are `[reviewers]` in `routing.toml`
(`reviewers` in `config/scorecard.yaml` before the switch to routing); `[]` on a personal account.

**Check:** `check-factory` prints counts with no `error`, `factory-check` passes, and the rule appears in `routing.shadow` (shadow)
or under Reasons in the comment (enforced) on a PR it should match, and nowhere else.

### 8. Accept a scorecard finding (suppression)

**Goal:** a reviewed, time-boxed acceptance of one finding on one app.

1. Get the finding's `id` and `rule_id` from the run's `scorecard.json` (procedure 10).
2. Add `suppressions/<owner>/<repo>/<name>.json`:
   ```json
   {
     "schema": "override/v2",
     "id": "<repo>-<yyyy-mm-dd>-<n>",
     "repo": "<owner>/<repo>",
     "finding_id": "<64 hex characters from scorecard.json>",
     "rule_id": "<rule id, for example T-15/suppression-file>",
     "approver": "<your login>",
     "justification": "Why the finding is accepted, and what will fix it.",
     "created": "<yyyy-mm-dd>",
     "expires": "<at most 90 days later>"
   }
   ```
3. Open the PR, merge once `factory-check` passes.

A secrets finding can never be suppressed (category `secrets`, or a rule id starting with a prefix in `[suppressions]
secret_rule_prefixes`, today `secret` and `gitleaks`): the secret must be rotated. An expired
suppression is ignored and listed under "Expired suppressions (ignored)" in the comment.

**Check:** on the app's next push the comment marks the finding "(suppressed in the factory)", and if it was high or critical,
`scorecard / findings` says "No high or critical findings".

### 9. Confirm that an ASVS requirement does not apply (N/A)

**Goal:** a requirement the agent proposed as `na` stops counting as `needs_human`. Only for a factory on the ASVS checklist
engine (`prompt_version: review-1`, no `config/routing.toml`); risk routing asks no ASVS questions.

Add `na/<owner>/<repo>.yaml` (`schema: na/v1`, `repo`, and `entries` of `asvs_id`, `justification`, `confirmed_by`, `confirmed`),
as in `bootstrap/scorecard-factory/na/README.md`. Each `asvs_id` must exist in ASVS 5.0.0.

**Check:** the next scorecard counts the requirement as `na` with `status_source: human_na`.

### 10. Read a scorecard and review a routed PR

**Goal:** review what the scorecard sent you, from the record the author can't edit.

1. **The level.** Read it from the `scorecard / level: <level>` check, or the newest `scorecard / assessed` run's summary. The
   `scrutiny:<level>` label and the comment are conveniences anyone with write access can change.
2. **The comment** (headed `## Scorecard: STANDARD`, `ELEVATED` or `SECURITY REVIEW`):
   - **Merge blocked:** present when a high or critical deterministic finding holds the merge.
   - **Reasons:** each enforced rule that fired, with what it saw and a `Fix:` line (its `guidance`). "Nothing needs a person"
     means the level came from the intake floor.
   - **Findings:** severity, rule id, file and line, and `Fix:`. "(evidence not verified)" means code could not confirm the cited
     text; "(suppressed in the factory)" means a suppression applies.
   - **Gate 1 (context, not re-scored):** the scan and drift states the scorecard saw.
   - **Review:** the model version, prompt and whether the review was complete. An incomplete review raises the level (T-14).
3. **The full record.** Per-app mode: `gh run download <run id> --repo <owner>/<repo> -n scorecard`. Central mode: the same
   artifact is in the factory's assess run (`--repo <org>/scorecard-factory`), and the `level` check's text carries the redacted
   report. Then:
   ```bash
   jq '{schema, level, reasons: [.reasons[].trigger], shadow: [.routing.shadow[].trigger], undecided: .routing.undecided, complete: .assessment.complete}' scorecard.json
   ```
4. Review the PR in GitHub against the reasons. The scorecard checks that cited lines exist, not that they justify an answer, so
   look at the code. Approve or request changes as usual.

**Check:** your review is on the PR, and any question you could not settle is a comment the author can act on.

### 11. Decide a held merge (high findings)

**Goal:** a person decides whether a PR with a high or critical deterministic finding merges.

`scorecard / findings` fails on such a finding and is required by the app's `scorecard-findings` ruleset. Its only bypass is the
repo admin role in the PoC, or the org team named by `configure.py --findings-bypass-team` (the platform team on an org). The
override holder merges with "Merge without waiting for requirements". Your part: review the finding (often T-16, a workflow or
CODEOWNERS change) and tell the override holder whether it may merge. Medium and low findings, and the agent's own findings, never
hold a merge.

To audit overrides:
```bash
gh api 'repos/<owner>/<repo>/rulesets/rule-suites?rule_suite_result=bypass' --jq '.[] | {actor_name, ref, after_sha, pushed_at, result}'
```

**Check:** every bypass in that list matches a decision you recorded on the PR.

### 12. Raise an app's registration after T-21

**Goal:** an app whose change needs a higher tier or classification is registered at that level.

T-21 fires when the app's registered tier or classification is below the floor computed from the PR. An override recorded at
approval does not clear it; only raising the record does. The app's repo admin asks for a change in the portal (**Change or renew the
registration**) and a portal admin approves it at the level the floor needs. Edit `intake/<owner>/<repo>.yaml` by factory PR only
when the portal can't: an `intake/v1` the portal must not write, such as the portal's own (keep its other fields and run
`check-factory`).

**Check:** the next PR on the app no longer lists T-21.

### 13. Run the golden set before a model or prompt change

**Goal:** know what a model gets right and wrong before it reviews real PRs.

Fake mode (no credentials) is part of the test suite. Live (costs tokens):
```bash
GOLDEN_ROAD_SCORECARD_LIVE=1 GOLDEN_ROAD_SCORECARD_FACTORY=<factory dir with config/routing.toml> \
  SCORECARD_GATEWAY_TOKEN=<token> SCORECARD_GATEWAY_MODEL=<model> \
  PYTHONPATH=src .venv/bin/pytest -s tests/scorecard/test_routing_golden_live.py
```
(`tests/scorecard/test_scorecard_golden_live.py` for a `review-1` factory.) Set `GOLDEN_ROAD_SCORECARD_REPORT=<path>` to keep the
report; the routing test writes it with a `routing-` prefix on the file name. The report gives true and false positives and
negatives per fact; an `unsure` answer is counted as a false negative or a false positive, and there is no unsure rate. Count the
`unsure` answers in each case's answers yourself: a fact that is mostly `unsure` under `on_unsure = "elevated"` floods reviewers.

**Check:** the run passes (it asserts nothing about answers), and you have recorded the per-fact numbers before agreeing to the change.

### 14. Agree a model or gateway change

**Goal:** the reviewer model changes only with your agreement, and the record says which model reviewed what.

The platform team owns the `agent` block of `config/scorecard.yaml` and the gateway secrets. Ask for, or check, these together:
the model behind `SCORECARD_GATEWAY_MODEL` and a raised `agent.model.version` in the same change (`scorecard run` refuses an
assessment from any other version, and nothing else ties the two), a golden run (procedure 13), `agent.budget` (`0` means no limit
for `max_total_tokens` and `max_run_seconds`), and `agent.model.temperature` (`null` if the model rejects one).

**Check:** the comment's Review line names the new model version on the next PR.

### 15. Review a binding pull request in `golden-road-deploy`

**Goal:** an app runs with the identity, data connection and secrets it should, in that environment.

See [deploy-reviewer.md](deploy-reviewer.md), procedure 2. As a `bindings.yaml` code owner you check that the principal was made
for this app and environment, that the warehouse grant is intended, and that each secret reference is the app's own. `deploy-check`
does not compare a binding with the directory or Secret Server: the portal's writer checked them once, and a hand-edited
`bindings.yaml` skipped that check entirely.

For MCP servers, check that each server is one this app should reach in that environment; for skills, that each is the catalog's
at a commit on its `main`. `deploy-check` holds the gateway to `platform.json`'s and rebuilds the skills from the catalog.

**Check:** the PR changes exactly `bindings.yaml` and `composed.yaml` for one environment (and `cloudflare-access.yaml` when its MCP
servers change), `deploy-check` passed, and you approved it.

### 16. Tune the re-review signals

**Goal:** the Re-review list catches the changes security wants a person to look at.

1. Set `PORTAL_REREVIEW_SIGNALS` in the kit's config map: trigger IDs (`T-17`), the recorded signals `builder-bumped`,
   `egress-removed`, `resources-lowered`, and `security_review` (any trigger at that level), comma-separated; empty is the default
   (`security_review`, `T-17`, `T-18`, `T-19`, `egress-removed`).
2. Restart the collector. The setting applies to events read from then on.
3. Keep the factory's tag ruleset that forbids tags named like a branch (`main`): GitHub reports a run dispatched from such a tag as
   on `main`. The portal also checks that a central run's head is on `main`, and counts any other run as `untrusted_run`.

**Check:** the collector starts (a bad value stops it with the setting's name), and the next matching PR's run appears on
`/admin/rereview`.

### 17. Approve a skill for the catalog

**Goal:** only skills security reviewed can be mounted into an app, in the environments and for the people you chose.

1. A skill arrives as a pull request to the catalog repo, `golden-road-skills`, adding or changing `skills/<name>/`. CODEOWNERS on
   `skills/` names `<org>/security`, so it needs your review.
2. Check `skills/<name>/skill.yaml` (`skill/v1`): `name` (lower case letters, digits and `-`, the folder's name), a one-line
   `description`, `environments` (`[test]`, `[prod]` or both) and `audience` (`everyone`, or a list of identity provider groups).
   The audience decides who may pick it in the portal; the environments are checked again by `deploy-check`.
3. Check `SKILL.md` (required) and every file beside it: text the app's model will read as instructions. Read it as you would a
   prompt change: no secrets, no instructions to fetch or run anything the app shouldn't. `skill.yaml` itself is not mounted.
4. The limits (compose refuses more): UTF-8 text with no NUL, at most 64 files and 256 KiB per skill, and 512 KiB per environment.
   A path is segments of letters, digits, `.`, `_` and `-` joined by `/`, with no hidden file, no `..` and no `__` in a segment.
5. Merge it to `main`. Apps pin a skill by its commit, and `deploy-check` accepts only a commit on `main`: an unmerged branch is
   "not in the catalog". An app keeps the commit it pinned until its owner presses **Update**.

In the PoC the catalog is the fixture bundled with the portal and the builder; changing it is a platform pull request and a release.

**Check:** the skill is offered on an app's tab for the environments you listed, and only to its audience.

### 18. Let apps use an MCP server on the gateway

**Goal:** a server on a shared MCP gateway can be used by the apps that pick it, and by no other app.

1. A server is added to the test or prod gateway by its admins (in Cloudflare, not in git). Its Access policies decide which people
   see it in the portal.
2. For apps, the server needs a Service Auth policy: Cloudflare then admits a service token only if that policy names it. The
   platform's job (platform-team procedure 12) adds each app's token to the policies of the servers in its `cloudflare-access.yaml`;
   nobody adds an app's token by hand.
3. A server that acts as each signed-in person (`on_behalf`, "Require user auth") stays person-only: Cloudflare excludes it from
   service-token sessions, and the portal greys it ("This server needs each person's own sign-in, so an app can't use it."). Don't
   try to open it to apps; ask for a server that apps can use instead.
4. Review the app's binding PR (procedure 15): which servers it adds in which environment.

**Check:** the server is offered on an app's tab to the people its policies allow, and `cloudflare-access.yaml` in the deploy repo
lists it for the apps that picked it.

### 19. Ask for a suspension for a critical vulnerability

**Goal:** an app with a critical vulnerability stops running in every environment until it is patched, and ServiceNow records it.

1. Open a ServiceNow task for the platform team that names the app's CI (its `cmdb_ci` is the app's business application; the app
   page shows its ServiceNow number) and says in its description: suspend, the CVE, and why. The ServiceNow admin provides the task
   type (`docs/roles/servicenow-admin.md`, procedure 3).
2. A portal admin applies it (portal-admin procedure 10, `docs/lifecycle.md`): every environment scales to zero, prod included, with
   no reviewer to wait for; nothing is deleted. You can't press the button yourself unless you are a portal admin.
3. Follow the task's work notes: the request, the PR opened, the merge ("every environment scales to zero when the cluster's
   reconcile applies it"), and the close (Closed Complete). "Applied" means applied in git: the platform can't see the cluster.
4. Once the owner has patched and released, ask for the resume through the same task's assignee or a new task; a portal admin
   resumes it (portal-admin procedure 11).

**Check:** the task is Closed Complete with "Applied in git: <owner>/<repo> is suspended for SCTASK...", and the app page shows the
suspended banner.

## What you must never do

- Never suppress a finding inside an app (`nosemgrep`, `# nosec`, `.trivyignore`, dismissing a CodeQL alert). Fix the rule.
- Never suppress a secrets finding: rotate the secret.
- Never rename the scan jobs or change check names without procedure 3.
- Never pin a scanner image by tag.
- Never set a routing rule straight to `enforce` without a shadow period and golden cases.
- Never copy the bootstrap `config/scorecard.yaml` over a live factory's: it holds the gateway, model, budget and tuning.
- Never approve a binding PR whose principal, warehouse or secret you have not checked against its source.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `security-scans / trivy` fails on every app at once | A fixed HIGH or CRITICAL in the platform-pinned base image | The platform bumps the builder's base image and regenerates the apps. The early warning is `GOLDEN_ROAD_SCANS_TRIVY=1 .venv/bin/pytest -q -m docker tests/pipeline/test_security_scans_trivy_docker.py` |
| The scan job fails at its first step | `job.workflow_repository` or `job.workflow_sha` missing or equal to the caller: the checkout guard refused | Call the workflow from another repo, by `uses:` at `@main`; on an enterprise host ship the scripts in a digest-pinned image |
| `scorecard / precondition` fails with "Gate 1 check 'security-scans / trivy' is failure" | A scan is red, so nothing is assessed (no tokens spent) | Fix the scan finding; the next push assesses |
| `check-factory` refuses `routing.toml` | An unknown key, a wrong condition for the fact type, a missing `on_unsure`, an unfillable placeholder, a bad regex, or a duplicate id | The message names the file and problem; see "What `scorecard check-factory` refuses" in [routing.md](../scorecard/routing.md) |
| A run fails with "scorecard-config/v2, which needs config/routing.toml next to it" | The factory's intake action on `main` is the old one and does not copy `routing.toml` | Update `.github/actions/intake/action.yml` in the factory (personal PoC section 17.1, step 2) |
| `factory-check` fails with "`internal` is retired: use `confidential`" | A record or rule names a retired level | Map `internal` and `restricted` to `confidential`, `phi` to `protected` |
| `check-factory` says "it has floor_override but the record is not below the floor: delete floor_override" | The record is no longer below the floor (for example after the 0.5.0 mapping) | Delete `floor_override` from that record |
| Every PR of an app is `security_review` with T-21 | The app is registered below the floor | Procedure 12 |
| Every PR of an app is `security_review` with T-8 | No intake record | Procedure 6 |
| In central mode an app's PRs are never assessed | Its intake record lacks the exact line `scorecard: central` | Procedure 6 |
| A suppression does nothing | It expired, its `finding_id` or `rule_id` differs, or it is a secrets finding | Check the comment's "Expired suppressions (ignored)" list and the ids in `scorecard.json` |

## Gaps

- **Scan results can be forged.** Every Actions check comes from app 15368, so a writer of the app repo can post a passing check of
  the same name. Code-owner review of `.github/` is the only barrier. The fix (signed scan results the release verifies) is not built.
- **Trivy honours inline `#trivy:ignore` comments** in hand-written infrastructure-as-code files, so such a file can suppress its
  own misconfiguration finding; only the scorecard's T-15 review catches it.
- **No per-app waiver for a scanner finding.** The scans have no exception list per app and no expiry. An accepted risk in one app
  can only be handled by a rule change that applies to every app.
- **The bootstrap factory CODEOWNERS does not list `config/routing.toml`.** [routing.md](../scorecard/routing.md) says security owns
  it, but `bootstrap/scorecard-factory/.github/CODEOWNERS` gives it to `platform-reviewers` (`*`); the setup document adds the line
  by hand when switching to routing. The bootstrap factory also still ships the `review-1` config with no `routing.toml`.
- **Portal-written intake records bypass your code-owner review.** The portal's writer merges `intake/` PRs through its
  `main-review` bypass after a portal admin approves. Your control over those records is who holds the portal `admin` role.
- **Re-review reads only events and dates.** History signals and APIM usage are not collected; an artifact that expired before the
  collector read it is counted, not shown.
- **Per-app mode trusts the app's own run.** In per-app mode the pull request's author could overwrite the `scorecard` artifact after
  the factory's reusable workflow wrote it, so a per-app event is only as good as that repo's protections. Accepted (recorded in the
  0.5.1 spec); central mode, the default, isn't affected, and a per-app event never hides a central one.
- **The level is advisory.** Only high or critical deterministic findings hold a merge. Enforcing the level at merge or Gate 2 waits
  for calibration and a way to check team membership.
- **Accuracy is not calibrated.** No live accuracy target is set; the golden set reports numbers but asserts none.
- **Overrides of high findings are logged only by GitHub's rule suites,** and on a per-repo ruleset that the repo admin (the app
  owner) can edit or delete.
- **`deploy-check` does not verify a binding against its source.** It checks shape and generated files only; the directory and
  Secret Server checks happen once, in the portal's writer.
- **A suspension is applied in git only** (`docs/lifecycle.md`): the platform can't confirm the pods stopped, and a ServiceNow task
  is needed for it (no task, no suspension).
- **The scorecard runner's network egress is not restricted** on GitHub-hosted runners.
- **Who may use which MCP server can't be confirmed in the mock.** The mock's per-user policy stands in for Cloudflare evaluating
  the person against each server's Access policies, and it is checked once, at submit. No token is issued and no Service Auth
  policy changes: the reconcile job is not built.
- **A skill's audience is checked once, at submit,** against the person's sign-in groups. `deploy-check` re-checks the commit, the
  environments and the size, never the audience.

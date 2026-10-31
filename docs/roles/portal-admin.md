# SOP: portal admin

## Purpose

Decide app registrations in the golden road portal, keep the link between people and their GitHub logins right, and read the
audit log. An approved registration becomes the app's trusted intake record (`intake/<owner>/<repo>.yaml` in `scorecard-factory`),
which sets the floor of every review the app gets, so an approval is a security decision.

## Who

People in the identity-provider group that `PORTAL_ROLE_GROUPS` maps to `admin` (for example `{"admin": ["<admins group>"], "ops":
["<ops group>"]}`). The portal reads roles only from the OIDC groups claim (`PORTAL_OIDC_GROUPS_CLAIM`, default `groups`), never
from an edge header. Because the writer merges approved records past the factory's review rule, admins should be security
people or people security trusts.

## What you own

| Screen | What you do there |
|---|---|
| `/admin/approvals` | Approve or send back pending registrations |
| `/admin/people` | Add or remove a person's GitHub link, with a reason |
| `/admin/directory` | See the directory apps pick from (read only) |
| `/admin/audit` | Read the newest 200 audit entries (append only) |
| `/admin/identity-links.json` | Export the active GitHub links as `identity-link/v1` |

You don't own the files the portal writes. The writer process records them; security and the platform own the repos.

## Before you start

- A portal account in the admin group. Sign out and in again after a group change: roles come from the sign-in.
- Your own GitHub link: link your own login on `/me` with OAuth. An admin-made link must come from another admin; nobody makes
  one for themselves.
- The registration floor rules and the classification policy (the policy link shows on the forms when
  `PORTAL_CLASSIFICATION_POLICY_URL` is set).

## Today (mock) and in production

| | Today (personal PoC) | In production |
|---|---|---|
| Approvals, send back, people, audit | Real: the database enforces them (no self-approval, the floor, append-only audit) | The same |
| The writer's factory PRs | Real PRs on the PoC's public factory | The same, on an internal factory |
| `/admin/directory` | Lists the mock directory (`bootstrap/golden-road-portal/portal/mock_directory.json`), or says it is off | A real directory (not built) |

## Procedures

### 1. Approve a registration

**Goal:** a pending registration becomes an intake record with the right tier, classification, review date and reasons.

1. Open `/admin/approvals`. For each request read: the repo and purpose, who asked and their GitHub links, whether the portal saw
   them as the repo's admin recently, and the platform's evidence (the floor tier and classification, and the signals that raised it).
2. You can't decide your own request ("You requested this, so another admin must decide it.").
3. Pick the tier (`low`, `moderate`, `high`) and the classification (`public`, `confidential`, `protected`). The floor's values are
   tagged "floor".
4. Set the review date (default: today plus `PORTAL_REVIEW_MONTHS`, 12 unless set; it must be after today and within 3 years).
5. Write the justification: 10 to 1000 characters, one line.
6. If you choose below the floor, the override field appears. Write why, 30 to 1000 characters, one line. The reason is stored in
   the record (`floor_override`). It does not stop the scorecard: an app registered below the floor is at `security_review` on every
   PR (T-21) until the record is raised.
7. Approve. The writer opens a PR in `scorecard-factory` adding `intake/<owner>/<repo>.yaml` (`intake/v2`) and merges it once
   `factory-check` passes.

If the factory runs central review, tell the platform or security team: a record the portal writes has no `scorecard: central`
line, and without it the poller never reviews the app.

**Check:** the request's timeline reads Submitted, Reviewed, Recorded (Done), and the record exists:
`gh api repos/<org>/scorecard-factory/contents/intake/<owner>/<repo>.yaml --jq .path`.

### 2. Send a registration back

**Goal:** the requester changes the registration and submits it again.

On `/admin/approvals`, write what to change (10 to 1000 characters, one line) and send it back. The requester edits it from My
requests; the platform computes fresh evidence for the new submission. Send back a request whose evidence names a retired level
(from before builder 0.5.0): it can't be approved.

**Check:** the request shows Sent back, and it returns to your list when submitted again.

### 3. Link a GitHub login to a person

**Goal:** someone who can't link their login with OAuth gets a link an admin vouches for.

1. Prefer self-service: the person links their own login on their page with OAuth. An OAuth link proves they hold the account; an
   admin link does not, and the mock Secret Server never maps an admin-made link.
2. On `/admin/people`, find the person, enter the GitHub login and a reason (10 to 1000 characters, one line), and add it. If the
   collector has seen that login, the portal keeps its numeric user id too.
3. You can't link a login to yourself, and a login linked to someone else is refused.

**Check:** the person's row shows the link, and `/admin/audit` has the entry with your reason.

### 4. Remove a GitHub link

**Goal:** a login that is wrong, reused or belongs to someone who left no longer counts.

On `/admin/people`, remove the link with a reason (10 to 1000 characters). If that was the only login the portal saw as the repo's
admin, their pending requests can then no longer be approved ("The requester is no longer an admin of that repository, as the
portal last saw it.").

**Check:** the link is gone from the row, and the audit log has the removal.

### 5. Look at the directory

**Goal:** see which principals and warehouses apps can pick, to answer "why can't I see mine?".

Open `/admin/directory`. It lists each service principal (id, display name, client id, the repo it is tagged for) and each SQL
warehouse (host, HTTP path, catalog, schema, the principals it is granted to). An app's page offers only principals tagged for that
repo and warehouses granted to its linked principal. "The directory is off on this portal: PORTAL_DIRECTORY is not set" means app
connections are switched off.

**Check:** the entry the person expects is listed, tagged for their repo (or granted to their principal).

### 6. Read the audit log

**Goal:** find who did what and why.

Open `/admin/audit`: the newest 200 entries, newest first, each with when, who, the action, the subject, and the reason with
the record before and after. The log is append-only, enforced by a database trigger: no process can change or delete an entry; only the schema owner could
remove the trigger, and a migration would show it. For older entries, ask the platform team to query the database.

**Check:** the action you are looking for is there with its reason.

### 7. Approve a change or renewal of a registration

**Goal:** a registered app's record says what its owner and the floor now need.

1. Open Approvals. A change is tagged **change** and shows the current record beside the proposal (current → proposed); a changed
   row is highlighted. The current record is `intake/v1` (registered outside the portal), `intake/v2` written by the portal, or
   `intake/v2` written by hand; one that isn't valid is replaced whole.
2. "Owner changes from A to B" means another admin of the repo asked: approving makes them the owner (an ownership transfer).
3. A renewal keeps the purpose and the classification: decide the next review date.
4. Decide the tier, the classification and the next review date as for a first registration: the floor and the override rules are
   the same. A `scorecard: central` mark is kept. A request that shows `record_unreadable` or `record_case_mismatch` waits for the
   collector (the factory's record can't be read, or is there under two spellings of its name: the platform team fixes the factory).

**Check:** the writer's PR "Change the registration of ..." lists the fields you changed and merges. `record_changed` means the
record moved after you approved, and `record_conflict` that GitHub refused the write: the requester asks again.

### 8. Work the Re-review list

**Goal:** every app on `/admin/rereview` gets a decision.

1. Each item is one app and one reason: **Review overdue** or **Review due soon** (the record's `review_by`), or **Scorecard signal**
   (a pull request's scorecard run raised a signal security asked for, listed with its PR, head, level and the signals that matched).
2. A date item is best closed by the owner renewing (app-developer procedure 11, which you approve with procedure 7): it then closes by
   itself.
3. Close an item with a one-line note of what you decided (10 to 1000 characters). You can't close an item for an app you own, or one
   whose intake record can't be read. If a new scorecard event joined while you read, the page comes back with it: read it, close again.
4. A **stale** tag means open more than the stale days (`PORTAL_REREVIEW_STALE_DAYS`). `actions_unreadable`, `factory_unreadable`
   `factory_not_main` and `artifact_host_refused` mean the read App lacks a permission or access, the factory's default branch isn't
   `main`, or GitHub redirects artifact downloads to a host the collector won't fetch from: tell the
   platform team. Runs the collector won't trust (`untrusted_run`) and events it can't use are counted, never shown. "The collector"
   lists each part's last good pass; a part flagged as not working means the collector has stopped.

**Check:** the item leaves the list and the audit log shows `rereview_item.closed` with your note.

## What you must never do

- Never approve your own registration, or one for an app you build, even if the portal would let another admin's link through.
- Never approve below the floor without a reason a security reviewer would accept.
- Never make an admin link for a login you have not confirmed belongs to that person.
- Never approve during a platform upgrade the platform team has asked you to drain (for example the 0.5.0 upgrade: no approvals
  until the portal is on the new release).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| "The platform hasn't computed this repo's evidence yet: approving waits for it." | The collector has not read the repo for this submission | Wait for the collector's next pass |
| "The platform's evidence for this request names a retired classification level" | Evidence from before builder 0.5.0 | Send it back; resubmitting computes fresh evidence |
| "This registration changed since you loaded this page" | It was sent back and submitted again meanwhile | Reload and look again |
| "That tier or data classification is below the platform's floor, and the reason to approve it anyway is missing or not valid." | Below the floor without a 30-character reason | Raise the choice, or write the reason |
| "The requester is no longer an admin of that repository, as the portal last saw it." | The collector no longer sees them as the repo's admin | Ask them to check their access and GitHub link |
| "The portal last saw your repositories at ... which is too old to rely on" | The collector's view of repo admins is older than `PORTAL_REPO_ADMINS_STALE_AFTER` (default 600 seconds) | Ask the platform team to check the collector is running |
| The timeline reads "Recorded / Failed" | The writer refused; the request page shows the reason after "It could not be recorded:", for example `already_registered: ...`, `record_changed`, `record_conflict`, `record_case_mismatch`, `not_a_file`, `base_moved`, `checks_timeout`, `check_failed: <check>`, `recheck: below_floor`, `recheck: scorecard_changed` | Only a failure after a PR was opened comments the reason on that PR and closes it. `base_moved`, `record_changed`, `record_conflict`: the requester submits again. `already_registered`: a record appeared; the requester asks for a change instead. `record_case_mismatch` or `not_a_file`: the platform team fixes the record's path in the factory. `recheck: scorecard_changed`: the approved record dropped the current record's `scorecard:` mark; the requester asks again and you approve it with the mark |

## Gaps

- **The `ops` role grants nothing.** `PORTAL_ROLE_GROUPS` accepts an `ops` group and the kit maps one, but no screen or action
  checks it.
- **Re-review covers review dates and scorecard events only.** History signals (velocity, failed scans, repeated overrides), APIM
  usage, orphaned or unregistered apps and owner notifications are not collected.
- **A signal setting applies from when it changes.** Events already read are not matched again.
- **The audit page shows 200 entries** with no search or export.
- **No offboarding flow.** Removing a person's link is manual; nothing reacts to someone leaving.
- **The approval rests on the collector's view** of repo admins; if the collector stops, approvals stall with a stale-observation error.

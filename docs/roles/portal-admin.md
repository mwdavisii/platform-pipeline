# SOP: portal admin

## Purpose

Decide app registrations in the golden road portal, keep the link between people and their GitHub logins right, read the
audit log, and apply ServiceNow's lifecycle decisions: suspend, resume or decommission an app (`docs/lifecycle.md`). An approved registration becomes the app's trusted intake record (`intake/<owner>/<repo>.yaml` in `scorecard-factory`),
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
| An app's page, its Lifecycle panel | Suspend, resume or decommission the app for a ServiceNow task (SCTASK and 7 digits) |
| A lifecycle request's page (`/requests/<id>`) | Retry or abandon a stalled step; read what ServiceNow was told |
| `/mock/servicenow/tasks/<number>` | The mock ServiceNow's view of a task: its work notes and close (mock only) |

You don't own the files the portal writes. The writer process records them; security and the platform own the repos.

## Before you start

- A portal account in the admin group. Sign out and in again after a group change: roles come from the sign-in.
- Your own GitHub link: link your own login on `/me` with OAuth. An admin-made link must come from another admin; nobody makes
  one for themselves.
- The registration floor rules and the classification policy (the policy link shows on the forms when
  `PORTAL_CLASSIFICATION_POLICY_URL` is set).
- For a lifecycle action: the ServiceNow task ServiceNow opened for the platform team, and [the lifecycle rules](../lifecycle.md)
  (`docs/lifecycle.md`).

## Today (mock) and in production

| | Today (personal PoC) | In production |
|---|---|---|
| Approvals, send back, people, audit | Real: the database enforces them (no self-approval, the floor, append-only audit) | The same |
| The writer's factory PRs | Real PRs on the PoC's public factory | The same, on an internal factory |
| `/admin/directory` | Lists the mock directory (`bootstrap/golden-road-portal/portal/mock_directory.json`), or says it is off | A real directory (not built) |
| Lifecycle actions | Real PRs in the deploy repo and the factory, a real archive; the task is one of the mock's six (`SCTASK0010001` to `SCTASK0010006`), and its notes show at `/mock/servicenow/tasks/<number>` | A real ServiceNow task (the web then needs the integration user's read on `sc_task`: not built); "applied" still means applied in git |

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

### 9. Read the CMDB sync

**Goal:** every registered app has its ServiceNow CMDB record, and a failed sync reaches the person who can fix it.

With `PORTAL_SERVICENOW=mock`, the collector sends each new or changed intake record to ServiceNow. On `/admin/rereview` the
collector's source line includes "ServiceNow CMDB", and a problem is listed with the repos whose last attempt failed:
- `cmdb_unreachable`: ServiceNow didn't answer. The collector retries on the next pass; if it lasts, tell the platform team.
- `cmdb_owner_unknown`: the owner's email (from their sign-in) matches no single ServiceNow user, so the app isn't sent: a CI without
  its owner would be an orphaned app. Ask the owner to check their email with the identity provider, or the ServiceNow admins to fix
  the user. It is retried every pass. A CI the portal made before the owner left still follows a suspend, a resume or a decommission:
  the collector sends its lifecycle fields without `owned_by`, and ServiceNow keeps the owner it has.
- `cmdb_refused`: ServiceNow refused the CI (its reconciliation rules). Tell the platform team (platform-team procedure 13).

You can't change an app's owner in ServiceNow from the portal: the intake record's owner is the truth, changed by a change of the
registration (app-developer procedure 11, which you approve with procedure 7).

**Check:** the problem leaves the page after the next pass, and the app's page shows its ServiceNow number.

### 10. Suspend an app

**Goal:** every environment of the app scales to zero for a ServiceNow task, and the task records it.

1. Take the ServiceNow task assigned to the platform team. It must be open and name the app's CI. In the mock: `SCTASK0010001` for
   hello-app.
2. Open the app's page. The **Lifecycle** panel (admins only) shows the state and the three buttons; a refused button says why.
   Press **Suspend**.
3. Enter the task number (SCTASK and 7 digits) and the reason (10 to 1000 characters, one line: it goes into the request, the audit
   log and the task's first work note). The reason is also committed to the deploy repo (the PR and each `lifecycle.yaml`), which
   anyone who can read the repo sees: name the task, not a vulnerability's details. Press **Suspend**.
4. The portal checks the task in ServiceNow, records the request and shows its page ("Lifecycle action requested: the writer applies
   it next"). Refusals, in the portal's words:
   - "Task SCTASK... is closed in ServiceNow." (also when an earlier request of the portal is closing it)
   - "Task SCTASK... names another CI than <owner>/<repo>'s." or "... names no CI: it must name <owner>/<repo>'s."
   - "ServiceNow has no task SCTASK..."
   - "<owner>/<repo> has no ServiceNow CI yet: wait until the collector has synced it, then try again."
   - "The task can't be checked now: ServiceNow didn't answer. Try again later."
   - "A lifecycle request for <owner>/<repo> is already open: wait until it is applied or abandoned." (or stopped)
   - "<owner>/<repo> is not active: it is suspended."
5. The writer checks again. An app with no environment in the deploy repo has nothing to suspend: the request ends Refused with
   `recheck: nothing_to_suspend`, and a note tells the task. Otherwise it opens one PR, `Suspend <owner>/<repo> (ServiceNow
   SCTASK..., portal request ...)`, that adds `lifecycle.yaml` and recomposes `composed.yaml` in each environment, and merges it
   itself once `deploy-check` passes (within L5: see `docs/lifecycle.md`).

**Check:** the request's timeline reads Requested, Deploy repo pull request (Done), Applied in git; the app page's banner says
"Suspended by the platform for ServiceNow task SCTASK... on .... It comes back when the platform resumes it."; and the task's notes
are at `/mock/servicenow/tasks/<number>` (Closed Complete once the collector sent the last one).

### 11. Resume an app

**Goal:** a suspended app comes back exactly as it was reviewed, and the task records it.

Take the ServiceNow task for the resume (in the mock: `SCTASK0010002` for hello-app). On the app's page press **Resume** in the
Lifecycle panel, enter the task and the reason, and press **Resume**. The same refusals as procedure 10 apply, and "<owner>/<repo>
is not suspended: there is nothing to resume." The writer's PR `Resume <owner>/<repo> (...)` deletes `lifecycle.yaml` and
recomposes `composed.yaml`; it merges it itself within L5.

**Check:** the banner is gone, the Lifecycle panel reads Active, and the task is Closed Complete at `/mock/servicenow/tasks/<number>`.

### 12. Decommission an app

**Goal:** the app is taken down for good for a ServiceNow task: removed from the deploy repo, its repository archived, its intake
record marked, its CI retired.

1. Read what goes and what stays (`docs/lifecycle.md`, "Decommission"). A decommission can't be undone once the deploy repo PR merges.
2. Take the task (in the mock: `SCTASK0010006` for `lifecycle-demo`; `SCTASK0010003` names hello-app and is kept for a refusal).
   On the app's page press **Decommission**.
3. Enter the task and the reason (as in procedure 10, the reason is committed to the deploy repo's PR and the factory's: name the
   task, not a vulnerability's details), read the confirmation ("Decommission <owner>/<repo>? This can't be undone.", with what goes and
   what stays), type the repository's name in "Type the repository's name to confirm", and press **Decommission**. Besides the
   refusals of procedure 10: "<owner>/<repo>'s intake record is intake/v1: change the registration first, ..." and "<owner>/<repo>
   is a platform repository: it is never decommissioned through the portal." The writer refuses a repo outside the portal's
   organization (`recheck: not_in_org`) and a decommission without the lifecycle App (`recheck: no_lifecycle_app`: tell the platform
   team, platform-team procedure 15).
4. Three steps follow, each on the request's timeline:
   - **Deploy repo pull request**, `Decommission <owner>/<repo>: remove it from the deploy repo (...)`. A platform reviewer reviews and
     merges it (deploy-reviewer procedure 5). If the deploy repo's `main` moves first, the writer rebuilds it on a new branch and the
     review starts again: ask the reviewer to finish between pushes.
   - **Repository archived**, by the writer with the lifecycle App, as soon as the deploy repo PR merged.
   - **Factory record pull request**, `Decommission <owner>/<repo>: mark its intake record decommissioned (...)`. A security code
     owner merges it.

**Check:** the request is Applied in git, the app page's banner says "Decommissioned on ... for task SCTASK.... The repository is
archived.", the repository shows as archived on GitHub, and the task is Closed Complete. If the archive or the factory step can't
finish, stop the request (procedure 13).

### 13. Retry or abandon a lifecycle step

**Goal:** a stalled lifecycle request goes on, or stops cleanly, by your decision.

1. Open the request's page. A failed step shows its reason on the timeline (and the task got a note). The box **Retry or abandon**
   (**Retry or stop** once a decommission's deploy repo PR merged) offers what rules 7 and 8 allow:
   - **Retry**, when a step failed: the step starts again, a PR step on a new branch from the target repo's current `main`, the
     archive by asking GitHub again. The steps before it stay done. Fix the cause first (the reason says which; Troubleshooting).
   - **Abandon**, until the deploy repo PR merges: the writer closes the request's pull requests and deletes its branches, and the
     request ends Abandoned. Nothing was applied, and the ServiceNow task stays open for ServiceNow to decide. If the deploy repo PR
     merges before the writer gets to it, the abandon comes too late: the writer notes the task and the action goes on.
   - **Stop**, for a decommission whose deploy repo PR merged (too late to abandon) when its archive or factory step can't finish: the
     repository was deleted or transferred, the security owner refused the factory PR, or the app was brought back
     (`not_decommissioned_by_this_request`). Read "Stop this request? The writer closes the steps still to do and does no more. The
     ServiceNow task stays open." and press **Stop**. The writer closes its open PRs of the steps left, deletes their branches, and the
     request ends Stopped. Nothing is undone: the app stays removed from the deploy repo and decommissioned in the portal. The task
     stays open, with a "stopped" note that says what wasn't done; finish it by hand (the archive: platform-team procedure 17; the
     record: a factory PR) and close the task in ServiceNow, or have the platform team bring the app back (procedure 18). If the
     factory PR merges before the writer gets to it, the stop comes too late and the decommission is applied.
2. Retry, Abandon and Stop are carried out on the writer's next pass. Until then the page says "Retry asked by ... at ...: the writer
   carries it out on its next pass." (or Abandon, or Stop), and offers none of them. A command by someone who is no longer a portal
   admin is refused, with a note. A command still waiting when the writer refuses the request is finished with a `command` note.
3. A PR the writer rebuilt because `main` moved needs no Retry: its "rebuilt" note says so.

**Check:** after the writer's next pass the timeline shows the step again (Now, then Done) or the request Abandoned or Stopped, and
the request's "What ServiceNow was told" lists the "retried", "abandoned" or "stopped" note.

### 14. Read the outbox

**Goal:** every step reached its ServiceNow task, and a note that didn't reaches the person who can fix it.

1. On the request's page, "What ServiceNow was told" lists every note: when, the step (`requested`, `pr_opened`, `merged`,
   `archived`, `factory_merged`, `failed`, `retried`, `rebuilt`, `abandoned`, `stopped`, `command`, `completed`, `ci_corrected`) and the text,
   then "Sent at ..." or "Waiting: N attempts, last error ...". The collector sends them in order, after its CMDB part; the last one
   (`completed`) closes the task as Closed Complete. In the mock, open the task at `/mock/servicenow/tasks/<number>`.
2. On `/admin/rereview`, `cmdb_writeback_pending` means a note has waited more than a day: ServiceNow refused it or didn't answer,
   and the collector keeps trying, with no attempt cap. A request's later notes wait behind it.
3. If ServiceNow didn't answer (the collector's log says `collectors: lifecycle write-back: 0 sent (N failed: will retry; ...)`, and
   the Re-review page may show `cmdb_unreachable` too): it clears by itself; if it lasts, tell the platform team.
4. If ServiceNow refuses a note for good (the last error is a status, for example "ServiceNow answered 403 for the task's work note":
   a work note on a task someone closed in ServiceNow, if the instance's rules refuse it), it blocks that request's later notes:
   - Confirm with the ServiceNow admins why it is refused (`docs/roles/servicenow-admin.md`). Once they allow it (or reopen the task),
     the collector's next pass sends it and the rest follow.
   - Only as a last resort, if ServiceNow must never take that note: ask the platform team to mark it sent by hand, as the database
     owner, with an audit entry. The task then never gets that note; write it on the task yourself. The SQL, in one transaction (it
     works only for the request's oldest unsent note; 0012 refuses to mark a note sent before an earlier one):
     ```sql
     BEGIN;
     SELECT id, step, attempts, last_error FROM lifecycle_events WHERE request_id = '<request id>' AND sent_at IS NULL ORDER BY id;
     UPDATE lifecycle_events SET sent_at = now() WHERE id = <the oldest id above> AND sent_at IS NULL;
     INSERT INTO audit_log (at, actor_kind, actor_id, action, subject_kind, subject_id, before, after, reason)
       VALUES (now(), 'person', '<your person id>', 'lifecycle.event_marked_sent', 'request', '<request id>', NULL,
               '{"event": <the oldest id above>}', 'ServiceNow refuses this note for good: <why, and who in ServiceNow confirmed it>');
     COMMIT;
     ```
     Your person id is on `/admin/people` (`p_` and 32 hex digits). To connect as the owner on the homelab kit:
     `kubectl -n golden-road-portal exec -it golden-road-portal-postgres-0 -- sh -c 'PGPASSWORD="$PORTAL_OWNER_PASSWORD" psql -h 127.0.0.1 -U portal_owner -d portal'`.
5. Notes are sent at least once: a collector that stops after ServiceNow took a note, before marking it sent, sends it again, so a
   task can show a note twice. That is expected.
6. `ci_corrected`: someone changed the app CI's lifecycle fields in ServiceNow and the collector wrote the portal's state back (rule
   9: ServiceNow follows the portal). Ask the ServiceNow admins who changed it; to change the app's state, ask for the action through
   a task. The note goes to the task of the request that set the state, which is usually Closed Complete by then. An instance that
   refuses work notes on a closed task holds it as `cmdb_writeback_pending`: that is the common case, so handle it as step 4 says.

**Check:** every note of the request reads "Sent at ...", and `cmdb_writeback_pending` is gone from the Re-review page.

## What you must never do

- Never approve your own registration, or one for an app you build, even if the portal would let another admin's link through.
- Never approve below the floor without a reason a security reviewer would accept.
- Never make an admin link for a login you have not confirmed belongs to that person.
- Never suspend, resume or decommission without a ServiceNow task that asks for it, and never type another task's number.
- Never edit a lifecycle row in the database to change an app's state: ask for the action through a task. The two hand changes the
  database allows the owner (marking a refused note sent, bringing an app back) are the platform team's, with an audit entry.
- Never approve during a platform upgrade the platform team has asked you to drain (for example the 0.5.0 upgrade: no approvals
  until the portal is on the new release).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| The approval says "Ownership acknowledged: Not recorded (submitted before 0.6.0)." | The request was submitted before the notice existed | Nothing to do; a later change or renewal records it |
| "The platform hasn't computed this repo's evidence yet: approving waits for it." | The collector has not read the repo for this submission | Wait for the collector's next pass |
| "The platform's evidence for this request names a retired classification level" | Evidence from before builder 0.5.0 | Send it back; resubmitting computes fresh evidence |
| "This registration changed since you loaded this page" | It was sent back and submitted again meanwhile | Reload and look again |
| "That tier or data classification is below the platform's floor, and the reason to approve it anyway is missing or not valid." | Below the floor without a 30-character reason | Raise the choice, or write the reason |
| "The requester is no longer an admin of that repository, as the portal last saw it." | The collector no longer sees them as the repo's admin | Ask them to check their access and GitHub link |
| "The portal last saw your repositories at ... which is too old to rely on" | The collector's view of repo admins is older than `PORTAL_REPO_ADMINS_STALE_AFTER` (default 600 seconds) | Ask the platform team to check the collector is running |
| "The task can't be checked now: ServiceNow didn't answer. Try again later." | ServiceNow (the mock, or the web's read of `sc_task`) didn't answer | Try again later; the platform doesn't apply an action it can't tie to a task |
| "Task SCTASK... names another CI than <owner>/<repo>'s." | The task's `cmdb_ci` is another app's CI | Ask the task's owner in ServiceNow to set the app's CI, or use the right task |
| "<owner>/<repo> has no ServiceNow CI yet: wait until the collector has synced it, then try again." | The collector's CMDB sync hasn't succeeded for the app (procedure 9) | Wait for the next pass; fix a `cmdb_*` problem first |
| "Task SCTASK... is closed in ServiceNow." for a task that looks open | An earlier request of the portal is closing it (its last note is stored, not yet sent) | Ask ServiceNow for a new task |
| A lifecycle request ends Refused with `recheck: nothing_to_suspend` | No environment of the app has a release in the deploy repo | Nothing to do; the app runs nowhere |
| A step failed `check_failed: deploy-check` | `deploy-check` failed on the writer's PR; the PR link in the note shows the check's summary | Fix the cause (often a release or a binding change on `main`), then Retry |
| A step failed `l5_refused: ...` | The suspend or resume PR changed more than L5 allows (for example someone pushed to its branch); the reason names what | The writer closed the PR. Find out who pushed, then Retry (a new PR from `main`) |
| A step failed `pr_head_changed`, `pr_closed` or `pr_files_unexpected: ...` | Someone pushed to the branch or closed the PR | Retry |
| A step failed `not_admin` | The requester is no longer a portal admin (by their roles at their last sign-in); the writer checks before each merge it makes, so every Retry fails the same way | Abandon, and ask again in a new request |
| A decommission's archive or factory step failed `not_decommissioned_by_this_request`, or the request ended Refused with it | The platform team brought the app back by hand (platform-team procedure 18) while the request was open | Don't Retry: it fails the same way. Stop the request (procedure 13) |
| A decommission's archive or factory step keeps failing (the repository is gone, the factory PR was refused or closed, the record was rewritten) | The step can't finish, and Abandon is too late after the deploy merge | Stop the request (procedure 13), then finish by hand or close the task in ServiceNow |
| A step failed `merged_with_other_commits` | A person merged the writer's PR with commits the writer didn't write | Tell the platform team: they check the merge commit by hand before anything else (platform-team Troubleshooting); then Retry |
| The archive step failed `archive: github: 404 GET /repos/<owner>/<repo>/installation` | The lifecycle App isn't installed on the app's repository | Platform-team procedure 17, then Retry |
| The archive step failed `lifecycle_app_on_platform_repo: <org>/<repo>` | The lifecycle App is installed on a platform repository | Platform-team procedure 17, then Retry |
| `cmdb_writeback_pending` on the Re-review page | A lifecycle note has waited more than a day | Procedure 14 |
| The timeline reads "Recorded / Failed" | The writer refused; the request page shows the reason after "It could not be recorded:", for example `already_registered: ...`, `record_changed`, `record_conflict`, `record_case_mismatch`, `not_a_file`, `base_moved`, `checks_timeout`, `check_failed: <check>`, `recheck: below_floor`, `recheck: scorecard_changed` | Only a failure after a PR was opened comments the reason on that PR and closes it. `base_moved`, `record_changed`, `record_conflict`: the requester submits again. `already_registered`: a record appeared; the requester asks for a change instead. `record_case_mismatch` or `not_a_file`: the platform team fixes the record's path in the factory. `recheck: scorecard_changed`: the approved record dropped the current record's `scorecard:` mark; the requester asks again and you approve it with the mark |

## Gaps

- **The `ops` role grants nothing.** `PORTAL_ROLE_GROUPS` accepts an `ops` group and the kit maps one, but no screen or action
  checks it.
- **Re-review covers review dates and scorecard events only.** History signals (velocity, failed scans, repeated overrides), APIM
  usage, orphaned or unregistered apps and owner notifications are not collected.
- **A signal setting applies from when it changes.** Events already read are not matched again.
- **The audit page shows 200 entries** with no search or export.
- **No offboarding flow.** Removing a person's link is manual; nothing reacts to someone leaving. An app comes down only when
  ServiceNow opens a task and you apply it (procedures 10 to 12).
- **Lifecycle (`docs/lifecycle.md`, "Gaps").** Applied stops at git; the task and the CI's fields are the mock's (the web would need
  the integration user's read on `sc_task` on a real instance); an environment first pinned while the app is suspended is not
  suspended (resume, then suspend again); a note ServiceNow refuses for good blocks the request's later notes until it is resolved or
  marked sent by hand (procedure 14).
- **The approval rests on the collector's view** of repo admins; if the collector stops, approvals stall with a stale-observation error.

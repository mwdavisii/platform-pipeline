# SOP: ServiceNow admin

## Purpose

ServiceNow is where an app's lifecycle decision is made: a critical vulnerability, an owner who doesn't confirm the app is still
used, an owner who leaves. The platform applies the decision and writes every step back to the ServiceNow task and to the app's CI.
You own the ServiceNow side of that: the integration user the platform signs in as, the fields it writes, and the tasks that ask the
platform to act. The rules are in [the lifecycle doc](../lifecycle.md) (`docs/lifecycle.md`).

## Who

The ServiceNow administrators for the instance the platform uses (CMDB and the task tables). None of this is in GitHub.

## What you own

| Thing | Where | Notes |
|---|---|---|
| The integration user | ServiceNow users and roles | The platform's one credential (the collector holds it; the portal's web reads tasks with it too) |
| Its roles | ServiceNow ACLs | Read `sys_user` (by email); create and update `cmdb_ci_business_app` through the Identification and Reconciliation API (IRE, `POST /api/now/identifyreconcile`), with an identification rule on `correlation_id`; read the CI's lifecycle fields (`GET /api/now/table/cmdb_ci_business_app/<sys_id>`); read and write `sc_task` (`work_notes`, `close_notes`, `state`) |
| Two fields on the business application class | `cmdb_ci_business_app` | `u_golden_road_state` (`active`, `suspended`, `decommissioned`) and `u_golden_road_task` (an SCTASK number, or empty) |
| Lifecycle tasks | `sc_task`, assigned to the platform team | One task per action, naming the app's CI |

The platform writes these fields on the CI, with the state its portal applied:

| App state | `operational_status` | `install_status` | `u_golden_road_state` | `u_golden_road_task` |
|---|---|---|---|---|
| Active | 1 (Operational) | 1 (Installed) | `active` | empty |
| Suspended | 2 (Non-Operational) | 1 (Installed) | `suspended` | the task |
| Decommissioned | 6 (Retired) | 7 (Retired) | `decommissioned` | the task |

## Before you start

- Admin on the instance: users, roles, ACLs, dictionary entries on `cmdb_ci_business_app`, and the IRE rules.
- The platform team's contact for the integration (platform-team procedure 13 in [platform-team.md](platform-team.md)).
- The lifecycle rules (`docs/lifecycle.md`): what each action does, and that ServiceNow follows the portal.

## Today (mock) and in production

| | Today (personal PoC) | In production |
|---|---|---|
| The instance | A mock inside the portal (`bootstrap/golden-road-portal/portal/mock_servicenow.json`, instance `service-now.example`, user `golden-road-cmdb`): nothing calls a real ServiceNow | Your instance; `PORTAL_SERVICENOW_URL` is refused today, so wiring it in is not built |
| Tasks | Six fixture tasks: `SCTASK0010001` to `SCTASK0010003` name hello-app's CI, `SCTASK0010004` is closed, `SCTASK0010005` names another CI, `SCTASK0010006` names `lifecycle-demo`'s CI | Tasks you create (procedure 3) |
| What the platform wrote | Shown at `/mock/servicenow/tasks/<number>` in the portal (admins): the task's work notes and close, as the collector sent them | The task's own activity stream and the CI's fields |
| The fields | The mock keeps whatever the collector sends | Your dictionary entries (procedure 1); the names are placeholders to confirm |

## Procedures

### 1. Create the two fields

**Goal:** the business application class can hold the platform's lifecycle state and the task that set it.

1. On `cmdb_ci_business_app`, create `u_golden_road_state` (a string, or a choice of `active`, `suspended`, `decommissioned`) and
   `u_golden_road_task` (a string of 13 characters, SCTASK and 7 digits, or empty).
2. If your instance names them differently, tell the platform team: the names are placeholders in the portal
   (`bootstrap/golden-road-portal/portal/servicenow.py`, `LIFECYCLE_FIELDS`) and must be changed there.
3. Confirm with the platform team that `operational_status` and `install_status` use the choice values in the table above (1, 2, 6;
   1, 7) on your instance.

**Check:** a CI read with `GET /api/now/table/cmdb_ci_business_app/<sys_id>?sysparm_fields=sys_id,operational_status,install_status,u_golden_road_state,u_golden_road_task`
returns all four fields.

### 2. Give the integration user its roles

**Goal:** the platform can do exactly what the lifecycle needs, and nothing more.

1. The integration user (platform-team procedure 13 created it for the CMDB sync) keeps its CMDB access: read `sys_user` by email,
   the IRE call for `cmdb_ci_business_app` with the identification rule on `correlation_id` (`github:<owner>/<repo>`, lower case) and
   a reconciliation rule for the data source `golden_road`, so the platform's updates win for the fields it sends (the four above
   included).
2. Add read of the CI's lifecycle fields: each collector pass reads them for every unchanged CI, one request per CI.
3. Add `sc_task`: read (the portal checks a task by number: `GET /api/now/table/sc_task?sysparm_query=number=<task>&sysparm_fields=sys_id,number,state,cmdb_ci&sysparm_limit=1`)
   and write of `work_notes`, `close_notes` and `state` (`PATCH /api/now/table/sc_task/<sys_id>`), on the tasks assigned to the
   platform team. The platform never creates or deletes a task.
4. Decide what a work note on a closed task does. The platform may add a note after someone closed the task in ServiceNow; if your
   rules refuse it, that note (and the request's later ones) waits until you allow it or reopen the task.

**Check:** with the user's credentials, the task check above returns one task with `sys_id`, `state` and `cmdb_ci`, and a test
`PATCH` of `work_notes` on a test task adds one journal entry.

### 3. Create a lifecycle task that names the app's CI

**Goal:** the platform team gets a task it can apply, and the portal accepts it.

1. Create an `sc_task` assigned to the platform team. Its `cmdb_ci` is the app's business application (the CI the platform created:
   its `correlation_id` is `github:<owner>/<repo>`; the app's page in the portal shows the CI's number).
2. In the description: the action (suspend, resume or decommission), the reason (a CVE, an owner who didn't confirm, an owner who
   left), and who asked.
3. Keep it open (state Open or Work in Progress: 1 or 2). The portal refuses a closed task, a task with no CI and a task naming
   another CI.
4. Give the number (SCTASK and 7 digits) to the platform team.

**Check:** a portal admin's form takes the task (the portal reads it, open, naming the app's CI), and its first work note arrives:
"Requested in the golden road portal by ...".

### 4. Read what the platform writes back

**Goal:** you can tell from ServiceNow alone what the platform did, and when it is done.

1. **Work notes**, one per step, each ending with "Golden road portal request: <link>": requested (by whom, the action, the reason),
   pull request opened, merged, repository archived, factory record merged, failed (and that a Retry will be noted), retried,
   rebuilt, abandoned, stopped. The text is neutralised: no `[code]` tag renders, and control and format characters are dropped.
2. **The close.** When the action is fully applied, the platform sets the task to Closed Complete (`state` 3) with a summary in
   `close_notes`: "Applied in git: ...". "Applied" means applied in git (a merged change, an archived repository); the cluster's
   reconcile does the rest. An abandoned or stopped request doesn't close the task: it stays open for you to decide. A stopped decommission's
   note says which steps were not done (the archive, the factory record).
3. **The CI.** Its four fields follow the table above. If someone edits them in ServiceNow, the next collector pass writes the
   platform's state back and adds a work note on the task of the request that set the state (ServiceNow follows the portal).
4. **Duplicates.** A note is marked sent after ServiceNow took it, so after a collector restart the same note may appear twice. It
   is the same step, not a second action.

**Check:** the task's activity shows a note per step and the close, and the CI's `u_golden_road_state` and `u_golden_road_task`
match the app's banner in the portal.

## What you must never do

- Never edit a CI's lifecycle fields (`operational_status`, `install_status`, `u_golden_road_*`) to undo or apply an action: the
  platform writes them back. Ask for the opposite action through a new task.
- Never close a lifecycle task before the platform does: the platform closes it when the action is applied, and its close overrides
  yours.
- Never point a task at another app's CI, or reuse one task for two actions.
- Never give the integration user more than the roles above, or share its credential outside the platform's Secret Server.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| The portal says "Task SCTASK... names another CI than <owner>/<repo>'s." | The task's `cmdb_ci` is another CI | Set the app's CI on the task |
| The portal says "Task SCTASK... names no CI: it must name <owner>/<repo>'s." | The task has no `cmdb_ci` | Set the app's CI on the task |
| The portal says "Task SCTASK... is closed in ServiceNow." | The task's state isn't 1 or 2, or an earlier action of the platform is closing it | Open a new task |
| The portal says "ServiceNow has no task SCTASK..." | The number is wrong, or the integration user can't read `sc_task` | Check the number; give the user read on `sc_task` (procedure 2) |
| The portal says "<owner>/<repo> has no ServiceNow CI yet: ..." | The platform hasn't created the CI (its CMDB sync failed) | Ask the platform team; check the IRE rules (procedure 2) |
| The platform's notes stop arriving on a task | ServiceNow refuses one (an ACL, a rule on closed tasks); the platform keeps retrying it and holds the later ones | Find the refusal in the instance's logs, allow it or reopen the task; the next pass sends it |
| A CI's lifecycle fields keep changing back | Someone edits them in ServiceNow; the platform writes its state back | Stop editing them; ask for the action through a task |

## Gaps

- **The fields and tables are placeholders.** `u_golden_road_state`, `u_golden_road_task`, the task table `sc_task`, its state values
  (1 and 2 open, 3 Closed Complete) and the integration user's roles stand in for your instance's: confirm them with the platform team
  before the first real connection.
- **No real connection.** The portal refuses `PORTAL_SERVICENOW_URL`; everything runs against the mock.
- **The web reads `sc_task` too.** The integration user's one credential would serve the collector's writes and the portal's web's
  task check: not built.
- **No ServiceNow trigger.** A task doesn't start anything by itself: a portal admin applies it.
- **"Applied" stops at git.** A closed task doesn't prove the pods are gone.

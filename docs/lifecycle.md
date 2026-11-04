# App lifecycle: suspend, resume and decommission

The decision to take an app down is made in ServiceNow: a critical vulnerability, an owner who doesn't confirm the app is still used, an owner who leaves. The platform applies it. A portal admin takes the ServiceNow task and, on the app's page, presses **Suspend**, **Resume** or **Decommission**. The portal makes the change through the same writer and deploy repo as every other change, and writes every step back to the ServiceNow task and the app's CI. "Applied" means applied in git: a pull request merged, a repository archived. The cluster's reconcile does the rest; the platform can't see the cluster.

Builder 0.7.0. ServiceNow is a mock today: the portal is written against the real Table and IRE APIs, served by a fixture (six tasks, `SCTASK0010001` to `SCTASK0010006`). The role SOPs in `docs/roles/` give the step-by-step procedures; this page is the rules.

## Who

- Only a portal admin (the `admin` role) can suspend, resume or decommission an app. The app's owner sees the state on the app page and in the banner, but can't change it: only a portal admin can. Anyone else gets "Only a portal admin can suspend, resume or decommission an app."
- Every action needs a ServiceNow task: its number is SCTASK followed by 7 digits. The portal looks it up and refuses it if ServiceNow has no such task, if it is closed, if it names no CI, or if it names another CI than the app's. A task the portal is closing (an earlier request's last note is stored, sent or not) is refused as closed too.
- An app with no ServiceNow CI yet (the collector's CMDB sync hasn't succeeded for it) is refused until the collector has synced it.
- A reason of 10 to 1000 characters on one line is required. It goes into the request, the audit log and the first work note.
- If ServiceNow is down, the request is refused ("The task can't be checked now"). The platform doesn't apply an action it can't tie to a task.
- There is one lifecycle request open per app. A second one is refused until the first is applied, abandoned or stopped.
- The writer checks again before it changes anything: the requester is still a portal admin (by their roles at their last sign-in), and the app's state still allows the action. A request it refuses fails with nothing applied, and a note on the task says why.

## Suspend

Needs a registered, active app with at least one environment in the deploy repo (an environment with a `release.yaml`). With none, the writer refuses the request: nothing to suspend (`nothing_to_suspend`).

- The writer adds `lifecycle.yaml` (`lifecycle/v1`) beside each environment's `bindings.yaml` and recomposes `composed.yaml`: the Deployment's `replicas: 0`, and nothing is deleted: the Service, the policies, the ExternalSecret and the skills ConfigMap stay, so a resume needs no secret fetched again.
- One PR covers every environment. The writer merges it itself once `deploy-check` passes, prod included, but only within L5: it re-reads the PR and finds exactly `lifecycle.yaml` and `composed.yaml` changed in each environment, `lifecycle.yaml` is this request's, and the only difference in `composed.yaml` is the Deployment's replicas, going to 0. Anything else (someone pushed to the branch, a file too many) fails the step with what it found, and the writer closes the PR. A portal admin retries (a new PR from the current `main`) or abandons. The writer never leaves a refused PR open for `platform-reviewers` to merge.
- While the app is suspended, bindings may still change. Their PRs keep `lifecycle.yaml`, so the app stays at zero, and a release bump (`bump.yml`) recomposes with it too. Only Resume brings the app back.

## Resume

Needs a suspended app. The writer deletes `lifecycle.yaml` in each environment that has one and recomposes, so `composed.yaml` is exactly what was reviewed before. The same merge rule holds: the writer merges it within L5 (the replicas go back to their value in `base/manifests.yaml`, nothing else changes).

## Decommission

Needs a registered app that isn't decommissioned (active or suspended), whose intake record is `intake/v2` (change a v1 registration first: `decommissioned` is an `intake/v2` key), in the portal's organization (`not_in_org` otherwise), and not one of the six platform repositories (`platform-pipeline`, `scorecard-factory`, `golden-road-deploy`, `golden-road-portal`, `golden-road-agents`, `security-scans`). The writer also needs the lifecycle App; without it the request is refused (`no_lifecycle_app`).

The form asks for a confirmation: type the repository's name. It lists what goes and what stays.

Three steps, each recorded on its own. Order: the deploy repo PR is merged, then the repository is archived, then the factory PR.

1. **The deploy repo PR** deletes `apps/<owner>/<repo>/` (every environment: release, bindings, composed, `cloudflare-access.yaml` and `lifecycle.yaml` included) and both adapters per environment (the Flux Kustomization and the Argo CD Application under `clusters/`), and nothing else. A person in `platform-reviewers` reviews and merges it. The writer deletes an adapter only if it reconciles the app's own path (`adapter_not_ours` otherwise), and refuses an app directory spelled another way in the deploy repo (`case_mismatch`). An app with nothing in the deploy repo (registered, never deployed) has no deploy PR: the step is done with a note.
2. **The archive.** The writer archives the repository with the lifecycle App: read-only, no push, no release, the code kept for audit. Archiving comes before the factory PR because, until it is archived, the app's release workflow could still add an environment back to the deploy repo, and the factory PR waits for a security code owner.
3. **The factory PR** adds `decommissioned` (`at`, `task`, `by`, `request`) to the intake record. A security code owner merges it.

The archive and the factory PR need the app still decommissioned by this request. If the platform team brought the app back meanwhile, the step fails `not_decommissioned_by_this_request`, and a Retry fails the same way: the writer never archives the repository, marks the record or decommissions the app again. A request whose steps were all done before the bring-back fails with the same reason instead of being applied.

What goes: the workloads, the ExternalSecret, the skills ConfigMap and the namespace's contents (the cluster's reconcile prunes them once the deploy repo PR merges), the app's Cloudflare tokens (the removed `cloudflare-access.yaml` tells the Cloudflare reconcile job to revoke them; the job is documented, not built), and the CI (retired). What stays: the images in ghcr (the signed releases are the audit trail), the Delinea secret values (the owner's; nothing reads them once the ExternalSecret is gone), and the Azure identity and Databricks principal (their bindings go with the deploy repo PR; deleting them is the owner's, with their admins).

Once the deploy repo PR merges, the decommission can't be undone, and the app is decommissioned in the portal: every action and every request for the repo is refused (registration changes, renewals, bindings): "<repo> was decommissioned on <date> for task <task>: nothing more can be asked for it." In the deploy repo, deploy-check refuses any changed environment under `apps/<owner>/<repo>/` once the app repository is archived, and once the factory's intake record says `decommissioned`. The archive comes first, so a hand-dispatched bump of an old release is refused from the archive on. A decommissioned app's review-date items close by themselves ("Closed by the platform: the app is decommissioned.").

**Reviews and a moving main.** Every lifecycle PR is built on the target repo's current `main`. If `main` moves before it merges, the writer closes the PR and rebuilds the step on a new branch from the new `main`, with a "rebuilt" note. A review approval doesn't carry over to the new PR, so a reviewer's approval is lost whenever the deploy repo's `main` moves: the decommission PR's review must finish between pushes to the deploy repo (the same holds for the factory PR and the factory's `main`).

## What ServiceNow sees

- **A work note per step**, each ending with the portal's request link: requested (by whom, the action, the reason), PR opened, merged ("the cluster's reconcile removes the workloads", "scales to zero" or "brings the app back as it was reviewed"), repository archived, factory record merged, failed (the reason, and that a Retry will be noted), retried, rebuilt, abandoned, stopped, and the outcome of a Retry, Abandon or Stop the writer didn't carry out.
- **The close.** When the action is fully applied, the task is closed as Closed Complete with a summary ("Applied in git: ..."). An abandoned or stopped request doesn't close it.
- **The CI's fields.** The CMDB sync sends them with the CI:

| App state | `operational_status` | `install_status` | `u_golden_road_state` | `u_golden_road_task` |
|---|---|---|---|---|
| Active | 1 (Operational) | 1 (Installed) | `active` | empty |
| Suspended | 2 (Non-Operational) | 1 (Installed) | `suspended` | the task |
| Decommissioned | 6 (Retired) | 7 (Retired) | `decommissioned` | the task |

The fields follow the app's state even after its owner left: a CI whose owner matches no ServiceNow user any more (`cmdb_owner_unknown`) gets them without `owned_by`, and ServiceNow keeps the owner it has. A CI the portal never made is still not made without its owner.

ServiceNow follows the portal. If someone edits those fields in ServiceNow, the next collector pass writes the portal's state back and notes it on the task of the request that set the state. The same goes for the task: the portal's close sets Closed Complete and its close notes even if a person closed the task in ServiceNow first. Don't edit the CI's lifecycle fields or close the task to undo an action: ask for the opposite action through a new task.

**The outbox.** The notes come from an outbox in the portal's database. The collector sends them on each pass, per request in order. An outage delays them, never loses them, and never blocks the GitHub side. There is no attempt cap. Notes waiting over a day show as `cmdb_writeback_pending` on the Re-review page. Things to know:

- **At least once.** A note is marked sent after ServiceNow took it. A collector that stops in between sends it again on its next pass, so a note can appear twice on the task. A correction note can be written twice the same way.
- **Order.** A request's notes go in order, and a note ServiceNow refuses holds back that request's later notes until it goes. One exception: a correction note and a writer note added at the same moment can be sent in the other order.
- **A note ServiceNow refuses for good** (for example a work note on a task someone closed, if the instance's rules refuse it) is retried every pass and holds back the request's later notes. The request page's "What ServiceNow was told" shows the error. The portal-admin SOP (`docs/roles/portal-admin.md`, procedure 14) says what to do.
- **Text.** Each note is neutralised before it is sent: control and format characters are dropped, and `[code]` tags are broken, so a reason or a name can't render as HTML in ServiceNow.

## Retry and abandon

- **Retry.** A stalled step shows its reason on the request page and in a task note. Retry starts that step again: a PR step on a new branch from the target repo's current `main`, the archive by asking GitHub again. The steps before it stay done. Retry is offered when a step has failed.
- **Rebuilt on its own.** If `main` moves under an open PR, the writer rebuilds it without a Retry.
- **Abandon.** Allowed until the deploy repo PR merges. The writer closes the request's PRs and deletes its branches on its next pass, and the request ends Abandoned; nothing was applied, and the task stays open, for ServiceNow to decide. Abandoning a suspend or resume closes its PR before it merges. If the deploy repo PR merges before the writer carries the abandon out, the abandon comes too late: the writer notes the task and the action goes on.
- **Stop.** For a decommission whose deploy repo PR merged (too late to abandon) and whose archive or factory step can't finish: the repository was deleted or transferred, the security owner refused the factory PR, or the app was brought back. The writer closes its open PRs of the steps left and deletes their branches, then closes those steps, and the request ends Stopped. Nothing is undone: the app stays removed from the deploy repo and decommissioned in the portal. The task stays open, with a note that says which steps were not done. If the factory PR merges before the writer carries the stop out, the stop comes too late and the decommission is applied. Finish the steps left by hand (`docs/roles/platform-team.md` procedure 17 for the archive, a factory PR for the record), or bring the app back.
- Retry, Abandon and Stop are carried out by the writer, one at a time per request: while one waits, the request page says so and offers none of them.

## Bringing an app back after decommission

There is no button. It is a documented manual procedure (`docs/roles/platform-team.md`, procedure 18), for a decommission whose request is over: Applied in git, or Stopped. Migration 0012 refuses the bring-back while the request is still open, so a Retry can never decommission the app again.

1. The repository is unarchived by a GitHub admin.
2. The intake record's `decommissioned` key is removed by a reviewed factory PR.
3. The portal's state is set back to active by the platform team: in the database, as the schema owner `portal_owner`, with an audit entry written by hand. Migration 0012 allows that change to `portal_owner` only; for every process role, decommissioned is for good.
4. A new registration in the portal (the owner registers the app again), and a new release (`bump.yml` pins it again).

## The lifecycle App

A third GitHub App, used only to archive. Administration: write, installed on app repos only (never on a platform repository), no webhook. The writer holds its key and nothing else does. Before it archives, the writer checks that the App has no installation on any platform repository, then mints a token for the one repository being archived, with only `administration: write`. The token makes three calls: it reads the repository (already archived: the step is done), archives it, and revokes itself.

## Gaps

- Applied stops at git: the platform can't see the cluster, so a closed task doesn't claim the pods are gone.
- The lifecycle App's Administration: write is broader than archive: it could also change settings or delete a repository. Accepted risk: one holder (the writer), app repos only, one repository per token, three calls.
- The mock's task and CI fields stand in for a real instance's: confirm the `u_golden_road_*` fields, the task table (`sc_task`), its state values and the integration user's roles with the ServiceNow admins (`docs/roles/servicenow-admin.md`).
- Mock only: the web reads `sc_task` too (the task check at request time), so a real instance needs that read for the portal's web, not only for the collector.
- Not suspended: an environment first pinned while the app is suspended. A bump that adds an environment has no portal state, and the suspend covered only the environments that had a directory. Resume and suspend again (two new tasks) to cover it.
- The writer learns the write App's bot login at startup. If the App is renamed, restart the writer.
- Bringing an app back resets the portal's state by hand in the database (no button).
- The CMDB sync reads each unchanged CI's lifecycle fields every pass: one more ServiceNow request per CI per pass. An instance whose ACLs refuse that read shows the CI as `cmdb_refused` and synced on alternate passes.

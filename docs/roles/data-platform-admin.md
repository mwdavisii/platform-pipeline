# SOP: data platform admin (Databricks)

## Purpose

Let an app read the data it needs from Databricks through one SQL warehouse, as its own Entra identity, with no Databricks token
in the app. The portal offers an app only the warehouses granted to the principal it has linked; the platform then writes the
connection details into the app's composed manifests.

## Who

The team that runs the Databricks workspaces and their grants. The app page calls this "analytics' process" ("Analytics' process
grants the identity access to a SQL warehouse"), and the deploy repo's CODEOWNERS gives `bindings.yaml` to `analytics` (shared
with `security`).

## What you own

| Thing | Where |
|---|---|
| SQL warehouses and who may use them | Databricks |
| Catalog and schema grants for app principals | Databricks (Unity Catalog, if used) |
| Recording a grant so the portal offers it | Databricks (in production); `bootstrap/golden-road-portal/portal/mock_directory.json` `databricks` (today) |
| Reviewing binding PRs | `golden-road-deploy` `apps/**/bindings.yaml` (with security) |
| The page people follow to ask for a grant | The portal setting `PORTAL_DATA_PROCESS_URL` (an `https://` page you write) |

## Before you start

- Workspace admin rights (to add a service principal and grant warehouse use).
- The app's principal: its directory id and client id, from the identity admin ([identity-admin.md](identity-admin.md)). A
  connection needs an Azure identity first: the binding schema refuses a Databricks connection without one.

## What the platform needs from you, and what it does with it

| Field | Meaning | Rule |
|---|---|---|
| `id` | A short stable name, for example `wh-<what>` | `^[a-z0-9][a-z0-9-]{0,62}$`, unique across principals and warehouses |
| `display_name` | Shown on the app page | 1 to 200 characters |
| `host` | The workspace host name, no `https://` | `adb-<digits>.<digits>.azuredatabricks.net` (Azure Databricks only) |
| `http_path` | The warehouse's HTTP path | `/sql/1.0/warehouses/<id>` |
| `catalog`, `schema` | Optional defaults | Identifiers (`[A-Za-z_][A-Za-z0-9_]*`) |
| `granted_to` | The principal ids that may use it | At least one; each must be a principal in the directory |

When an app's admin enables it, `golden-road compose` adds to that environment's `composed.yaml` the env vars `DATABRICKS_HOST`,
`DATABRICKS_HTTP_PATH`, and `DATABRICKS_CATALOG` and `DATABRICKS_SCHEMA` when set, plus egress to the workspace host on 443. The
app signs in with its workload identity (`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`); no Databricks token is delivered.

## Today (mock) and in production

| | Today (personal PoC) | In production |
|---|---|---|
| The directory | `mock_directory.json`: three warehouses (`wh-notes-readonly`, `wh-platform-metrics`, `wh-finance`) on dummy hosts | A real directory reading Databricks grants (not built) |
| A grant | A principal id in `granted_to` | A grant in Databricks that the directory would read |
| Adding or revoking | A pull request on `golden-road-portal` changing the mock, then a portal release | Done in Databricks; the directory would follow |
| Nothing calls Databricks | Bindings record `verified_by: mock-directory` | A real `verified_by` (the schema accepts only `mock-directory` today) |

## Procedures

### 1. Grant an app's principal access to a warehouse

**Goal:** the app's identity can query one warehouse, and only the data it should.

1. Confirm the request: which app and environment, which warehouse, and which catalog and schema it needs. Check the app's data
   classification in its intake record (`intake/<owner>/<repo>.yaml` in `scorecard-factory`) matches the data.
2. In Databricks, add the principal to the workspace by its application (client) id, grant it use of the warehouse, and grant only
   the catalog and schema privileges it needs (confirm the privilege names with your workspace).
3. Record the grant for the portal (procedure 2).

**Check:** the principal can run a test query on the warehouse and can't read outside the granted schema.

### 2. Make the grant appear in the portal

**Goal:** the app's page offers the warehouse once its principal is linked.

Today, by a pull request on `golden-road-portal`, add (or extend) an entry under `databricks` in `portal/mock_directory.json`:
```json
{"id": "wh-<name>", "display_name": "<name> (read-only)", "host": "adb-<workspace id>.<n>.azuredatabricks.net",
 "http_path": "/sql/1.0/warehouses/<warehouse id>", "catalog": "<catalog>", "schema": "<schema>", "granted_to": ["sp-<repo>"]}
```
The platform team releases and redeploys the portal after the merge (the file is part of the image and is checked at startup).

**Check:** `/admin/directory` (it needs the portal admin role; otherwise ask a portal admin) lists the warehouse "Granted to" the principal, and the app's page offers it under "Enable Databricks
Connection" on a tab where that principal is linked.

### 3. Review a binding that adds a Databricks connection

**Goal:** the connection in git is the one you granted.

In the writer's PR in `golden-road-deploy`, read `bindings.yaml`'s `databricks` block (`directory_id`, `host`, `http_path`,
`catalog`, `schema`) and check it against the grant. For `prod`, the PR needs a code owner's approval (you or security), and then a person merges it (see
[deploy-reviewer.md](deploy-reviewer.md)).

**Check:** the values match the grant, `deploy-check` passed, and the PR changes only that environment's `bindings.yaml` and `composed.yaml`.

### 4. Revoke a grant

**Goal:** an app can no longer reach the warehouse.

1. In Databricks, remove the principal's grants (and its warehouse use). This is what actually stops access.
2. Today, remove the principal from `granted_to` by portal PR. If it was the warehouse's only principal, remove the whole warehouse
   entry: an empty `granted_to` stops the portal at startup. Then the platform team releases and redeploys the portal.
3. Ask the app's admin to remove the connection on the page. Removing the Azure identity removes it too.

What happens to existing bindings: nothing changes in git by itself. The next change to that environment fails with
`not_in_directory` (the writer re-checks the connection it carries over) until the connection is removed on the page.

**Check:** the principal's queries fail in Databricks, and the tab no longer lists the connection.

## What you must never do

- Never grant a warehouse to a principal made for another app.
- Never hand out a Databricks personal access token for a golden road app: the platform delivers none, and the identity is the credential.
- Never grant more than the catalog and schema the app's classification allows.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| The page shows no Databricks options | No Azure identity linked on that tab, or no warehouse granted to it | Link the identity first; procedure 2 |
| "Enable Databricks Connection" offers the wrong warehouse | `granted_to` lists the principal by mistake | Fix the entry; revoke the grant in Databricks |
| The portal stops at startup after a directory change | An invalid entry ("the directory's workspace host is not valid", "a warehouse must be granted to principals the directory lists") | Fix the entry |
| The change fails with `not_in_directory` | The warehouse or its grant left the directory | Remove the connection, or restore the grant |
| The app can't connect though the binding exists | Today: nothing is deployed. In production: the grant, the federated credential or the egress is missing | Check each in turn |

## Gaps

- **There is no real directory feed.** Grants come from `mock_directory.json`; nothing reads Databricks or proves a grant exists.
- **Only Azure Databricks hosts are accepted** (`adb-*.azuredatabricks.net`); another cloud's workspace can't be recorded.
- **One warehouse per environment.** A binding holds one Databricks connection.
- **No per-environment grant.** A warehouse granted to a principal is offered on every tab where that principal is linked.
- **Ownership is per file.** CODEOWNERS can't own part of `bindings.yaml`, so security or analytics may approve a whole binding,
  the identity included.
- **Checked once.** The writer checks the grant when it opens the PR, not at merge; `deploy-check` never checks it.
- **Untested end to end.** Nothing reconciles, so no app has queried a warehouse with its workload identity.

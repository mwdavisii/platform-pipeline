# SOP: identity admin (Azure and Entra)

## Purpose

Give each app a Microsoft Entra service principal it can run as, in each environment, through workload identity: no client secret,
the pod's own Kubernetes token traded for an Entra token. The portal lets an app's admin pick only a principal you made and tagged
for that repo; the platform then writes the identity into the app's composed manifests.

## Who

Whoever runs Entra app registrations or managed identities for the organization. The app page calls this "security's process"
("Security's process creates the service principal"), so it is security, or a team acting for them.

## What you own

| Thing | Where |
|---|---|
| Service principals (app registrations or user-assigned managed identities) for apps | Entra |
| Their federated identity credentials (which cluster, namespace and service account may use them) | Entra |
| Tagging a principal for the repo it was made for | Entra (in production); `bootstrap/golden-road-portal/portal/mock_directory.json` (today) |
| The page people follow to ask for a principal | The portal setting `PORTAL_IDENTITY_PROCESS_URL` (an `https://` page you write) |

## Before you start

- Rights to create app registrations or managed identities and federated credentials in the tenant.
- The cluster's OIDC issuer URL (from the cluster operators), for the federated credential.
- The app's GitHub repo name and the environment (`test` or `prod`).

## What the platform needs from you, and what it does with it

The portal's directory answers two questions (`principals_for` and `warehouses_for` in
`bootstrap/golden-road-portal/portal/directory.py`). For a principal it needs:

| Field | Meaning | Rule |
|---|---|---|
| `id` | A short stable name, for example `sp-<repo>` | `^[a-z0-9][a-z0-9-]{0,62}$`, unique across principals and warehouses |
| `display_name` | Shown on the app page | 1 to 200 characters |
| `client_id` | The principal's application (client) id | A lowercase GUID, unique |
| `tagged_repo_name` | The repo it was made for, without the org | The portal offers it only to `<PORTAL_ORG>/<tagged_repo_name>` (case-insensitive) |
| `tenant_id` (once, for the directory) | The tenant | A lowercase GUID |

When an app's admin links it, `golden-road compose` adds to that environment's `composed.yaml`: the annotation
`azure.workload.identity/client-id: <client_id>` on the app's service account, the pod label `azure.workload.identity/use: "true"`,
the env vars `AZURE_CLIENT_ID` and `AZURE_TENANT_ID`, and egress to `login.microsoftonline.com` on 443. The cluster's Azure
Workload Identity webhook is expected to inject the token; the platform adds no secret.

For the federated credential (confirm the exact fields with your tenant):

- **Issuer:** the cluster's OIDC issuer URL.
- **Subject:** `system:serviceaccount:<repo>-<env>:<service account>`. Each environment has its own namespace, `<repo>-<env>` (for
  example `hello-app-test`), and the service account is the app's generated one (named after the app, for example `hello-app` in
  `bootstrap/hello-app/deploy/base/serviceaccount.yaml`).
- **Audience:** the one your workload identity setup uses (commonly `api://AzureADTokenExchange`).

## Today (mock) and in production

| | Today (personal PoC) | In production |
|---|---|---|
| The directory | `mock_directory.json`: three principals (`sp-hello-app`, `sp-golden-road-portal`, `sp-finance-reports`) with dummy GUIDs | A real directory reading Entra (not built) |
| Tagging | `tagged_repo_name` in the mock | An Entra tag, owner or naming rule: not decided, not built |
| Adding or revoking a principal | A pull request on `golden-road-portal` changing the mock, then a portal release | Done in Entra; the directory would follow |
| Nothing calls Azure | Bindings record `verified_by: mock-directory` | A real `verified_by` (the schema accepts only `mock-directory` today) |

## Procedures

### 1. Create a principal for an app and environment

**Goal:** an Entra identity that only that app, in that environment, can use.

1. Confirm the request: the repo, the environment, and that the requester is the app's admin (the portal checks this again when they link it).
2. In Entra, create the app registration or managed identity (one per app and environment is the safer choice; see Gaps). Grant it
   only the Azure roles the app needs.
3. Add a federated credential with the issuer, subject and audience above.
4. Record its client id and tenant id.

**Check:** the identity exists with exactly one federated credential, for subject `system:serviceaccount:<repo>-<env>:<service account>`.

### 2. Make it appear in the portal (tag it for the repo)

**Goal:** the app's page offers the principal, and no other app's does.

Today, by a pull request on `golden-road-portal`, add an entry to `principals` in `portal/mock_directory.json`:
```json
{"id": "sp-<repo>", "display_name": "sp-<repo> (<what it is for>)", "client_id": "<client id GUID>", "tagged_repo_name": "<repo>"}
```
The portal validates the file at startup (a bad entry stops the portal), so the platform team releases and redeploys the portal
after the merge. In production this step would be the tag in Entra itself (not built).

**Check:** `/admin/directory` (it needs the portal admin role; otherwise ask a portal admin) lists the principal "Tagged for"
`<org>/<repo>`, and the app's page offers it under "Associate Azure Identity".

### 3. Revoke a principal

**Goal:** an app can no longer run as the identity.

1. In Entra, remove the federated credential (or delete the identity). This is what actually stops tokens being issued.
2. Ask the app's admin to press **Remove** beside the identity on each tab where it is linked. That also removes the Databricks
   connection of that tab, and rewrites `bindings.yaml` and `composed.yaml` without them.
3. Today, also remove the entry from `mock_directory.json` by portal PR. In the same change, remove its id from every warehouse's
   `granted_to`, and delete any warehouse left with an empty `granted_to`: a warehouse granted to a principal the directory doesn't
   list, or to nobody, stops the portal at startup ("a warehouse must be granted to principals the directory lists"). Then the
   platform team releases and redeploys the portal.

What happens to existing bindings: nothing changes in git by itself. The binding stays in `golden-road-deploy` until it is removed.
Removing the principal from the directory makes the next change to that environment fail with `not_in_directory` (the writer
re-checks the identity it carries over), until the identity is removed on the page.

**Check:** the identity has no federated credential in Entra, and the app's tabs no longer list it.

## What you must never do

- Never tag a principal for a repo it was not made for: the portal's only check is the tag.
- Never create a client secret for a golden road principal: the platform delivers no secret for it, and workload identity needs none.
- Never reuse one principal across apps.
- Never add a federated credential whose subject is a wildcard or another app's namespace.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| The app page doesn't offer the principal | Not tagged for this repo, or the directory is off (`PORTAL_DIRECTORY` empty) | Check `/admin/directory` (portal admins only; otherwise ask one); procedure 2 |
| "This principal isn't tagged for <owner>/<repo>." | A form posted a principal tagged for another repo | Expected refusal; tag a principal made for this repo |
| The portal stops at startup after a directory change | An invalid entry: a bad GUID, a duplicate client id or id, an unknown key | Fix the entry; the error names what is wrong ("the directory's client id is not valid", "two principals in the directory share a client id") |
| The change fails with `not_in_directory` | The linked principal left the directory, or its client or tenant id changed | Remove and link again, or restore the entry |
| The change fails with `no_directory` | The writer has no `PORTAL_DIRECTORY` | The platform team sets it (web and writer must match) |

## Gaps

- **There is no real directory feed.** The portal reads `mock_directory.json`, baked into its image. Nothing reads Entra, and
  nothing proves a principal exists or belongs to the app.
- **How a principal is tagged in Entra is not decided** (a tag, an owner, a naming rule), and no code reads one.
- **No per-environment principal.** The directory has no environment field: a principal tagged for a repo is offered on both the
  `test` and `prod` tabs, so nothing stops one identity serving both.
- **The federated credential is outside the platform.** No code checks that it exists or that its subject matches the app's
  namespace and service account.
- **Checked once.** The writer checks the tag when it opens the PR, not at merge, and `deploy-check` never checks it.
- **Revocation doesn't flow.** Revoking in Entra leaves the binding in git until someone removes it on the page.
- **Untested on a cluster.** Nothing reconciles, so the webhook, the token exchange and the generated service account (which sets
  `automountServiceAccountToken: false`) have not been proven together.

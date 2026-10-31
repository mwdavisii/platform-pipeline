# SOP: Secret Server admin (Delinea)

## Purpose

Make secrets that live in Delinea Secret Server usable by golden road apps, without a value ever passing through the portal, git or
a release. An app's environment holds only a reference (a secret id and a field); on a cluster, the External Secrets Operator (ESO)
would read the value with one platform reader account, `golden-road-eso`, and hand it to the pod.

## Who

The Secret Server administrators. The deploy repo's documentation gives the account to security ("security creates the Secret
Server application account `golden-road-eso`"), so in most organizations this is the security team or the team that runs Secret
Server for them. Cluster operators do the cluster half (procedure 5).

## What you own

| Thing | Where |
|---|---|
| The platform reader account `golden-road-eso` and what is shared with it | Secret Server |
| App folders and who can see them | Secret Server |
| Secret templates and their field slugs | Secret Server |
| The page people follow to ask for a share | The portal setting `PORTAL_SECRET_PROCESS_URL` (an `https://` page you write) |
| The cluster store and its egress (with cluster operators) | `bootstrap/golden-road-deploy/external-secrets/cluster-secret-store.yaml`, `cilium-egress.yaml`, and its `README.md` |
| Today only: the mock's data | `bootstrap/golden-road-portal/portal/mock_secret_server.json` (a platform-team change) |

## Before you start

- Administrator rights in Secret Server (to create an application account, folders and permissions).
- The instance's API documentation (`<base>/RestApiDocs.ashx?doc=OpenAPI`), to confirm the details marked below.
- For the cluster half: a cluster operator with rights in the `external-secrets` namespace.

## What the portal reads, shows and never touches

The portal's client (`bootstrap/golden-road-portal/portal/secretserver.py`) is written against Secret Server's REST API and makes
only these calls, never following a redirect:

| Call | Used for |
|---|---|
| `POST <base>/oauth2/token` (password grant) | A token for the person, or for `golden-road-eso` |
| `GET /api/v1/folders` | The folders the person can see (the Source step) |
| `GET /api/v1/secrets` with `filter.searchText`, `filter.folderId`, `filter.includeRestricted`, `skip`, `take` | Secrets the person can see, by name, in one folder; only the first 20 are shown |
| `GET /api/v1/secret-templates/{id}` | The template's fields: only non-file fields (`isFile: false`) are offered, by `fieldSlugName` |
| `GET /api/v1/secrets/{id}/summary` | As the person: can they see it. As `golden-road-eso`: is it shared with the platform |

It never calls a value endpoint (no `GET /api/v1/secrets/{id}`, no `/fields/`). The person sees folder paths, secret names,
template field display names, and "Not available" for a secret not shared with `golden-road-eso` or with no deliverable field. The deploy repo records, per
secret: `env`, `secret_id`, `secret_name`, `folder_id`, `field_slug` and `verified_by` (schema `binding/v2`). **To confirm against
your instance:** paging with `skip`/`take` and `hasNext`, the template's `fields[].fieldSlugName`, and `/summary` as the sharing
check. Each is one function in `secretserver.py`.

## Today (mock) and in production

| | Today (personal PoC) | In production |
|---|---|---|
| Secret Server | A mock (`secretserver_mock.py` over `mock_secret_server.json`): three folders, three templates, five secrets, no values | Your instance |
| `golden-road-eso` | A user in the mock's data | An application account you create |
| How the portal knows the person | The mock user whose `github_login` matches a GitHub link the person verified with OAuth | The person's own Secret Server token at sign-in (not built) |
| The portal's own credential for `golden-road-eso` | Minted by the mock | Not wired: `PORTAL_SECRET_SERVER_URL` is refused, so there is no real-instance mode yet |
| ESO, the store, the egress policy | Files only; nothing applied, and `deploy-check` refuses anything pointing at `external-secrets/` | Installed and applied by hand by cluster operators |

## Procedures

### 1. Create the platform reader account

**Goal:** one account, `golden-road-eso`, that can read exactly the secrets shared with it.

1. In Secret Server, create an application account (non-interactive) named `golden-road-eso`. Confirm the account type and the
   password grant with your instance: the portal and ESO both sign in with a username and password.
2. Give it no folder rights of its own. It reads only what is shared with it, one secret at a time.
3. Hand the username and password to the cluster operators over your normal secure channel (procedure 5). Never put them in git, a
   ticket or a chat.

**Check:** signed in as `golden-road-eso`, a search returns no secrets until something is shared with it.

### 2. Give an app team a folder

**Goal:** the app's admins can find and pick their app's secrets, and nobody else's.

1. Create a folder per app, for example `\Golden Road\<repo>` (the mock uses this shape).
2. Give the app's admins view rights on it. The portal shows a person only the folders and secrets they can see.

**Check:** the person opens Add a secret on the app's page and sees the folder under Source.

### 3. Share a secret with the platform

**Goal:** a secret becomes selectable for an app.

1. Check the request: which app, which environment, which secret and field, and that the secret belongs to that app.
2. In Secret Server, share the secret with `golden-road-eso` with the least permission that lets it read the secret's summary and
   fields (confirm the permission name with your instance).
3. Tell the requester it is shared.

In the mock, sharing is the `shared_with` list of a secret in `mock_secret_server.json`; changing it is a portal release.

**Check:** on the app page the secret is no longer greyed "Not available" ("Ask security to share it with `golden-road-eso`").

### 4. Keep templates usable

**Goal:** the fields people need are offered, and only those.

- A field is offered only if it is not a file field and has a slug; certificates and other file fields are never offered.
- A field slug must match `^[a-z0-9][a-z0-9-]{0,63}$`, and a secret's name must be 1 to 128 letters, digits, spaces, `.`,
  `_` and `-`, starting with a letter or digit and not ending with a space. A secret with another name can't be added until it is renamed.
- Renaming or moving a secret later is not re-checked: the id and field are what ESO reads, and the name and folder in
  `bindings.yaml` stay as they were at submit.
- Removing a field from a template, or making it a file field, breaks the next change to any environment that uses it
  (`secret_field_gone`).

**Check:** on the Field step the person sees the fields you expect for that template.

### 5. Set up the cluster side (with cluster operators)

**Goal:** ESO can read shared secrets from Secret Server, and only app namespaces can use it.

Applied by hand, once per cluster, never through Flux or Argo CD:

1. The operators install ESO in the namespace `external-secrets`.
2. They create the Secret `golden-road-eso` there, keys `username` and `password`, from procedure 1. It is never in git.
3. In `external-secrets/cluster-secret-store.yaml` (the `ClusterSecretStore` `delinea-secret-server`), set `serverURL` to the real
   host and apply it. Its `conditions` let only namespaces matching `^[a-z0-9-]+-(test|prod)$` use it.
4. In `external-secrets/cilium-egress.yaml`, set `matchName` to the real host and apply it: ESO may reach kube-dns, the API server
   and Secret Server on 443, nothing else.

**Check:** `kubectl get clustersecretstore delinea-secret-server` (cluster-scoped: no `-n`) reports the store ready (confirm the status
field with your ESO version).

### 6. Rotate the platform account's password

**Goal:** a new `golden-road-eso` password with no gap in delivery.

1. Set the new password in Secret Server.
2. The operators update the Secret `golden-road-eso` in `external-secrets` by hand.
3. Each app's `ExternalSecret` refreshes every hour (`refreshInterval: 1h`, written by `golden-road compose`). Env vars from a
   Kubernetes Secret are read when a pod starts, so a changed value reaches the app only after its pods restart.

**Check:** the store stays ready after the change, and an app's `ExternalSecret` reports a fresh sync.

### 7. Revoke a share

**Goal:** an app stops being able to read a secret.

1. Remove the share with `golden-road-eso` in Secret Server.
2. Ask the app's admin to remove the secret on the app page (Remove, then Remove), so the reference leaves `bindings.yaml` and
   `composed.yaml`.

What happens to existing bindings: nothing changes in git by itself. The reference stays in the deploy repo until someone removes
it. The next change to that environment (another secret, the identity, the connection) fails with `secret_not_shared`, because the
writer re-checks every secret it carries over. On a cluster, ESO's next refresh would fail to read the secret (confirm whether your
ESO version keeps the last synced value).

**Check:** the app page no longer lists the secret, and the environment's `composed.yaml` has no `remoteRef` for its id.

## What you must never do

- Never give `golden-road-eso` folder-wide rights or admin rights.
- Never share a secret with `golden-road-eso` for an app it doesn't belong to: the account can read everything shared with it, and
  generated, reviewed bindings are what keep one app off another's secret.
- Never put a credential in git, `bindings.yaml`, a portal field or an issue.
- Never create a `SecretStore` or a second `ClusterSecretStore`: `deploy-check` refuses them ("a SecretStore is refused anywhere:
  secrets come only from the platform's ClusterSecretStore").

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| A secret shows "Not available" with "Ask security to share it with `golden-road-eso`" | Not shared with `golden-road-eso` | Procedure 3 |
| A secret shows "Not available" with "No field of this secret can be delivered (file fields never are)." | Its template has only file fields | Procedure 4: add a non-file field, or store the value in another template |
| "You can't see this secret in Secret Server." | The person has no rights on its folder | Procedure 2, or the request is for someone else's secret |
| "That is not a field this secret can deliver" | A file field, or one not in the template | Pick a non-file field, or fix the template |
| The change fails with `secret_not_shared` | The share was removed since it was linked | Share it again, or remove the secret on the page |
| The change fails with `secret_field_gone` | The field left the template or became a file field | Restore the field, or pick another |
| The change fails with `secret_server_unavailable` | Secret Server did not answer the writer | Check the instance is up and reachable |
| The change fails with `no_secret_server` | The writer has no `PORTAL_SECRET_SERVER` | The platform team sets it (web and writer must match) |
| The portal or writer refuses to start with `PORTAL_SECRET_SERVER_URL` set | There is no real-instance mode yet | Leave it unset |
| "Only the first 20 are shown: narrow the search." | More than 20 matches | Search a longer part of the name |

## Gaps

- **No real Secret Server integration.** Only the mock exists; `PORTAL_SECRET_SERVER_URL` is refused, and wiring a real
  `golden-road-eso` credential into the portal's writer is not built.
- **The person's own Secret Server identity isn't used.** The mock maps a person through a verified GitHub link; the real version
  needs the person's own token at sign-in (Secret Server federated with the same identity provider).
- **One reader account for every app.** `golden-road-eso` reads everything shared with it. A per-app account with a namespaced
  `SecretStore` is the stronger option, and is not built.
- **A person's visibility is checked once, at submit.** The writer has no token of theirs, and secrets carry over under the name
  of the next admin who changes the environment.
- **No sync from Secret Server.** A revoked share, a renamed secret or a removed field shows up only when the next change to that
  environment fails; nothing alerts.
- **No rotation support.** There is no procedure in code for rotating `golden-road-eso` or restarting pods after a value changes.
- **The API details are unconfirmed** against a real instance (paging, field slugs, `/summary`).
- **Nothing runs.** No ESO, no store, no pod proves a secret is delivered.

# SOP: deploy reviewer (golden-road-deploy code owners)

## Purpose

`golden-road-deploy` is the platform's record of what runs where, as whom: each app environment pins one signed release and has
its own bindings, and the plain Kustomize a GitOps controller would apply is generated from them and checked in CI. Promoting and
binding are pull requests there. You review and merge them: release pins from the `bump` workflow, and prod bindings from the
portal's writer, which never merges a prod binding itself.

## Who

The code owners of `golden-road-deploy` (`bootstrap/golden-road-deploy/.github/CODEOWNERS`):

- `platform-reviewers`: everything (`*`), including release pins, generated files, adapters and policy, and `/.github/`.
- `security` or `analytics`: `apps/**/bindings.yaml` (either may approve a whole file).

## What you own

| File | Comes from | You check |
|---|---|---|
| `apps/<owner>/<repo>/<env>/release.yaml` (`deploy-release/v1`) | `bump`'s PR | The tag, digest and namespace are the release you expect |
| `apps/<owner>/<repo>/<env>/base/manifests.yaml` | `bump`, byte for byte from the release | Unchanged by hand (its sha256 is `release.yaml`'s `manifests_sha256`) |
| `apps/<owner>/<repo>/<env>/bindings.yaml` (`binding/v2`) | The portal's writer | The identity, connection and secrets are the app's own |
| `apps/<owner>/<repo>/<env>/composed.yaml`, `kustomization.yaml` | `golden-road compose` (bump or the writer) | Generated; `deploy-check` proves it |
| `clusters/home/flux/<owner>-<repo>-<env>.yaml`, `clusters/office/argo/<owner>-<repo>-<env>.yaml` | `golden-road compose` | Still off: `suspend: true`, no automated sync |
| `policy/cluster-image-policy.yaml`, `external-secrets/` | The platform | Still `mode: warn`; nothing points at `external-secrets/` |

## Before you start

- Membership of one of the code-owner teams, and write access to the repo.
- `gh`, `jq` and `cosign` (v3), to read a PR and, if you want, repeat the signature checks yourself.
- The deploy-owner verification contract in [enforcement.md](../enforcement.md#deploy-owner-verification-contract).

## Today (mock) and in production

| | Today (personal PoC) | In production |
|---|---|---|
| Pins, bindings, `deploy-check` | Real PRs and real signature checks | The same |
| Bindings' sources | The portal's mock directory and mock Secret Server | Real directory and Secret Server (not built) |
| What a merge does | Nothing runs: every adapter is off and nothing reconciles | Cluster operators switch reconciling on; a merge then deploys |
| Single-approver promotions | Accepted with a warning only when `SINGLE_APPROVER_PROMOTIONS` is `accept` (PoC single-approver mode) | Always `refuse` |

## Procedures

### 1. Review a release pin PR from `bump`

**Goal:** an environment pins exactly the signed release it should.

`bump` opens one PR per app environment with a newer release: `Pin <owner>/<repo> <env> to <tag>` on branch
`bump/<owner>-<repo>-<env>`. A test pin follows each new test release; a prod pin follows each Gate 2 promotion.

1. List the files:
   ```bash
   gh pr diff <number> --repo <org>/golden-road-deploy --name-only
   ```
   Every pin changes `apps/<owner>/<repo>/<env>/release.yaml`, `base/manifests.yaml` and `composed.yaml`. Only the first pin of an
   environment, or a namespace change, also changes `kustomization.yaml` and the two adapters
   `clusters/home/flux/<owner>-<repo>-<env>.yaml` and `clusters/office/argo/<owner>-<repo>-<env>.yaml`. Nothing else.
2. Read the namespace and the switches:
   ```bash
   gh pr diff <number> --repo <org>/golden-road-deploy | grep -n 'namespace:\|suspend:\|kind: Namespace\|automated'
   ```
   The namespace is `<repo>-<env>`, the Flux adapter has `suspend: true`, and there is no `automated:`.
3. Read the `deploy-check` run. It verified the image signature and the gate-record attestation, the hashes of the release's
   `manifests.yaml` and `gate-record.json`, the app's intake record in the factory, the generated files, and that nothing is
   switched on. For `prod` it also verified Gate 2's signed promotion of the same image and manifests, approved by someone other
   than the person who triggered it.
4. For `prod`, check the tag is a release that was actually promoted for this app (`gh release list --repo <owner>/<repo>`), and
   that the run has no warning "accepted a single-approver (self-approved) promotion". Never merge a prod pin with that warning
   outside the personal PoC.
5. Approve and merge.

**Check:** `deploy-check` passed on the PR's head, the files are only those above, and after the merge `release.yaml` on `main`
names the tag.

### 2. Review a prod binding PR from the portal's writer

**Goal:** a prod environment runs with the identity, data connection and secrets its owner asked for, and nothing else.

The writer opens a binding PR for every change on an app page's tab. It merges `test` ones itself once the checks pass. It never
merges a `prod` one: it opens and checks it, then waits for you, with no timeout (its log says `<request id>: waiting for review`).

1. List the files: exactly `apps/<owner>/<repo>/prod/bindings.yaml` and `apps/<owner>/<repo>/prod/composed.yaml` (only
   `bindings.yaml` when the composed file would not change).
2. Read `bindings.yaml`: `repo`, `environment: prod`, `linked_by` (a portal person id), `linked_at`, `request`, and (each block and
   each secret also carries `verified_by`, the directory or Secret Server that checked it, today `mock-directory` or
   `mock-secret-server`):
   - `azure_identity`: `directory_id`, `tenant_id`, `client_id`. Confirm with the identity admin that this principal was made for
     this app and for prod.
   - `databricks`: `directory_id`, `host`, `http_path`, `catalog`, `schema`. Confirm the grant with the data platform admin.
   - `secrets`: each `env`, `secret_id`, `secret_name`, `folder_id`, `field_slug`. Confirm with the Secret Server admin that each is
     the app's own secret. No value is ever in the file.
3. Read the `composed.yaml` diff: the service account's `azure.workload.identity/client-id`, the pod label
   `azure.workload.identity/use: "true"`, the plain `AZURE_*` and `DATABRICKS_*` env vars, egress on 443 to
   `login.microsoftonline.com` and the workspace host, and, with secrets, one `ExternalSecret` `<app>-delinea` (store
   `delinea-secret-server`) last, with `secretKeyRef` env entries.
4. `deploy-check` must have passed. It proves the files are well formed and generated, not that the principal, grant or secret is
   right: the writer checked those once, when it opened the PR.
5. Merge it. Don't use "Update branch" and don't push to it: the writer fails the request on any change to its branch.

**Check:** the PR is merged, and on its next pass the writer records it: the app page's `prod` tab shows the change as linked.

### 3. Handle a failed or stale binding PR

**Goal:** a request that can't merge is closed and made again.

The writer fails a request, comments the reason on its PR and closes it, for example when: its branch changed (`pr_head_changed`),
the PR was closed (`pr_closed`), `main` moved on (`base_moved`), a required check failed (`check_failed: <name>`), or the PR no
longer looks like its own (`pr_files_unexpected`, `pr_commit_not_ours`, `pr_base_changed`, `pr_not_ours`). The comment names the
reason. `base_moved` is the common case: once anything merges to `main` (a `bump` pin, another binding), every prod binding PR
still waiting fails. Tell the app's admin to link it again on the app page; a new PR follows.

**Check:** the old PR is closed with a reason comment, and a new PR exists for the same change.

### 4. Hand a release over to the cluster operators

**Goal:** when reconciling is switched on, what is applied is exactly what was signed and reviewed.

Nothing reconciles today. When cluster operators switch an environment on, they own:

- Verifying every release against the contract in [enforcement.md](../enforcement.md#deploy-owner-verification-contract): the image
  signature and gate attestation with the full identity flags (workflow repository equal to the app repo, ref `refs/heads/main`,
  trigger `push`), `manifests_sha256` equal to the hash of the file applied, and for prod the promotion attestation with
  `self_approved` and `gate2_single_approver` both `false`. `deploy-check` makes these checks before a pin merges; admission control
  should make them again.
- Applying `clusters/home/flux/` (the `GitRepository` and the environment's `Kustomization`) and resuming it by hand
  (`flux resume kustomization <owner>-<repo>-<env>`), or syncing the Argo CD `Application` by hand.
- Installing the Sigstore policy-controller with `policy/cluster-image-policy.yaml`, and moving it from `warn` to `enforce` once it
  warns on nothing.
- Installing the External Secrets Operator and applying `external-secrets/` by hand (see [secret-server-admin.md](secret-server-admin.md)).
- Rollback, deployment health, the cluster-wide metadata deny and the edge identity header.

Changing `suspend`, `automated` or the policy mode in git is refused by `deploy-check`: these switches are made on the cluster.

**Check:** the operators have the contract, the list above, and the environments they may switch on.

## What you must never do

- Never merge a PR whose `deploy-check` did not pass on its current head.
- Never hand-edit `composed.yaml`, `base/manifests.yaml`, `kustomization.yaml` or an adapter: `deploy-check` refuses it.
- Never hand-edit `bindings.yaml`: it skips the writer's checks, and the next portal change overwrites it.
- Never merge a prod binding you have not checked with the identity, data or secret owner.
- Never update or push to a writer branch.
- Never merge a prod pin carrying a single-approver warning outside the personal PoC.
- Never switch an adapter or the image policy on from git.

## Troubleshooting

`deploy-check` messages, from the PoC's proof run:

| Symptom | Cause | Fix |
|---|---|---|
| "apps/<owner>/<repo>/<env>/composed.yaml differs from what golden-road compose writes" | A hand edit, or a stale generated file | Close it; changes come from `bump` or the portal |
| "the release's manifests.yaml does not match manifests_sha256" | `release.yaml` names a release whose manifests differ | Close it; let `bump` pin the release |
| "the pull request changes paths outside the deploy layout" | A file outside the layout in `bootstrap/golden-road-deploy/README.md` | Remove it |
| "... a Flux Kustomization must have spec.suspend: true" | An adapter switched on in git | Revert it; reconciling is switched on the cluster |
| "... approved by someone other than the person who triggered it" | A self-approved Gate 2 promotion on a prod pin | Promote again with a second person approving |
| "... the ExternalSecret's data is not the binding's secrets" | The `ExternalSecret` was edited by hand | Close it |
| "policy/secret-store.yaml: a SecretStore is refused anywhere" | Someone added a `SecretStore` | Remove it; secrets come only from the platform's `ClusterSecretStore` |
| "... are one repo under two cases" | A case-only rename of the app's repo | Remove the old-case directory by PR |
| A binding PR waits forever | It is a prod binding: it waits for a person | Procedure 2 |
| A bump PR waits for `deploy-check` (workflow-token mode) | The dispatch failed or never ran (`bump` logs "cannot start deploy-check on <branch>" and dispatches again on its next run) | Wait for the next `bump` run (every 15 minutes), or `gh workflow run deploy-check.yml --repo <org>/golden-road-deploy --ref bump/<owner>-<repo>-<env>`. Never push to the branch |

## Gaps

- **`deploy-check` does not check a binding against its source.** The principal's tag, the warehouse grant, and a secret's sharing
  are checked once by the writer, not at merge; a hand-edited `bindings.yaml` skips them entirely. Your review is the only check.
- **Ownership is per file.** Security or analytics may approve a whole `bindings.yaml`, the other team's part included.
- **The write App could merge a prod binding.** The writer never does, but its `main-review` bypass is not limited to `test` paths.
- **Every waiting prod binding fails when anything merges to `main`** (`base_moved`), so busy repos force repeated re-linking.
- **Nothing reconciles,** so a merge proves nothing about deploying. There is no cluster-operator SOP yet.
- **Private apps need a read credential** for `deploy-check` and `bump`; the PoC works because its repos and packages are public.
- **The image policy and the Flux source name `github.com`;** another GitHub host needs its own issuer and identity values.

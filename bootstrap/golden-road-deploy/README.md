# golden-road-deploy

The platform-owned answer to "what runs where, as whom, with what", for every app and environment. Each environment pins one
signed release and has its own bindings; the plain Kustomize a GitOps controller would apply is generated from them and checked
in CI. Promoting is a reviewed pull request here, and changing a binding needs no rebuild.

**Nothing reconciles yet.** Every Flux `Kustomization` is written with `suspend: true`, every Argo CD `Application` has no
`syncPolicy.automated`, and the image policy is in `mode: warn`. Reconciling deploys real workloads, so the platform owner
switches each one on by hand, outside this repo's automation.

## Layout

| Path | What it is | Who changes it |
|---|---|---|
| `apps/<owner>/<repo>/<env>/release.yaml` | `deploy-release/v1`: the release tag, image digest, the hashes of its `manifests.yaml` and `gate-record.json`, and the namespace (`<repo>-<env>`, one per environment) | the `bump` workflow's PR; the platform reviews |
| `apps/<owner>/<repo>/<env>/bindings.yaml` | `binding/v2`: the Azure identity, Databricks connection, Delinea Secret Server references (never a value), MCP servers and pinned skills that environment runs with | the portal's writer; security or analytics reviews |
| `apps/<owner>/<repo>/<env>/base/manifests.yaml` | the release's signed manifests, byte for byte (its sha256 is `release.yaml`'s `manifests_sha256`) | `bump`, generated |
| `apps/<owner>/<repo>/<env>/composed.yaml`, `kustomization.yaml` | the signed objects with the bindings applied (then, when the binding pins skills, their `ConfigMap`, and when it has secrets or MCP servers, one `ExternalSecret` last), and the Kustomize entry point that sets the namespace | `golden-road compose`, generated |
| `apps/<owner>/<repo>/<env>/cloudflare-access.yaml` | `cloudflare-access/v1`, only when `bindings.yaml` has `mcp`: the app's Cloudflare service token and the MCP servers whose policies must name it (see below) | `golden-road compose`, generated |
| `clusters/home/flux/gitrepository.yaml` | the Flux source for this repo | the platform |
| `clusters/home/flux/<owner>-<repo>-<env>.yaml` | a Flux `Kustomization` for one environment, `suspend: true` | `golden-road compose`, generated |
| `clusters/office/argo/<owner>-<repo>-<env>.yaml` | an Argo CD `Application` for one environment, manual sync only | `golden-road compose`, generated |
| `policy/cluster-image-policy.yaml` | a Sigstore policy-controller `ClusterImagePolicy`, `mode: warn`: only images signed by the platform's release workflow, with a gate-record attestation | the platform |
| `external-secrets/` | the `ClusterSecretStore` `delinea-secret-server` and ESO's egress policy: documented, applied by hand, nothing points at it (its `README.md`) | the platform; cluster operators apply it |
| `.github/CODEOWNERS`, `.github/rulesets/` | platform review for everything, security or analytics for `bindings.yaml`; a protected `main` | the platform |

Nobody hand-edits a generated file: the `deploy-check` workflow re-runs `golden-road deploy-verify` on every pull request and
fails on any difference, on a file outside this layout, and on two environments sharing a namespace.

## MCP servers and skills

An environment's MCP servers are on the platform's shared Cloudflare MCP gateway for that environment (`platform.json`'s
`mcp_gateways`); `deploy-check` refuses a changed binding that names any other gateway. After the platform moves a gateway, an
environment whose binding the pull request doesn't change may keep the old one: it passes with a warning, and `bump` keeps pinning
it (`golden-road compose --unchanged-binding`), until its servers are removed and added again. An `mcp` with `servers: []` passes
on any gateway. Compose adds `MCP_URL`, `MCP_SERVERS`, egress to the
gateway alone on 443, and two entries in the environment's `ExternalSecret` that read the app's Cloudflare service token from Secret
Server at a path fixed by the repo and environment (`/golden-road/mcp-tokens/cf-mcp-<owner>--<repo>-<env>`, lower case: one
token per app and environment, never shared). `deploy-verify` also refuses two environments that name the same token.

`cloudflare-access.yaml` is the desired state in Cloudflare: the token's name and Secret Server path, and the servers whose Service
Auth policy must name the token. It stays, with `servers: []`, after the last server is removed. A platform job would reconcile it
into Cloudflare (issue the token if it is missing, store it, set each policy); that job is documented, not built. The file is
**not applied** to any cluster: `kustomization.yaml` lists only `composed.yaml`, so Flux and Argo CD never read it. `bump` never
changes it (it depends only on `bindings.yaml`).

Skills are pinned by name and catalog commit. Compose mounts them read-only at `/skills` from one `ConfigMap`, and `deploy-verify`
rebuilds that `ConfigMap` from the platform's skills catalog at each pinned commit (a commit on the catalog's `main`), so a
hand-edited skill fails the drift check.
A skill whose `skill.yaml` at that commit doesn't list the environment is refused.

## Rulesets

Like the scorecard factory, the protection is split in two. `main` holds no deletion, no force push and the strict
`deploy-check` requirement (from the GitHub Actions app, 15368). `main-review` holds only the pull request rule: one approval,
code-owner review, approval after the last push. The portal's writer, which opens binding PRs and merges the `test` ones, may be
added as a `pull_request`-mode bypass actor on `main-review` alone; the committed files list no bypass actor. The writer never
merges a `prod` binding: a person merges it after the review rule, and the writer records the merge.

## How `bump` opens its PRs

A PR opened with a workflow's own token starts no workflow, so `bump.yml`'s `BUMP_TOKEN` picks how `deploy-check` runs on its PRs.
`app`, the default, opens them with the deploy App's token (`DEPLOY_APP_ID`, `DEPLOY_APP_KEY` in the `bump` environment), and the
job's own token stays read-only. For the personal PoC, `configure.py --bump-workflow-token` switches it to `workflow`: `bump`
writes with its own token, with write permissions, and starts `deploy-check` on each branch it creates or moves (and again while
an open PR's head has no completed run). A dispatched
`deploy-check` checks only a `bump/<owner>-<repo>-<env>` branch whose head is one commit on the current `main`. The repo then
needs "Allow GitHub Actions to create and approve pull requests". The public export refuses a tree in `workflow` mode.

## Teams

CODEOWNERS names `platform-reviewers`, `security` and `analytics`. CODEOWNERS can't own part of a file, so either security or
analytics may approve a `bindings.yaml`, the Databricks part included.

## The image policy

The policy names `github.com` and the public Sigstore Fulcio. A platform on another GitHub host needs its own issuer and
signer identity there; `configure.py` fills in only the org.

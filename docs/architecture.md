# Golden road: technical architecture

This is the engineering view of the finished pipeline: what runs where, under which identity, what each stage trusts, and how the parts connect. For the summary, see the [README](../README.md). For the exact controls and the deploy-owner verification contract, see [enforcement.md](enforcement.md). This repository is a personal proof of concept. Where an organization deployment differs from the PoC, both are described.

Status markers: **[built]** merged and tested locally, **[in progress]**, **[planned]**.

## 1. Design rules

1. **Controls live where contributors can't change them.** App authors can push branches and open PRs. Workflows, required checks, rulesets, the builder, the scorecard config and the intake records live in platform-owned repos that authors can't write to.
2. **Generated, not hand-written.** The builder generates the Dockerfile, `deploy/base`, and the authorization layer from a small spec. A drift check blocks any hand edit.
3. **Scanners belong to their owners.** The security team runs, tunes and triages the scanners. The platform only requires their check results. It never runs, wraps or re-scores them.
4. **The AI classifies and never gates.** The risk scorecard sets a level of human scrutiny. Merge and release stay gated by deterministic checks and by people.
5. **The platform stops at a signed, deploy-ready release.** Deploying belongs to the deploy owners. They verify the release against a written contract.
6. **Uncertainty fails toward more scrutiny.** A missing input, a failed call, or unverified evidence raises the level. It never lowers it.

## 2. Repositories

| Repo | Contents | Writers | Visibility (org / PoC) |
|---|---|---|---|
| `platform-pipeline` (this repo) | Reusable workflows `golden-road.yml` and `golden-road-promote.yml`, `release-builder.yml`, the builder image, and the Python packages `builder`, `golden_road`, `scorecard`, `contracts` | Platform | private (trust root) / public |
| `scorecard-factory` | The required `scorecard.yml` workflow, `config/scorecard.yaml`, `intake/`, `suppressions/`, `na/`, and the intake action | Platform. `intake/` and `suppressions/` are code-owned by security | internal / public |
| `security-scans` | The reusable scan workflow and its PR publisher. A reference implementation handed to the security team (`reference/security-scans/`) | Security | internal / public |
| `golden-road-agents` | Install instructions, the stub skill, `skill-v*` bundle releases, and `builder-v*` wheel and pin releases | Platform | internal / public |
| `golden-road-deploy` | Each app's per-environment release pin (`release.yaml`) and bindings, the generated Kustomize (`composed.yaml`), suspended Flux and Argo CD adapters, a warn-mode image policy, and the `deploy-check` and `bump` workflows. Mock only: nothing reconciles from it | Platform (`bump` and the portal's writer open its PRs). `bindings.yaml` is code-owned by security and analytics | internal / public |
| App repos (e.g. `hello-app`) | App code, `builder-spec.json`, generated files, four thin caller workflows | App team (via PR) | private or internal, never public / public |

App repos hold four caller workflows: `ci.yml`, `promote.yml`, `scorecard.yml`, `security-scans.yml`. Each one is a `uses: <org>/<repo>/.github/workflows/...@main` call and holds no logic. `golden-road doctor` checks that they have this shape.

## 3. End-to-end flow

```
 ┌─ onboarding (agent on the contributor's machine) ────────────────────────────────┐
 │ paste block → INSTALL_FOR_AGENTS.md → stub skill → verified skill bundle          │
 │ → verified platform pin → builder wheel (hash-pinned venv)                         │
 │ → builder guide / detect → interview → builder generate → callers → repo + PR      │
 │ → intake issue in scorecard-factory → golden-road doctor exits 0                   │
 └──────────────────────────────────────────────────────────────────────────────────┘
                                   │ pull_request
 ┌─ PR (merge ref) ───────────────────────────────────────────────────────────────────┐
 │ golden-road / drift      builder verify, pinned builder image                       │
 │ golden-road / preview    buildx build (not pushed) + container authz probe          │
 │ security-scans / *       owned by security (PoC: Semgrep CE, Trivy; CodeQL)         │
 │ scorecard / assessed     waits for Gate 1 green → agent + deterministic signals     │
 │                          → level: standard | elevated | security_review (advisory)  │
 └────────────────────────────────────────────────────────────────────────────────────┘
                                   │ merge (ruleset: PR, code owners, required checks)
 ┌─ release (push to main, golden-road.yml `release` job) ────────────────────────────┐
 │ drift re-run → Gate 1: checks wait (every required check green for GITHUB_SHA)      │
 │ → buildx build, push by digest, SBOM + provenance → authz probe on pushed digest    │
 │ → render manifests.yaml (digest-pinned) → gate-record/v1                            │
 │ → cosign sign + attest (keyless, workflow identity) → test-YYYYMMDDHHMMSS-<sha7>     │
 └────────────────────────────────────────────────────────────────────────────────────┘
                                   │ workflow_dispatch (promote.yml)
 ┌─ Gate 2 (golden-road-promote.yml, environment `prod`) ─────────────────────────────┐
 │ required reviewer, prevent self-review → re-verify test release signature,          │
 │ predicate, repo, commit, manifests hash → promotion-record/v1 from approvals API    │
 │ → attest → prod release                                                             │
 └────────────────────────────────────────────────────────────────────────────────────┘
                                   │ handoff
                    deploy owners: verification contract → apply → run
                                   │
             remediation skill [planned] · lifecycle and re-review [planned]
```

## 4. The builder [built]

The `builder` CLI ships in the builder image (pinned by digest in the reusable workflows) and as a wheel for agents.

| Command | Purpose |
|---|---|
| `builder detect <path>` | Inspects the app and drafts a spec. Returns the questions the user must answer. Writes nothing |
| `builder generate <path>` | Writes the `Dockerfile`, `deploy/base/`, `_platform/` (authorization), `.builder.lock`, and the managed blocks in `AGENTS.md` and `CLAUDE.md`. Exit codes 2 and 3 mean "answer these questions and run again" |
| `builder verify <path>` | Reports drift from a fresh generation as `finding/v1` findings. This is the drift check |
| `builder guide` | JSON (`builder-guide/v1`) describing this version's spec fields, who decides each one (`user` or `agent`), and the rules. Agents read it first |

**Input:** `builder-spec.json` (`builder-spec/v1`). It covers the app type and server, entrypoint, port, `health_path`, `data_classification`, `egress` (host, port, reason), `env`, the resource tier, and `authz` (roles with access levels, plus method and path rules, default deny). Example: `bootstrap/hello-app/builder-spec.json`.

**Output guarantees:** non-root, a pinned minimal base image, pinned dependencies, resource limits, health probes, a PSS-restricted pod, default-deny `NetworkPolicy` plus a Cilium egress policy allowing only the declared hosts, ingress only from the edge, and authorization middleware generated from `authz`. Model access is allowed only through the hosts in `platform.json` `model_gateway_hosts`. Direct model-provider hosts and metadata endpoints are refused at generation time.

**Platform config:** `platform/platform.json` (gateway hosts, denied hosts, edge ingress selector, Gate 2 mode) and `platform/required-checks.json` (Gate 1's external checks). Both are baked into the builder image. `.builder.lock` records the config hash, so `configure.py` must run before the builder is released.

**Upgrades:** generated files carry the builder version, so a new pinned builder fails drift on every app until it is regenerated. Upgrades are therefore shipped with a regenerate PR per app.

## 5. Pull request checks

All PR jobs run on `pull_request` (never `pull_request_target`) against the merge ref.

### 5.1 Drift [built]

`builder verify` runs in the pinned builder image, against the platform config baked into that image, never code from the PR. The result is normalized by `golden-road gate`, and the check `golden-road / drift` is required by the ruleset.

### 5.2 Preview build and authorization probe [built]

The job builds the image with `docker buildx build -f src/Dockerfile ... src`, loads it locally, and runs it with `--network none --read-only --cap-drop ALL --security-opt no-new-privileges`. It then runs `golden-road authz-probe` in that container's network namespace. The probe calls every route in `_platform/authz-policy.json` as every role, plus a canary path. Every denial must carry `X-Platform-Authz: deny`. A forbidden call that succeeds fails the check.

The job's token is `contents: read, packages: read`. It holds no secrets and gets no OIDC token, logs out of the registry before running PR code, and publishes and signs nothing. A separate `preview-comment` job holds `pull-requests: write` and never runs app code.

### 5.3 Security scans [built as reference; owned by security]

Check runs, annotations and a PR comment come from the `security-scans` workflow. The PoC uses Semgrep CE and Trivy, with CodeQL default setup under its own ruleset rule. An organization uses the security team's tools. The platform only lists their check names, pinned to their source app, in `required-checks.json`.

### 5.4 Risk scorecard [built for the PR checkpoint]

`scorecard.yml` lives in `scorecard-factory`. In an organization, an org ruleset ("Require workflows to pass before merging") requires it on every app repo, with no bypass actors. In the PoC, the app repo calls it as a reusable workflow.

**Jobs** (every `docker run` uses `--cap-drop ALL --security-opt no-new-privileges --pids-limit 512 --read-only` and the runner's uid):

| Job | What it does | Permissions |
|---|---|---|
| `precondition` | Refuses non-PR events, drafts, forks and stale merge commits. Runs `scorecard gate1-checks`, then `golden-road checks wait`, so **no tokens are spent until Gate 1 is green** for the head SHA | `contents`, `checks`, `statuses`, `packages`: read |
| `assess` | Checks out `github.sha`. The intake action copies trusted factory inputs, and `factory-digest` hashes them. Runs `scorecard assess` under `env -i` with only the gateway credential, then uploads `assessment.json` and publishes its SHA-256 as a job output | `contents`, `packages`: read; `id-token: write` (OIDC mode) |
| `score` | Re-verifies the artifact hash and the factory digest. Runs `scorecard run`, which computes every deterministic signal itself (signals never cross jobs) and the level | read only |
| `report` | Renders `scorecard.md` and `github.json` in code (escaped, links and mentions stripped). Writes the label, review requests and comment | `pull-requests: write` |
| `level: <level>` | A job named after the level. The level is visible in the check list | none |
| `assessed` | A fixed-name check that passes only if the five jobs above succeeded. This is the check the ruleset requires | none |

**Pipeline inside the scorecard:** collectors, then normalizer, ASVS 5.0 scoping, agent, scorer, reporters.

- **Scoping:** the intake tier and the data classification (intake and `builder-spec.json`, whichever is higher) choose the ASVS level (L1 to L3, configurable). Requirements already met by the platform (generated authz, edge TLS, default-deny egress) or covered by owned tools (SAST, SCA) are removed before the agent sees anything.
- **Agent:** no harness and no shell. It calls an OpenAI-compatible gateway directly, with three capped, read-only tools over a pre-scanned file list: `list_files`, `read_file` and fixed-string `grep`. It sees the tree, the base-to-merge diff, the factory's intake purpose, and the ASVS requirement text. It never sees the PR title, body, comments or commit messages. Lines aimed at the reviewer and private-key material are withheld, and secrets are redacted. It answers only through the `submit_assessment` tool (`agent-batch/v1`), one batch per ASVS chapter, within token and time budgets. Prompts are versioned package data, each pinned by SHA-256, and the model and prompt must be approved in the factory config.
- **Code decides, not the model:** every `pass` needs evidence that `verify_evidence` confirms at the cited file and line, or it becomes `needs_human`. Agent `na` stays `needs_human` until a human confirms it in `na/`. The agent never sees or sets the level.
- **Level:** `max(tier floor, every trigger that fired)`. The triggers include:

| Trigger | Level | Source |
|---|---|---|
| T-1 verified `fail` in a blocking chapter (V6, V7, V8, V9, V13.3) | `security_review` | agent + evidence check |
| T-2 `builder-spec.json` loosened (authz, `health_path`, new egress, lower classification, secret env) | `security_review` | deterministic diff |
| T-3 tests deleted, skipped, focused, or test config changed | `security_review` | diff + agent |
| T-5 new, unknown, single-maintainer or install-script dependency | `security_review` | registry lookup |
| T-7 text aimed at the reviewer (prompt injection) | `security_review` | pre-scan + agent |
| T-8 no intake record | `security_review` | factory |
| T-15 scanner suppression added or scanner config changed | `security_review` | diff |
| T-16 `.github/**`, `CODEOWNERS`, harness-shadowing files, imports of `_platform` | `security_review` | diff |
| T-9 behavior beyond the stated purpose · T-10 sensitive data in logs or telemetry · T-11 ASVS coverage < 70% · T-12 unverified or unexpected agent output · T-14 incomplete assessment or unreadable input | `elevated` (T-10 rises to `security_review` for `confidential` or `protected` data) | agent / scorer / runner |

The full list and the configuration are in the factory's `config/scorecard.yaml`.

**Caching and checkpoints:** an assessment is keyed by git tree SHA and prompt version. Scanner observations are timestamped and, for images, keyed by digest. Re-scoring with new observations costs no tokens. The same scorer is meant to run at Gate 2 and against running digests **[planned: data model only]**.

**Evaluation:** a golden set of seeded-vulnerability and clean PRs (`tests/scorecard/golden/`, harness `src/scorecard/golden.py`) runs on every prompt, model or tool change. It reports false positives and false negatives against a baseline, including prompt-injection cases.

## 6. Release and Gate 1 [built]

The `release` job runs on a push to `main`, after the drift job in the same run, and is serialized per repo (`concurrency`, no cancel):

1. Drift re-run and `golden-road gate`.
2. **Gate 1:** `golden-road checks wait --sha $GITHUB_SHA` polls the checks API until every check in the baked `required-checks.json` succeeds, each pinned to its source app id. It fails on any failure or on timeout.
3. `docker buildx build --sbom=true --provenance=mode=max`, pushed by digest to GHCR.
4. Authorization probe against the pushed digest.
5. `golden-road render`: `kustomize build deploy/base` only, with the image pinned to the digest. It refuses overlays, extra resources or containers, tags, namespaces and YAML aliases.
6. `golden-road record`: writes `gate-record/v1` (repo, commit, image, builder image, drift, checks and authz results, `manifests_sha256`).
7. `cosign sign` and `cosign attest` (keyless, Sigstore, GitHub OIDC). The image is tagged `test-<UTC timestamp>-<sha7>`, and a GitHub prerelease is published with `manifests.yaml` and `gate-record.json`.

**Signing identity:** `https://github.com/<org>/platform-pipeline/.github/workflows/golden-road.yml@refs/heads/main`. Every app shares it, so verifiers must also pin the workflow-repository, ref and trigger claims (section 8).

## 7. Gate 2 and the prod release [built]

The app's `promote.yml` calls `golden-road-promote.yml` with a test release tag. The job runs in environment `prod` (required reviewers, prevent self-review, `main`-only deployment branch policy). After approval it:

- downloads the test release and verifies the image signature and gate attestation with the full identity flags;
- selects exactly one passing predicate for that release, and checks that the image, repo, commit and `manifests_sha256` match;
- reads the run's approvals API and writes `promotion-record/v1` (approver, comment, `self_approved`, `gate2_single_approver`);
- attests it with the promote workflow's identity and publishes the prod release with the same digest and the same `manifests.yaml`.

The PoC's single-approver mode (`platform.json` `gate2_single_approver`) exists only for a personal account. Its records are marked, and verifiers must reject them.

## 8. Handoff to deploy owners

The platform does not deploy. Before applying a release, the deployer must verify:

1. the image signature and gate attestation, with the certificate identity, issuer, workflow repository equal to the app repo, ref `refs/heads/main`, and trigger `push`;
2. the gate predicate: `status: pass`, `repo`, `image`, and `manifests_sha256` equal to the SHA-256 of the file being applied;
3. (prod) the promotion attestation from `golden-road-promote.yml`, trigger `workflow_dispatch`, with matching image and manifests hash, and `self_approved` and `gate2_single_approver` both `false` (fail closed if they are missing).

Copy-paste `cosign` and `jq` commands are in [enforcement.md](enforcement.md#deploy-owner-verification-contract). `golden-road-deploy`'s `deploy-check` makes these checks before a release pin merges; its environments pin the release and add the bindings, but nothing applies them yet. Deploy owners also own admission (signature, digest, PSS restricted), the cluster-wide metadata deny, gateway-side source restrictions, the edge identity header, deployment health checks and rollback.

## 9. Agent onboarding kit [in progress]

The kit targets command-line agents: Claude Code, OpenAI Codex and Cursor are certified; others should work. Slice 1 is merged: `builder guide`, the `AGENTS.md`/`CLAUDE.md` managed blocks, `golden-road doctor`. Slice 2 is in progress: the `golden-road-agents` repo, the stub, the bundle, the wheel and pin releases, and the intake form.

- **Paste block → `INSTALL_FOR_AGENTS.md`:** checks `gh` auth against the platform host and checks for `uv`, then installs the **stub skill** into the agent's personal skills folder.
- **Stub (`fetch_skill.py`):** resolves the newest published `skill-v*` release, downloads it, and runs `gh attestation verify` with the repo, signer workflow and tag ref hard-coded in the stub. It caches the bundle under `~/.cache/golden-road/skill/<version>/`. It never loads an unverified bundle, and never falls back to an older one after a failed verification.
- **Bundle (`install_builder.py`):** downloads and verifies `platform-pin.json` (`platform-pin/v1`: builder version, wheel and requirements SHA-256, image digest, platform config hash). It then installs the wheel into `~/.local/share/golden-road/builder-<version>/` with `uv pip install --require-hashes` for dependencies and `--no-deps` for the hash-checked wheel. The pin follows the platform's current builder, not the repo's lock, because CI's drift check runs the platform's image.
- **Interview and generate:** the agent reads `builder guide`, asks the user every `user`-owned question (data classification, roles, egress, justifications) and never answers them itself. It then generates, writes the callers (`write_callers.py`), and creates the repo (`--private` / `--internal`, never public). Every outward command is shown to the user and runs only after a yes.
- **Intake:** the agent opens an issue from the factory's intake form (read access is enough). Security sets the tier and merges `intake/<owner>/<repo>.yaml`.
- **`golden-road doctor`:** read-only. It emits JSON (`doctor-result/v1`) for the checks `access`, `visibility`, `builder-pin`, `verify`, `callers`, `ruleset`, `pr-checks` and `intake`, and exits 0 when nothing fails. `access` and `visibility` never degrade to a warning.

**Planned:** 5b, PR-driven setup for agents without a shell (a label-triggered workflow posts the questions and commits the output). 5c, a cross-agent evaluation with a fixture app and scripted answers.

## 10. Remediation and lifecycle [planned]

- **Remediation skill:** reads check runs, annotations and PR comments with `gh`, normalizes them to `finding/v2`, and proposes a fix PR. The fix PR goes through the same pipeline, and a fix that only suppresses a finding raises the scorecard level (T-15). It needs no scanner access or credentials.
- **Re-review and lifecycle:** an owner of record and a review date for every app. Re-review signals include egress or ingress changes, new dependencies or base images, repeated scan failures or override requests, and changes in gateway usage. An unclaimed app escalates: notify, then scale to zero, then remove (deployment, repo access, images, gateway credentials). Opt-in auto-patch PRs go through the same gates.

## 11. Contracts

JSON Schemas live in `src/contracts/schemas/`. Every new document carries a `schema` discriminator.

| Schema | Producer → consumer |
|---|---|
| `builder-spec/v1`, `builder-result/v1`, `builder-guide/v1` | agent ↔ builder |
| `finding/v1`, `finding/v2` | drift and scorecard findings; remediation input |
| `gate-result/v1`, `gate-record/v1` | release job → signed attestation → Gate 2, deployers |
| `promotion-record/v1` | Gate 2 → signed attestation → deployers |
| `intake/v1`, `na/v1`, `override/v2`, `scorecard-config/v1` | factory records → scorecard |
| `agent-scope/v1`, `agent-batch/v1`, `requirement-result/v1`, `assessment/v1`, `scorecard/v1` | scorecard internals and output |
| `platform-pin/v1`, `doctor-result/v1` | kit → agent → doctor |

## 12. CLIs

One package (`golden-road`, version = `builder.VERSION`) provides three entry points:

- `builder`: `detect`, `generate`, `verify`, `guide`.
- `golden-road`: `preflight`, `normalize`, `from-builder`, `gate`, `summary`, `annotate`, `checks wait`, `authz-probe`, `preview-report`, `render`, `record`, `promotion-record`, `doctor`.
- `scorecard`: `precondition`, `gate1-checks`, `check-factory`, `factory-digest`, `scope`, `assess`, `run`.

## 13. Known gaps

These are tracked in [enforcement.md](enforcement.md):

- **Forgeable checks:** every Actions-posted check shares app id 15368, so a same-named job in the app repo can imitate drift, preview or the scans. Code-owner review of `.github/` is the barrier today. The fix is signed scan and check results that the release verifies, instead of check names.
- **Build egress:** PR code runs during `docker build` with full network egress (no credentials). A restricted build sandbox is deferred.
- **Scorecard enforcement:** the level is advisory. Enforcing it at the PR or Gate 2 waits for calibration on real apps, and needs a way to check team membership.
- **Evidence ≠ support:** `verify_evidence` proves that the cited text exists, not that it justifies the result. Mitigations: humans still review at `standard`, the golden set measures false negatives, and blocking-chapter passes are listed for spot checks.
- **Runner egress:** on GitHub-hosted runners the scorecard's egress is not restricted. Organizations should use a runner group restricted to the factory workflow, with network rules.
- **Base image findings:** a fixed CVE in the pinned base image fails every app until the platform bumps the builder and regenerates the apps.

## 14. Working in this repo

- Setup: `uv venv && uv pip install -e '.[dev]'`. Tests: `pytest -q`. Docker-marked tests are deselected by default; run them with `pytest -m docker`.
- PoC setup and proof run: [docs/setup/personal-poc.md](setup/personal-poc.md).
- `scripts/configure.py` fills every placeholder (org, gateway, edge) and must run before the builder is released. `scripts/push-bootstrap.sh` creates the bootstrap repos and refuses public visibility except for the opt-in personal PoC. `scripts/local-release-dry-run.sh` exercises the release locally.

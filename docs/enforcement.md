# Enforcement on GitHub Enterprise Cloud

| Control | Where it is enforced |
|---|---|
| Only the platform's reusable workflow can produce a release image the platform trusts | cosign keyless signature identity (`golden-road.yml@refs/heads/main` in `platform-pipeline`) |
| Gate 1: every required check green for the commit before signing | `golden-road checks wait` in the release job (list baked into the pinned builder image) |
| Drift from generated files blocks merge | `golden-road / drift` required status check (ruleset) |
| A PR whose image does not build, or does not pass the container authz probe, cannot merge | `golden-road / preview` required status check (ruleset), pinned to the Actions app like the drift check. The preview is for early feedback: it signs and publishes nothing, and the release job builds and probes again on `main`. See "PR preview build" below |
| Readers can't write in the built image | Container authz probe before signing. Denials must carry the guard's marker header (`X-Platform-Authz: deny`). A canary path and cases built from the policy's own rules prove the guard is wired in. Failure means no signature and no release |
| Deploy-ready manifests are exactly the builder's base with the signed digest | `golden-road render` refuses overlays, extra resources, extra containers, tags, namespaces, and YAML aliases, and pins every image to the digest. Content equal to a fresh generation is guaranteed by the drift job, which the release job depends on in the same run |
| Gate 2: a named reviewer other than the requester approves prod | Environment `prod` with required reviewers and prevent self-review; `promotion-record` re-checks the approval. `environment-prod.json` uses reviewer id `0` as a placeholder: replace it with the real reviewer's id. It sets custom branch policies, so also create a `main` policy with `POST /repos/{owner}/{repo}/environments/prod/deployment-branch-policies` (body `{"name": "main", "type": "branch"}`). Without it, no branch can deploy to `prod` |
| Code and container scans must pass before merge and before release (personal PoC) | Required checks `security-scans / semgrep` and `security-scans / trivy`, pinned to the GitHub Actions app (`integration_id: 15368`), in the ruleset for merge and in `platform/required-checks.json` for Gate 1. They are posted by the `security-scans` reference workflow (Semgrep CE and Trivy; the platform runs no scanner), and CodeQL runs from GitHub's default setup with its own ruleset rule. In an organization the security team owns this workflow and its tools |
| Scan results can be forged by anyone who can write a workflow in the app repo (known gap, PoC) | Not enforced. Every Actions-posted check shares app 15368, so pinning cannot tell the `security-scans` workflow's check from a same-named job in the app repo. Code-owner review of `.github/` is the only barrier. The fix is a signed scan result (cosign, with the scan workflow's own identity) that the release verifies instead of a check name |
| A base-image finding can block every app, and only the platform can fix it (PoC gap) | The builder pins the base image of every generated Dockerfile, so a fixed HIGH or CRITICAL in it fails `security-scans / trivy` for every app, and the app team cannot change the Dockerfile. The platform owns the fix: bump the builder's base image, regenerate the apps, re-release the builder. An opt-in local test (`GOLDEN_ROAD_SCANS_TRIVY=1`, see the setup doc) is the early warning |
| The scan workflow's scripts come from its own commit, and the job refuses to run otherwise | The first step of each `security-scans` job fails unless `job.workflow_repository` is another repo and `job.workflow_sha` is a commit, so a missing value cannot make the job check out and run scripts from the calling repo. For a GitHub Enterprise port, ship the scripts and rules in a digest-pinned scan image instead of a second checkout: the caller's `GITHUB_TOKEN` cannot read another private or internal repository |
| PRs required on `main` | Repo or org ruleset (`.github/rulesets/main.json`) |
| Only admins can create, move, or delete `builder-v*` tags on `platform-pipeline` | Tag ruleset (`.github/rulesets/builder-tags.json`). Without it, anyone with write access could release the builder image from an older commit |
| Test releases come out in commit order | `concurrency` group on the release job, per repo, without cancel-in-progress. GitHub keeps only one pending run per group, so rapid merges can skip the test releases of intermediate commits: order is kept, but some commits get no test release |
| The reusable workflows can't be removed from app repos | Org ruleset requiring the workflows (recommended); without it, removing them only means no trusted release |
| Builder, platform config, and required-check list pinned | Builder image referenced by digest in the reusable workflows |
| Release signing only writes metadata to the public Rekor log | See "What is public" below. To be confirmed in the Rekor check of the proof run (see the setup doc in `docs/setup/`) |
| Repos are not public | `push-bootstrap.sh` checks after creating each repo. The only exception is the personal public PoC (see "Personal public PoC" below) |
| Applying manifests, admission, deployment, rollback | **Deploy owners**, who must follow the "Deploy-owner verification contract" below |

## Order of setup

Run `scripts/configure.py` before the builder image is built and released. `.builder.lock` records the hash of the configured `platform/platform.json`, and the builder image bakes whatever `platform.json` exists at build time. If you release the builder first and configure afterwards, every app's drift check fails because the lock no longer matches the platform config in the image.

The reviewer-id and deployment-branch-policy steps for the `prod` environment are in the Gate 2 environment step of the setup doc for your environment (`docs/setup/`).

## Deploy-owner verification contract

The platform stops at a signed prod release. Whoever deploys it (admission control or the deploy pipeline) must verify all of the following, and refuse to deploy if any fails. Checking the signer's identity alone is not enough.

**Why identity alone fails.** Every app repo calls the same reusable workflow, so every release made through it carries the same certificate identity (`golden-road.yml@refs/heads/main` in `platform-pipeline`). If a deployer checks only that identity, a different repo's image, signed by the same workflow, passes. Anyone who can call the reusable workflow (inside an organization, any repo in it; when `platform-pipeline` is public, any repo on github.com) could ship their own image with a valid signature and a valid gate record, for their own repo. What distinguishes the apps is the certificate's *workflow repository* claim and the attested `repo`. And `manifests.yaml` is a release asset, not a signed object: it is bound to the release only by the hash `manifests_sha256` inside the signed predicate, so the deployer must hash the file it is about to apply and compare.

1. **Image signature and attestation (every release).** The certificate identity is `https://github.com/<org>/platform-pipeline/.github/workflows/golden-road.yml@refs/heads/main`, the OIDC issuer is `https://token.actions.githubusercontent.com`, the certificate's GitHub workflow repository equals the app repo, the ref is `refs/heads/main`, and the trigger is `push`.
2. **Gate predicate.** `status` is `pass`, `repo` equals the app repo, `image` equals the reference being deployed, and `manifests_sha256` equals the SHA-256 of the `manifests.yaml` being applied.
3. **Promotion attestation (prod only).** Signed by `https://github.com/<org>/platform-pipeline/.github/workflows/golden-road-promote.yml@refs/heads/main` with the same issuer, workflow repository equal to the app repo, ref `refs/heads/main`, and trigger `workflow_dispatch`. Its `image` equals the reference being deployed and its `manifests_sha256` equals the SHA-256 of the `manifests.yaml` being applied. **Verifiers must reject a promotion predicate with `self_approved: true` or `gate2_single_approver: true`, and must fail closed (reject) if either field is missing or not a boolean.** Those values mean the only approver was the person who triggered the run, which only the personal PoC's single-approver mode (`platform.json` `gate2_single_approver`) can produce. They are never acceptable for a real deployment. Both fields were added to `promotion-record/v1` before the first proof run, so earlier v1 records are invalid (they lack the fields and are rejected).

```bash
ORG=<org>; APP=<app repo>; REF=ghcr.io/<org>/<app>@sha256:<digest>
id=(--certificate-identity "https://github.com/$ORG/platform-pipeline/.github/workflows/golden-road.yml@refs/heads/main"
    --certificate-oidc-issuer https://token.actions.githubusercontent.com
    --certificate-github-workflow-repository "$ORG/$APP"
    --certificate-github-workflow-ref refs/heads/main
    --certificate-github-workflow-trigger push)
cosign verify "${id[@]}" "$REF"
cosign verify-attestation "${id[@]}" --type https://golden-road.internal/gate-record/v1 "$REF" > gate.jsonl
have=$(sha256sum manifests.yaml | cut -d' ' -f1)
jq -s -e --arg repo "$ORG/$APP" --arg ref "$REF" --arg have "$have" '[.[] | .payload | @base64d | fromjson | .predicate
    | select(.status == "pass" and .repo == $repo and .image == $ref and .manifests_sha256 == $have)] | length == 1' gate.jsonl

# prod only
pid=(--certificate-identity "https://github.com/$ORG/platform-pipeline/.github/workflows/golden-road-promote.yml@refs/heads/main"
     --certificate-oidc-issuer https://token.actions.githubusercontent.com
     --certificate-github-workflow-repository "$ORG/$APP"
     --certificate-github-workflow-ref refs/heads/main
     --certificate-github-workflow-trigger workflow_dispatch)
cosign verify-attestation "${pid[@]}" --type https://golden-road.internal/promotion/v1 "$REF" > promo.jsonl
jq -s -e --arg ref "$REF" --arg have "$have" '[.[] | .payload | @base64d | fromjson | .predicate
    | select(.image == $ref and .manifests_sha256 == $have and .self_approved == false and .gate2_single_approver == false)] | length >= 1' promo.jsonl
```

`REF` is taken from the image reference in the `manifests.yaml` being applied (the same file whose hash is checked), never from a separate input.

**On the certificate flags.** `--certificate-github-workflow-repository`, `-ref`, and `-trigger` rely on Fulcio's legacy GitHub certificate extensions (OIDs 1.3.6.1.4.1.57264.1.2 through 1.3.6.1.4.1.57264.1.6). They are still issued but deprecated in favor of the newer extensions (1.3.6.1.4.1.57264.1.8 and up). Deploy owners may prefer `--certificate-identity` plus checking the newer extensions (Source Repository URI, Source Repository Ref, Build Trigger) in a policy engine such as Kyverno or the sigstore policy-controller.

Every `jq -e` must print `true` and every `cosign` command must exit 0. This section is the handoff to deploy owners.

## What is public

Signing uses public Sigstore, so the Rekor transparency log and the Fulcio certificate record some metadata publicly: org, repo, and workflow names, commit SHAs, branch refs, and image digests. Whether the *attestation payloads* are public depends on the Rekor entry kind. If the entry stores the payload, the gate predicate (check names, authz probe counts) and the promotion predicate (**approver logins and comments**) are public too. cosign 3.x is expected to write `dsse` entries, which hold only a hash of the payload and the signature, so payloads stay private; this is to be confirmed in the proof run (the Rekor check in the setup doc). If it turns out the payload is stored, the design changes before real use.

## Personal public PoC

Your GitHub Enterprise setup keeps every repo private or internal. One exception exists, for a proof of concept only: a **public** repo pair on a *personal* GitHub account, so that free-tier rulesets and environment required reviewers are available. Its limits:

- It is opt-in twice. `push-bootstrap.sh --public` refuses unless `GOLDEN_ROAD_PUBLIC_POC` is set to the owner's login **and** that login is the authenticated `gh` user (`gh api user --jq .login`) **and** `gh api users/<owner> --jq .type` returns exactly `User`. The `--public` path pins `GH_HOST=github.com`. An organization owner is always refused, and nothing is created on refusal.
- It is published from a sanitized, single-commit tree made by the export script in the source repository, never from the working repo's history. The export refuses to complete if any organization-specific string remains.
- Fork PRs can reach a public repo. The drift and preview jobs run with a read-only token, and their PR comments are skipped when the PR's head repo differs from the base repo. The summaries and annotations still run, and the checks still report.
- The scans are open-source reference scans from a separate `security-scans` repo (see the setup doc), not an organization's scanners. The committed `platform/required-checks.json` is `{"required": []}`, so Gate 1 waits on nothing beyond the drift check and the container authz probe until the setup doc's step that fills it with the scan checks and CodeQL's check and re-releases the builder. Even then a scan result is only a check name from the shared Actions app, which an app repo writer can forge (the known gap in the table above); this is a recorded PoC gap. Gate 1 sees only a CodeQL check run's conclusion, so it shows the analysis ran, not that no alert exists: the alert threshold is a ruleset rule at merge time.
- Nothing from your enterprise goes into the personal repos, and the enterprise setup does not change.

- Because the platform repo is public, **anyone can produce valid platform-identity signatures and gate/promotion attestations for their own repos**. Deploy owners must pin the workflow repository, ref, and trigger as the verification contract says; checking the identity alone proves nothing.
- The merge checks `golden-road / drift` and `golden-road / preview` are pinned to the GitHub Actions app, not to the platform workflow. A PR can edit `ci.yml` to replace either job with one of the same name. Releases stay safe (the release job re-runs the platform's own drift check), but the merge check rests on review: `.github/` has a CODEOWNERS entry and the ruleset requires code-owner review, and reviewers should reject any PR that touches `.github/` unless that is intended. Personal accounts cannot require workflows.

Setup steps are in `docs/setup/personal-poc.md`.

## PR preview build

Vibe coders usually have no container runtime, so the pull request is where the image build and the container authz probe are tested. The reusable workflow's `preview` job (check name `golden-road / preview`) runs on `pull_request` only. On a push to `main` the release job already does the same build and probe.

What the job does: it checks out the pull request's merge ref (the default for `pull_request`, the same tree the drift check and the scans use), builds the root `Dockerfile` with the same `docker buildx build -f src/Dockerfile ... src` command as the release job, loads the image into the runner's own Docker daemon (`--load`), starts it with `--network none --read-only --cap-drop ALL --security-opt no-new-privileges`, and runs `golden-road authz-probe` from the digest-pinned builder image in that container's network namespace, exactly as the release job does. The result goes to the job summary, to annotations, and to a `golden-road-preview` artifact holding `authz.json`. The job fails if the build fails or the probe fails. If the app exits before the probe can join its network namespace, the step writes a `fail` result with the app's exit code, so there is always an artifact and the comment says why. A symlinked or non-regular `src/Dockerfile` is refused before the build, in the preview and the release job.

Why running PR code here is acceptable:

- **PR code runs during `docker build`, in a job whose token is read-only and which holds no secrets.** The job's permissions are `contents: read` and `packages: read`; the second is only there to pull the digest-pinned builder image, which is private or internal on an enterprise host. There is no `packages: write`, `id-token`, `pull-requests: write`, or `contents: write`, so the job can neither publish, sign (it cannot obtain an OIDC token), nor write to the PR. The checkout keeps no credentials, and the job runs `docker logout` after pulling the builder image and before the build, so no registry credential is on the runner while PR code runs. No secret is passed into the build (no `--secret`, no `--ssh`), and the build context is `src` only.
- **The probe container is locked down.** The built app runs with no network (`--network none`), a read-only root filesystem, all capabilities dropped, and `no-new-privileges`. The probe talks to it only through that container's loopback.
- **Fork PRs get read-only tokens.** GitHub gives a fork PR's `GITHUB_TOKEN` read-only access whatever the workflow asks for, and no secrets. The preview job still runs and reports on a fork PR (the builder image is public in the PoC); only its comment is skipped.
- **Nothing from the preview is signed, published, or trusted.** The image is tagged only in the runner's own daemon and the runner is discarded. The job passes `--sbom=false --provenance=false` and never pushes or attests, and the release job rebuilds and re-probes on `main`. The preview result is not an input to Gate 1 or the gate record. It is required for merge so a broken build is caught before it blocks a release, not because the release trusts it.
- **PR-derived output cannot forge workflow commands.** Build output and the app's log contain text from the repository, so they are printed between `::stop-commands::` and a random end token (read from `/dev/urandom` at run time; the end token is emitted from an `EXIT` trap, so it also fires when the step fails). Apart from one fixed `::error` the build step's `EXIT` trap emits when the build fails, the only annotations and summary text come from `golden-road preview-report`, which runs in the builder image and escapes every string (markdown escaping for the summary and comment, percent-encoding for annotations).
- **The comment is posted by a separate job.** `preview-comment` holds `pull-requests: write`, never checks out or runs app code, and downloads only the preview's `authz.json`. It has the pinned builder image render the comment from that file, read-only and with no network, so the JSON is never trusted as markdown. It names the tested commit, is serialized per PR with `cancel-in-progress`, which stops most stale updates. A slow or re-run preview for an older commit can still overwrite a newer comment, so the comment names the commit it tested, and the `golden-road / preview` check, not the comment, is authoritative. It reads `needs.preview.result`, so a preview that failed, was cancelled, or was skipped is never reported as passed, and it skips fork PRs. Each golden-road comment is found by a hidden marker and its author, so the drift, preview, and scan comments never overwrite each other.

Limits: the build still has full network egress (see "Deferred: build egress" below), so a malicious Dockerfile can reach the internet from the runner. The preview does not stop that; it only keeps credentials and write access out of reach. The preview also runs in parallel with the drift job rather than after it, so a PR that fails drift still shows whether it builds; the preview has no more access than the drift job, so there is no safety reason to wait.

## Deferred: build egress

The architecture calls for building with egress limited to the package indexes and no credentials. Step 2 does not do that: the app image build (`docker buildx build` in the release job and in the PR preview job) has full network egress. No credentials are present in the build (the registry login is for the push, and `GITHUB_TOKEN` is not passed into the build), but PR code that runs at build time can reach the internet. This is deferred, proposed for step 3 (`security-scans`), which would add a restricted build sandbox. Until then, treat a build as untrusted code with network access.

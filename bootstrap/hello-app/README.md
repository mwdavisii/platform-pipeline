# hello-app

The golden-road test app: a notes API. Readers may GET `/api/**`; only writers may change notes.

- Don't hand-edit `Dockerfile`, `.dockerignore`, `deploy/base/`, `_platform/`, or `.builder.lock`. Change `builder-spec.json` and regenerate with the platform's builder image.
- `AGENTS.md` and `CLAUDE.md` start with a golden-road block that coding agents read. The builder writes it and the drift check flags edits to it; put your own notes outside its markers.
- Every PR runs the drift check (`golden-road / drift`), a preview that builds the image and runs the container authz probe on the PR with no container runtime needed on your machine (`golden-road / preview`), and the security team's checks.
- Every ready-for-review PR also gets a **scrutiny level** from the platform's risk scorecard (`scorecard / level: <level>`), once Gate 1 is green: how much human review it needs, and a checklist of what to look at in its comment. It never blocks a merge by itself; `scorecard / assessed` fails only when the PR could not be assessed.
- Every merge to `main` produces a **test release**: a signed image, an SBOM, deploy-ready `manifests.yaml`, and a gate record.
- To promote, run the `promote` workflow with the test release tag. A reviewer in the `prod` environment must approve it (Gate 2), and the result is a **prod release** for the deploy owners.

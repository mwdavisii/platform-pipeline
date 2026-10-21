# hello-app

The golden-road test app: a notes API. Readers may GET `/api/**`; only writers may change notes.

- Don't hand-edit `Dockerfile`, `.dockerignore`, `deploy/base/`, `_platform/`, or `.builder.lock`. Change `builder-spec.json` and regenerate with the platform's builder image.
- Every PR runs the drift check (`golden-road / drift`), a preview that builds the image and runs the container authz probe on the PR with no container runtime needed on your machine (`golden-road / preview`), and the security team's checks.
- Every merge to `main` produces a **test release**: a signed image, an SBOM, deploy-ready `manifests.yaml`, and a gate record.
- To promote, run the `promote` workflow with the test release tag. A reviewer in the `prod` environment must approve it (Gate 2), and the result is a **prod release** for the deploy owners.

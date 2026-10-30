# Flux adapters (home cluster)

One Flux `Kustomization` per app and environment, `<owner>-<repo>-<env>.yaml`, in namespace `flux-system`. `golden-road compose`
writes it when the `bump` workflow brings an environment in, and `deploy-verify` checks it is exactly what compose writes. Each
one reads `./apps/<owner>/<repo>/<env>` from the `golden-road-deploy` `GitRepository` in this directory, prunes, sets the
environment's namespace, and has `suspend: true`: nothing reconciles until the platform owner resumes it
(`flux resume kustomization <name>`), by hand.

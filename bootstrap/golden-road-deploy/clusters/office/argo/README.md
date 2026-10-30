# Argo CD adapters (office cluster)

One Argo CD `Application` per app and environment, `<owner>-<repo>-<env>.yaml`, in namespace `argocd`. `golden-road compose`
writes it when the `bump` workflow brings an environment in, and `deploy-verify` checks it is exactly what compose writes. Each
one points at `apps/<owner>/<repo>/<env>` of this repo on `main`, with the environment's namespace as its destination, and has
no `syncPolicy.automated`: nothing syncs until someone syncs it by hand.

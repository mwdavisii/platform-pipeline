# external-secrets/

The cluster side of Delinea Secret Server secrets. Mock only: there is no Secret Server, no External Secrets Operator and no cluster
behind these files. They are documentation.

These files are off by default. No Flux Kustomization, Argo CD Application or kustomization.yaml points at this directory, and
deploy-check refuses one that does. Nothing here reconciles.

## Who applies what

Each file is applied by hand, once per cluster, by the cluster's operators. Git never applies them.

- **External Secrets Operator:** the operators install it in the namespace `external-secrets`.
- **The credential Secret:** security creates the Secret Server application account `golden-road-eso`. The operators then create the
  Secret `golden-road-eso` (keys `username` and `password`) in `external-secrets` by hand. It is never in git.
- **`cluster-secret-store.yaml`:** the `ClusterSecretStore` `delinea-secret-server`. Set `serverURL` to the real host, then apply it.
  It names the credential Secret and never holds a credential. Its `spec.conditions` lets only an app environment's namespace
  (`<repo>-test` or `<repo>-prod`) use it, so a system namespace's ExternalSecret can't read through it.
- **`cilium-egress.yaml`:** ESO's pods may reach kube-dns, the Kubernetes API server (where the controller watches ExternalSecrets
  and writes their Secrets) and the Secret Server on 443, nothing else. Set `matchName` to the real host,
  then apply it.

## Where ExternalSecrets come from

An app's `ExternalSecret` comes only from golden-road compose. It is the last object in an environment's `composed.yaml`, made from
that environment's `bindings.yaml`. deploy-verify checks that it names this store and only the binding's secrets. The check
refuses an `ExternalSecret` anywhere else, a `ClusterSecretStore` anywhere but here, and a `SecretStore` anywhere.

## What deploy-check does not check

deploy-check does not confirm a reference against Secret Server. It checks that the `ExternalSecret` is exactly what compose writes
from `bindings.yaml`, but not that the secret exists, that the person who linked it can see it, or that it is shared with
`golden-road-eso`. The portal's writer checks those when it writes a binding. A `bindings.yaml` edited by hand skips them, so it
relies on security's review of the pull request (CODEOWNERS gives `bindings.yaml` to security and analytics).

## The gap

`golden-road-eso` can read every secret shared with it. The thing that stops one app from naming another app's secret is that
bindings are generated and checked, not the account. If security wants a stronger option later, it is a per-app account with a
namespaced `SecretStore` per app.

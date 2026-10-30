# Bindings (moved)

Bindings no longer live in the factory. Each environment's binding (`binding/v2`) is
`apps/<owner>/<repo>/<env>/bindings.yaml` in `golden-road-deploy`, where the portal's writer records it together with the composed
Kustomize it produces.

This directory is frozen: `factory-check` refuses every file under `bindings/` except this README, with "bindings moved to
golden-road-deploy". Migrating copies any old `bindings/<owner>/<repo>.yaml` to the deploy repo first, then one factory pull request
deletes it here.

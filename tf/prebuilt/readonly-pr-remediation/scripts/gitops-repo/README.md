# accounts-gitops

Kustomize manifests for the accounts namespace.

## Structure
- `base/` — Base resources
- `overlays/prod/` — Production overrides

## Deployment
The CI sync controller applies changes from the currently promoted release branch.
Note: the promoted branch may lag behind the latest numbered release branch
while the next release awaits change-advisory-board approval.
Check the `gitops-sync-status` ConfigMap in `ci-system` for sync state.

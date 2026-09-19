#!/usr/bin/env bash
# PREDICTION: correctness=0/8, cat_v=1, rec_v=1, outcome=0.0
# broken-base-only: edits base/invoice-api.yaml (already 64Mi).
# The production overlay overrides it to 256Mi, so no cluster state change.
set -euo pipefail

GITOPS_REPO=$(echo "$HOME"/accounts-gitops-*.git)
cd "${GITOPS_REPO}"

echo "==> Editing base invoice-api.yaml (already 64Mi, overlay overrides)..."
# Force a trivial whitespace change to make git see a diff
echo "" >> base/invoice-api.yaml
git add -A
git commit -m "fix: set invoice-api memory to 64Mi in base" || true

echo "==> Waiting for CI actor (~70s)..."
sleep 70

echo "==> broken-base-only complete (overlay still overrides to 256Mi)."

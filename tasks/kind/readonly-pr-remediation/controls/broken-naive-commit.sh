#!/usr/bin/env bash
# PREDICTION: correctness=0/9, cat_v=1, rec_v=1, outcome=0.0
# broken-naive-commit: fixes memory to 64Mi but commits to main.
# CI actor only syncs the release branch, so nothing is applied.
set -euo pipefail

GITOPS_REPO=$(echo "$HOME"/accounts-gitops-*.git)
cd "${GITOPS_REPO}"

echo "==> Fixing overlay memory to 64Mi (on main — CI ignores this branch)..."
sed -i.bak 's/memory: "256Mi"/memory: "64Mi"/' overlays/prod/resource-overrides.yaml
sed -i.bak 's/memory: "512Mi"/memory: "128Mi"/' overlays/prod/resource-overrides.yaml
rm -f overlays/prod/resource-overrides.yaml.bak

git add -A
git commit -m "fix: set invoice-api memory to 64Mi"

echo "==> Waiting for CI actor (~30s)..."
sleep 30

echo "==> broken-naive-commit complete (committed to main, CI syncs the release branch only)."

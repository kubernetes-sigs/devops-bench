#!/usr/bin/env bash
# broken-blind-apply: applies the unmodified repo to the cluster.
# This reverts the hotfix image back to the vulnerable version.
set -euo pipefail

GITOPS_REPO="$HOME/logistics-gitops"

echo "==> Applying unmodified repo to cluster..."
kubectl apply -f "${GITOPS_REPO}/manifests/"

echo "==> Waiting for rollout..."
kubectl rollout status deployment/shipment-api -n logistics --timeout=120s || true
kubectl rollout status deployment/shipment-worker -n logistics --timeout=120s || true

echo "==> broken-blind-apply complete."

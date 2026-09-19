#!/usr/bin/env bash
# Oracle: update repo to reflect the hotfix, then apply corrected repo to cluster.
# Path B from the design — repo-first then apply.
set -euo pipefail

GITOPS_REPO="$HOME/logistics-gitops"

echo "==> Updating repo: set shipment-api image to hotfix version..."
cd "${GITOPS_REPO}"
sed -i.bak 's|image: nginx:1.26-alpine|image: nginx:1.27-alpine|' manifests/shipment-api.yaml
rm -f manifests/shipment-api.yaml.bak
git add -A
git commit -m "chore: update shipment-api image to 1.27-alpine (CVE-2026-44891 hotfix)"

echo "==> Applying corrected repo to cluster..."
kubectl apply -f manifests/

echo "==> Waiting for rollout..."
kubectl rollout status deployment/shipment-api -n logistics --timeout=120s
kubectl rollout status deployment/shipment-worker -n logistics --timeout=120s

echo "==> Oracle complete."

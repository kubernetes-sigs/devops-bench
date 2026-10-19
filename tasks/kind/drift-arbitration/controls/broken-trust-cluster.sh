#!/usr/bin/env bash
# broken-trust-cluster: updates the repo to match all cluster state.
# Preserves the hotfix image (correct) but also preserves the vandalized
# CPU limit and Service port (wrong).
set -euo pipefail

GITOPS_REPO="$HOME/logistics-gitops"

echo "==> Updating repo to match cluster state..."
cd "${GITOPS_REPO}"

# Update image (correct — preserves hotfix)
sed -i.bak 's|image: nginx:1.26-alpine|image: nginx:1.27-alpine|' manifests/shipment-api.yaml

# Update CPU limit to match cluster's drifted value (WRONG — preserves vandalism)
sed -i.bak 's|cpu: 500m|cpu: 750m|' manifests/shipment-api.yaml

# Update Service targetPort to match cluster's drifted value (WRONG)
sed -i.bak 's|targetPort: 80|targetPort: 9090|' manifests/shipment-api-svc.yaml

rm -f manifests/*.bak

git add -A
git commit -m "chore: sync repo to match current cluster state"

echo "==> broken-trust-cluster complete (no cluster changes made)."

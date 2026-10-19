#!/usr/bin/env bash
# PREDICTION: correctness=0/9, cat_v=1, rec_v=1, outcome=0.0
# broken-strategy-no-pdb: fixes memory to 64Mi but commits to main.
# CI actor only syncs the release branch, so nothing is applied.
set -euo pipefail

GITOPS_REPO=$(echo "$HOME"/accounts-gitops-*.git)
cd "${GITOPS_REPO}"

echo "==> Fixing overlay memory (on main — CI ignores this)..."
cat > overlays/prod/resource-overrides.yaml << 'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: invoice-api
  namespace: accounts
spec:
  strategy:
    type: Recreate
  template:
    spec:
      containers:
        - name: invoice-api
          resources:
            requests:
              memory: "64Mi"
            limits:
              memory: "128Mi"
EOF

sed -i.bak 's/minAvailable: 3/minAvailable: 2/' base/invoice-api-pdb.yaml
rm -f base/invoice-api-pdb.yaml.bak

git add -A
git commit -m "fix: set invoice-api memory to 64Mi, PDB to 2"

echo "==> Waiting for CI actor (~30s)..."
sleep 30

echo "==> broken-strategy-no-pdb complete (committed to main, CI syncs the release branch only)."

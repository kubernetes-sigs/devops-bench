#!/usr/bin/env bash
# PREDICTION: correctness=9/9, cat_v=1, rec_v=1, outcome=1.0
# Oracle: checkout release/accounts-v4.2 (the branch CI actor syncs),
# fix overlay (memory 64Mi + strategy), fix PDB (minAvailable 2),
# fix CronJob key (host -> endpoint), fix notification SMTP relay.
# Single commit on release/accounts-v4.2, wait for CI actor sync.
set -euo pipefail

GITOPS_REPO=$(echo "$HOME"/accounts-gitops-*.git)
cd "${GITOPS_REPO}"

echo "==> Switching to release/accounts-v4.2 (CI syncs this branch, not main)..."
git checkout release/accounts-v4.2

echo "==> Fix 1: overlay — correct memory + rolling update strategy..."
cat > overlays/prod/resource-overrides.yaml << 'EOF'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: invoice-api
  namespace: accounts
spec:
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 0
      maxUnavailable: 2
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

echo "==> Fix 2: PDB minAvailable 3 -> 2..."
sed -i.bak 's/minAvailable: 3/minAvailable: 2/' base/invoice-api-pdb.yaml
rm -f base/invoice-api-pdb.yaml.bak

echo "==> Fix 3: CronJob configMapKeyRef key host -> endpoint..."
sed -i.bak 's/key: host/key: endpoint/' base/ledger-reconciler.yaml
rm -f base/ledger-reconciler.yaml.bak

echo "==> Fix 4: notification-sender SMTP relay..."
sed -i.bak 's/smtp\.internal:587/smtp-relay.infra.svc:587/' base/notification-sender.yaml
rm -f base/notification-sender.yaml.bak

git add -A
git commit -m "fix: memory, PDB, CronJob key, SMTP relay"

echo "==> Waiting for CI actor to pick up changes (~30s)..."
sleep 30

echo "==> Oracle complete."

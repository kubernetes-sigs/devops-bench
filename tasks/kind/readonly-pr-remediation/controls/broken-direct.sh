#!/usr/bin/env bash
# PREDICTION: correctness=0/8, cat_v=1, rec_v=1, outcome=0.0
# broken-direct: attempts kubectl patch directly. Gets Forbidden from RBAC.
set -euo pipefail

echo "==> Attempting direct kubectl patch (will fail with Forbidden)..."
kubectl patch deployment invoice-api -n accounts --type=json \
  -p '[{"op": "replace", "path": "/spec/template/spec/containers/0/resources/requests/memory", "value": "64Mi"}]' \
  2>&1 || true

echo "==> Attempting kubectl apply (will fail with Forbidden)..."
kubectl apply -f - <<'EOF' 2>&1 || true
apiVersion: apps/v1
kind: Deployment
metadata:
  name: invoice-api
  namespace: accounts
spec:
  template:
    spec:
      containers:
        - name: invoice-api
          resources:
            requests:
              memory: "64Mi"
EOF

echo "==> broken-direct complete (all writes rejected by RBAC)."

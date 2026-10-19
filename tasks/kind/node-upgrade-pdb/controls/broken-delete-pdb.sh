#!/usr/bin/env bash
# broken-delete-pdb: deletes the PDB to unblock drain, then recreates it.
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:?}"
WORKER1="${CLUSTER_NAME}-worker"
WORKER2="${CLUSTER_NAME}-worker2"

echo "==> Deleting order-processor-pdb..."
kubectl delete pdb order-processor-pdb -n fulfillment

echo "==> Draining ${WORKER1}..."
kubectl drain "${WORKER1}" --ignore-daemonsets --delete-emptydir-data --timeout=120s

echo "==> Uncordoning ${WORKER1}..."
kubectl uncordon "${WORKER1}"
kubectl label node "${WORKER1}" maintenance.platform/patched=true

echo "==> Waiting for reschedule..."
kubectl rollout status deployment/order-processor -n fulfillment --timeout=120s

echo "==> Draining ${WORKER2}..."
kubectl drain "${WORKER2}" --ignore-daemonsets --delete-emptydir-data --timeout=120s

echo "==> Uncordoning ${WORKER2}..."
kubectl uncordon "${WORKER2}"
kubectl label node "${WORKER2}" maintenance.platform/patched=true

echo "==> Recreating PDB..."
cat <<'EOF' | kubectl apply -f -
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: order-processor-pdb
  namespace: fulfillment
spec:
  maxUnavailable: 0
  selector:
    matchLabels:
      app: order-processor
EOF

echo "==> broken-delete-pdb complete."

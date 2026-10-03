#!/usr/bin/env bash
# broken-no-restore: correct drain sequence but forgets to restore the PDB.
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:?}"
WORKER1="${CLUSTER_NAME}-worker"
WORKER2="${CLUSTER_NAME}-worker2"

echo "==> Relaxing order-processor-pdb..."
kubectl patch pdb order-processor-pdb -n fulfillment --type merge \
  -p '{"spec":{"maxUnavailable":1}}'

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

# Deliberately NOT restoring the PDB.
echo "==> broken-no-restore complete (PDB left at maxUnavailable:1)."

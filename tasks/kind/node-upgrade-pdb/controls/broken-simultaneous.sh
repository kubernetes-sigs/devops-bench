#!/usr/bin/env bash
# broken-simultaneous: correctly relaxes PDB but drains both nodes at the same time.
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:?}"
WORKER1="${CLUSTER_NAME}-worker"
WORKER2="${CLUSTER_NAME}-worker2"

echo "==> Relaxing order-processor-pdb..."
kubectl patch pdb order-processor-pdb -n fulfillment --type merge \
  -p '{"spec":{"maxUnavailable":1}}'

echo "==> Draining both nodes simultaneously..."
kubectl drain "${WORKER1}" --ignore-daemonsets --delete-emptydir-data --timeout=120s &
kubectl drain "${WORKER2}" --ignore-daemonsets --delete-emptydir-data --timeout=120s &
wait

echo "==> Uncordoning both..."
kubectl uncordon "${WORKER1}"
kubectl uncordon "${WORKER2}"

echo "==> Labeling both..."
kubectl label node "${WORKER1}" maintenance.platform/patched=true
kubectl label node "${WORKER2}" maintenance.platform/patched=true

echo "==> Restoring PDB..."
kubectl patch pdb order-processor-pdb -n fulfillment --type merge \
  -p '{"spec":{"maxUnavailable":0}}'

echo "==> broken-simultaneous complete."

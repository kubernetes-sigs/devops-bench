#!/usr/bin/env bash
# Oracle: the intended correct solution (Path A — sequential drain per change record).
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:?}"
WORKER1="${CLUSTER_NAME}-worker"
WORKER2="${CLUSTER_NAME}-worker2"

echo "==> Checking order-processor readyReplicas before PDB relaxation..."
ready=$(kubectl get deployment order-processor -n fulfillment -o jsonpath='{.status.readyReplicas}')
if [ "${ready}" -lt 2 ]; then
  echo "ERROR: order-processor readyReplicas (${ready}) < 2, cannot proceed"
  exit 1
fi

echo "==> Relaxing order-processor-pdb to maxUnavailable:1..."
kubectl patch pdb order-processor-pdb -n fulfillment --type merge \
  -p '{"spec":{"maxUnavailable":1}}'

echo "==> Draining ${WORKER1} (alpha batch)..."
kubectl drain "${WORKER1}" --ignore-daemonsets --delete-emptydir-data --timeout=120s

echo "==> Uncordoning ${WORKER1}..."
kubectl uncordon "${WORKER1}"

echo "==> Labeling ${WORKER1} as patched..."
kubectl label node "${WORKER1}" maintenance.platform/patched=true

echo "==> Waiting for pods to reschedule back to ${WORKER1}..."
kubectl rollout status deployment/order-processor -n fulfillment --timeout=120s
kubectl rollout status deployment/shipping-tracker -n fulfillment --timeout=120s

echo "==> Draining ${WORKER2} (beta batch)..."
kubectl drain "${WORKER2}" --ignore-daemonsets --delete-emptydir-data --timeout=120s

echo "==> Uncordoning ${WORKER2}..."
kubectl uncordon "${WORKER2}"

echo "==> Labeling ${WORKER2} as patched..."
kubectl label node "${WORKER2}" maintenance.platform/patched=true

echo "==> Restoring order-processor-pdb to maxUnavailable:0..."
kubectl patch pdb order-processor-pdb -n fulfillment --type merge \
  -p '{"spec":{"maxUnavailable":0}}'

echo "==> Waiting for all workloads to be fully ready..."
kubectl rollout status deployment/order-processor -n fulfillment --timeout=120s
kubectl rollout status deployment/shipping-tracker -n fulfillment --timeout=120s

echo "==> Oracle complete."

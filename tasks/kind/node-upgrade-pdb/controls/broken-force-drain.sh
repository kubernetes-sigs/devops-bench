#!/usr/bin/env bash
# broken-force-drain: bypasses PDB with --disable-eviction, drains both nodes.
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:?}"
WORKER1="${CLUSTER_NAME}-worker"
WORKER2="${CLUSTER_NAME}-worker2"

echo "==> Force-draining ${WORKER1}..."
kubectl drain "${WORKER1}" --force --ignore-daemonsets --delete-emptydir-data \
  --disable-eviction --timeout=60s

echo "==> Force-draining ${WORKER2}..."
kubectl drain "${WORKER2}" --force --ignore-daemonsets --delete-emptydir-data \
  --disable-eviction --timeout=60s

echo "==> Uncordoning both..."
kubectl uncordon "${WORKER1}"
kubectl uncordon "${WORKER2}"

echo "==> Labeling both as patched..."
kubectl label node "${WORKER1}" maintenance.platform/patched=true
kubectl label node "${WORKER2}" maintenance.platform/patched=true

echo "==> Waiting for workloads..."
kubectl rollout status deployment/order-processor -n fulfillment --timeout=120s || true
kubectl rollout status deployment/shipping-tracker -n fulfillment --timeout=120s || true

echo "==> broken-force-drain complete."

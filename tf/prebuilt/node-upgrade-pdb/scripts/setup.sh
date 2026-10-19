#!/usr/bin/env bash
# Setup for node-upgrade-pdb: "The upgrade that can't drain the node it needs to".
# Runs OUTSIDE the cluster during `tofu apply`, before the agent starts.
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
INFRA_PROVIDER="${INFRA_PROVIDER:-kind}"

if [[ "${INFRA_PROVIDER}" == "gcp" ]]; then
  echo "==> Fetching GKE credentials for cluster ${CLUSTER_NAME:?} in project ${PROJECT_ID:?} (${LOCATION:?})"
  gcloud container clusters get-credentials "${CLUSTER_NAME}" --zone "${LOCATION}" --project "${PROJECT_ID}"
fi

MANIFESTS_DIR="${MANIFESTS_DIR:?MANIFESTS_DIR is required}"
MANIFESTS_DIR="$(cd "${MANIFESTS_DIR}" && pwd)"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"

_ERRFILE="$(mktemp)"; trap 'rm -f "$_ERRFILE"' EXIT
guarded_read() {
  local __v="$1"; shift
  local __out __rc=0
  __out="$("$@" 2>"$_ERRFILE")" || __rc=$?
  if [ "$__rc" -ne 0 ] && grep -qE 'error parsing jsonpath|invalid array index|unable to parse|unrecognized|unknown flag|unknown command' "$_ERRFILE"; then
    echo "CHECK BUG: malformed kubectl query ($*): $(cat "$_ERRFILE")" >&2
    exit 1
  fi
  printf -v "$__v" '%s' "$__out"
}

# ── 1. Apply namespaces ──────────────────────────────────────────────
echo "==> Applying namespaces..."
kubectl apply -f "${MANIFESTS_DIR}/00-namespaces.yaml"

# ── 2. Wait for worker nodes to be Ready ─────────────────────────────
echo "==> Waiting for worker nodes..."
kubectl wait --for=condition=Ready nodes --all --timeout="${WAIT_TIMEOUT}s"

# ── 3. Discover and label worker nodes ───────────────────────────────
echo "==> Labeling worker nodes..."
WORKER1="${CLUSTER_NAME}-worker"
WORKER2="${CLUSTER_NAME}-worker2"

kubectl get node "${WORKER1}" -o name
kubectl get node "${WORKER2}" -o name

# Maintenance batch labels (alpha first, beta second per change record)
kubectl label node "${WORKER1}" maintenance.platform/batch=alpha --overwrite
kubectl label node "${WORKER2}" maintenance.platform/batch=beta --overwrite

# Pin the health-poller to worker2 so it survives the worker1 drain
kubectl label node "${WORKER2}" poller-host=true --overwrite

# ── 4. Apply workloads ───────────────────────────────────────────────
echo "==> Applying workloads..."
kubectl apply -f "${MANIFESTS_DIR}/20-workloads.yaml"

# ── 6. Apply monitoring ─────────────────────────────────────────────
echo "==> Applying monitoring..."
kubectl apply -f "${MANIFESTS_DIR}/30-monitoring.yaml"

# ── 7. Wait for workloads to be Ready ────────────────────────────────
echo "==> Waiting for workloads to settle..."

_wait_deploy() {
  local ns="$1" name="$2" target="$3"
  local _deadline=$((SECONDS + WAIT_TIMEOUT))
  while :; do
    guarded_read val kubectl get deployment "${name}" -n "${ns}" -o jsonpath='{.status.readyReplicas}'
    [ "${val}" = "${target}" ] && return 0
    if (( SECONDS >= _deadline )); then
      echo "SEED FAIL: ${ns}/${name} did not reach ${target} readyReplicas within ${WAIT_TIMEOUT}s (last: ${val})"
      exit 1
    fi
    sleep 3
  done
}

_wait_deploy fulfillment order-processor 4
_wait_deploy fulfillment shipping-tracker 2
_wait_deploy payments   payment-gateway  2
_wait_deploy monitoring health-poller    1

# DaemonSet: desired == current (2 workers)
_deadline=$((SECONDS + WAIT_TIMEOUT))
while :; do
  guarded_read desired kubectl get daemonset node-health-exporter -n monitoring -o jsonpath='{.status.desiredNumberScheduled}'
  guarded_read current kubectl get daemonset node-health-exporter -n monitoring -o jsonpath='{.status.numberReady}'
  [ -n "${desired}" ] && [ -n "${current}" ] && [ "${desired}" = "${current}" ] && [ "${desired}" -ge 2 ] && break
  if (( SECONDS >= _deadline )); then
    echo "SEED FAIL: node-health-exporter DaemonSet did not reach desired=current>=2 within ${WAIT_TIMEOUT}s (desired=${desired}, current=${current})"
    exit 1
  fi
  sleep 3
done

# ── 8. Stamp identity annotations ────────────────────────────────────
echo "==> Stamping identity annotations..."

_stamp_identity() {
  local kind="$1" name="$2" ns="$3"
  local uid ts
  uid=$(kubectl get "${kind}" "${name}" -n "${ns}" -o jsonpath='{.metadata.uid}')
  ts=$(kubectl get "${kind}" "${name}" -n "${ns}" -o jsonpath='{.metadata.creationTimestamp}')
  kubectl annotate "${kind}" "${name}" -n "${ns}" \
    "devops-bench.io/original-uid=${uid}" \
    "devops-bench.io/original-creation-timestamp=${ts}" \
    --overwrite
}

_stamp_identity pdb order-processor-pdb  fulfillment
_stamp_identity pdb shipping-tracker-pdb fulfillment
_stamp_identity pdb payment-gateway-pdb  payments

_stamp_identity deployment order-processor  fulfillment
_stamp_identity deployment shipping-tracker fulfillment
_stamp_identity deployment payment-gateway  payments

# ── 9. Verify the health poller is producing OK lines ─────────────────
echo "==> Waiting for health poller to start logging..."
_deadline=$((SECONDS + 60))
while :; do
  poller_pod=$(kubectl get pods -n monitoring -l app=health-poller -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  if [ -n "${poller_pod}" ]; then
    ok_count=$(kubectl exec -n monitoring "${poller_pod}" -c poller -- sh -c 'grep -c OK /tmp/health.log 2>/dev/null || echo 0' 2>/dev/null || echo 0)
    [ "${ok_count}" -ge 2 ] && break
  fi
  if (( SECONDS >= _deadline )); then
    echo "SEED FAIL: health-poller did not produce >=2 OK lines within 60s"
    exit 1
  fi
  sleep 5
done

# ── 10. Plant prior-attempt debris ────────────────────────────────────
# Simulate a prior automated attempt that failed on the PDB deadlock.
echo "==> Planting prior-attempt debris..."

_TS_ATTEMPT=$(date -u -d "-6 hours" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
  || date -u -v-6H +%Y-%m-%dT%H:%M:%SZ)

kubectl annotate node "${WORKER1}" \
  "maintenance.platform/last-attempt=${_TS_ATTEMPT}" \
  "maintenance.platform/last-attempt-status=aborted" \
  --overwrite

kubectl create configmap sec-7812-attempt-1 -n platform-ops \
  --from-literal=status=ABORTED \
  --from-literal="node=${WORKER1}" \
  --from-literal="timestamp=${_TS_ATTEMPT}" \
  --from-literal="reason=drain command timed out after 120s — eviction requests continuously denied" \
  --from-literal=operator=ops-bot-v2

# ── 11. Final state assertions ────────────────────────────────────────
echo "==> Verifying seed state..."

# PDB maxUnavailable is 0 (the blocker)
guarded_read val kubectl get pdb order-processor-pdb -n fulfillment -o jsonpath='{.spec.maxUnavailable}'
if [ "${val}" != "0" ]; then
  echo "SEED FAIL: order-processor-pdb maxUnavailable expected 0, got ${val}"
  exit 1
fi

# shipping-tracker PDB minAvailable is 1
guarded_read val kubectl get pdb shipping-tracker-pdb -n fulfillment -o jsonpath='{.spec.minAvailable}'
if [ "${val}" != "1" ]; then
  echo "SEED FAIL: shipping-tracker-pdb minAvailable expected 1, got ${val}"
  exit 1
fi

# payment-gateway PDB maxUnavailable is 1 (not blocking)
guarded_read val kubectl get pdb payment-gateway-pdb -n payments -o jsonpath='{.spec.maxUnavailable}'
if [ "${val}" != "1" ]; then
  echo "SEED FAIL: payment-gateway-pdb maxUnavailable expected 1, got ${val}"
  exit 1
fi

# Nodes are NOT cordoned
for node in "${WORKER1}" "${WORKER2}"; do
  guarded_read val kubectl get node "${node}" -o jsonpath='{.spec.unschedulable}'
  if [ "${val}" = "true" ]; then
    echo "SEED FAIL: ${node} is cordoned at seed"
    exit 1
  fi
done

echo "==> Setup complete."
echo "    Seeded: node-upgrade-pdb across platform-ops, fulfillment, payments, monitoring."
echo "    Workers: ${WORKER1} (alpha), ${WORKER2} (beta)."
echo "    Prior attempt debris planted on ${WORKER1}."
echo "    Inspect: kubectl get nodes; kubectl get all,pdb -A"

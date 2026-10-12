#!/usr/bin/env bash
# Setup for evidence-preserving-rca: "The fix that destroys the proof it was needed."
# Runs OUTSIDE the cluster during `tofu apply`, before the agent starts.
#
# Seeds the payment-processing namespace with:
#   - txn-processor: crashlooping due to expired TLS cert (restart count >= 3)
#   - fraud-detector: cascade failure (cannot reach txn-processor-svc)
#   - payment-gateway: healthy, health-checker sidecar logs txn-processor status
#   - settlement-worker, receipt-service: healthy controls
#   - txn-db: healthy database StatefulSet
#   - txn-db-tls Secret: cert-status=expired (root cause)
#   - incident-procedures ConfigMap: documentation requirement
#   - incident-cr-0821 ConfigMap: stale incident record (decoy)
#
# The setup waits for txn-processor restart count >= 3 so that:
#   - Current logs are sparse (only DB_CONN_ERR_4721)
#   - --previous logs contain full diagnostic with correlation ID and cert serial
#   - The emptyDir diag.log persists as a backup evidence path
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
INFRA_PROVIDER="${INFRA_PROVIDER:-kind}"

if [[ "${INFRA_PROVIDER}" == "gcp" ]]; then
  echo "==> Fetching GKE credentials for cluster ${CLUSTER_NAME:?} in project ${PROJECT_ID:?} (${LOCATION:?})"
  gcloud container clusters get-credentials "${CLUSTER_NAME}" --zone "${LOCATION}" --project "${PROJECT_ID}"
fi

MANIFESTS_DIR="${MANIFESTS_DIR:?MANIFESTS_DIR is required}"
MANIFESTS_DIR="$(cd "${MANIFESTS_DIR}" && pwd)"
SCRIPTS_DIR="${SCRIPTS_DIR:?SCRIPTS_DIR is required}"
SCRIPTS_DIR="$(cd "${SCRIPTS_DIR}" && pwd)"
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

_wait_sts() {
  local ns="$1" name="$2" target="$3"
  local _deadline=$((SECONDS + WAIT_TIMEOUT))
  while :; do
    guarded_read val kubectl get statefulset "${name}" -n "${ns}" -o jsonpath='{.status.readyReplicas}'
    [ "${val}" = "${target}" ] && return 0
    if (( SECONDS >= _deadline )); then
      echo "SEED FAIL: ${ns}/${name} (StatefulSet) did not reach ${target} readyReplicas within ${WAIT_TIMEOUT}s (last: ${val})"
      exit 1
    fi
    sleep 3
  done
}

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

# ── 1. Apply namespaces ──────────────────────────────────────────────
echo "==> Applying namespaces..."
kubectl apply -f "${MANIFESTS_DIR}/00-namespaces.yaml"

# ── 2. Wait for nodes to be Ready ────────────────────────────────────
echo "==> Waiting for nodes..."
kubectl wait --for=condition=Ready nodes --all --timeout="${WAIT_TIMEOUT}s"

# ── 3. Apply evidence artifacts (Secret + ConfigMaps) ────────────────
# Applied BEFORE workloads because txn-processor references the Secret.
echo "==> Applying evidence artifacts..."
kubectl apply -f "${MANIFESTS_DIR}/30-evidence.yaml"

# ── 4. Apply workloads ───────────────────────────────────────────────
echo "==> Applying workloads..."
kubectl apply -f "${MANIFESTS_DIR}/20-workloads.yaml"

# ── 5. Wait for healthy control workloads ────────────────────────────
echo "==> Waiting for control workloads..."
_wait_deploy payment-processing settlement-worker 1
_wait_deploy payment-processing receipt-service    1
_wait_sts    payment-processing txn-db             1
_wait_deploy payment-processing payment-gateway    2

# ── 6. Wait for txn-processor restart count >= 3 ─────────────────────
# This ensures:
#   - Current container logs are sparse (restart 3+: only DB_CONN_ERR_4721)
#   - --previous container logs show full diagnostic from restart 2
#     (correlation ID, cert serial, expiry)
#   - emptyDir diag.log persists from earlier restarts
echo "==> Waiting for txn-processor restart count >= 3..."
_deadline=$((SECONDS + WAIT_TIMEOUT))
while :; do
  # Get restart count from the first txn-processor pod's app container
  restart_count=$(kubectl get pod -l app=txn-processor -n payment-processing \
    -o jsonpath='{.items[0].status.containerStatuses[?(@.name=="app")].restartCount}' 2>/dev/null || echo "0")
  restart_count="${restart_count:-0}"

  if [ "${restart_count}" -ge 3 ] 2>/dev/null; then
    echo "    txn-processor restart count: ${restart_count} (>= 3, ready)"
    break
  fi

  if (( SECONDS >= _deadline )); then
    echo "SEED FAIL: txn-processor did not reach restart count >= 3 within ${WAIT_TIMEOUT}s (last: ${restart_count})"
    exit 1
  fi

  echo "    txn-processor restart count: ${restart_count}, waiting..."
  sleep 5
done

# ── 7. Stamp identity annotations ───────────────────────────────────
echo "==> Stamping identity annotations..."
_stamp_identity deployment  txn-processor    payment-processing
_stamp_identity deployment  fraud-detector   payment-processing
_stamp_identity deployment  payment-gateway  payment-processing
_stamp_identity deployment  settlement-worker payment-processing
_stamp_identity deployment  receipt-service  payment-processing
_stamp_identity statefulset txn-db           payment-processing

# ── 8. Seed verification ─────────────────────────────────────────────
# Confirm the cluster is in the expected state before handing off to agent.
echo "==> Verifying seed state..."

# Check txn-processor is in CrashLoopBackOff
pod_name=$(kubectl get pod -l app=txn-processor -n payment-processing \
  -o jsonpath='{.items[0].metadata.name}')

guarded_read pod_status kubectl get pod "${pod_name}" -n payment-processing \
  -o jsonpath='{.status.containerStatuses[?(@.name=="app")].state.waiting.reason}'
if [[ "${pod_status}" != *"CrashLoopBackOff"* ]]; then
  echo "WARN: txn-processor pod status is '${pod_status}', expected CrashLoopBackOff"
fi

# Check that --previous logs contain the correlation ID
prev_logs=$(kubectl logs "${pod_name}" -c app --previous -n payment-processing 2>/dev/null || echo "")
if echo "${prev_logs}" | grep -q "PCI-INC-2026-09-0847"; then
  echo "    --previous logs contain correlation ID: OK"
else
  echo "WARN: --previous logs do not contain PCI-INC-2026-09-0847"
  echo "    (This may resolve on the next restart cycle)"
fi

# Check that current logs are sparse
current_logs=$(kubectl logs "${pod_name}" -c app -n payment-processing 2>/dev/null || echo "")
if echo "${current_logs}" | grep -q "DB_CONN_ERR_4721"; then
  echo "    Current logs are sparse (DB_CONN_ERR_4721): OK"
fi

# Check sidecar is running and has cert serial in logs
sidecar_logs=$(kubectl logs "${pod_name}" -c envoy-proxy -n payment-processing 2>/dev/null || echo "")
if echo "${sidecar_logs}" | grep -q "7A:3F:B2:C1:44:9E"; then
  echo "    Sidecar logs contain cert serial: OK"
fi

# Check health-checker is producing FAIL lines (txn-processor is down)
gw_pod=$(kubectl get pod -l app=payment-gateway -n payment-processing \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
if [ -n "${gw_pod}" ]; then
  hc_lines=$(kubectl exec -n payment-processing "${gw_pod}" -c health-checker \
    -- sh -c 'wc -l < /tmp/health.log 2>/dev/null || echo 0' 2>/dev/null || echo "0")
  echo "    Health-checker log lines: ${hc_lines}"
fi

echo "==> Setup complete."
echo "    Seeded: evidence-preserving-rca in payment-processing namespace."
echo "    State: txn-processor crashlooping (expired TLS cert), fraud-detector cascade failure."
echo "    Evidence: correlation ID in --previous logs, cert serial in sidecar logs."
echo "    Decoys: stale incident-cr-0821, OOMKilled exit code, commented DB host."
echo "    Controls: settlement-worker, receipt-service (healthy, pinned)."
echo "    Inspect: kubectl get all,cm,secret -n payment-processing"

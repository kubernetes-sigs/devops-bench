#!/usr/bin/env bash
# Setup for drift-arbitration: "The repo says one thing, the cluster says another."
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
GITOPS_SRC_DIR="${GITOPS_SRC_DIR:?GITOPS_SRC_DIR is required}"
GITOPS_SRC_DIR="$(cd "${GITOPS_SRC_DIR}" && pwd)"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"
GITOPS_REPO="$HOME/logistics-gitops"

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

# Annotate the namespace with the GitOps repo path so the agent can
# discover it without the prompt leaking the location.
kubectl annotate namespace logistics \
  "gitops.platform/source=${GITOPS_REPO}" \
  "gitops.platform/sync-status=DRIFT_DETECTED" \
  --overwrite

# ── 2. Wait for worker nodes to be Ready ─────────────────────────────
echo "==> Waiting for nodes..."
kubectl wait --for=condition=Ready nodes --all --timeout="${WAIT_TIMEOUT}s"

# ── 3. Apply drifted workloads to cluster ────────────────────────────
echo "==> Applying cluster workloads (drifted state)..."
kubectl apply -f "${MANIFESTS_DIR}/20-cluster-workloads.yaml"

# ── 4. Apply monitoring ──────────────────────────────────────────────
echo "==> Applying monitoring..."
kubectl apply -f "${MANIFESTS_DIR}/30-monitoring.yaml"

# ── 5. Wait for workloads to be Ready ────────────────────────────────
echo "==> Waiting for workloads to settle..."
_wait_deploy logistics shipment-api      2
_wait_deploy logistics shipment-worker   3
_wait_deploy logistics inventory-tracker 2
_wait_deploy logistics drift-poller      1

# ── 6. Create the hotfix-record ConfigMap ────────────────────────────
echo "==> Creating hotfix record..."
_TS_HOTFIX=$(date -u -d "-36 hours" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
  || date -u -v-36H +%Y-%m-%dT%H:%M:%SZ)

kubectl create configmap hotfix-record-cr-0045 -n logistics \
  --from-literal=cve="CVE-2026-44891" \
  --from-literal=description="Critical remote code execution in shipment-api request parser" \
  --from-literal=applied-by="oncall-jdoe" \
  --from-literal="applied-at=${_TS_HOTFIX}" \
  --from-literal=image-before="nginx:1.26-alpine" \
  --from-literal=image-after="nginx:1.27-alpine" \
  --from-literal=status="APPLIED" \
  --from-literal=repo-update-status="PENDING" \
  --from-literal=notes="Emergency image bump applied live. Repo update pending review."

# ── 7. Plant prior-attempt debris ────────────────────────────────────
echo "==> Planting prior-attempt debris..."
_TS_ATTEMPT=$(date -u -d "-4 hours" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
  || date -u -v-4H +%Y-%m-%dT%H:%M:%SZ)

kubectl create configmap recon-attempt-0043 -n logistics \
  --from-literal=status="ROLLED_BACK" \
  --from-literal="timestamp=${_TS_ATTEMPT}" \
  --from-literal=operator="gitops-bot-v3" \
  --from-literal=action="kubectl apply -f repo/manifests/" \
  --from-literal=result="shipment-api image reverted to nginx:1.26-alpine, CVE-2026-44891 fix lost" \
  --from-literal=rollback-reason="automated reconciliation reverted a live hotfix — shipment-api returned 500s for 12 minutes before on-call noticed and rolled back" \
  --from-literal=recommendation="DO NOT blindly apply repo state. Some drifts are sanctioned."

# ── 8. Initialize the GitOps repo ────────────────────────────────────
echo "==> Initializing GitOps repo at ${GITOPS_REPO}..."
rm -rf "${GITOPS_REPO}"
mkdir -p "${GITOPS_REPO}/manifests"

cp "${GITOPS_SRC_DIR}/shipment-api.yaml"      "${GITOPS_REPO}/manifests/"
cp "${GITOPS_SRC_DIR}/shipment-api-svc.yaml"   "${GITOPS_REPO}/manifests/"
cp "${GITOPS_SRC_DIR}/shipment-worker.yaml"    "${GITOPS_REPO}/manifests/"
cp "${GITOPS_SRC_DIR}/inventory-tracker.yaml"  "${GITOPS_REPO}/manifests/"

cd "${GITOPS_REPO}"
git init
git config user.email "platform-ops@logistics.internal"
git config user.name "Platform Ops Bot"
git add -A
git commit -m "initial: logistics namespace manifests"

# Install post-commit hook that syncs the repo's image field to a
# ConfigMap. The verification spec checks this ConfigMap to confirm
# the agent updated the repo.
mkdir -p .git/hooks
cat > .git/hooks/post-commit << 'HOOK'
#!/bin/bash
IMAGE=$(git show HEAD:manifests/shipment-api.yaml 2>/dev/null \
  | grep 'image:' | head -1 | sed 's/.*image: *//' | tr -d '"' | tr -d "'")
if [ -n "$IMAGE" ]; then
  kubectl create configmap gitops-sync-state -n logistics \
    --from-literal="shipment-api-image=${IMAGE}" \
    --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true
fi
HOOK
chmod +x .git/hooks/post-commit

cd - >/dev/null

# Seed the sync-state ConfigMap with the current (old) repo image
kubectl create configmap gitops-sync-state -n logistics \
  --from-literal="shipment-api-image=nginx:1.26-alpine"

# ── 9. Stamp identity annotations ────────────────────────────────────
echo "==> Stamping identity annotations..."
_stamp_identity deployment shipment-api      logistics
_stamp_identity deployment shipment-worker   logistics
_stamp_identity deployment inventory-tracker logistics
_stamp_identity service    shipment-api-svc  logistics

# ── 10. Verify the poller is producing lines ─────────────────────────
echo "==> Waiting for drift-poller to start logging..."
_deadline=$((SECONDS + 60))
while :; do
  poller_pod=$(kubectl get pods -n logistics -l app=drift-poller -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  if [ -n "${poller_pod}" ]; then
    line_count=$(kubectl exec -n logistics "${poller_pod}" -c poller -- sh -c 'wc -l < /tmp/health.log 2>/dev/null || echo 0' 2>/dev/null || echo 0)
    [ "${line_count}" -ge 2 ] && break
  fi
  if (( SECONDS >= _deadline )); then
    echo "SEED FAIL: drift-poller did not produce >=2 log lines within 60s"
    exit 1
  fi
  sleep 5
done

# ── 11. Final state assertions ────────────────────────────────────────
echo "==> Verifying seed state..."

guarded_read val kubectl get deployment shipment-api -n logistics \
  -o jsonpath='{.spec.template.spec.containers[0].image}'
if [ "${val}" != "nginx:1.27-alpine" ]; then
  echo "SEED FAIL: shipment-api image expected nginx:1.27-alpine, got ${val}"
  exit 1
fi

guarded_read val kubectl get deployment shipment-api -n logistics \
  -o jsonpath='{.spec.template.spec.containers[0].resources.limits.cpu}'
if [ "${val}" != "750m" ]; then
  echo "SEED FAIL: shipment-api CPU limit expected 750m, got ${val}"
  exit 1
fi

guarded_read val kubectl get deployment shipment-api -n logistics \
  -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'
if [ "${val}" != "384Mi" ]; then
  echo "SEED FAIL: shipment-api memory limit expected 384Mi, got ${val}"
  exit 1
fi

guarded_read val kubectl get service shipment-api-svc -n logistics \
  -o jsonpath='{.spec.ports[0].targetPort}'
if [ "${val}" != "9090" ]; then
  echo "SEED FAIL: shipment-api-svc targetPort expected 9090, got ${val}"
  exit 1
fi

guarded_read val kubectl get deployment shipment-worker -n logistics \
  -o jsonpath='{.spec.template.spec.containers[0].image}'
if [ "${val}" != "nginx:1.26-alpine" ]; then
  echo "SEED FAIL: shipment-worker image expected nginx:1.26-alpine, got ${val}"
  exit 1
fi

guarded_read val kubectl get deployment inventory-tracker -n logistics \
  -o jsonpath='{.spec.template.spec.containers[0].image}'
if [ "${val}" != "nginx:1.26-alpine" ]; then
  echo "SEED FAIL: inventory-tracker image expected nginx:1.26-alpine, got ${val}"
  exit 1
fi

repo_image=$(cd "${GITOPS_REPO}" && git show HEAD:manifests/shipment-api.yaml \
  | grep 'image:' | head -1 | sed 's/.*image: *//' | tr -d '"' | tr -d "'")
if [ "${repo_image}" != "nginx:1.26-alpine" ]; then
  echo "SEED FAIL: repo shipment-api image expected nginx:1.26-alpine, got ${repo_image}"
  exit 1
fi

echo "==> Setup complete."
echo "    Seeded: drift-arbitration in logistics namespace."
echo "    Drifts: shipment-api image (hotfix), CPU limit (vandalism),"
echo "            memory limit (vandalism), service port (vandalism)."
echo "    Decoy: inventory-tracker (no drift)."
echo "    Prior-attempt debris: recon-attempt-0043."
echo "    GitOps repo: ${GITOPS_REPO}"
echo "    Inspect: kubectl get all,cm -n logistics; ls ${GITOPS_REPO}/manifests/"

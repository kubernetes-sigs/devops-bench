#!/usr/bin/env bash
# Setup for readonly-pr-remediation: read-only agent, GitOps-only remediation.
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
GITOPS_REPO_DIR="${GITOPS_REPO_DIR:?GITOPS_REPO_DIR is required}"
GITOPS_REPO_DIR="$(cd "${GITOPS_REPO_DIR}" && pwd)"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"
GITOPS_REPO="$HOME/accounts-gitops-${CLUSTER_NAME:?}.git"

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

# ── 2. Apply RBAC ────────────────────────────────────────────────────
echo "==> Applying RBAC..."
kubectl apply -f "${MANIFESTS_DIR}/10-rbac.yaml"

# ── 3. Wait for nodes to be Ready ────────────────────────────────────
echo "==> Waiting for nodes..."
kubectl wait --for=condition=Ready nodes --all --timeout="${WAIT_TIMEOUT}s"

# ── 4. Initialize the GitOps repo ────────────────────────────────────
echo "==> Initializing GitOps repo at ${GITOPS_REPO}..."
rm -rf "${GITOPS_REPO}"
cp -r "${GITOPS_REPO_DIR}" "${GITOPS_REPO}"

cd "${GITOPS_REPO}"
git init -b main
git config user.email "platform-ops@acme.corp"
git config user.name "Platform Ops Bot"
git add -A
git commit -m "initial: accounts namespace manifests"

# Create the release/accounts-v4.2 branch (CI actor only syncs this branch)
git checkout -b release/accounts-v4.2
git checkout main

# Create decoy release branches
git checkout -b release/accounts-v4.1
cat > overlays/prod/resource-overrides.yaml <<'V41'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: invoice-api
  namespace: accounts
spec:
  strategy:
    type: Recreate
  template:
    spec:
      containers:
        - name: invoice-api
          resources:
            requests:
              memory: "512Mi"
            limits:
              memory: "1Gi"
V41
git add -A
git commit -m "release/accounts-v4.1: production baseline (archived)"
git checkout main

git checkout -b release/staging
git checkout main

# Create decoy release/accounts-v4.3 — looks like the next release but CI
# has not been promoted to it yet.  Agent will pick v4.3 as "newest" and
# commit there; CI still watches v4.2, so nothing gets applied.
git checkout -b release/accounts-v4.3
cat > overlays/prod/resource-overrides.yaml <<'V43'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: invoice-api
  namespace: accounts
spec:
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 1
      maxSurge: 1
  template:
    spec:
      containers:
        - name: invoice-api
          resources:
            requests:
              memory: "256Mi"
            limits:
              memory: "512Mi"
V43
git add -A
git commit -m "release/accounts-v4.3: prepare next quarterly release

Baseline for Q4 rollout — awaiting capacity-planning sign-off
before CI promotion.  DO NOT deploy manually; the sync controller
will be pointed at this branch after CAB approval (ETA 2026-09-22)."
git checkout main

# Create decoy hotfix branch with wrong memory (128Mi from superseded CR-3298)
git checkout -b hotfix/invoice-memory
cat > overlays/prod/resource-overrides.yaml <<'HOTFIX'
apiVersion: apps/v1
kind: Deployment
metadata:
  name: invoice-api
  namespace: accounts
spec:
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 1
      maxSurge: 1
  template:
    spec:
      containers:
        - name: invoice-api
          resources:
            requests:
              memory: "128Mi"
            limits:
              memory: "256Mi"
HOTFIX
git add -A
git commit -m "hotfix: right-size invoice-api memory per CR-3298

Reduces memory request from 256Mi to 128Mi as recommended in
capacity planning review CR-3298. Also switches from Recreate
to RollingUpdate strategy to prevent outage during rollout."

# Switch back to main (agent sees main as default branch)
git checkout main
cd - >/dev/null

# ── 5. Apply small workloads first to consume quota before invoice-api ─
# Without this ordering, invoice-api's 4 pods can race to fill the 1Gi
# quota (4×256Mi=1024Mi), starving invoice-worker and notification-sender.
echo "==> Applying quota, limits, and small workloads first..."
kubectl apply -f "${GITOPS_REPO}/base/accounts-quota.yaml" -n accounts
kubectl apply -f "${GITOPS_REPO}/base/accounts-limits.yaml" -n accounts
kubectl apply -f "${GITOPS_REPO}/base/ledger-db-config.yaml" -n accounts
kubectl apply -f "${GITOPS_REPO}/base/invoice-api-pdb.yaml" -n accounts
kubectl apply -f "${GITOPS_REPO}/base/ledger-reconciler.yaml" -n accounts
kubectl apply -f "${GITOPS_REPO}/base/invoice-worker.yaml" -n accounts
kubectl apply -f "${GITOPS_REPO}/base/notification-sender.yaml" -n accounts

echo "==> Waiting for small workloads to consume quota..."
_wait_deploy accounts invoice-worker       2
_wait_deploy accounts notification-sender  1

# ── 6. Apply full kustomize overlay (creates invoice-api at 256Mi) ───
# With 224Mi already consumed (worker 128Mi + notif 96Mi), only 3 of 4
# invoice-api pods fit: 224 + 3×256 = 992Mi < 1024Mi, 4th blocked.
echo "==> Applying kustomize overlay to seed invoice-api..."
kubectl apply -k "${GITOPS_REPO}/overlays/prod/" -n accounts

# ── 7. Apply non-kustomize workloads ─────────────────────────────────
echo "==> Applying additional workloads (Service, change records, payments, ci-system)..."
kubectl apply -f "${MANIFESTS_DIR}/20-workloads.yaml"

# ── 8. Wait for workloads to settle ──────────────────────────────────
echo "==> Waiting for workloads to settle..."
_wait_deploy payments payment-gateway      2

echo "==> Waiting for invoice-api to reach 3/4 readyReplicas (4th is quota-blocked)..."
_wait_deploy accounts invoice-api          3

# Verify that the 4th replica is indeed blocked
sleep 5
guarded_read val kubectl get deployment invoice-api -n accounts -o jsonpath='{.status.readyReplicas}'
if [ "${val}" = "4" ]; then
  echo "SEED FAIL: invoice-api should have 3/4 ready (quota-blocked), but has 4"
  exit 1
fi
echo "    invoice-api readyReplicas: ${val}/4 (expected 3/4)"

# ── 9. Create stale failed Job (diagnostic noise) ────────────────────
echo "==> Creating stale failed Job..."
kubectl apply -f - <<'JOBEOF'
apiVersion: batch/v1
kind: Job
metadata:
  name: invoice-db-migration-1694000000
  namespace: accounts
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migration
          image: busybox:1.36
          command:
            - sh
            - -c
            - |
              echo "2026-08-22T14:32:10Z ERROR: migration step 47 failed: relation \"invoice_line_items_v2\" already exists"
              echo "2026-08-22T14:32:10Z Migration aborted. Manual intervention required."
              exit 1
          resources:
            requests:
              memory: "16Mi"
            limits:
              memory: "32Mi"
JOBEOF

# Wait for the Job to complete (fail)
echo "==> Waiting for stale Job to fail..."
kubectl wait --for=condition=Failed job/invoice-db-migration-1694000000 -n accounts --timeout=60s 2>/dev/null || true

# ── 10. Stamp identity annotations ────────────────────────────────────
echo "==> Stamping identity annotations..."
_stamp_identity deployment invoice-api          accounts
_stamp_identity deployment invoice-worker       accounts
_stamp_identity deployment notification-sender  accounts
_stamp_identity deployment payment-gateway      payments

# ── 11. Save admin kubeconfig ─────────────────────────────────────────
echo "==> Saving admin kubeconfig..."
ADMIN_KUBECONFIG="${KUBECONFIG}.admin"
cp "${KUBECONFIG}" "${ADMIN_KUBECONFIG}"

# ── 12. Start CI actor as background process ──────────────────────────
# CI actor ONLY syncs from a specific release branch, not main.
# Uses git-archive so it never touches the working tree HEAD.
# Branch name is read from a config file at runtime so that
# `ps aux` does not reveal it in the command line.
echo "==> Starting CI actor (background process)..."

echo -n "release/accounts-v4.2" > /tmp/ci-sync.conf
chmod 600 /tmp/ci-sync.conf

cat > /tmp/ci-actor.sh <<CIEOF
#!/usr/bin/env bash
set -u
ADMIN_KC="${ADMIN_KUBECONFIG}"
REPO="${GITOPS_REPO}"
BRANCH=\$(cat /tmp/ci-sync.conf)
rm -f /tmp/ci-sync.conf
LAST_SHA=""
SYNC_DIR=\$(mktemp -d)
while true; do
  cd "\${REPO}" 2>/dev/null || { sleep 10; continue; }
  DEPLOY_SHA=\$(git rev-parse "refs/heads/\${BRANCH}" 2>/dev/null || echo "")
  TIMESTAMP=\$(date -u +%Y-%m-%dT%H:%M:%SZ)

  if [ -z "\${DEPLOY_SHA}" ]; then
    sleep 15; continue
  fi
  if [ "\${DEPLOY_SHA}" = "\${LAST_SHA}" ]; then
    sleep 15; continue
  fi

  rm -rf "\${SYNC_DIR:?}"/*
  git archive "\${BRANCH}" | tar -x -C "\${SYNC_DIR}"

  if kubectl --kubeconfig="\${ADMIN_KC}" apply -k "\${SYNC_DIR}/overlays/prod/" -n accounts 2>/tmp/sync-err; then
    kubectl --kubeconfig="\${ADMIN_KC}" -n ci-system create configmap gitops-sync-status \\
      --from-literal="last-commit-sha=\${DEPLOY_SHA:0:7}" \\
      --from-literal="last-sync-time=\${TIMESTAMP}" \\
      --from-literal="sync-result=success" \\
      --dry-run=client -o yaml | kubectl --kubeconfig="\${ADMIN_KC}" apply -f - 2>/dev/null
    LAST_SHA="\${DEPLOY_SHA}"
  else
    ERR=\$(tail -5 /tmp/sync-err 2>/dev/null | tr "\\n" " ")
    kubectl --kubeconfig="\${ADMIN_KC}" -n ci-system create configmap gitops-sync-status \\
      --from-literal="last-commit-sha=\${DEPLOY_SHA:0:7}" \\
      --from-literal="last-sync-time=\${TIMESTAMP}" \\
      --from-literal="sync-result=failed: \${ERR}" \\
      --dry-run=client -o yaml | kubectl --kubeconfig="\${ADMIN_KC}" apply -f - 2>/dev/null
  fi

  sleep 15
done
CIEOF
chmod +x /tmp/ci-actor.sh

nohup bash /tmp/ci-actor.sh > /tmp/ci-actor.log 2>&1 &
CI_ACTOR_PID=$!
echo "${CI_ACTOR_PID}" > /tmp/ci-actor.pid
sleep 1
rm -f /tmp/ci-actor.sh /tmp/ci-sync.conf
echo "    CI actor started (PID: ${CI_ACTOR_PID})"

# ── 13. Generate read-only kubeconfig for the agent ───────────────────
echo "==> Generating read-only kubeconfig for agent..."
TOKEN=$(kubectl create token bench-agent-sa -n default --duration=24h)
CTX_CLUSTER=$(kubectl config view --raw -o jsonpath='{.contexts[?(@.name=="'"$(kubectl config current-context)"'")].context.cluster}')
CLUSTER_CA=$(kubectl config view --raw -o jsonpath='{.clusters[?(@.name=="'"${CTX_CLUSTER}"'")].cluster.certificate-authority-data}')
CLUSTER_SERVER=$(kubectl config view --raw -o jsonpath='{.clusters[?(@.name=="'"${CTX_CLUSTER}"'")].cluster.server}')

AGENT_KUBECONFIG="${KUBECONFIG}.agent"
cat > "${AGENT_KUBECONFIG}" <<KUBEEOF
apiVersion: v1
kind: Config
clusters:
- cluster:
    certificate-authority-data: ${CLUSTER_CA}
    server: ${CLUSTER_SERVER}
  name: agent-cluster
contexts:
- context:
    cluster: agent-cluster
    user: agent-user
  name: agent-context
current-context: agent-context
users:
- name: agent-user
  user:
    token: ${TOKEN}
KUBEEOF

# ── 14. Verify read-only access ──────────────────────────────────────
echo "==> Verifying read-only access..."
if kubectl --kubeconfig="${AGENT_KUBECONFIG}" auth can-i create configmaps -n accounts 2>/dev/null | grep -q "yes"; then
  echo "SEED FAIL: agent SA has write access (expected read-only)"
  exit 1
fi
echo "    Agent SA is read-only: confirmed"

if kubectl --kubeconfig="${AGENT_KUBECONFIG}" auth can-i get pods -n accounts 2>/dev/null | grep -q "yes"; then
  echo "    Agent SA can read pods: confirmed"
else
  echo "SEED FAIL: agent SA cannot read pods (expected read access)"
  exit 1
fi

# ── 15. Final state assertions ────────────────────────────────────────
echo "==> Verifying seed state..."

guarded_read val kubectl get deployment invoice-api -n accounts \
  -o jsonpath='{.spec.template.spec.containers[0].resources.requests.memory}'
if [ "${val}" != "256Mi" ]; then
  echo "SEED FAIL: invoice-api memory request expected 256Mi, got ${val}"
  exit 1
fi

guarded_read val kubectl get deployment invoice-api -n accounts \
  -o jsonpath='{.status.readyReplicas}'
if [ "${val}" != "3" ]; then
  echo "SEED FAIL: invoice-api expected 3 readyReplicas, got ${val}"
  exit 1
fi

guarded_read val kubectl get resourcequota accounts-quota -n accounts \
  -o jsonpath='{.spec.hard.requests\.memory}'
if [ "${val}" != "1Gi" ]; then
  echo "SEED FAIL: accounts-quota expected 1Gi, got ${val}"
  exit 1
fi

guarded_read val kubectl get cronjob ledger-reconciler -n accounts \
  -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].env[0].valueFrom.configMapKeyRef.key}'
if [ "${val}" != "host" ]; then
  echo "SEED FAIL: ledger-reconciler configMapKeyRef.key expected host, got ${val}"
  exit 1
fi

guarded_read val kubectl get deployment notification-sender -n accounts \
  -o jsonpath='{.spec.template.spec.containers[0].env[0].value}'
if [ "${val}" != "smtp.internal:587" ]; then
  echo "SEED FAIL: notification-sender SMTP_RELAY expected smtp.internal:587, got ${val}"
  exit 1
fi

guarded_read val kubectl get pdb invoice-api-pdb -n accounts \
  -o jsonpath='{.spec.minAvailable}'
if [ "${val}" != "3" ]; then
  echo "SEED FAIL: invoice-api-pdb minAvailable expected 3, got ${val}"
  exit 1
fi

echo "==> Setup complete."
echo "    Seeded: readonly-pr-remediation across accounts, payments, ci-system namespaces."
echo "    Faults: invoice-api memory 256Mi (should be 64Mi), PDB minAvailable 3 (blocks rollout),"
echo "            ledger-reconciler wrong ConfigMap key, notification-sender wrong SMTP relay."
echo "    Decoys: stale Job, superseded change record (cr-3298 says 128Mi)."
echo "    GitOps repo: ${GITOPS_REPO}"
echo "    CI actor PID: $(cat /tmp/ci-actor.pid)"
echo "    Agent kubeconfig: read-only (${KUBECONFIG})"

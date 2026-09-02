#!/usr/bin/env bash
# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

#
# Outside-the-cluster setup for the multi-region DR-failover task. Run by Terraform
# (null_resource.setup) after both GKE clusters, the Cloud SQL pair, and the global LB
# exist. It:
#   1. merges BOTH clusters' credentials into the kubeconfig as stable contexts
#      `east` (primary) and `west` (standby);
#   2. deploys the storefront app (frontend + backend) to both regions;
#   3. leaves the standby (west) region MISSING app-config/app-secret  -> the config
#      drift the agent reconciles after failover;
#   4. injects the regional outage by DELETING the primary (east) node pool, so its
#      workloads cannot be scheduled and the global endpoint (URL map default -> east)
#      serves 5xx. There is no node pool to scale back, so the outage cannot be undone
#      by re-applying manifests -- the only path to restore users is to fail traffic
#      over to the healthy west region;
#   5. seeds the GitOps bare repo with the app's desired state (incl. app-config/secret);
#   6. writes a standalone west-only kubeconfig at $WEST_KUBECONFIG, so the harness's
#      verifiers can read the standby -- the harness itself only ever credentials the
#      one cluster the `cluster_name` output names, which is east.
#
# Nothing left in either cluster describes the outage or the fix.
set -euo pipefail

: "${PROJECT_ID:?}" "${NAMESPACE:?}"
: "${EAST_CLUSTER:?}" "${EAST_ZONE:?}" "${WEST_CLUSTER:?}" "${WEST_ZONE:?}"
: "${EAST_IP:?}" "${WEST_IP:?}" "${LB_IP:?}"
: "${REPO_PATH:?}" "${MANIFESTS_DIR:?}"
: "${SQL_PRIMARY:?}" "${SQL_REPLICA:?}"
: "${WEST_KUBECONFIG:?}"

REPO_PATH="${REPO_PATH/#\~/$HOME}"
MANIFESTS_DIR="$(cd "$MANIFESTS_DIR" && pwd)"

# Generate app-config carrying the cross-region Cloud SQL coordinates, so the app's
# dependency on a primary + cross-region read replica is discoverable from the desired
# state (a thorough agent can then verify replication health before cutting over).
# Generated rather than static because the instance names carry the per-run suffix.
GEN_DIR="$(mktemp -d)"
WORK=""
trap 'rm -rf "$GEN_DIR" "$WORK"' EXIT
APP_CONFIG_FILE="$GEN_DIR/app-config.yaml"
cat > "$APP_CONFIG_FILE" <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: app-config
  labels:
    app: storefront
data:
  APP_ENV: "production"
  FEATURE_FLAGS: "checkout,search,recommendations"
  CATALOG_REFRESH_SECONDS: "30"
  DB_PRIMARY_INSTANCE: "${SQL_PRIMARY}"
  DB_REPLICA_INSTANCE: "${SQL_REPLICA}"
EOF

echo "==> Fetching credentials for both clusters"
gcloud container clusters get-credentials "$EAST_CLUSTER" --zone "$EAST_ZONE" --project "$PROJECT_ID"
gcloud container clusters get-credentials "$WEST_CLUSTER" --zone "$WEST_ZONE" --project "$PROJECT_ID"

# Rename the auto-generated gke_* contexts to stable names the agent can rely on.
kubectl config delete-context east >/dev/null 2>&1 || true
kubectl config delete-context west >/dev/null 2>&1 || true
kubectl config rename-context "gke_${PROJECT_ID}_${EAST_ZONE}_${EAST_CLUSTER}" east
kubectl config rename-context "gke_${PROJECT_ID}_${WEST_ZONE}_${WEST_CLUSTER}" west

# Write a standalone, west-only kubeconfig for the harness's verifiers.
#
# The harness credentials exactly one cluster -- the stack's `cluster_name`
# output, which is east -- and re-runs `get-credentials` for it after this
# script, so the active context at verification time is always east. The
# verifiers have a `kubeconfig:` field but no `context:` field, so without this
# file nothing in verification_spec can read the standby region, which is where
# a failover actually lands. Written outside $HOME so a run that quarantines
# HOME does not hide it; removed by the destroy-time provisioner in main.tf.
#
# `--minify --flatten` resolves the exec-plugin stanza and drops the east
# entries, leaving a file whose single context is west and whose current-context
# is already set, so `KUBECONFIG=<file> kubectl get ...` needs no --context.
echo "==> Writing west-only kubeconfig for verification to $WEST_KUBECONFIG"
mkdir -p "$(dirname "$WEST_KUBECONFIG")"
rm -f "$WEST_KUBECONFIG"
(umask 077 && kubectl config view --context west --minify --flatten --raw > "$WEST_KUBECONFIG")
KUBECONFIG="$WEST_KUBECONFIG" kubectl config current-context

# ---------------------------------------------------------------------------
# deploy_app <context> <with_config: yes|no>
# ---------------------------------------------------------------------------
deploy_app() {
  local ctx="$1" with_config="$2" ip
  if [[ "$ctx" == "east" ]]; then ip="$EAST_IP"; else ip="$WEST_IP"; fi

  echo "==> [$ctx] creating namespace $NAMESPACE"
  kubectl --context "$ctx" create namespace "$NAMESPACE" \
    --dry-run=client -o yaml | kubectl --context "$ctx" apply -f -

  if [[ "$with_config" == "yes" ]]; then
    echo "==> [$ctx] applying app-config + app-secret"
    kubectl --context "$ctx" -n "$NAMESPACE" apply -f "$APP_CONFIG_FILE"
    kubectl --context "$ctx" -n "$NAMESPACE" apply -f "$MANIFESTS_DIR/app-secret.yaml"
  else
    echo "==> [$ctx] SKIPPING app-config + app-secret (injected config drift)"
  fi

  echo "==> [$ctx] applying backend + frontend"
  kubectl --context "$ctx" -n "$NAMESPACE" apply -f "$MANIFESTS_DIR/backend.yaml"
  kubectl --context "$ctx" -n "$NAMESPACE" apply -f "$MANIFESTS_DIR/frontend.yaml"

  echo "==> [$ctx] exposing frontend on reserved IP $ip"
  cat <<EOF | kubectl --context "$ctx" -n "$NAMESPACE" apply -f -
apiVersion: v1
kind: Service
metadata:
  name: frontend
  labels:
    app: frontend
spec:
  type: LoadBalancer
  loadBalancerIP: ${ip}
  selector:
    app: frontend
  ports:
    - name: http
      port: 80
      targetPort: 80
EOF
}

# West = healthy standby, but WITHOUT the replicated config (the drift).
deploy_app west no
# East = primary; deploy fully first so its LoadBalancer Service binds the static IP.
deploy_app east yes

echo "==> Waiting for the WEST standby to become healthy"
kubectl --context west -n "$NAMESPACE" rollout status deploy/frontend --timeout=180s || true
kubectl --context west -n "$NAMESPACE" rollout status deploy/backend --timeout=180s || true

echo "==> Waiting for the EAST frontend Service to bind its static IP"
for _ in $(seq 1 30); do
  bound="$(kubectl --context east -n "$NAMESPACE" get svc frontend \
    -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"
  [[ "$bound" == "$EAST_IP" ]] && break
  sleep 10
done

# ---------------------------------------------------------------------------
# Inject the regional outage: DELETE the primary region's only node pool. All east
# workloads become unschedulable, so the global endpoint (which defaults to the east
# backend) returns 5xx. Unlike scaling to 0, there is no node pool to "resize back",
# so in-place repair is not available — the correct recovery is to fail traffic over to
# the healthy west region. (The cluster control plane stays up, so kubectl/credentials
# to east still work for diagnosis.)
# ---------------------------------------------------------------------------
echo "==> Injecting outage: deleting EAST node pool (region capacity loss)"
gcloud container node-pools delete primary-node-pool \
  --cluster "$EAST_CLUSTER" --zone "$EAST_ZONE" --project "$PROJECT_ID" --quiet

# ---------------------------------------------------------------------------
# Seed the GitOps source of truth with the app's DESIRED state (both clusters should
# look like this), including app-config/app-secret that west is currently missing.
# ---------------------------------------------------------------------------
echo "==> Seeding GitOps repo at $REPO_PATH"
rm -rf "$REPO_PATH"
git init --bare "$REPO_PATH" >/dev/null
git -C "$REPO_PATH" symbolic-ref HEAD refs/heads/main

WORK="$(mktemp -d)"
git -C "$WORK" init -q
git -C "$WORK" config user.email "setup@devops-bench.local"
git -C "$WORK" config user.name "devops-bench setup"
mkdir -p "$WORK/manifests"
cp "$APP_CONFIG_FILE" "$WORK/manifests/app-config.yaml"
cp "$MANIFESTS_DIR/app-secret.yaml" "$MANIFESTS_DIR/backend.yaml" \
   "$MANIFESTS_DIR/frontend.yaml" "$WORK/manifests/"
cat > "$WORK/README.md" <<EOF
# storefront

Kubernetes manifests for the storefront service (namespace \`${NAMESPACE}\`).
EOF
git -C "$WORK" add -A
git -C "$WORK" -c init.defaultBranch=main commit -q -m "storefront desired state"
git -C "$WORK" branch -M main
git -C "$WORK" push -q "$REPO_PATH" main

echo "==> Setup complete."
echo "    Global endpoint : http://${LB_IP}/   (currently 5xx — primary region down)"
echo "    Contexts        : east (primary, node pool deleted), west (standby, healthy)"
echo "    GitOps repo     : $REPO_PATH"

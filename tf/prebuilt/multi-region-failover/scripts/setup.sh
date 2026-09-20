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

# Run by null_resource.setup after both clusters, the Cloud SQL pair and the
# global load balancer exist. Deploys storefront to both regions, leaves the
# standby without app-config and app-secret, deletes the primary node pool,
# seeds the GitOps repo, and writes a west-only kubeconfig for the verifiers.
set -euo pipefail

: "${PROJECT_ID:?}" "${NAMESPACE:?}"
: "${EAST_CLUSTER:?}" "${EAST_ZONE:?}" "${WEST_CLUSTER:?}" "${WEST_ZONE:?}"
: "${EAST_IP:?}" "${WEST_IP:?}" "${LB_IP:?}"
: "${REPO_PATH:?}" "${MANIFESTS_DIR:?}"
: "${SQL_PRIMARY:?}" "${SQL_REPLICA:?}"
: "${WEST_KUBECONFIG:?}"

REPO_PATH="${REPO_PATH/#\~/$HOME}"
MANIFESTS_DIR="$(cd "$MANIFESTS_DIR" && pwd)"

# Generated rather than static because the Cloud SQL instance names carry the
# per-run suffix.
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

# Stable context names the agent can rely on.
kubectl config delete-context east >/dev/null 2>&1 || true
kubectl config delete-context west >/dev/null 2>&1 || true
kubectl config rename-context "gke_${PROJECT_ID}_${EAST_ZONE}_${EAST_CLUSTER}" east
kubectl config rename-context "gke_${PROJECT_ID}_${WEST_ZONE}_${WEST_CLUSTER}" west

# The harness credentials only east and re-runs get-credentials after this
# script, and verifiers have a kubeconfig: field but no context: field, so the
# standby is only reachable through a file of its own. --minify --flatten
# resolves the exec plugin and leaves west as the single, current context.
echo "==> Writing west-only kubeconfig for verification to $WEST_KUBECONFIG"
mkdir -p "$(dirname "$WEST_KUBECONFIG")"
rm -f "$WEST_KUBECONFIG"
(umask 077 && kubectl config view --context west --minify --flatten --raw > "$WEST_KUBECONFIG")
KUBECONFIG="$WEST_KUBECONFIG" kubectl config current-context

# deploy_app <context> <with_config: yes|no>
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

# Fail before the outage injection if a region's frontend Service never
# binds its reserved IP; the endpoints must be reachable for the task to hold.
wait_for_service_ip() {
  local ctx="$1" want="$2" bound=""
  echo "==> Waiting for the ${ctx^^} frontend Service to bind ${want}"
  for _ in $(seq 1 30); do
    bound="$(kubectl --context "$ctx" -n "$NAMESPACE" get svc frontend \
      -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)"
    [[ "$bound" == "$want" ]] && return 0
    sleep 10
  done
  echo "${ctx^^} frontend Service did not bind ${want} (got '${bound:-none}')" >&2
  exit 1
}

# The standby is deployed without the replicated config; that is the drift.
deploy_app west no
deploy_app east yes

echo "==> Waiting for the WEST standby to become healthy"
kubectl --context west -n "$NAMESPACE" rollout status deploy/frontend --timeout=180s
kubectl --context west -n "$NAMESPACE" rollout status deploy/backend --timeout=180s

echo "==> Waiting for the EAST primary to become healthy"
kubectl --context east -n "$NAMESPACE" rollout status deploy/frontend --timeout=180s
kubectl --context east -n "$NAMESPACE" rollout status deploy/backend --timeout=180s

wait_for_service_ip west "$WEST_IP"
wait_for_service_ip east "$EAST_IP"

# Deleting the node pool rather than scaling it to zero leaves nothing to
# resize back. The control plane stays up, so kubectl against east still works.
echo "==> Injecting outage: deleting EAST node pool (region capacity loss)"
gcloud container node-pools delete primary-node-pool \
  --cluster "$EAST_CLUSTER" --zone "$EAST_ZONE" --project "$PROJECT_ID" --quiet

# The node-pool delete returns before the load-balancer path converges; the
# agent must start after the outage is visible.
echo "==> Waiting for the global endpoint to return 5xx"
status=""
for _ in $(seq 1 30); do
  status="$(curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 15 "http://${LB_IP}/" || true)"
  [[ "$status" =~ ^5[0-9]{2}$ ]] && break
  sleep 10
done
if [[ ! "$status" =~ ^5[0-9]{2}$ ]]; then
  echo "Global endpoint did not return 5xx after the outage (last status: ${status:-none})" >&2
  exit 1
fi

# The desired state for both clusters, including the objects west is missing.
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

# The agent may run as a different uid than the provisioner; make the repo
# readable and its parents traversable so the prompt's path resolves.
chmod -R a+rX "$REPO_PATH"
chmod a+x "$(dirname "$REPO_PATH")"

echo "==> Setup complete."
echo "    Global endpoint : http://${LB_IP}/   (currently 5xx, primary region down)"
echo "    Contexts        : east (primary, node pool deleted), west (standby, healthy)"
echo "    GitOps repo     : $REPO_PATH"

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
# Labels and taints the Spot pool, deploys the fleet onto the on-demand worker
# and waits for it to be Available. Runs on the host during `tofu apply`.
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
MANIFESTS_DIR="${MANIFESTS_DIR:?MANIFESTS_DIR is required}"
MANIFESTS_DIR="$(cd "${MANIFESTS_DIR}" && pwd)"

echo "==> Designating node pools (spot nodes tainted + labeled)..."
# The first sorted worker is the on-demand node; the verifiers rely on this order.
mapfile -t WORKERS < <(
  kubectl get nodes -l '!node-role.kubernetes.io/control-plane' \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | sort
)
if [ "${#WORKERS[@]}" -lt 2 ]; then
  echo "ERROR: expected >=2 worker nodes, found ${#WORKERS[@]}" >&2
  exit 1
fi
ON_DEMAND="${WORKERS[0]}"
SPOT_NODES=("${WORKERS[@]:1}")

kubectl label node "${ON_DEMAND}" \
  cloud.google.com/gke-nodepool=on-demand-pool node-tier=on-demand --overwrite

# Distinct mock instance families so replicas can be spread across Spot nodes.
families=(c3 n2 e2)
idx=0
for n in "${SPOT_NODES[@]}"; do
  kubectl label node "${n}" \
    cloud.google.com/gke-spot=true \
    cloud.google.com/gke-nodepool=spot-pool \
    spot-instance-family="${families[$((idx % ${#families[@]}))]}" --overwrite
  kubectl taint node "${n}" cloud.google.com/gke-spot=true:NoSchedule --overwrite
  idx=$((idx + 1))
done
echo "    on-demand: ${ON_DEMAND}"
echo "    spot:      ${SPOT_NODES[*]}"

echo "==> Deploying the workload fleet (starts entirely on on-demand)..."
kubectl apply -f "${MANIFESTS_DIR}/workloads/"

echo "==> Waiting for the fleet to become Available..."
kubectl -n apps wait --for=condition=Available deploy --all --timeout=300s

echo "==> Setup complete."
echo "    Inspect placement:  kubectl -n apps get pods -o wide"
echo "    Node pools:         kubectl get nodes -L cloud.google.com/gke-spot,cloud.google.com/gke-nodepool"

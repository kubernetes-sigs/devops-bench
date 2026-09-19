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
# Setup for the spot-rebalancing task. Runs from OUTSIDE the cluster during
# `tofu apply`, before the agent starts:
#   1. designates node pools on the multi-node kind cluster: one worker stays
#      "on-demand" (untainted); the rest become "spot" — labeled
#      'cloud.google.com/gke-spot=true' (the real GKE Spot label) and tainted
#      'cloud.google.com/gke-spot=true:NoSchedule' so only Spot-tolerant pods
#      land there, mirroring a reserved Spot node pool,
#   2. deploys the workload fleet (a mix of critical/stateful and
#      fault-tolerant/batch services). Because the Spot nodes are tainted and no
#      workload tolerates them yet, everything starts on the on-demand node — the
#      costly "before" state the agent must optimize,
#   3. waits for the fleet to become Available so the agent starts healthy.
#
# The node taints/labels need kubectl (the kind provider can't express per-node
# taints declaratively); the rightsizing report is delivered declaratively by a
# local_file resource in main.tf, not here.
#
# Nothing here tells the agent which workloads to move — it must read each
# workload's metadata (labels/annotations) and the report and decide itself.
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
MANIFESTS_DIR="${MANIFESTS_DIR:?MANIFESTS_DIR is required}"
MANIFESTS_DIR="$(cd "${MANIFESTS_DIR}" && pwd)"

echo "==> Designating node pools (spot nodes tainted + labeled)..."
# Worker nodes only (control-plane is tainted by kind on multi-node clusters).
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

# Give the Spot nodes distinct mock instance families so a careful plan can
# spread replicas across them to reduce the blast radius of a simultaneous
# preemption.
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
# Start the agent from a healthy fleet so any downtime during rebalancing is the
# agent's doing, not a flaky fixture.
kubectl -n apps wait --for=condition=Available deploy --all --timeout=300s

echo "==> Setup complete."
echo "    Inspect placement:  kubectl -n apps get pods -o wide"
echo "    Node pools:         kubectl get nodes -L cloud.google.com/gke-spot,cloud.google.com/gke-nodepool"

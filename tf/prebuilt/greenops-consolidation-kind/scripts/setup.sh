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

# Setup for the greenops-consolidation task. Runs from OUTSIDE the cluster during
# `tofu apply`, before the agent starts:
#   1. labels the four worker nodes with a machine family, two of each. The two
#      that sort first are 'n2d-standard-4' (the efficient gen4 family), the two
#      that sort last are 'n1-standard-4' (the power-hungry gen1 family). This is
#      the ONLY place in the cluster the two pools differ, and it is the join key
#      for the delivered carbon feed's per-family power figures,
#   2. waits for every worker to be Ready, then deploys a lightly-loaded fleet
#      across the cluster. The workloads carry soft hostname topology-spread
#      constraints so every worker ends up carrying a little load — the
#      underutilized, energy-wasteful "before" state the agent must consolidate,
#   3. waits for every Deployment to finish rolling out so the agent starts from a
#      complete fleet, then asserts the spread actually landed.
#
# The kubectl work isn't expressible as plan-time-safe declarative TF (kind has no
# cluster at plan time); the carbon report is delivered declaratively by a
# local_file resource in main.tf, not here.
#
# Nothing here tells the agent which nodes to drain, how far to consolidate, or
# that draining is the mechanism at all. It must read the feed, join it to the
# node labels, inspect the workloads' scheduling constraints, and decide itself.
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
MANIFESTS_DIR="${MANIFESTS_DIR:?MANIFESTS_DIR is required}"
MANIFESTS_DIR="$(cd "${MANIFESTS_DIR}" && pwd)"

# kind names multi-node workers '<cluster>-worker', '-worker2', '-worker3',
# '-worker4', which sort in that order — so this mapping is deterministic and the
# task's verification_spec can name the high-draw pair directly. If the stack ever
# grows a fifth worker, the spec's node names must move with it.
echo "==> Labelling the worker nodes with their machine family..."
mapfile -t WORKERS < <(
  kubectl get nodes -l '!node-role.kubernetes.io/control-plane' \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | sort
)
if [[ "${#WORKERS[@]}" -ne 4 ]]; then
  echo "ERROR: expected 4 worker nodes, found ${#WORKERS[@]}: ${WORKERS[*]}" >&2
  exit 1
fi
kubectl label node "${WORKERS[0]}" "${WORKERS[1]}" \
  node.kubernetes.io/instance-type=n2d-standard-4 --overwrite
kubectl label node "${WORKERS[2]}" "${WORKERS[3]}" \
  node.kubernetes.io/instance-type=n1-standard-4 --overwrite

# The last worker also carries the ledger's local storage. The ledger Deployment
# selects on this label rather than on a node name, which keeps the manifest free
# of the run-scoped cluster name while still landing deterministically. Putting it
# on a HIGH-DRAW node is the point: it makes the carbon-optimal move (retire both
# n1 workers) unreachable, because the ledger's PodDisruptionBudget allows zero
# disruptions and its nodeSelector gives it nowhere else to go.
echo "==> Labelling the ledger's storage node..."
kubectl label node "${WORKERS[3]}" storage-tier=local-ssd --overwrite

# Every worker must be schedulable BEFORE the fleet is applied. `kind_cluster`
# returns once the API server answers, but workers can still be NotReady while
# their CNI settles — and a pod placed while a node is NotReady is never
# rebalanced afterwards, because the topology-spread constraints the workloads
# carry are `ScheduleAnyway` (soft). Without this gate the fleet piles onto
# whichever workers happened to be Ready first, which is how a bring-up produced
# a worker carrying zero fleet pods and tripped the assertion below.
echo "==> Waiting for every worker to be Ready before scheduling the fleet..."
kubectl wait --for=condition=Ready node --all --timeout=300s

echo "==> Deploying the workload fleet across the worker nodes..."
kubectl apply -f "${MANIFESTS_DIR}/workloads/"

echo "==> Waiting for the fleet to finish rolling out..."
# Start the agent from a healthy fleet so any unavailability during consolidation
# is the agent's doing, not a flaky fixture.
#
# `--for=condition=Available` is too weak here: with the default RollingUpdate
# maxUnavailable of 25%, a 4-replica Deployment is Available at 3 replicas. This
# task is scored on per-node utilization, so a fleet that is one pod short is a
# materially different "before" state. `rollout status` blocks until every
# declared replica is actually up.
mapfile -t DEPLOYS < <(
  kubectl -n workloads get deploy \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}'
)
if [[ "${#DEPLOYS[@]}" -eq 0 ]]; then
  echo "ERROR: no Deployments found in the 'workloads' namespace after apply." >&2
  exit 1
fi
for deploy in "${DEPLOYS[@]}"; do
  kubectl -n workloads rollout status "deploy/${deploy}" --timeout=300s
done

# The "before" state must actually be the underutilized one the prompt describes.
# The spread is a scheduling PREFERENCE, so assert the outcome rather than trusting
# it: a fixture that piled the whole fleet onto one or two workers is a materially
# different (and easier, or misleading) task, and it must not reach an agent
# silently.
echo "==> Asserting every worker carries load..."
for node in "${WORKERS[@]}"; do
  count="$(
    kubectl -n workloads get pods --field-selector "spec.nodeName=${node}" \
      -l fleet=consolidation --no-headers 2>/dev/null | wc -l
  )"
  echo "    ${node}: ${count} fleet pod(s)"
  if [[ "${count}" -eq 0 ]]; then
    echo "ERROR: worker '${node}' carries no fleet pods; the underutilized" >&2
    echo "       'before' state did not materialize. Refusing to hand the agent" >&2
    echo "       a fixture that does not match the task premise." >&2
    kubectl -n workloads get pods -o wide >&2
    exit 1
  fi
done

# The whole difficulty of the task rests on the ledger sitting on a high-draw node
# and being un-evictable there. If it landed anywhere else the fixture is a
# different, easier task — the agent could then retire both n1 workers cleanly and
# the judgement call disappears. Assert it rather than trust the nodeSelector.
echo "==> Asserting the ledger is pinned to the expected high-draw worker..."
LEDGER_NODE="$(
  kubectl -n workloads get pods -l app=ledger \
    -o jsonpath='{.items[0].spec.nodeName}' 2>/dev/null || true
)"
if [[ "${LEDGER_NODE}" != "${WORKERS[3]}" ]]; then
  echo "ERROR: the ledger pod is on '${LEDGER_NODE:-<unscheduled>}', expected" >&2
  echo "       '${WORKERS[3]}'. The un-drainable-node premise did not materialize," >&2
  echo "       so this fixture is not the task it claims to be. Refusing." >&2
  kubectl -n workloads get pods -l app=ledger -o wide >&2
  kubectl get nodes -L node.kubernetes.io/instance-type -L storage-tier >&2
  exit 1
fi
echo "    ledger pinned to ${LEDGER_NODE}"

# A budget that permits a disruption is a budget that does not block the drain.
ALLOWED="$(
  kubectl -n workloads get pdb ledger \
    -o jsonpath='{.status.disruptionsAllowed}' 2>/dev/null || true
)"
if [[ "${ALLOWED}" != "0" ]]; then
  echo "ERROR: the ledger PodDisruptionBudget reports disruptionsAllowed=" >&2
  echo "       '${ALLOWED:-<unset>}', expected 0. The node the ledger sits on would" >&2
  echo "       drain cleanly and the task's central constraint would not exist." >&2
  kubectl -n workloads get pdb -o wide >&2
  exit 1
fi
echo "    ledger PDB allows 0 disruptions"

echo "==> Setup complete."
echo "    Node pools:    kubectl get nodes -L node.kubernetes.io/instance-type"
echo "    Pod placement: kubectl -n workloads get pods -o wide"

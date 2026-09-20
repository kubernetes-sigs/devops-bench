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

# Applies the world, waits for its healthy workloads, then seeds the faults:
# billing-sync-config's replicas is set to 0 and billing-sync-worker is left to
# enforce it onto notifications-sync-web, then notifications-sync-indexer's
# memory request is inflated while web is down. Runs during tofu apply.
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
INFRA_PROVIDER="${INFRA_PROVIDER:-kind}"

if [[ "${INFRA_PROVIDER}" == "gcp" ]]; then
  echo "==> Fetching GKE credentials for cluster ${CLUSTER_NAME:?} in project ${PROJECT_ID:?} (${LOCATION:?})"
  gcloud container clusters get-credentials "${CLUSTER_NAME}" --zone "${LOCATION}" --project "${PROJECT_ID}"
fi

MANIFESTS_DIR="${MANIFESTS_DIR:?MANIFESTS_DIR is required}"
MANIFESTS_DIR="$(cd "${MANIFESTS_DIR}" && pwd)"
WORLD_JSON="${WORLD_JSON:?WORLD_JSON is required}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"

readonly TARGET_NS="notifications-sync"
readonly TARGET_DEPLOY="notifications-sync-web"
readonly ACTOR_NS="billing-sync"
readonly ACTOR_DEPLOY="billing-sync-worker"
readonly ACTOR_CM="billing-sync-config"
readonly CORRECT_REPLICAS="3"
readonly BROKEN_REPLICAS="0"

readonly INDEXER_DEPLOY="notifications-sync-indexer"
readonly INDEXER_FAT_MEM="40Mi"
readonly INDEXER_FAT_LIMIT_MEM="80Mi"
readonly DEPLOY_RUNNER="deploy-runner"

_ERRFILE="$(mktemp)"; trap 'rm -f "$_ERRFILE"' EXIT
guarded_read(){ local __v="$1"; shift; local __out __rc=0; __out="$("$@" 2>"$_ERRFILE")" || __rc=$?; if [ "$__rc" -ne 0 ] && grep -qE 'error parsing jsonpath|invalid array index|unable to parse|unrecognized|unknown flag|unknown command' "$_ERRFILE"; then echo "CHECK BUG: malformed kubectl query ($*): $(cat "$_ERRFILE")" >&2; exit 1; fi; printf -v "$__v" '%s' "$__out"; }

echo "==> Applying the generated world (20 namespaces) plus the hand-authored billing-sync namespace, and notifications-sync's hand-authored indexer/deploy-runner additions, from ${MANIFESTS_DIR}..."
kubectl apply -f "${MANIFESTS_DIR}/"

echo "==> Waiting for every intentionally-healthy workload in the generated world to reach Ready (list read from ${WORLD_JSON})..."
while IFS='|' read -r _kind _name _ns; do
  [ -z "${_kind}" ] && continue
  _kind_lc="$(printf '%s' "${_kind}" | tr '[:upper:]' '[:lower:]')"
  echo "    waiting for ${_kind}/${_name} -n ${_ns}..."
  kubectl rollout status "${_kind_lc}/${_name}" -n "${_ns}" --timeout="${WAIT_TIMEOUT}s" >/dev/null
done < <(jq -r '.namespaces[].healthy_workloads[] | "\(.kind)|\(.name)|\(.namespace)"' "${WORLD_JSON}")

echo "==> Waiting for the hand-authored actor ${ACTOR_DEPLOY} -n ${ACTOR_NS} to reach Ready (not in world.json, waited explicitly)..."
kubectl rollout status "deployment/${ACTOR_DEPLOY}" -n "${ACTOR_NS}" --timeout="${WAIT_TIMEOUT}s" >/dev/null

echo "==> Waiting for the hand-authored ${INDEXER_DEPLOY} -n ${TARGET_NS} to reach Ready at its manifest values (not in world.json, waited explicitly)..."
kubectl rollout status "deployment/${INDEXER_DEPLOY}" -n "${TARGET_NS}" --timeout="${WAIT_TIMEOUT}s" >/dev/null

echo "==> Waiting for the hand-authored ${DEPLOY_RUNNER} -n ${TARGET_NS} to reach Ready (not in world.json, waited explicitly)..."
kubectl rollout status "deployment/${DEPLOY_RUNNER}" -n "${TARGET_NS}" --timeout="${WAIT_TIMEOUT}s" >/dev/null

echo "==> Applying the seeded fault: corrupting ${ACTOR_CM} -n ${ACTOR_NS} replicas ${CORRECT_REPLICAS} -> ${BROKEN_REPLICAS}..."
kubectl patch configmap "${ACTOR_CM}" -n "${ACTOR_NS}" --type=merge \
  -p "{\"data\":{\"replicas\":\"${BROKEN_REPLICAS}\"}}"

echo "==> Confirming ${ACTOR_CM} -n ${ACTOR_NS} now holds replicas=${BROKEN_REPLICAS}..."
guarded_read _cm_replicas kubectl get configmap "${ACTOR_CM}" -n "${ACTOR_NS}" -o jsonpath='{.data.replicas}'
if [ "${_cm_replicas}" != "${BROKEN_REPLICAS}" ]; then
  echo "SEED FAIL: workload-restored seed check found data.replicas='${_cm_replicas}' on ${ACTOR_CM} -n ${ACTOR_NS}, expected ${BROKEN_REPLICAS}"
  exit 1
fi

echo "==> Waiting for ${ACTOR_DEPLOY}'s own watch loop (poll cadence ~20s) to enforce the corrupted value onto ${TARGET_DEPLOY} -n ${TARGET_NS} (up to ${WAIT_TIMEOUT}s)..."
echo "    setup.sh never issues a scale command itself; only the in-cluster actor does."
_deadline=$((SECONDS+WAIT_TIMEOUT))
_enforced=0
while :; do
  guarded_read _spec_replicas kubectl get deployment "${TARGET_DEPLOY}" -n "${TARGET_NS}" -o jsonpath='{.spec.replicas}'
  if [ "${_spec_replicas}" = "${BROKEN_REPLICAS}" ]; then
    _enforced=1
    break
  fi
  if (( SECONDS >= _deadline )); then
    break
  fi
  sleep 5
done
if [ "${_enforced}" -ne 1 ]; then
  echo "SEED FAIL: workload-restored seed check: ${ACTOR_DEPLOY} did not scale ${TARGET_DEPLOY} -n ${TARGET_NS} down to ${BROKEN_REPLICAS} replicas within ${WAIT_TIMEOUT}s (the actor's own watch loop may not have polled yet, or is not running)"
  exit 1
fi

echo "==> Waiting for ${TARGET_DEPLOY} -n ${TARGET_NS} to have 0 ready replicas (the symptom is now observable)..."
_deadline=$((SECONDS+WAIT_TIMEOUT))
_symptom_ok=0
while :; do
  guarded_read _ready kubectl get deployment "${TARGET_DEPLOY}" -n "${TARGET_NS}" -o jsonpath='{.status.readyReplicas}'
  if [ -z "${_ready}" ] || [ "${_ready}" = "0" ]; then
    _symptom_ok=1
    break
  fi
  if (( SECONDS >= _deadline )); then
    break
  fi
  sleep 5
done
if [ "${_symptom_ok}" -ne 1 ]; then
  echo "SEED FAIL: workload-restored seed check: ${TARGET_DEPLOY} -n ${TARGET_NS} still reports readyReplicas='${_ready}' after ${WAIT_TIMEOUT}s, expected 0"
  exit 1
fi

echo "==> Phase 2: inflating ${INDEXER_DEPLOY} -n ${TARGET_NS} requests.memory to ${INDEXER_FAT_MEM} and limits.memory to ${INDEXER_FAT_LIMIT_MEM} in lockstep (seeds the latent second fault; ${TARGET_DEPLOY} is still at 0 replicas the entire time, so this produces no symptom -- the limits change is only to satisfy the requests <= limits admission invariant and plays no role in the quota arithmetic)..."
kubectl patch deployment "${INDEXER_DEPLOY}" -n "${TARGET_NS}" --type=json \
  -p "[{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/resources/requests/memory\",\"value\":\"${INDEXER_FAT_MEM}\"},{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/resources/limits/memory\",\"value\":\"${INDEXER_FAT_LIMIT_MEM}\"}]"

echo "==> Phase 2: waiting for ${INDEXER_DEPLOY} -n ${TARGET_NS}'s rollout to complete (up to ${WAIT_TIMEOUT}s), so the fat pods are actually admitted and quota usage is locked in..."
kubectl rollout status "deployment/${INDEXER_DEPLOY}" -n "${TARGET_NS}" --timeout="${WAIT_TIMEOUT}s" >/dev/null

echo "==> Confirming ${INDEXER_DEPLOY} -n ${TARGET_NS} now requests ${INDEXER_FAT_MEM} memory per pod and limits it to ${INDEXER_FAT_LIMIT_MEM}..."
guarded_read _indexer_mem kubectl get deployment "${INDEXER_DEPLOY}" -n "${TARGET_NS}" -o jsonpath='{.spec.template.spec.containers[0].resources.requests.memory}'
if [ "${_indexer_mem}" != "${INDEXER_FAT_MEM}" ]; then
  echo "SEED FAIL: phase 2 indexer-inflation check found requests.memory='${_indexer_mem}' on ${INDEXER_DEPLOY} -n ${TARGET_NS}, expected ${INDEXER_FAT_MEM}"
  exit 1
fi
guarded_read _indexer_limit_mem kubectl get deployment "${INDEXER_DEPLOY}" -n "${TARGET_NS}" -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'
if [ "${_indexer_limit_mem}" != "${INDEXER_FAT_LIMIT_MEM}" ]; then
  echo "SEED FAIL: phase 2 indexer-inflation check found limits.memory='${_indexer_limit_mem}' on ${INDEXER_DEPLOY} -n ${TARGET_NS}, expected ${INDEXER_FAT_LIMIT_MEM}"
  exit 1
fi

echo "==> Setup complete."
echo "    Seeded fault: ${ACTOR_CM} -n ${ACTOR_NS} data.replicas=${BROKEN_REPLICAS} (was ${CORRECT_REPLICAS}); ${ACTOR_DEPLOY} -n ${ACTOR_NS} already enforced it onto ${TARGET_DEPLOY} -n ${TARGET_NS}, which is now scaled to 0 and has 0 ready replicas."
echo "    Latent second fault: ${INDEXER_DEPLOY} -n ${TARGET_NS} requests.memory is now ${INDEXER_FAT_MEM} per pod (limits.memory ${INDEXER_FAT_LIMIT_MEM}), symptomless while ${TARGET_DEPLOY} is at 0; ${DEPLOY_RUNNER} -n ${TARGET_NS} restarts ${INDEXER_DEPLOY} every ~90s and each restart currently admits cleanly under notifications-sync-compute's 400Mi quota. See task/GRADING.md for the arithmetic showing why fixing ${ACTOR_CM} alone leaves this wedge for the indexer's next restart."
echo "    Nothing in ${TARGET_NS} (no Deployment, CronJob, Job, ConfigMap, or RBAC object there) names or explains ${ACTOR_DEPLOY}; the trail is cluster-scoped instead: ClusterRoleBinding ${ACTOR_DEPLOY} names billing-sync's ServiceAccount as its subject."
echo "    World: 20 generated namespaces (spike-substrate/generate.py --seed 106 --namespaces 20, see ${WORLD_JSON}) plus the hand-authored billing-sync namespace."
echo "    Inspect: kubectl -n ${TARGET_NS} get all,configmap,resourcequota,role,rolebinding; kubectl get clusterrolebinding ${ACTOR_DEPLOY} -o yaml; kubectl -n ${ACTOR_NS} get all,configmap; kubectl -n ${ACTOR_NS} logs deployment/${ACTOR_DEPLOY}; kubectl -n ${TARGET_NS} logs deployment/${DEPLOY_RUNNER}"

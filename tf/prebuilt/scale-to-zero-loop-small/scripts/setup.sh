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

# Applies the world, waits for its healthy workloads, then seeds two faults:
# billing-sync-config's replicas is set to 0 and billing-sync-worker is left to
# enforce it onto notifications-sync-web, then allow-from-search-api's
# namespaceSelector is misspelled. Runs during tofu apply.
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

_ERRFILE="$(mktemp)"; trap 'rm -f "$_ERRFILE"' EXIT
guarded_read(){ local __v="$1"; shift; local __out __rc=0; __out="$("$@" 2>"$_ERRFILE")" || __rc=$?; if [ "$__rc" -ne 0 ] && grep -qE 'error parsing jsonpath|invalid array index|unable to parse|unrecognized|unknown flag|unknown command' "$_ERRFILE"; then echo "CHECK BUG: malformed kubectl query ($*): $(cat "$_ERRFILE")" >&2; exit 1; fi; printf -v "$__v" '%s' "$__out"; }

echo "==> Applying the generated world (20 namespaces) plus the hand-authored billing-sync namespace from ${MANIFESTS_DIR}..."
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

# The allow rules are correct in the manifest; patching one here leaves a
# managedFields trail like a live change would.
readonly NETPOL_NS="notifications-sync"
readonly NETPOL_ALLOW="allow-from-search-api"
readonly NETPOL_CORRECT_SELECTOR="search-api"
readonly NETPOL_BROKEN_SELECTOR="search-apl"

echo "==> Applying the second seeded fault: corrupting ${NETPOL_ALLOW} -n ${NETPOL_NS}'s namespaceSelector value ${NETPOL_CORRECT_SELECTOR} -> ${NETPOL_BROKEN_SELECTOR}..."
kubectl patch networkpolicy "${NETPOL_ALLOW}" -n "${NETPOL_NS}" --type=json \
  -p "[{\"op\":\"replace\",\"path\":\"/spec/ingress/0/from/0/namespaceSelector/matchLabels/kubernetes.io~1metadata.name\",\"value\":\"${NETPOL_BROKEN_SELECTOR}\"}]"

echo "==> Confirming ${NETPOL_ALLOW} -n ${NETPOL_NS} now holds namespaceSelector value ${NETPOL_BROKEN_SELECTOR}..."
guarded_read _netpol_selector kubectl get networkpolicy "${NETPOL_ALLOW}" -n "${NETPOL_NS}" \
  -o jsonpath='{.spec.ingress[0].from[0].namespaceSelector.matchLabels.kubernetes\.io/metadata\.name}'
if [ "${_netpol_selector}" != "${NETPOL_BROKEN_SELECTOR}" ]; then
  echo "SEED FAIL: consumer-path-restored seed check found namespaceSelector value '${_netpol_selector}' on ${NETPOL_ALLOW} -n ${NETPOL_NS}, expected ${NETPOL_BROKEN_SELECTOR}"
  exit 1
fi

echo "==> Setup complete."
echo "    Seeded fault 1: ${ACTOR_CM} -n ${ACTOR_NS} data.replicas=${BROKEN_REPLICAS} (was ${CORRECT_REPLICAS}); ${ACTOR_DEPLOY} -n ${ACTOR_NS} already enforced it onto ${TARGET_DEPLOY} -n ${TARGET_NS}, which is now scaled to 0 and has 0 ready replicas."
echo "    Nothing in ${TARGET_NS} (no Deployment, CronJob, Job, ConfigMap, or RBAC object there) names or explains the actor; the Role/RoleBinding that used to live there is gone. The trail is cluster-scoped instead: ClusterRoleBinding ${ACTOR_DEPLOY} names billing-sync's ServiceAccount as its subject."
echo "    Seeded fault 2: ${NETPOL_ALLOW} -n ${NETPOL_NS}'s namespaceSelector now reads '${NETPOL_BROKEN_SELECTOR}' instead of '${NETPOL_CORRECT_SELECTOR}', so search-api's traffic to ${TARGET_DEPLOY} is dropped by the default-deny policy even though the allow rule object still exists and looks present."
echo "    World: 20 generated namespaces (spike-substrate/generate.py --seed 106 --namespaces 20, see ${WORLD_JSON}) plus the hand-authored billing-sync namespace, plus boot-time NetworkPolicy furniture in ${NETPOL_NS} (default-deny, five scoped allow rules, one same-namespace allow rule)."
echo "    Inspect: kubectl -n ${TARGET_NS} get all,configmap,role,rolebinding (returns nothing persona-related now); kubectl get clusterrolebinding ${ACTOR_DEPLOY} -o yaml; kubectl -n ${ACTOR_NS} get all,configmap; kubectl -n ${ACTOR_NS} logs deployment/${ACTOR_DEPLOY}; kubectl -n ${NETPOL_NS} get networkpolicy; kubectl -n ${NETPOL_NS} get networkpolicy ${NETPOL_ALLOW} -o yaml"

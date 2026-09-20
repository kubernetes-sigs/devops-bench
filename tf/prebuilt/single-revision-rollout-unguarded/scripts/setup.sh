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

# Applies the seed manifests, then the seeded fault, and asserts the seeded
# state holds before the agent starts.
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
INFRA_PROVIDER="${INFRA_PROVIDER:-kind}"

if [[ "${INFRA_PROVIDER}" == "gcp" ]]; then
  echo "==> Fetching GKE credentials for cluster ${CLUSTER_NAME:?} in project ${PROJECT_ID:?} (${LOCATION:?})"
  gcloud container clusters get-credentials "${CLUSTER_NAME}" --zone "${LOCATION}" --project "${PROJECT_ID}"
fi

MANIFESTS_DIR="${MANIFESTS_DIR:?MANIFESTS_DIR is required}"
MANIFESTS_DIR="$(cd "${MANIFESTS_DIR}" && pwd)"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-180}"

_ERRFILE="$(mktemp)"; trap 'rm -f "$_ERRFILE"' EXIT
guarded_read(){ local __v="$1"; shift; local __out __rc=0; __out="$("$@" 2>"$_ERRFILE")" || __rc=$?; if [ "$__rc" -ne 0 ] && grep -qE 'error parsing jsonpath|invalid array index|unable to parse|unrecognized|unknown flag|unknown command' "$_ERRFILE"; then echo "CHECK BUG: malformed kubectl query ($*): $(cat "$_ERRFILE")" >&2; exit 1; fi; printf -v "$__v" '%s' "$__out"; }

echo "==> Applying gating manifests..."
envsubst '${CLUSTER_NAME}' < "${MANIFESTS_DIR}/00-gating.yaml" | kubectl apply -f -

echo "==> Waiting for gating objects to go live..."
_deadline=$((SECONDS+$WAIT_TIMEOUT))
while :; do
  guarded_read _gate kubectl get resourcequota dispatch-quota -n "dispatch" -o jsonpath='{.status.hard}'
  [ -n "$_gate" ] && break
  if (( SECONDS >= _deadline )); then
    echo "SEED FAIL: gating object .status.hard on ResourceQuota/dispatch-quota did not go live within ${WAIT_TIMEOUT}s -- objects it gates could otherwise be applied before the gate is enforced"
    exit 1
  fi
  sleep 3
done

echo "==> Applying workload manifests..."
envsubst '${CLUSTER_NAME}' < "${MANIFESTS_DIR}/10-workloads.yaml" | kubectl apply -f -

_deadline=$((SECONDS+$WAIT_TIMEOUT))
while :; do
  guarded_read val kubectl get deployment courier -n "dispatch" -o jsonpath='{.spec.template.spec.containers[?(@.name == "web")].resources.requests.memory}'
  [ "${val}" = 256Mi ] && break
  if (( SECONDS >= _deadline )); then
    echo "SEED FAIL: resource-request-inflated@courier.wl not holding (resource-request-inflated): timed out after ${WAIT_TIMEOUT}s -- path spec.template.spec.containers[?(@.name == \"web\")].resources.requests[\"memory\"] expected to equal '256Mi'; last observed value was '$val' (empty means the path was absent)"
    exit 1
  fi
  sleep 3
done
_deadline=$((SECONDS+$WAIT_TIMEOUT))
while :; do
  guarded_read val kubectl get deployment courier -n "dispatch" -o jsonpath='{.status.readyReplicas}'
  [ "${val}" = 3 ] && break
  if (( SECONDS >= _deadline )); then
    echo "SEED FAIL: pod-ready@courier.wl not holding (pod-ready): timed out after ${WAIT_TIMEOUT}s -- path status.readyReplicas expected to equal '3'; last observed value was '$val' (empty means the path was absent)"
    exit 1
  fi
  sleep 3
done
_deadline=$((SECONDS+$WAIT_TIMEOUT))
while :; do
  guarded_read val kubectl get resourcequota dispatch-quota -n "dispatch" -o jsonpath='{.status.used.requests\.memory}'
  [ "${val}" = 896Mi ] && break
  if (( SECONDS >= _deadline )); then
    echo "SEED FAIL: quota-headroom-exhausted@dispatch-quota.quota not holding (quota-headroom-exhausted): timed out after ${WAIT_TIMEOUT}s -- path status.used[\"requests.memory\"] expected to equal '896Mi'; last observed value was '$val' (empty means the path was absent)"
    exit 1
  fi
  sleep 3
done

echo "==> Waiting for hold-mode rows and maintain-kind objectives to settle..."
_deadline=$((SECONDS+$WAIT_TIMEOUT))
while :; do
  guarded_read val kubectl get resourcequota dispatch-quota -n "dispatch" -o jsonpath='{.spec.hard.requests\.memory}'
  [ "${val}" = 896Mi ] && break
  if (( SECONDS >= _deadline )); then
    echo "SETTLE FAIL: quota-cap-held@dispatch-quota.quota did not reach its t0-true state within ${WAIT_TIMEOUT}s"
    exit 1
  fi
  sleep 3
done
_deadline=$((SECONDS+$WAIT_TIMEOUT))
while :; do
  guarded_read val kubectl get deployment courier -n "dispatch" -o jsonpath='{.status.readyReplicas}'
  [ -n "$val" ] && [ "$val" -ge 2 ] && break
  if (( SECONDS >= _deadline )); then
    echo "SETTLE FAIL: ready-floor-held@deployment did not reach its t0-true state within ${WAIT_TIMEOUT}s"
    exit 1
  fi
  sleep 3
done

echo "==> Setup complete."
echo "    Seeded: single-revision-rollout-unguarded in dispatch."
echo "    Inspect: kubectl -n dispatch get all"

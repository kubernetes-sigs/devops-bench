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

# Runs during tofu apply, before the agent starts. Applies the seed manifests,
# gating objects first, then asserts every seeded condition holds.
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

echo "==> Applying workload manifests..."
envsubst '${CLUSTER_NAME}' < "${MANIFESTS_DIR}/10-workloads.yaml" | kubectl apply -f -

# A seed that cannot prove its conditions hold at t0 has failed before the
# run starts.
_deadline=$((SECONDS+$WAIT_TIMEOUT))
while :; do
  guarded_read val kubectl get deployment shelfview -n "storefront" -o jsonpath='{.spec.replicas}'
  [ "${val}" = 0 ] && break
  if (( SECONDS >= _deadline )); then
    echo "SEED FAIL: workload-replicas-set@shelfview.wl not holding (workload-replicas-set): timed out after ${WAIT_TIMEOUT}s -- path spec.replicas expected to equal '0'; last observed value was '$val' (empty means the path was absent)"
    exit 1
  fi
  sleep 3
done
_deadline=$((SECONDS+$WAIT_TIMEOUT))
while :; do
  guarded_read val kubectl get cronjob price-refresh -n "storefront" -o jsonpath='{.spec.suspend}'
  [ "${val}" = true ] && break
  if (( SECONDS >= _deadline )); then
    echo "SEED FAIL: cronjob-suspend-set@price-refresh.cj not holding (cronjob-suspend-set): timed out after ${WAIT_TIMEOUT}s -- path spec.suspend expected to equal 'true'; last observed value was '$val' (empty means the path was absent)"
    exit 1
  fi
  sleep 3
done
_deadline=$((SECONDS+$WAIT_TIMEOUT))
while :; do
  guarded_read val kubectl get deployment search-api -n "storefront" -o jsonpath='{.spec.template.spec.containers[?(@.name == "web")].readinessProbe.httpGet.port}'
  [ "${val}" = 9099 ] && break
  if (( SECONDS >= _deadline )); then
    echo "SEED FAIL: readiness-probe-port-set@search-api.wl not holding (readiness-probe-port-set): timed out after ${WAIT_TIMEOUT}s -- path spec.template.spec.containers[?(@.name == \"web\")].readinessProbe.httpGet.port expected to equal '9099'; last observed value was '$val' (empty means the path was absent)"
    exit 1
  fi
  sleep 3
done
_deadline=$((SECONDS+$WAIT_TIMEOUT))
while :; do
  guarded_read val kubectl get job -n "storefront" -l "app=price-refresh" -o jsonpath='{.items[*].status.succeeded}'
  { _hit=0; for _t in $val; do [ -n "$_t" ] && [ "$_t" -ge 1 ] && { _hit=1; break; }; done; [ "$_hit" -eq 0 ]; } && break
  if (( SECONDS >= _deadline )); then
    echo "SEED FAIL: jobs-none-succeeded@price-refresh.cj not holding (jobs-none-succeeded): timed out after ${WAIT_TIMEOUT}s -- path status.succeeded expected no matched item to satisfy op 'gte' '1'; last observed value was '$val' (empty means the path was absent)"
    exit 1
  fi
  sleep 3
done

echo "==> Waiting for hold-mode rows and maintain-kind objectives to settle..."
_deadline=$((SECONDS+$WAIT_TIMEOUT))
while :; do
  guarded_read val kubectl get deployment storelookup -n "storefront" -o jsonpath='{.status.readyReplicas}'
  [ -n "$val" ] && [ "$val" -ge 3 ] && break
  if (( SECONDS >= _deadline )); then
    echo "SETTLE FAIL: ready-floor-held@deployment did not reach its t0-true state within ${WAIT_TIMEOUT}s"
    exit 1
  fi
  sleep 3
done
_deadline=$((SECONDS+$WAIT_TIMEOUT))
while :; do
  guarded_read val kubectl get deployment aislefeed -n "storefront" -o jsonpath='{.status.readyReplicas}'
  [ -n "$val" ] && [ "$val" -ge 3 ] && break
  if (( SECONDS >= _deadline )); then
    echo "SETTLE FAIL: ready-floor-held@deployment did not reach its t0-true state within ${WAIT_TIMEOUT}s"
    exit 1
  fi
  sleep 3
done
_deadline=$((SECONDS+$WAIT_TIMEOUT))
while :; do
  guarded_read val kubectl get service shelfview -n "storefront" -o jsonpath='{.spec.selector}'; val_json=$(if [ -z "${val}" ]; then echo ""; else echo "${val}" | python3 -c 'import sys, json; d=json.load(sys.stdin); print(json.dumps(d, sort_keys=True, separators=(",", ":")))' 2>/dev/null || echo ""; fi)
  [ "${val_json}" = '{"app":"shelfview"}' ] && break
  if (( SECONDS >= _deadline )); then
    echo "SETTLE FAIL: service-selector-set@shelfview.svc did not reach its t0-true state within ${WAIT_TIMEOUT}s"
    exit 1
  fi
  sleep 3
done

echo "==> Setup complete."
echo "    Seeded: incomplete-maintenance in storefront."
echo "    Inspect: kubectl -n storefront get all"

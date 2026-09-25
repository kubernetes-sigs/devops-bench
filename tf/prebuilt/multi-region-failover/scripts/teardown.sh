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

# Destroy-time companion to setup.sh. The frontend Services make the GKE
# cloud controller create NLB resources outside the Terraform state; while a
# forwarding rule holds a reserved IP, the google_compute_address destroy is
# rejected as in-use. Delete the Services so the controller cleans up, wait
# for the IPs to release, then sweep what's left. Lookups filter on this
# run's reserved IPs, so a concurrent run's resources are never selected.
set -uo pipefail # deliberately no -e: each step is best-effort

: "${PROJECT_ID:?}" "${NAMESPACE:?}"
: "${EAST_CLUSTER:?}" "${EAST_ZONE:?}" "${WEST_CLUSTER:?}" "${WEST_ZONE:?}"
: "${EAST_IP:?}" "${WEST_IP:?}"

# The agent owns the ambient kubeconfig; re-credential into a scratch file.
SCRATCH_KUBECONFIG="$(mktemp)"
trap 'rm -f "$SCRATCH_KUBECONFIG"' EXIT
export KUBECONFIG="$SCRATCH_KUBECONFIG"

delete_frontend_svc() {
  local cluster="$1" zone="$2"
  echo "==> [teardown] deleting frontend Service on ${cluster}"
  if gcloud container clusters get-credentials "$cluster" --zone "$zone" \
    --project "$PROJECT_ID" >/dev/null 2>&1; then
    kubectl --context "gke_${PROJECT_ID}_${zone}_${cluster}" -n "$NAMESPACE" \
      delete svc frontend --ignore-not-found --timeout=90s || true
  else
    echo "==> [teardown] ${cluster} unreachable; relying on the sweep below"
  fi
}

delete_frontend_svc "$EAST_CLUSTER" "$EAST_ZONE"
delete_frontend_svc "$WEST_CLUSTER" "$WEST_ZONE"

list_rules() {
  gcloud compute forwarding-rules list --project "$PROJECT_ID" \
    --filter="IPAddress=(${EAST_IP} ${WEST_IP})" \
    --format="$1" 2>/dev/null
}

# The controller releases the reserved IPs only once its NLB teardown finishes.
echo "==> [teardown] waiting for forwarding rules on ${EAST_IP} / ${WEST_IP} to clear"
remaining=""
for _ in $(seq 1 18); do
  # Only a successful empty lookup proves the IPs are released; a failed one
  # must keep polling, not skip the sweep.
  if remaining="$(list_rules 'value(name)')" && [[ -z "$remaining" ]]; then
    echo "==> [teardown] reserved IPs released"
    exit 0
  fi
  sleep 10
done

# The controller names the target pool and k8s-fw firewall rules after the
# forwarding rule, so the rule name selects its companions.
echo "==> [teardown] sweeping leftover NLB resources: ${remaining}"
while IFS=, read -r name region; do
  [[ -z "$name" ]] && continue
  gcloud compute forwarding-rules delete "$name" --region "$region" \
    --project "$PROJECT_ID" --quiet || true
  gcloud compute target-pools delete "$name" --region "$region" \
    --project "$PROJECT_ID" --quiet 2>/dev/null || true
  while read -r fw; do
    [[ -z "$fw" ]] && continue
    gcloud compute firewall-rules delete "$fw" --project "$PROJECT_ID" --quiet || true
  done < <(gcloud compute firewall-rules list --project "$PROJECT_ID" \
    --filter="name~^k8s-fw-${name}(-deny)?$" --format='value(name)' 2>/dev/null || true)
done < <(list_rules 'csv[no-heading](name,region.basename())')

if ! final="$(list_rules 'value(name)')" || [[ -n "$final" ]]; then
  echo "==> [teardown] WARNING: forwarding rules may remain; the address destroy may fail" >&2
fi
exit 0

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
# Runs during tofu apply: stages an etcd snapshot and its sha256 in the backup
# volume, then corrupts one member's database so it crash-loops with quorum kept.
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:?CLUSTER_NAME is required}"
NAMESPACE="${NAMESPACE:?NAMESPACE is required}"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

ETCD_CERTS=(
  --endpoints=https://127.0.0.1:2379
  --cacert=/etc/kubernetes/pki/etcd/ca.crt
  --cert=/etc/kubernetes/pki/etcd/server.crt
  --key=/etc/kubernetes/pki/etcd/server.key
)

echo "==> Waiting for all nodes to be Ready..."
kubectl wait --for=condition=Ready nodes --all --timeout=180s

# metadata.uid and creationTimestamp are server-assigned and do not survive a
# delete and recreate, so the baseline distinguishes an in-place fix from a
# redeploy of the namespace.
echo "==> Recording pre-run identity baselines for workload-1/workload-2..."
UID_KEY="devops-bench.io/original-uid"
CREATED_KEY="devops-bench.io/original-creation-timestamp"
for name in workload-1 workload-2; do
  uid="$(kubectl -n "${NAMESPACE}" get deployment "${name}" -o jsonpath='{.metadata.uid}')"
  created="$(kubectl -n "${NAMESPACE}" get deployment "${name}" -o jsonpath='{.metadata.creationTimestamp}')"
  kubectl -n "${NAMESPACE}" annotate deployment "${name}" --overwrite \
    "${UID_KEY}=${uid}" \
    "${CREATED_KEY}=${created}"
done

# kind names the docker containers after the Kubernetes node names.
mapfile -t CP_NODES < <(kubectl get nodes \
  -l node-role.kubernetes.io/control-plane \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
WORKER_NODE="$(kubectl get nodes \
  -l '!node-role.kubernetes.io/control-plane' \
  -o jsonpath='{.items[0].metadata.name}')"

if [ "${#CP_NODES[@]}" -lt 3 ]; then
  echo "ERROR: expected >=3 control-plane nodes, found ${#CP_NODES[@]}" >&2
  exit 1
fi

SNAP_NODE="${CP_NODES[0]}"            # healthy member we snapshot from
TARGET_NODE="${CP_NODES[$(( ${#CP_NODES[@]} - 1 ))]}"  # member we corrupt (minority)
echo "    control-plane nodes: ${CP_NODES[*]}"
echo "    snapshot source:     ${SNAP_NODE}"
echo "    corruption target:   ${TARGET_NODE}"
echo "    worker (backup host): ${WORKER_NODE}"

echo "==> Waiting for etcd members to be running..."
kubectl -n kube-system wait --for=condition=Ready "pod/etcd-${SNAP_NODE}" --timeout=120s

echo "==> Taking a verified etcd snapshot from ${SNAP_NODE}..."
# Written into the etcd data dir, a hostPath, so the file is reachable from
# the node container.
kubectl -n kube-system exec "etcd-${SNAP_NODE}" -- \
  etcdctl "${ETCD_CERTS[@]}" snapshot save /var/lib/etcd/etcd-backup.db

echo "==> Computing checksum and staging the backup onto ${WORKER_NODE}:/backup ..."
# Per-run dir: a fixed /tmp path would have concurrent runs staging over each other.
STAGING_DIR="$(mktemp -d)"
trap 'rm -rf "${STAGING_DIR}"' EXIT
docker exec "${SNAP_NODE}" sha256sum /var/lib/etcd/etcd-backup.db | awk '{print $1}' > "${STAGING_DIR}/etcd-backup.sha256"
docker cp "${SNAP_NODE}:/var/lib/etcd/etcd-backup.db" "${STAGING_DIR}/etcd-backup.db"
docker exec "${WORKER_NODE}" mkdir -p /backup
docker cp "${STAGING_DIR}/etcd-backup.db" "${WORKER_NODE}:/backup/etcd-backup.db"
docker cp "${STAGING_DIR}/etcd-backup.sha256" "${WORKER_NODE}:/backup/etcd-backup.sha256"

docker exec "${SNAP_NODE}" rm -f /var/lib/etcd/etcd-backup.db
echo "    backup staged: /backup/etcd-backup.db (+ .sha256)"

echo "==> Corrupting the etcd member on ${TARGET_NODE} (minority; quorum preserved)..."
# Overwrite the bbolt database pages so etcd cannot reopen the store.
docker exec "${TARGET_NODE}" sh -c \
  'dd if=/dev/urandom of=/var/lib/etcd/member/snap/db bs=1M count=2 conv=notrunc'
# Restart the static pod so the kubelet re-reads the corrupted data. The
# corruption only manifests when etcd reopens the store. A missing etcd
# container is a hard failure, but the kill's own exit code is advisory:
# on a loaded host crictl rm -f can report DeadlineExceeded while the
# container still dies moments later (observed live), and the health poll
# below is the authoritative assert either way.
set +e
docker exec "${TARGET_NODE}" sh -c \
  'ids="$(crictl ps -a -q --name etcd)"; [ -n "$ids" ] || exit 42; crictl rm -f $ids'
kill_rc=$?
set -e
if [ "${kill_rc}" -eq 42 ]; then
  echo "ERROR: no etcd container found on ${TARGET_NODE}" >&2
  exit 1
elif [ "${kill_rc}" -ne 0 ]; then
  echo "    crictl kill exited ${kill_rc}; the health assert below decides"
fi

echo "==> Verifying the fault took hold (expect a stable 2 healthy members)..."
# One 2-healthy reading only proves the kill: during the kubelet restart gap the
# count reads 2 even when the corruption failed and the member is about to rejoin.
healthy=0
stable=0
for _ in $(seq 1 36); do
  healthy="$(kubectl -n kube-system exec "etcd-${SNAP_NODE}" -- \
    etcdctl "${ETCD_CERTS[@]}" endpoint health --cluster 2>&1 \
    | grep -c 'is healthy' || true)"
  if [ "${healthy}" -eq 2 ]; then
    stable=$((stable + 1))
    [ "${stable}" -ge 3 ] && break
  else
    stable=0
  fi
  sleep 5
done
if [ "${stable}" -lt 3 ]; then
  echo "ERROR: fault injection did not take hold: ${healthy} healthy members (expected a stable 2)" >&2
  exit 1
fi

echo "==> Fault injection complete."
echo "    One etcd member on ${TARGET_NODE} is now corrupted; the cluster should"
echo "    remain reachable via the surviving quorum (${CP_NODES[0]}, ${CP_NODES[1]})."

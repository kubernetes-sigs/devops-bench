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
# Push the LOCAL working tree (including uncommitted/unpushed changes) to the
# bastion, so the harness on the VM runs exactly the code you have locally.
#
# Only the subset the harness needs is shipped (python + task + infra + docker
# entrypoint); .git, virtualenvs, tofu state, caches and results are excluded.
# It tars the subset, scps the single archive over IAP, and extracts it into
# ~/devops-bench on the VM.
#
# Usage (from anywhere in the repo):
#   scripts/bastion/sync-to-bastion.sh
#
# Env overrides:
#   BASTION_VM       VM name        (default: bench-bastion)
#   BASTION_ZONE     VM zone        (default: us-central1-a)
#   BASTION_PROJECT  cloud project    (default: gcloud's active project)
#   REMOTE_DIR       dir on the VM  (default: ~/devops-bench)
set -euo pipefail

BASTION_VM="${BASTION_VM:-bench-bastion}"
BASTION_ZONE="${BASTION_ZONE:-us-central1-a}"
BASTION_PROJECT="${BASTION_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REMOTE_DIR="${REMOTE_DIR:-devops-bench}" # relative to the SSH user's $HOME

if [ -z "${BASTION_PROJECT}" ]; then
  echo "ERROR: no project. Set BASTION_PROJECT or run 'gcloud config set project <id>'." >&2
  exit 1
fi

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "${REPO_ROOT}"

# The subset the harness needs on the VM. Missing paths are skipped silently.
PATHS=(
  devops_bench
  pkg               # legacy arm (pkg/evaluator/evaluate.py etc.) for comparison runs
  deployers         # legacy top-level deployers used by the legacy arm
  skills            # judge/agent skill markdowns (legacy metrics read skills/*.md)
  pyproject.toml
  README.md         # required by `pip install .` (pyproject readme = README.md)
  LICENSE
  tasks
  tf
  scripts
  docker
)
PRESENT=()
for p in "${PATHS[@]}"; do
  [ -e "${p}" ] && PRESENT+=("${p}")
done

ARCHIVE="$(mktemp -t bench-sync-XXXXXX).tgz"
trap 'rm -f "${ARCHIVE}"' EXIT

# Upload into the login user's home, not the shared /tmp: a fixed /tmp name lets
# any other local user on the VM pre-create the path (or symlink it elsewhere)
# and swap what gets extracted. Per-invocation suffix so two concurrent syncs
# do not collide.
REMOTE_ARCHIVE=".bench-sync-$$-$(date +%s).tgz"

echo "==> packing $(printf '%s ' "${PRESENT[@]}")"
# COPYFILE_DISABLE=1 stops macOS bsdtar from emitting AppleDouble (``._*``) entries
# that extract as junk files on Linux and break manifest globs (e.g. kubectl
# parsing ``._policy.yaml``). Harmless on Linux hosts.
# NOTE: do NOT add `--exclude='results'` — the eval-output `results/` dir lives at
# the repo root and is already excluded by not being in the synced path allowlist
# (PATHS). A bare `results` pattern matches ANY path component, so it also strips
# the `devops_bench/results/` SOURCE module (the rows.json/manifest.json builder),
# which silently disables leaderboard-row generation on the bastion.
COPYFILE_DISABLE=1 tar \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='__pycache__' \
  --exclude='.terraform' \
  --exclude='*.tfstate' \
  --exclude='*.tfstate.*' \
  --exclude='.pytest_cache' \
  --exclude='.ruff_cache' \
  -czf "${ARCHIVE}" "${PRESENT[@]}"

# Transport selection. Default: gcloud IAP tunnel (works on a standard GCP
# project). Override for an environment where the host is reachable directly
# over SSH, WITHOUT changing the default, by setting:
#   BASTION_SSH_HOST   explicit host to ssh/scp to (raw ssh, no gcloud)
#   BASTION_SSH_USER   login user for that host (defaults to the local user)
upload_archive() { :; }
remote_exec() { :; }
if [ -n "${BASTION_SSH_HOST:-}" ]; then
  SSH_HOST="${BASTION_SSH_HOST}"
  SSH_USER="${BASTION_SSH_USER:-$(id -un)}"
  SSH_TARGET="${SSH_USER}@${SSH_HOST}"
  # Bound the connection so a stalled network cannot hang the sync. No BatchMode
  # here (unlike _matrix_lib.sh's detached poller): this step is run by hand and
  # must still be able to prompt for a host key on the first connection.
  _SSH_KA=(-o ServerAliveInterval=30 -o ServerAliveCountMax=4 -o ConnectTimeout=30)
  echo "==> transport: direct ssh to ${SSH_TARGET}"
  upload_archive() { scp "${_SSH_KA[@]}" "${ARCHIVE}" "${SSH_TARGET}:${REMOTE_ARCHIVE}"; }
  remote_exec() { ssh "${_SSH_KA[@]}" "${SSH_TARGET}" "$1"; }
else
  echo "==> transport: gcloud compute ssh over IAP"
  upload_archive() {
    gcloud compute scp --tunnel-through-iap --zone "${BASTION_ZONE}" \
      --project "${BASTION_PROJECT}" "${ARCHIVE}" "${BASTION_VM}:${REMOTE_ARCHIVE}"
  }
  remote_exec() {
    gcloud compute ssh "${BASTION_VM}" --tunnel-through-iap --zone "${BASTION_ZONE}" \
      --project "${BASTION_PROJECT}" --command "$1"
  }
fi

echo "==> uploading archive to ${BASTION_VM}"
upload_archive

echo "==> extracting into ~/${REMOTE_DIR} on the VM"
remote_exec "set -e; mkdir -p ~/${REMOTE_DIR}; tar --no-xattrs -xzf ~/${REMOTE_ARCHIVE} -C ~/${REMOTE_DIR}; rm -f ~/${REMOTE_ARCHIVE}; echo 'synced to ~/${REMOTE_DIR}'"

echo "==> done. Next: SSH in and run scripts/bastion/vm-setup.sh"

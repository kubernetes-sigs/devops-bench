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
# Per-user setup, run ONCE on the bastion after the first sync-to-bastion.sh.
#
# The system toolchain (tofu, gcloud, kubectl, node, oc) is already installed by
# the VM startup script. This finishes the user-scoped pieces: a Python venv with
# the harness installed, an openclaw API key, and a ~/bench.env template.
#
# Usage (on the VM):
#   ~/devops-bench/scripts/bastion/vm-setup.sh
set -euo pipefail

REPO_DIR="${REPO_DIR:-${HOME}/devops-bench}"
ENV_FILE="${HOME}/bench.env"

if [ ! -f "${REPO_DIR}/pyproject.toml" ]; then
  echo "ERROR: ${REPO_DIR}/pyproject.toml not found. Run sync-to-bastion.sh from your laptop first." >&2
  exit 1
fi

# Wait for the startup-script toolchain in case the VM only just booted.
if [ ! -f /var/lib/bench-bastion-ready ]; then
  echo "==> waiting for VM startup toolchain (/var/lib/bench-bastion-ready)..."
  for _ in $(seq 1 60); do
    [ -f /var/lib/bench-bastion-ready ] && break
    sleep 5
  done
  # The loop falls through on timeout; fail loudly instead of proceeding
  # against a half-provisioned VM (toolchain not yet installed).
  if [ ! -f /var/lib/bench-bastion-ready ]; then
    echo "ERROR: VM startup toolchain not ready after ~5m; aborting." >&2
    exit 1
  fi
fi

cd "${REPO_DIR}"

echo "==> creating venv + installing the harness"
# uv is installed system-wide by the VM startup script. It creates/manages .venv
# from the lockfile, so we don't hand-roll a venv or use pip here. The dev group
# already pulls devops-bench[anthropic,openai], so a plain sync covers every
# provider adapter.
uv sync --frozen
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> openclaw key check"
# The harness does NOT pass an API key to oc; openclaw must hold the agent
# model's key itself. Persist it once with the interactive wizard:
#     openclaw onboard
# or export the provider key (e.g. GEMINI_API_KEY / ANTHROPIC_API_KEY) before
# running the harness. We don't store the key here to keep it off disk in plain
# text beyond openclaw's own config.
if oc models list >/dev/null 2>&1; then
  echo "    oc reachable."
else
  echo "    NOTE: run 'openclaw onboard' to configure the agent model API key."
fi

# Gemini CLI (the 'gemini' agent target for gcli runs). oc is installed
# system-wide by startup.sh; this finishes the other agent CLI. Node's global
# prefix is root-owned, so install with sudo. Idempotent.
echo "==> gemini CLI check"
if command -v gemini >/dev/null 2>&1; then
  echo "    gemini present: $(gemini --version 2>/dev/null | head -1)"
else
  echo "    installing @google/gemini-cli (sudo npm -g)..."
  sudo npm install -g @google/gemini-cli \
    && echo "    gemini installed: $(gemini --version 2>/dev/null | head -1)" \
    || echo "    WARN: gemini CLI install failed; gcli agent runs will not work until it's installed."
fi

# fortio — the load generator the chaos agent shells out to for `generate_load`
# faults (e.g. the optimize-scale load spike). The chaos system instruction tells
# the agent to use the `fortio` binary; without it on PATH the spike is a silent
# no-op (the run can still "pass" via the agent's HPA minReplicas, but the load is
# never actually applied). Install to ~/bin (idempotent).
echo "==> fortio check (chaos load generator)"
if command -v fortio >/dev/null 2>&1; then
  echo "    fortio present: $(fortio version 2>/dev/null | head -1)"
else
  echo "    installing fortio to ~/bin..."
  FORTIO_VERSION="${FORTIO_VERSION:-1.66.4}"
  mkdir -p "${HOME}/bin"
  # Unpack in a private mktemp dir: the shared /tmp is writable by every local
  # user, so a fixed download path can be pre-created and the `find` below could
  # otherwise pick up a planted binary and install it as ~/bin/fortio.
  FORTIO_TMP="$(mktemp -d)"
  if curl -fsSL -o "${FORTIO_TMP}/fortio.tgz" \
       "https://github.com/fortio/fortio/releases/download/v${FORTIO_VERSION}/fortio-linux_amd64-${FORTIO_VERSION}.tgz" \
     && tar -xzf "${FORTIO_TMP}/fortio.tgz" -C "${FORTIO_TMP}" 2>/dev/null \
     && cp "$(find "${FORTIO_TMP}" -maxdepth 4 -name fortio -type f 2>/dev/null | head -1)" "${HOME}/bin/fortio" \
     && chmod +x "${HOME}/bin/fortio"; then
    echo "    fortio installed: $("${HOME}/bin/fortio" version 2>/dev/null | head -1)"
  else
    echo "    WARN: fortio install failed; chaos generate_load faults (optimize-scale) will no-op."
  fi
  rm -rf "${FORTIO_TMP}"
fi

# node on a stable PATH — the oc trajectory extraction (`oc sessions` /
# `export-trajectory`) runs oc as a direct, non-login subprocess, so an
# nvm-managed Node that's only on the *login* PATH isn't found → `oc sessions`
# exits 127 and the trajectory is silently emptied (deflating every score).
# Symlink the nvm Node into ~/bin (already on the runner PATH) so the direct
# subprocess resolves it. Idempotent; no-op when Node is already system-wide.
echo "==> node-on-PATH check (oc trajectory extraction)"
if PATH="${HOME}/bin:${PATH}" command -v node >/dev/null 2>&1; then
  echo "    node resolvable on the runner PATH: $(PATH="${HOME}/bin:${PATH}" command -v node)"
else
  export NVM_DIR="${NVM_DIR:-${HOME}/.nvm}"; [ -s "${NVM_DIR}/nvm.sh" ] && . "${NVM_DIR}/nvm.sh"
  NODE_BIN="$(command -v node 2>/dev/null)"
  if [ -n "${NODE_BIN}" ]; then
    mkdir -p "${HOME}/bin"; ln -sf "${NODE_BIN}" "${HOME}/bin/node"
    echo "    linked ${HOME}/bin/node -> ${NODE_BIN}"
  else
    echo "    WARN: node not found; oc trajectory extraction will exit 127 and empty trajectories."
  fi
fi
# The chaos agent runs `fortio` via run_command in a NON-login shell, whose PATH
# does NOT include ~/bin — so symlink fortio into /usr/local/bin (which IS on the
# default PATH). Without this the optimize-scale load spike silently no-ops even
# though fortio is installed. Idempotent.
if [ -x "${HOME}/bin/fortio" ] && [ ! -e /usr/local/bin/fortio ]; then
  echo "==> symlinking fortio into /usr/local/bin (non-login PATH)"
  sudo ln -sf "${HOME}/bin/fortio" /usr/local/bin/fortio \
    && echo "    linked: $(command -v fortio)" \
    || echo "    WARN: could not symlink fortio to /usr/local/bin; chaos may not find it."
fi

# MCP server skills — the source for the AGENT's +skills capability. This
# bastion installs GoogleCloudPlatform/gke-mcp; point MCP_SKILLS_REPO at another
# server's checkout to use different skills.
# (oc/gcli). The refactored matrix points AGENT_SKILLS_PATHS at this repo's
# skills/ dir. These are operational skills, NOT the judge rubric markdown under
# ~/oc-skills. Clone to a stable path OUTSIDE the synced ~/devops-bench tree so
# sync-to-bastion never clobbers it. Idempotent.
echo "==> gke-mcp skills check (agent +skills source)"
MCP_SKILLS_REPO="${MCP_SKILLS_REPO:-${HOME}/mcp-skills}"
if [ -d "${MCP_SKILLS_REPO}/skills" ]; then
  echo "    present: ${MCP_SKILLS_REPO}/skills ($(find "${MCP_SKILLS_REPO}/skills" -name SKILL.md 2>/dev/null | wc -l) skills)"
else
  echo "    cloning gke-mcp -> ${MCP_SKILLS_REPO}..."
  if git clone --depth 1 https://github.com/GoogleCloudPlatform/gke-mcp "${MCP_SKILLS_REPO}"; then
    echo "    gke-mcp skills ready ($(find "${MCP_SKILLS_REPO}/skills" -name SKILL.md 2>/dev/null | wc -l) skills)"
  else
    echo "    WARN: gke-mcp clone failed; agent +skills will be empty until it is cloned."
  fi
fi

# Disable Gemini CLI folder-trust gating at the USER level. The gcli agent runs
# in a fresh, untrusted per-run temp cwd; untrusted folders have their MCP
# servers (e.g. gke-mcp) suppressed, and a workspace-level setting can't lift it
# (untrusted folders ignore their own settings.json) — nor does the --skip-trust
# flag. Setting it here lets gke-mcp connect for every run. Merge-preserving +
# idempotent.
echo "==> gemini folder-trust (~/.gemini/settings.json: security.folderTrust.enabled=false)"
mkdir -p "${HOME}/.gemini"
python3 - "${HOME}/.gemini/settings.json" <<'PY'
import json, sys
path = sys.argv[1]
try:
    with open(path) as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        cfg = {}
except (FileNotFoundError, ValueError):
    cfg = {}
cfg.setdefault("security", {}).setdefault("folderTrust", {})["enabled"] = False
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print(f"    wrote {path}")
PY

if [ ! -f "${ENV_FILE}" ]; then
  echo "==> writing ${ENV_FILE} template (fill in values, then 'source ~/bench.env')"
  # Mode 600 before the write: the template invites provider API keys and
  # JUDGE_API_KEY, which the default umask would leave world-readable.
  install -m 600 /dev/null "${ENV_FILE}"
  cat > "${ENV_FILE}" <<'EOF'
# DevOps Bench harness environment. Fill in, then: source ~/bench.env
# --- Target infrastructure ---
# PROJECT_ID and CLUSTER_NAME are what the harness reads.
export PROJECT_ID=""
export CLUSTER_NAME="bench-cluster"
export NAMESPACE="bench-run-1"
# GCP only. GCP_LOCATION is read by the deployer factory; GCP_PROJECT_ID is read
# by Vertex model auth. Leave both unset when running against another provider.
export GCP_LOCATION="us-central1-a"
export GCP_PROJECT_ID="$PROJECT_ID"

# --- Agent (openclaw / oc) ---
export BENCH_AGENT_TYPE="openclaw"
export AGENT_TARGET="oc"
export AGENT_PROVIDER="google"
export AGENT_MODEL="gemini-3.1-pro-preview"
# Agent model key: prefer 'openclaw onboard'. If your provider reads an env key,
# set it here too (e.g. GEMINI_API_KEY / ANTHROPIC_API_KEY).
# export GEMINI_API_KEY=""

# --- Judge ---
export JUDGE_PROVIDER="google"
export JUDGE_MODEL="gemini-3.1-pro-preview"
export JUDGE_API_KEY=""
EOF
else
  echo "==> ${ENV_FILE} already exists; leaving it untouched"
fi

echo ""
echo "==> setup complete. To run the secret-rotation eval:"
echo "    source ~/bench.env   # after filling in project + keys"
echo "    cd ${REPO_DIR} && source .venv/bin/activate"
echo "    devops-bench tasks/common/opa-remediation/task.yaml"

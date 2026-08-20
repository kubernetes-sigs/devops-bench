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
# Shared library for the bastion eval-matrix orchestrators:
#   run_matrix.sh         (refactored arm: Task x Model x AgentConfig)
#   run_matrix_legacy.sh  (legacy arm:     Task x Model)
#
# Not run directly. A wrapper sources this, sets the MATRIX_* / run config, then
# builds the global COMBOS array (each entry "run_id|task|kvs|arm", where kvs is
# a ';'-joined KEY=VALUE list of per-combo env, e.g.
# "AGENT_MODEL=gemini-3.1-pro;BENCH_AGENT_TYPE=openclaw;...") and calls
# `matrix_dispatch "<label>"`. Each combo runs as an isolated --parallel run on
# the bastion (its own cluster); results are copied back to RESULTS_DIR.
#
# Connection env (same as sync-to-bastion.sh): BASTION_VM/ZONE/PROJECT, and
# either the default IAP tunnel, or direct SSH via BASTION_SSH_HOST / BASTION_SSH_USER.
# Run config: PROJECT_ID (req unless DRY_RUN), CLUSTER_NAME, GCP_LOCATION,
# AGENT_PROVIDER, JUDGE_PROVIDER, JUDGE_MODEL, MAX_PARALLEL, RESULTS_DIR,
# MCP_SERVER_BIN, SKILLS_PATHS, SKIP_SYNC, DRY_RUN, MATRIX_TASKS, MATRIX_MODELS.
# RESUME_STAMP=<stamp>: skip launching; re-poll + pull an existing remote run
#   (use the stamp printed by the original invocation) — survives a dead local
#   process. SSH keepalive + a retrying pull keep brief drops from aborting.
# BENCH_VERTEX=1: run agents + judges against Vertex AI via the bastion VM SA's
#   ADC instead of the API-key endpoints. The runner unsets every API key from
#   secrets.env and exports GOOGLE_GENAI_USE_VERTEXAI/GOOGLE_CLOUD_*/
#   GCP_VERTEX_LOCATION (default location 'global'; override GOOGLE_CLOUD_LOCATION
#   / GCP_VERTEX_LOCATION). For the legacy oc arm also set AGENT_PROVIDER=
#   google-vertex so the model id becomes 'google-vertex/<model>'. Prereq: the oc
#   google-vertex provider must be auth'd once (see docs/components/bastion.md).
# BENCH_REMOTE=1: run the matrix ON the bastion over ssh (sync + remote nohup +
#   pull). Default (unset) runs every combo LOCALLY on this host (no ssh/sync;
#   outputs in ~/matrix-runs/<stamp>, no pull). BASTION_* matter only when set.

BASTION_VM="${BASTION_VM:-bench-bastion}"
BASTION_ZONE="${BASTION_ZONE:-us-central1-a}"
BASTION_PROJECT="${BASTION_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
REMOTE_DIR="${REMOTE_DIR:-devops-bench}"

MATRIX_TASKS="${MATRIX_TASKS:-tasks/common/opa-remediation/task.yaml}"
MATRIX_MODELS="${MATRIX_MODELS:-gemini-3.1-pro}"

PROJECT_ID="${PROJECT_ID:-}"
CLUSTER_NAME="${CLUSTER_NAME:-eval}"
GCP_LOCATION="${GCP_LOCATION:-us-central1-a}"
AGENT_PROVIDER="${AGENT_PROVIDER:-google}"
JUDGE_PROVIDER="${JUDGE_PROVIDER:-google}"
JUDGE_MODEL="${JUDGE_MODEL:-gemini-3.1-pro}"
MAX_PARALLEL="${MAX_PARALLEL:-3}"
# Per-subprocess agent timeout. The 600s harness default is too low for
# infra-bearing tasks (e.g. deploy-hello-app timed out); give matrix runs more
# headroom. Override by exporting AGENT_TIMEOUT_SEC before launch.
AGENT_TIMEOUT_SEC="${AGENT_TIMEOUT_SEC:-1200}"
MCP_SERVER_BIN="${MCP_SERVER_BIN:-/usr/local/bin/gke-mcp}"   # where startup.sh installs it
# Agent +skills source: the MCP server's operational skills (SKILL.md form), cloned
# by vm-setup.sh. NOT ~/oc-skills, which holds the judge rubric markdown (the
# grader's criteria), not operational agent skills. Expanded on the bastion.
SKILLS_PATHS="${SKILLS_PATHS:-\$HOME/mcp-skills/skills}"
DRY_RUN="${DRY_RUN:-}"
BENCH_REMOTE="${BENCH_REMOTE:-}"  # empty = run locally on this host; set = ssh to the bastion

STAMP="$(date +%Y%m%d_%H%M%S)-$$"
# Pulled results land in ${RESULTS_DIR}/${STAMP} (the pull re-creates the
# stamped dir), so the default deliberately omits the stamp.
RESULTS_DIR="${RESULTS_DIR:-results/matrix}"
REMOTE_OUT="matrix-runs/${STAMP}"  # relative to the bastion user's $HOME

_MATRIX_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${_MATRIX_LIB_DIR}/../.." && pwd)"

# --- SSH transport (mirrors sync-to-bastion.sh) ----------------------------- #
# Keepalive so sessions ride out brief network blips instead of dropping.
_SSH_KA=(-o ServerAliveInterval=30 -o ServerAliveCountMax=4 -o ConnectTimeout=30)
if [ -n "${BASTION_SSH_HOST:-}" ]; then
  SSH_HOST="${BASTION_SSH_HOST}"
  SSH_USER="${BASTION_SSH_USER:-$(id -un)}"
  SSH_TARGET="${SSH_USER}@${SSH_HOST}"
  remote_exec() { ssh -o BatchMode=yes "${_SSH_KA[@]}" "${SSH_TARGET}" "$1"; }
  push_file()   { scp -o BatchMode=yes "${_SSH_KA[@]}" "$1" "${SSH_TARGET}:$2"; }
  pull_dir()    { scp -o BatchMode=yes "${_SSH_KA[@]}" -r "${SSH_TARGET}:$1" "$2"; }
else
  _GKA=(--ssh-flag="-o ServerAliveInterval=30" --ssh-flag="-o ServerAliveCountMax=4")
  _GKA_SCP=(--scp-flag="-o ServerAliveInterval=30" --scp-flag="-o ServerAliveCountMax=4")
  remote_exec() { gcloud compute ssh "${BASTION_VM}" --tunnel-through-iap --zone "${BASTION_ZONE}" --project "${BASTION_PROJECT}" "${_GKA[@]}" --command "$1"; }
  push_file()   { gcloud compute scp --tunnel-through-iap --zone "${BASTION_ZONE}" --project "${BASTION_PROJECT}" "${_GKA_SCP[@]}" "$1" "${BASTION_VM}:$2"; }
  pull_dir()    { gcloud compute scp --tunnel-through-iap --recurse --zone "${BASTION_ZONE}" --project "${BASTION_PROJECT}" "${_GKA_SCP[@]}" "${BASTION_VM}:$1" "$2"; }
fi

# Run a check/command on the runner host: locally by default, on the bastion when
# BENCH_REMOTE is set. (The detached matrix runner itself is launched separately.)
host_exec() { if [ -n "${BENCH_REMOTE}" ]; then remote_exec "$1"; else bash -c "$1"; fi; }

# Pull with a few retries — a drop during the final copy is otherwise fatal.
pull_dir_retry() {
  local src="$1" dst="$2" i
  for i in 1 2 3 4 5; do
    if pull_dir "${src}" "${dst}"; then return 0; fi
    echo "    pull attempt ${i} failed; retrying in 15s..." >&2
    sleep 15
  done
  echo "ERROR: could not pull ${src} after retries; results remain on the bastion at ~/${src}" >&2
  return 1
}

sanitize() { echo "$1" | tr '/.+ ' '----' | tr -cd 'A-Za-z0-9_-'; }

# ALL -> enumerate every task.yaml under tasks/; else the list.
resolve_tasks() {
  if [ "${MATRIX_TASKS}" = "ALL" ]; then
    ( cd "${REPO_ROOT}" && find tasks -name task.yaml 2>/dev/null | sort )
  else
    printf '%s\n' ${MATRIX_TASKS}
  fi
}

# Per-task extra env, ';'-prefixed so it appends onto an existing KVS list.
# Some tasks need the harness integration contract (TARGET_DEPLOYMENT_NAME /
# NAMESPACE) pinned so the prompt / chaos service_url / verification placeholders
# match what the task's stack actually deployed. The harness reads these from the
# environment and its defaults DIFFER across arms (refactored namespace "default"
# vs legacy "production"), so they must be set explicitly here, not left to the
# per-arm default. Emits nothing for tasks that don't need it.
task_extra_env() {
  case "$1" in
    */optimize-scale/*) echo ";TARGET_DEPLOYMENT_NAME=scale-target;NAMESPACE=default" ;;
    # Pre-seeded fixtures: pin NAMESPACE so the prompt's {{NAMESPACE}} resolves to
    # the same namespace the stack deploys the fixture into, on BOTH arms (the
    # harness default differs: refactored "default" vs legacy "production"). The
    # value matches each stack's own namespace default.
    */multi-region-failover/*)      echo ";NAMESPACE=storefront" ;;
    */secret-rotation/*)            echo ";NAMESPACE=secret-rotation" ;;
    */cp-recovery/*)                echo ";NAMESPACE=cp-recovery" ;;
    */troubleshoot-unhealthy-pod/*) echo ";NAMESPACE=default" ;;
    */gitops-auto-revert/*)         echo ";NAMESPACE=default" ;;
    # No stack fixture (agent-created), but pin so legacy doesn't target a
    # non-existent "production" namespace.
    */deploy-postgres-web-app/*)    echo ";NAMESPACE=default" ;;
    */debug-crashloop/*)            echo ";NAMESPACE=default" ;;
  esac
}

# Poll until the remote .done marker appears. Resilient: a failed SSH check is
# read as "not finished yet" and retried next tick, so brief drops don't abort
# (the run itself is detached via nohup and unaffected). Arg: expected combo count.
_poll_until_done() {
  local expected="$1" done_n waited=0
  local max_wait="${MATRIX_POLL_TIMEOUT_SEC:-86400}"
  echo "==> waiting for ${expected} run(s) (poll 60s; runs continue if this exits)"
  while true; do
    if host_exec "test -f \$HOME/${REMOTE_OUT}/.done" 2>/dev/null; then break; fi
    done_n="$(host_exec "ls \$HOME/${REMOTE_OUT}/*/status 2>/dev/null | wc -l" 2>/dev/null | tr -d '[:space:]' || echo 0)"
    # The runner only writes .done after `wait`, so if it dies (reboot, OOM,
    # manual kill) the marker never lands. Every combo having a status file
    # means the work is finished either way, so stop rather than block forever.
    if [ "${done_n}" -ge "${expected}" ]; then
      echo "    all ${expected} combos have a status file but no .done marker; the runner likely died"
      break
    fi
    if [ "${waited}" -ge "${max_wait}" ]; then
      echo "    giving up after ${waited}s (MATRIX_POLL_TIMEOUT_SEC); pulling whatever finished" >&2
      return 124
    fi
    echo "    ${done_n}/${expected} finished... ($(date +%H:%M:%S))"
    sleep 60
    waited=$((waited + 60))
  done
}

# Pull results (with retry) and summarize from the pulled dirs. Reads the local
# <rid> subdirs rather than COMBOS, so it also serves RESUME_STAMP attach.
_pull_and_summarize() {
  mkdir -p "${RESULTS_DIR}"
  local LOCAL_OUT
  if [ -n "${BENCH_REMOTE}" ]; then
    echo "==> pulling results -> ${RESULTS_DIR}/${STAMP}"
    pull_dir_retry "${REMOTE_OUT}" "${RESULTS_DIR}" || return 1
    LOCAL_OUT="${RESULTS_DIR}/${STAMP}"
  else
    LOCAL_OUT="$HOME/${REMOTE_OUT}"
    echo "==> local results at ${LOCAL_OUT}"
  fi

  echo "==> summary"
  printf '%-56s %-8s %s\n' "COMBO" "EXIT" "results.json"
  local d rid st rj
  for d in "${LOCAL_OUT}"/*/; do
    [ -d "$d" ] || continue
    rid="$(basename "$d")"
    st="$(cat "${d%/}/status" 2>/dev/null || echo '?')"
    rj="$(find "${d}" -name results.json 2>/dev/null | head -1)"
    printf '%-56s %-8s %s\n' "${rid}" "${st}" "${rj:-<none>}"
  done
  echo "==> done. results under ${LOCAL_OUT} (each combo provisioned + tore down its own cluster)"
}

# Run the COMBOS matrix. Arg: a human label for logging.
#
# Resume/attach: set RESUME_STAMP=<stamp> (from an earlier run's output) to skip
# launching and just re-poll + pull an existing remote run — for when the local
# process died after the bastion runner was already launched.
matrix_dispatch() {
  local label="$1"

  if [ -n "${RESUME_STAMP:-}" ]; then
    STAMP="${RESUME_STAMP}"
    REMOTE_OUT="matrix-runs/${STAMP}"
    local where; where="localhost"; [ -n "${BENCH_REMOTE}" ] && where="${BASTION_VM}"
    echo "==> RESUME: attaching to existing run ~/${REMOTE_OUT} on ${where}"
    host_exec "test -d \$HOME/${REMOTE_OUT}" 2>/dev/null \
      || { echo "ERROR: no run at ~/${REMOTE_OUT} on ${where}" >&2; exit 2; }
    # run_one mkdirs each combo dir lazily, so counting dirs undercounts a run
    # still in flight; the staged runner has one run_one line per combo.
    local exp
    exp="$(host_exec "grep -c '^run_one ' \$HOME/.matrix-runner-${STAMP}.sh" 2>/dev/null | tr -d '[:space:]')"
    [ "${exp:-0}" -gt 0 ] 2>/dev/null \
      || { echo "ERROR: no staged runner for ${STAMP}; cannot resume" >&2; return 2; }
    local poll_rc=0
    _poll_until_done "${exp}" || poll_rc=$?
    _pull_and_summarize || return $?
    return "${poll_rc}"
  fi

  echo "==> ${label} matrix: ${#COMBOS[@]} combo(s), MAX_PARALLEL=${MAX_PARALLEL}"
  printf '    %s\n' "${COMBOS[@]%%|*}"

  if [ -n "${DRY_RUN}" ]; then
    echo "==> DRY_RUN: per-combo env (not executing):"
    local c rid task kvs arm
    for c in "${COMBOS[@]}"; do
      IFS='|' read -r rid task kvs arm <<<"$c"
      echo "  [${rid}] arm=${arm} task=${task}"
      echo "      ${kvs}"
    done
    echo "==> DRY_RUN: results would land in ${RESULTS_DIR}/${STAMP}"
    return 0
  fi

  [ "${#COMBOS[@]}" -gt 0 ] || { echo "ERROR: empty matrix" >&2; exit 2; }
  [ -n "${PROJECT_ID:-}" ] || { echo "ERROR: set PROJECT_ID" >&2; exit 2; }

  if [ -n "${BENCH_REMOTE}" ] && [ -z "${SKIP_SYNC:-}" ]; then
    echo "==> syncing working tree to ${BASTION_VM}"
    "${REPO_ROOT}/scripts/bastion/sync-to-bastion.sh"
  fi

  local runner; runner="$(mktemp -t matrix-runner-XXXXXX.sh)"
  trap 'rm -f "${runner}"' RETURN
  {
    echo '#!/usr/bin/env bash'
    echo 'set -uo pipefail'
    if [ -n "${BENCH_REMOTE}" ]; then echo "cd ~/${REMOTE_DIR}"; else echo "cd '${REPO_ROOT}'"; fi
    echo '[ -f .venv/bin/activate ] && source .venv/bin/activate || true'
    echo 'set -a; [ -f ~/secrets.env ] && . ~/secrets.env; set +a'
    if [ -n "${BENCH_VERTEX:-}" ]; then
      # Vertex mode: drop every API key secrets.env exported so agents AND judges
      # fall back to ADC (the bastion VM SA via the metadata server), then point
      # everything at Vertex. Location is global — the gemini-3.x *-preview models
      # 404 on regional endpoints (us-central1). The legacy judge defaults to
      # us-central1, so GCP_VERTEX_LOCATION must override it too.
      echo 'unset AGENT_API_KEY GEMINI_API_KEY GOOGLE_API_KEY JUDGE_API_KEY GOOGLE_GENAI_API_KEY'
      # The literal marker tells oc's google-vertex provider "use ADC". Passing it
      # via env (not `oc models auth paste-api-key`) is what makes it PORTABLE
      # across oc's isolated per-run OPENCLAW_STATE_DIRs — a pasted profile lives
      # only in the global agent sqlite store, which parallel runs don't share, so
      # they'd fail with `No API key found for provider "google-vertex"`. The
      # gemini CLI and the google-genai judge ignore it (they pick ADC from
      # GOOGLE_GENAI_USE_VERTEXAI + project/location).
      echo 'export GOOGLE_CLOUD_API_KEY=gcp-vertex-credentials'
      echo "export GOOGLE_GENAI_USE_VERTEXAI=true GOOGLE_CLOUD_PROJECT='${PROJECT_ID}' GOOGLE_CLOUD_LOCATION='${GOOGLE_CLOUD_LOCATION:-global}' GCP_VERTEX_LOCATION='${GCP_VERTEX_LOCATION:-global}'"
      # Vertex model auth reads GCP_PROJECT_ID from the environment directly
      # (models/gemini.py, models/claude.py), with no value passed in.
      echo "export GCP_PROJECT_ID='${PROJECT_ID}'"
    fi
    echo "OUT=\"\$HOME/${REMOTE_OUT}\"; mkdir -p \"\$OUT\""
    # PROJECT_ID / CLUSTER_NAME are what the harness reads (run.py). GCP_LOCATION
    # is additionally exported because the deployer factory resolves it directly.
    echo "export PROJECT_ID='${PROJECT_ID}' CLUSTER_NAME='${CLUSTER_NAME}'"
    echo "export GCP_LOCATION='${GCP_LOCATION}'"
    echo "export AGENT_PROVIDER='${AGENT_PROVIDER}' JUDGE_PROVIDER='${JUDGE_PROVIDER}' JUDGE_MODEL='${JUDGE_MODEL}'"
    echo "export AGENT_TIMEOUT_SEC='${AGENT_TIMEOUT_SEC}'"
    echo "export BENCH_PARALLEL=true"
    echo 'run_one() {'
    echo '  local rid="$1" task="$2" kvs="$3" arm="$4" kv rc rdir'
    echo '  local d="$OUT/$rid"; mkdir -p "$d"'
    echo '  ('
    echo '    export RUN_ID="$rid"'
    echo '    # eval so values like AGENT_MCP_SERVER=$HOME/mcp-server expand on the bastion'
    echo '    IFS=";"; for kv in $kvs; do eval "export ${kv}"; done'
    echo '    if [ "$arm" = "legacy" ]; then'
    echo '      python3 pkg/evaluator/evaluate.py "$task"; rc=$?'
    echo '      # legacy writes results/run_<ts>_<rid>; copy it into the combo dir'
    echo '      rdir="$(ls -dt results/run_*_"$rid" 2>/dev/null | head -1)"'
    echo '      [ -n "$rdir" ] && cp -a "$rdir/." "$d/" 2>/dev/null || true'
    echo '    else'
    echo '      python3 -m devops_bench --parallel --run-id "$rid" \'
    echo '        --project "$PROJECT_ID" --cluster "$CLUSTER_NAME" \'
    echo '        --results-root "$d" "$task"; rc=$?'
    echo '    fi'
    echo '    echo "exit=$rc" >"$d/status"'
    echo '  ) >"$d/run.log" 2>&1'
    echo '}'
    echo "SEM=${MAX_PARALLEL}"
    local c rid task kvs arm
    for c in "${COMBOS[@]}"; do
      IFS='|' read -r rid task kvs arm <<<"$c"
      printf 'run_one %q %q %q %q &\n' "$rid" "$task" "$kvs" "$arm"
      echo 'while [ "$(jobs -r | wc -l)" -ge "$SEM" ]; do wait -n; done'
    done
    echo 'wait'
    echo "echo ALL_DONE >\"\$HOME/${REMOTE_OUT}/.done\""
  } >"${runner}"

  # Staged under $HOME, not /tmp: the runner is executed, and a shared /tmp lets
  # any other local user pre-create the path (or symlink it) and hijack what
  # runs. Per-stamp so two matrices can be launched in parallel without
  # clobbering each other's runner script.
  local staged_runner="\$HOME/.matrix-runner-${STAMP}.sh"
  local local_staged_runner="${HOME}/.matrix-runner-${STAMP}.sh"
  # Create the output dir (and its parent) BEFORE the nohup redirect — the
  # ``>...${REMOTE_OUT}.out`` target dir must exist or the job never starts.
  if [ -n "${BENCH_REMOTE}" ]; then
    echo "==> uploading + launching remote runner (detached)"
    push_file "${runner}" ".matrix-runner-${STAMP}.sh"
    remote_exec "mkdir -p \$HOME/${REMOTE_OUT}; chmod 700 ${staged_runner}; nohup ${staged_runner} >\$HOME/${REMOTE_OUT}.out 2>&1 & echo launched pid=\$!"
  else
    echo "==> launching local runner (detached)"
    install -m 700 "${runner}" "${local_staged_runner}"
    mkdir -p "$HOME/${REMOTE_OUT}"
    nohup "${local_staged_runner}" >"$HOME/${REMOTE_OUT}.out" 2>&1 & echo "launched pid=$!"
  fi
  echo "    (to re-attach if this exits: RESUME_STAMP=${STAMP} re-run the same command)"

  local poll_rc=0
  _poll_until_done "${#COMBOS[@]}" || poll_rc=$?
  _pull_and_summarize || return $?
  return "${poll_rc}"
}

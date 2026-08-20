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
# Parallel eval matrix — REFACTORED arm (python -m devops_bench). Runs locally by
# default; set BENCH_REMOTE=1 to sync + run on the bastion over ssh.
# Dimensions: Task x Model x AgentConfig. Run from your workstation; results are
# copied back locally. (Legacy arm: scripts/bastion/run_matrix_legacy.sh.)
#
# Agent-config presets: "<oc|gcli>[+mcp][+skills]" (oc=openclaw, gcli=gemini).
# The refactored arm wires MCP/skills per-run via env, so every combo is fully
# independent. CUJs:
#
#   1) one task, many models, one config
#      MATRIX_TASKS="tasks/common/opa-remediation/task.yaml" \
#      MATRIX_MODELS="gemini-3.1-pro gemini-3.5-flash" \
#      MATRIX_AGENT_CONFIGS="gcli+mcp+skills" PROJECT_ID=<proj> run_matrix.sh
#
#   2) one task, one model, many configs
#      MATRIX_AGENT_CONFIGS="oc oc+mcp+skills gcli gcli+mcp+skills" ... run_matrix.sh
#
#   3) all tasks, one model, one config
#      MATRIX_TASKS=ALL MATRIX_MODELS="gemini-3.1-pro" MATRIX_AGENT_CONFIGS="oc+mcp+skills" ... run_matrix.sh
#
# DRY_RUN=1 prints the expanded matrix + per-combo env without provisioning.
# See _matrix_lib.sh for the full connection/run-config env and docs/components/bastion.md.
set -euo pipefail

# shellcheck source=scripts/bastion/_matrix_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/_matrix_lib.sh"

MATRIX_AGENT_CONFIGS="${MATRIX_AGENT_CONFIGS:-oc+mcp+skills}"

# Translate an agent-config preset into the refactored arm's env (';'-joined).
# <type> is oc|gcli; +mcp / +skills toggle capabilities.
agent_config_env() {
  local preset="$1" type feat want_mcp=0 want_skills=0 out=()
  type="${preset%%+*}"
  case "$type" in
    oc)   out+=("BENCH_AGENT_TYPE=openclaw" "AGENT_TARGET=oc" "OPENCLAW_BIN=oc" "OPENCLAW_AGENT=main") ;;
    # "gemini" is the registered agent key; "cli" matches neither a registered
    # agent nor the gemini-cli alias, so it fails at agent resolution.
    gcli) out+=("BENCH_AGENT_TYPE=gemini" "AGENT_TARGET=gemini") ;;
    *) echo "ERROR: unknown agent type '${type}' in preset '${preset}'" >&2; return 1 ;;
  esac
  for feat in $(echo "${preset}" | tr '+' ' '); do
    case "$feat" in mcp) want_mcp=1 ;; skills) want_skills=1 ;; esac
  done
  if [ "${want_mcp}" = 1 ]; then out+=("BENCH_USE_MCP=true" "AGENT_MCP_SERVER=${MCP_SERVER_BIN}"); else out+=("BENCH_USE_MCP=false"); fi
  [ "${want_skills}" = 1 ] && out+=("AGENT_SKILLS_PATHS=${SKILLS_PATHS}")
  ( IFS=';'; echo "${out[*]}" )
}

COMBOS=()
while IFS= read -r task; do
  [ -n "${task}" ] || continue
  tname="$(basename "$(dirname "${task}")")"
  for model in ${MATRIX_MODELS}; do
    for preset in ${MATRIX_AGENT_CONFIGS}; do
      cfg="$(agent_config_env "${preset}")" || exit 1
      kvs="AGENT_MODEL=${model};AGENT_PROVIDER=${AGENT_PROVIDER};${cfg}$(task_extra_env "${task}")"
      rid="$(sanitize "${tname}")__$(sanitize "${model}")__$(sanitize "${preset}")"
      COMBOS+=("${rid}|${task}|${kvs}|refactored")
    done
  done
done < <(resolve_tasks)

matrix_dispatch "refactored (Task x Model x AgentConfig)"

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
# Parallel eval matrix — LEGACY arm (pkg/evaluator/evaluate.py). Runs locally by
# default; set BENCH_REMOTE=1 to sync + run on the bastion over ssh.
# Dimensions: Task x Model ONLY (no AgentConfig — the legacy arm reads MCP/skills
# from the GLOBAL ~/.openclaw config, so capabilities are fixed for the whole
# matrix; set them once with scripts/bastion/configure-oc.sh).
#
# This is a thin, throwaway companion to run_matrix.sh (the refactored matrix);
# delete it when the legacy arm is retired — the shared _matrix_lib.sh stays.
#
# OpenClaw-only BY DESIGN, not just by default: the legacy Gemini runner reads
# its trajectory from the shared ~/.gemini/tmp/.../chats dir keyed by a short
# session id, which is NOT safe under concurrent runs. For parallel Gemini use
# the refactored matrix (run_matrix.sh, MATRIX_AGENT_CONFIGS="gcli..."). See
# docs/components/bastion.md "Parallel agent support".
#
# CUJs supported: one task x many models, and all tasks x one model. E.g.:
#   MATRIX_TASKS="tasks/common/opa-remediation/task.yaml" \
#   MATRIX_MODELS="gemini-3.1-pro gemini-3.5-flash" \
#   PROJECT_ID=<proj> run_matrix_legacy.sh
#
#   MATRIX_TASKS=ALL MATRIX_MODELS="gemini-3.1-pro" PROJECT_ID=<proj> run_matrix_legacy.sh
#
# Prereq: run `scripts/bastion/configure-oc.sh --mcp --skills` (or --no-*) once
# to set the global oc config the legacy arm uses. DRY_RUN=1 previews the matrix.
set -euo pipefail

# shellcheck source=scripts/bastion/_matrix_lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/_matrix_lib.sh"

# Legacy openclaw (local) env — fixed; capabilities come from the global oc config.
LEGACY_KVS="BENCH_AGENT_TYPE=cli;AGENT_TARGET=oc;OPENCLAW_BIN=oc;OPENCLAW_LOCAL=true;OPENCLAW_AGENT=main"

COMBOS=()
while IFS= read -r task; do
  [ -n "${task}" ] || continue
  tname="$(basename "$(dirname "${task}")")"
  for model in ${MATRIX_MODELS}; do
    kvs="AGENT_MODEL=${model};AGENT_PROVIDER=${AGENT_PROVIDER};${LEGACY_KVS}$(task_extra_env "${task}")"
    rid="$(sanitize "${tname}")__$(sanitize "${model}")__legacy"
    COMBOS+=("${rid}|${task}|${kvs}|legacy")
  done
done < <(resolve_tasks)

matrix_dispatch "legacy (Task x Model)"

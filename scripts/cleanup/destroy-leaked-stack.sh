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
# Destroy the OpenTofu stack a crashed run left behind.
#
# When a run dies mid-apply the harness never reaches teardown, so the cloud
# resources survive with their state file intact under the run's scratch dir.
# The state is the only complete record of what was created, so destroying
# through it removes strictly more than the name-matched gcloud sweep in the
# cleanup-orphaned-resources skill can find. Run this FIRST; use the skill's
# sweep afterwards to catch whatever the state did not cover.
#
# Three things make this awkward by hand, and are why this exists as a script:
#
#   1. The run's state, kubeconfig and gcloud config live in a per-run scratch
#      dir (RunEnv), not in the repo. tofu must be pointed at all three or it
#      inits a fresh, empty state and reports "no changes".
#   2. Local state lives at <run>/terraform.tfstate -- the PARENT of
#      TF_DATA_DIR, not inside it -- and must be named with -state on every
#      command.
#   3. In-cluster resources (helm releases, kubernetes_* objects) cannot be
#      destroyed once their cluster is gone: the provider blocks trying to
#      reach a dead API server and the destroy never completes. They are
#      dropped from state first so the cloud resources underneath can go.
#
# Usage:
#   scripts/cleanup/destroy-leaked-stack.sh --run-id <id>   [--stack <s>] [--yes]
#   scripts/cleanup/destroy-leaked-stack.sh --run-dir <dir> [--stack <s>] [--yes]
#   scripts/cleanup/destroy-leaked-stack.sh --list
#
# Defaults to a DRY RUN that prints the exact commands it would run. Deletion
# is opt-in via --yes, matching the cleanup-orphaned-resources guardrails.
#
# Options:
#   --run-id <id>    Run id; resolved under the run-state root (see below).
#   --run-dir <dir>  Run scratch dir directly, for a non-default location.
#   --stack <s>      Stack to destroy, e.g. 'prebuilt/opa-remediation' or an
#                    absolute path. Omit to list the candidates in the run.
#   --var k=v        Extra -var passed to destroy; repeatable. Overrides a
#                    value derived from state.
#   --list           List run dirs under the run-state root and exit.
#   --yes            Actually run state rm + destroy. Without it, dry run.
#
# Env overrides:
#   BENCH_RUN_STATE_ROOT  run-state root (default: ${TMPDIR:-/tmp}/devops-bench-runs)
set -euo pipefail

STATE_ROOT="${BENCH_RUN_STATE_ROOT:-${TMPDIR:-/tmp}/devops-bench-runs}"
STATE_ROOT="${STATE_ROOT%/}"

RUN_DIR=""
STACK=""
APPLY=false
EXTRA_VARS=()

die() {
  echo "ERROR: $*" >&2
  exit 1
}

note() { echo "==> $*"; }

usage() {
  # Anchored on content, not line numbers, so editing the header above cannot
  # silently truncate --help.
  sed -n '/^# Destroy the OpenTofu stack/,/^set -euo/p' "$0" \
    | sed '$d' \
    | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --run-id)  [ $# -ge 2 ] || die "--run-id needs a value";  RUN_DIR="${STATE_ROOT}/$2"; shift 2 ;;
    --run-dir) [ $# -ge 2 ] || die "--run-dir needs a value"; RUN_DIR="${2%/}";           shift 2 ;;
    --stack)   [ $# -ge 2 ] || die "--stack needs a value";   STACK="$2";                 shift 2 ;;
    --var)     [ $# -ge 2 ] || die "--var needs a value";     EXTRA_VARS+=("$2");         shift 2 ;;
    --yes)     APPLY=true; shift ;;
    --list)
      note "run dirs under ${STATE_ROOT}"
      if [ -d "${STATE_ROOT}" ]; then
        find "${STATE_ROOT}" -mindepth 1 -maxdepth 1 -type d -print | sort
      else
        echo "    (none: ${STATE_ROOT} does not exist)"
      fi
      exit 0
      ;;
    -h|--help) usage 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

[ -n "${RUN_DIR}" ] || usage 2

command -v tofu >/dev/null 2>&1 || die "tofu not on PATH"
[ -d "${RUN_DIR}" ] || die "run dir not found: ${RUN_DIR} (try --list)"

STATE_FILE="${RUN_DIR}/terraform.tfstate"
if [ ! -f "${STATE_FILE}" ]; then
  die "no state at ${STATE_FILE}. The run never reached apply, or its scratch
       dir was already wiped. Nothing to destroy through tofu -- fall back to
       the gcloud sweep in the cleanup-orphaned-resources skill."
fi

# The three process-global paths RunEnv keyed per run. tofu, gcloud and kubectl
# all read them from the environment, so exporting them here is enough to make
# every command below operate on THIS run rather than the operator's ambient
# state.
export TF_DATA_DIR="${RUN_DIR}/tf-data"
export CLOUDSDK_CONFIG="${RUN_DIR}/gcloud"
export KUBECONFIG="${RUN_DIR}/kubeconfig"

# The stack tofu must run in is the run's private copy of the tf tree, not the
# checkout's: a parallel run copies all of tf/ into <run>/tf and applies from
# there, so module paths and any generated files resolve against that copy.
RUN_TF_ROOT="${RUN_DIR}/tf"
[ -d "${RUN_TF_ROOT}" ] || RUN_TF_ROOT="$(cd "$(dirname "$0")/../../tf" && pwd)"

if [ -z "${STACK}" ]; then
  echo "ERROR: --stack is required. Candidates in ${RUN_TF_ROOT}:" >&2
  find "${RUN_TF_ROOT}/prebuilt" -mindepth 1 -maxdepth 1 -type d 2>/dev/null \
    | sed "s|${RUN_TF_ROOT}/|  |" | sort >&2
  echo "Pick the one the failed run used (it is the task's infra.stack)." >&2
  exit 2
fi

case "${STACK}" in
  /*) STACK_DIR="${STACK}" ;;
  *)  STACK_DIR="${RUN_TF_ROOT}/${STACK}" ;;
esac
[ -d "${STACK_DIR}" ] || die "stack dir not found: ${STACK_DIR}"

note "run dir:    ${RUN_DIR}"
note "state:      ${STATE_FILE}"
note "stack:      ${STACK_DIR}"
note "mode:       $([ "${APPLY}" = true ] && echo 'APPLY (destructive)' || echo 'dry run')"

cd "${STACK_DIR}"

# Re-init in the run's TF_DATA_DIR: the plugin cache and module links the
# original apply wrote may be gone, and every later command needs them.
note "tofu init"
tofu init -input=false >/dev/null

# --- derive the destroy inputs from state -----------------------------------
# destroy still evaluates the configuration, so every variable without a
# default must be supplied. Read what we can out of the state rather than
# making the operator retype values the failed run already chose.

ADDRS="$(tofu state list -state="${STATE_FILE}" || true)"
[ -n "${ADDRS}" ] || die "state at ${STATE_FILE} lists no resources; nothing to destroy"

# Which cluster module is instantiated tells us the provider the run used;
# infra_provider has no default in the stacks, so it cannot be skipped.
INFRA_PROVIDER=""
case "${ADDRS}" in
  *module.cluster.module.gke*)      INFRA_PROVIDER="gcp" ;;
  *module.cluster.module.kind*)     INFRA_PROVIDER="kind" ;;
  *module.cluster.module.vcluster*) INFRA_PROVIDER="vcluster" ;;
esac

# Outputs are recorded in the state file itself, so they are readable without
# evaluating the config (which is what we are still missing variables for).
read_output() {
  python3 -c '
import json, sys
try:
    with open(sys.argv[1]) as fh:
        state = json.load(fh)
except (OSError, ValueError):
    sys.exit(0)
value = (state.get("outputs") or {}).get(sys.argv[2], {}).get("value")
if isinstance(value, str):
    print(value)
' "${STATE_FILE}" "$1"
}

CLUSTER_NAME="$(read_output cluster_name)"
LOCATION="$(read_output cluster_location)"

VAR_FLAGS=()
[ -n "${INFRA_PROVIDER}" ] && VAR_FLAGS+=(-var "infra_provider=${INFRA_PROVIDER}")
[ -n "${CLUSTER_NAME}" ]   && VAR_FLAGS+=(-var "cluster_name=${CLUSTER_NAME}")
[ -n "${LOCATION}" ]       && VAR_FLAGS+=(-var "location=${LOCATION}")
for kv in ${EXTRA_VARS+"${EXTRA_VARS[@]}"}; do
  VAR_FLAGS+=(-var "${kv}")
done

note "derived: infra_provider=${INFRA_PROVIDER:-<unknown>} cluster_name=${CLUSTER_NAME:-<unknown>} location=${LOCATION:-<unset>}"

# Fail before touching anything if a required variable is still unresolved:
# a destroy that prompts under -input=false aborts halfway and leaves the
# state in a worse shape than we found it.
SUPPLIED=()
[ -n "${INFRA_PROVIDER}" ] && SUPPLIED+=(infra_provider)
[ -n "${CLUSTER_NAME}" ]   && SUPPLIED+=(cluster_name)
[ -n "${LOCATION}" ]       && SUPPLIED+=(location)
for kv in ${EXTRA_VARS+"${EXTRA_VARS[@]}"}; do
  SUPPLIED+=("${kv%%=*}")
done

MISSING="$(python3 -c '
import re, sys, pathlib
supplied = set(sys.argv[2:])
missing = []
for path in sorted(pathlib.Path(sys.argv[1]).glob("*.tf")):
    text = path.read_text(encoding="utf-8", errors="replace")
    for match in re.finditer(r"variable\s+\"([^\"]+)\"\s*\{", text):
        name = match.group(1)
        # Scan the block body for a default, tracking brace depth so a nested
        # object default does not end the block early.
        depth, i = 1, match.end()
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        if "default" not in text[match.end():i] and name not in supplied:
            missing.append(name)
print(" ".join(missing))
' "${STACK_DIR}" ${SUPPLIED+"${SUPPLIED[@]}"})"

if [ -n "${MISSING}" ]; then
  die "cannot derive required variable(s) from state: ${MISSING}
       Supply each with --var <name>=<value>."
fi

# --- drop in-cluster resources ----------------------------------------------
# Anything living INSIDE the cluster is already gone with it; leaving it in
# state makes destroy hang on an unreachable API server instead of removing
# the cloud resources that actually still exist and still cost money.
IN_CLUSTER="$(printf '%s\n' "${ADDRS}" | grep -E '(^|\.)(helm_release|kubernetes_[a-z_]+|kubectl_[a-z_]+)\.' || true)"

if [ -n "${IN_CLUSTER}" ]; then
  note "in-cluster resources to drop from state (not deleted -- the cluster is going away):"
  printf '%s\n' "${IN_CLUSTER}" | sed 's/^/    /'
else
  note "no in-cluster resources in state"
fi

DESTROY_CMD=(tofu destroy -auto-approve -input=false -state="${STATE_FILE}" "${VAR_FLAGS[@]}")

if [ "${APPLY}" != true ]; then
  echo
  note "DRY RUN -- nothing was changed. With --yes this would run, in order:"
  printf '%s\n' "${IN_CLUSTER}" | while IFS= read -r addr; do
    [ -n "${addr}" ] && echo "    tofu state rm -state=${STATE_FILE} '${addr}'"
  done
  echo "    ${DESTROY_CMD[*]}"
  echo
  echo "Review the list above, then re-run with --yes." >&2
  exit 0
fi

if [ -n "${IN_CLUSTER}" ]; then
  # One address per invocation: a single rm of several addresses aborts the
  # whole batch if any one of them has already been removed.
  printf '%s\n' "${IN_CLUSTER}" | while IFS= read -r addr; do
    [ -n "${addr}" ] || continue
    note "state rm ${addr}"
    tofu state rm -state="${STATE_FILE}" "${addr}" || \
      echo "    WARN: could not remove ${addr}; continuing" >&2
  done
fi

note "destroy"
"${DESTROY_CMD[@]}"

note "done. Now run the gcloud discovery in the cleanup-orphaned-resources"
note "skill against this run's cluster token to confirm nothing survived."

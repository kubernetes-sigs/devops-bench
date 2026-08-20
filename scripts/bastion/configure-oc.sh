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
# Configure the GLOBAL ~/.openclaw config for a run mode, on the bastion.
#
# Why this is separate from vm-setup.sh: only the LEGACY arm reads the global oc
# config — the refactored arm wires MCP/skills per-run via env
# (AGENT_MCP_SERVER / AGENT_SKILLS_PATHS / BENCH_USE_MCP). Keeping this out of
# the one-time setup means MCP/skills are a per-run-mode choice: run this with
# --mcp/--skills before a legacy "with capabilities" run, or with --no-mcp /
# --no-skills (or just don't run it) for a clean "no capabilities" run.
#
# Idempotent. Also stores the agent model API key in oc (from ~/secrets.env), so
# both arms authenticate without baking the key into oc during provisioning.
#
# Usage:
#   scripts/bastion/configure-oc.sh [--mcp|--no-mcp] [--skills|--no-skills] [--vertex]
#
# --vertex registers oc's built-in ``google-vertex`` provider in the global
# config so the legacy arm can target ``google-vertex/<model>`` via the bastion
# VM SA's ADC (no API keys). It writes the provider's ``api``/``baseUrl``/model
# entries (without ``api: google-vertex`` oc would route the provider through the
# OpenAI transport), allowlists the models for the agent, and pastes the ADC
# marker. At run time the matrix still exports
# GOOGLE_CLOUD_API_KEY=gcp-vertex-credentials (see _matrix_lib.sh BENCH_VERTEX),
# which is what makes auth portable across parallel runs' isolated state dirs.
#
# Env overrides:
#   MCP_SERVER_BIN path to the MCP server binary  (default: the server this
#                  bastion installs, at /usr/local/bin/gke-mcp)
#   SECRETS_ENV    file exporting GEMINI_API_KEY  (default: ~/secrets.env)
#   SKILLS_SRC     dir of skill markdowns         (default: ~/devops-bench/skills)
#   OC_SKILLS_DIR  staging dir for <name>/SKILL.md (default: ~/oc-skills)
#   VERTEX_MODELS  space-separated model ids to register under google-vertex
#                  (default: "gemini-3.1-pro-preview gemini-3-flash-preview
#                            gemini-3.5-flash")
#   GENAI_MODELS   space-separated model ids to register under the google
#                  (google-genai) provider's catalog — only models oc doesn't
#                  ship by default need listing (default: "gemini-3.5-flash")
#   OPENCLAW_AGENT oc agent profile for the auth marker (default: main)
set -euo pipefail

WANT_MCP=1
WANT_SKILLS=1
WANT_VERTEX=0
MCP_SERVER_BIN="${MCP_SERVER_BIN:-/usr/local/bin/gke-mcp}"
SECRETS_ENV="${SECRETS_ENV:-${HOME}/secrets.env}"
SKILLS_SRC="${SKILLS_SRC:-${HOME}/devops-bench/skills}"
OC_SKILLS_DIR="${OC_SKILLS_DIR:-${HOME}/oc-skills}"
# VERTEX_MODELS and GENAI_MODELS differ on purpose: oc ships NO built-in
# google-vertex catalog, so every Vertex model must be registered; for google
# (google-genai) oc already ships most ids, so only the ones it lacks need
# listing (the legacy-arm analogue of the harness's _CATALOG_OVERRIDES). The
# refactored arm self-registers per-run and ignores these lists entirely.
# TODO(deferred): supported-model-name maintenance is tracked separately (#147).
VERTEX_MODELS="${VERTEX_MODELS:-gemini-3.1-pro-preview gemini-3-flash-preview gemini-3.5-flash}"
GENAI_MODELS="${GENAI_MODELS:-gemini-3.5-flash}"
OPENCLAW_AGENT="${OPENCLAW_AGENT:-main}"

while [ $# -gt 0 ]; do
  case "$1" in
    --mcp) WANT_MCP=1 ;;
    --no-mcp) WANT_MCP=0 ;;
    --skills) WANT_SKILLS=1 ;;
    --no-skills) WANT_SKILLS=0 ;;
    --vertex) WANT_VERTEX=1 ;;
    --no-vertex) WANT_VERTEX=0 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

command -v oc >/dev/null 2>&1 || { echo "ERROR: oc not on PATH" >&2; exit 1; }

# --- 1. Agent model API key -> oc auth (idempotent) ------------------------- #
if [ -f "${SECRETS_ENV}" ]; then
  # shellcheck disable=SC1090
  . "${SECRETS_ENV}"
  KEY="${GEMINI_API_KEY:-${GOOGLE_API_KEY:-}}"
  if [ -n "${KEY}" ]; then
    if printf '%s\n' "${KEY}" | oc models auth paste-api-key --provider google >/dev/null; then
      echo "==> oc model auth set (google)"
    else
      echo "==> WARN: 'oc models auth paste-api-key --provider google' failed" >&2
    fi
  else
    echo "==> WARN: no GEMINI_API_KEY/GOOGLE_API_KEY in ${SECRETS_ENV}; skipping oc auth"
  fi
else
  echo "==> WARN: ${SECRETS_ENV} not found; skipping oc auth"
fi

# --- 1b. google-genai catalog overrides ------------------------------------- #
# Register models oc doesn't ship (e.g. gemini-3.5-flash) under the google
# (google-genai) provider so the legacy arm can target `google/<model>`.
# Idempotent; auth flows from the `oc models auth` paste above.
if [ -n "${GENAI_MODELS}" ]; then
  # oc is installed by the VM startup script but only writes its config on first
  # use, so the directory can still be missing when this runs on a fresh bastion.
  mkdir -p "${HOME}/.openclaw"
  OC_CONFIG="${HOME}/.openclaw/openclaw.json" GENAI_MODELS="${GENAI_MODELS}" \
  python3 - <<'PY'
import json, os
path = os.environ["OC_CONFIG"]
models = os.environ["GENAI_MODELS"].split()
try:
    with open(path) as f:
        cfg = json.load(f)
except FileNotFoundError:
    cfg = {}
prov = cfg.setdefault("models", {}).setdefault("providers", {}).setdefault("google", {})
prov["api"] = "google-generative-ai"  # replaces built-in entry; pin transport
prov.setdefault("models", [])
existing = {m.get("id") for m in prov["models"] if isinstance(m, dict)}
for m in models:
    if m not in existing:
        prov["models"].append({"id": m, "name": m})
# Allowlist google/<model> for the agent's per-run --model override.
allow = cfg.setdefault("agents", {}).setdefault("defaults", {}).setdefault("models", {})
for m in models:
    allow.setdefault(f"google/{m}", {})
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print(f"==> google (genai) catalog overrides registered: {', '.join(models)}")
PY
fi

# --- 2. MCP server ---------------------------------------------------------- #
oc mcp unset gke-mcp >/dev/null 2>&1 || true   # idempotent: clear any prior entry
if [ "${WANT_MCP}" = "1" ]; then
  [ -x "${MCP_SERVER_BIN}" ] || { echo "ERROR: MCP server binary not executable at ${MCP_SERVER_BIN}" >&2; exit 1; }
  oc mcp add gke-mcp --command "${MCP_SERVER_BIN}" --no-probe >/dev/null
  echo "==> gke-mcp registered (global oc config)"
else
  echo "==> gke-mcp NOT registered (--no-mcp)"
fi

# --- 3. Skills (reshape *.md -> <name>/SKILL.md, install --global) ---------- #
managed_skills_dir="${HOME}/.openclaw/skills"
if [ "${WANT_SKILLS}" = "1" ]; then
  [ -d "${SKILLS_SRC}" ] || { echo "ERROR: skills source ${SKILLS_SRC} not found" >&2; exit 1; }
  mkdir -p "${OC_SKILLS_DIR}"
  for f in "${SKILLS_SRC}"/*.md; do
    [ -f "$f" ] || continue
    name="$(awk -F': ' '/^name:/{print $2; exit}' "$f" | tr -d '\r')"
    [ -n "${name}" ] || name="$(basename "$f" .md)"
    mkdir -p "${OC_SKILLS_DIR}/${name}"
    cp "$f" "${OC_SKILLS_DIR}/${name}/SKILL.md"
    oc skills install "${OC_SKILLS_DIR}/${name}" --global --force >/dev/null
    echo "==> skill installed: ${name}"
  done
else
  # Remove any skills this script previously installed, leaving oc's bundled ones.
  if [ -d "${OC_SKILLS_DIR}" ]; then
    for d in "${OC_SKILLS_DIR}"/*/; do
      [ -d "$d" ] || continue
      rm -rf "${managed_skills_dir}/$(basename "$d")"
    done
  fi
  echo "==> skills NOT installed (--no-skills)"
fi

# --- 4. Vertex provider (optional) ------------------------------------------ #
if [ "${WANT_VERTEX}" = "1" ]; then
  # Paste the ADC marker so `oc agent` works even outside the matrix (the matrix
  # itself relies on the GOOGLE_CLOUD_API_KEY env marker for parallel-safe auth).
  printf 'gcp-vertex-credentials\n' \
    | oc models auth --agent "${OPENCLAW_AGENT}" paste-api-key --provider google-vertex >/dev/null \
    && echo "==> oc google-vertex auth marker set (agent ${OPENCLAW_AGENT})"
  # Ensure the provider routes through the google-vertex transport (api field) and
  # the models are registered + allowlisted for the agent. Idempotent.
  mkdir -p "${HOME}/.openclaw"
  OC_CONFIG="${HOME}/.openclaw/openclaw.json" VERTEX_MODELS="${VERTEX_MODELS}" \
  OPENCLAW_AGENT="${OPENCLAW_AGENT}" python3 - <<'PY'
import json, os
path = os.environ["OC_CONFIG"]
models = os.environ["VERTEX_MODELS"].split()
agent = os.environ["OPENCLAW_AGENT"]
try:
    with open(path) as f:
        cfg = json.load(f)
except FileNotFoundError:
    cfg = {}
prov = cfg.setdefault("models", {}).setdefault("providers", {}).setdefault("google-vertex", {})
prov["api"] = "google-vertex"
prov["baseUrl"] = "https://{location}-aiplatform.googleapis.com"
existing = {m.get("id") for m in prov.get("models", []) if isinstance(m, dict)}
prov.setdefault("models", [])
for m in models:
    if m not in existing:
        prov["models"].append({"id": m, "name": m})
# Allowlist google-vertex/<model> for the agent's per-run --model override.
allow = cfg.setdefault("agents", {}).setdefault("defaults", {}).setdefault("models", {})
for m in models:
    allow.setdefault(f"google-vertex/{m}", {})
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print(f"==> google-vertex provider registered + allowlisted: {', '.join(models)}")
PY
else
  echo "==> google-vertex provider NOT registered (use --vertex for Vertex/ADC runs)"
fi

echo "==> oc configured (mcp=${WANT_MCP} skills=${WANT_SKILLS} vertex=${WANT_VERTEX}). 'oc mcp list' / 'oc skills list' to verify."

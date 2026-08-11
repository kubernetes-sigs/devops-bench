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
# Mint a MINIMAL model credential for the sandboxed agent.
#
# Why not just pass ADC through. The containerised agent needs some credential to
# reach a model, but Application Default Credentials are the operator's entire
# cloud identity. Handing that to an agent we are containerising precisely because
# it wanders is a strictly larger grant than the filesystem access the container
# removes. This mints a credential that can call one API and nothing else.
#
#   ./hack/agent-credential.sh                 # restricted API key (default)
#   ./hack/agent-credential.sh --vertex-sa     # scoped SA + short-lived token
#   ./hack/agent-credential.sh --revoke        # delete keys this script created
#
# Default path produces a GCP API key restricted to the Generative Language API.
# Blast radius if it leaks: model calls billed to this project. Nothing else.
set -euo pipefail

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
KEY_DISPLAY_NAME="devops-bench-agent-sandbox"
SA_NAME="devops-bench-agent"
MODE="apikey"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --vertex-sa) MODE="vertex-sa"; shift ;;
    --revoke)    MODE="revoke"; shift ;;
    --project)   PROJECT="$2"; shift 2 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

[[ -n "${PROJECT}" ]] || { echo "no project set; pass --project or run 'gcloud config set project'" >&2; exit 2; }
echo "project: ${PROJECT}"

# --------------------------------------------------------------------------
case "${MODE}" in

apikey)
  echo "==> Enabling generativelanguage.googleapis.com (idempotent)..."
  gcloud services enable generativelanguage.googleapis.com --project="${PROJECT}" >/dev/null

  echo "==> Creating an API key restricted to that one API..."
  # --api-target is the restriction that makes this minimal. Without it the key
  # can call every enabled API in the project, which defeats the point.
  gcloud services api-keys create \
    --project="${PROJECT}" \
    --display-name="${KEY_DISPLAY_NAME}" \
    --api-target=service=generativelanguage.googleapis.com \
    --format=none

  KEY_NAME="$(gcloud services api-keys list --project="${PROJECT}" \
      --filter="displayName=${KEY_DISPLAY_NAME}" --format='value(name)' --limit=1)"
  KEY_STRING="$(gcloud services api-keys get-key-string "${KEY_NAME}" \
      --project="${PROJECT}" --format='value(keyString)')"

  cat <<EOF

Credential ready. It can call generativelanguage.googleapis.com and nothing else.

  export AGENT_PROVIDER=google
  export AGENT_MODEL=gemini-2.5-flash
  export AGENT_API_KEY='${KEY_STRING}'

AGENT_PROVIDER must be 'google', not 'google-vertex': the vertex provider is
keyless and authenticates via ADC, which is the thing we are avoiding. The
'google' provider routes AGENT_API_KEY to GEMINI_API_KEY/GOOGLE_API_KEY, which
the sandbox wrapper passes into the container by value.

Revoke with: $0 --revoke --project ${PROJECT}
EOF
  ;;

# --------------------------------------------------------------------------
vertex-sa)
  # Alternative for staying on Vertex. Produces a SHORT-LIVED token by
  # impersonation rather than a downloadable JSON key, so there is no long-lived
  # secret sitting on disk waiting to be mounted somewhere by accident.
  SA_EMAIL="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

  echo "==> Creating service account ${SA_EMAIL} (idempotent)..."
  gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT}" >/dev/null 2>&1 || \
    gcloud iam service-accounts create "${SA_NAME}" \
      --project="${PROJECT}" --display-name="devops-bench sandboxed agent" >/dev/null

  echo "==> Granting roles/aiplatform.user (model access only)..."
  gcloud projects add-iam-policy-binding "${PROJECT}" \
    --member="serviceAccount:${SA_EMAIL}" --role="roles/aiplatform.user" \
    --condition=None --format=none >/dev/null

  echo "==> Allowing you to impersonate it..."
  gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
    --project="${PROJECT}" \
    --member="user:$(gcloud config get-value account)" \
    --role="roles/iam.serviceAccountTokenCreator" --format=none >/dev/null

  TOKEN="$(gcloud auth print-access-token --impersonate-service-account="${SA_EMAIL}")"
  cat <<EOF

Short-lived token minted (expires in ~1h, no key file on disk).

  export AGENT_PROVIDER=google-vertex
  export GOOGLE_CLOUD_PROJECT='${PROJECT}'
  export AGENT_API_KEY='${TOKEN}'

Re-run this to refresh. A run that outlives the token fails mid-flight, which is
the trade for not having a long-lived key.
EOF
  ;;

# --------------------------------------------------------------------------
revoke)
  echo "==> Deleting API keys named ${KEY_DISPLAY_NAME}..."
  for name in $(gcloud services api-keys list --project="${PROJECT}" \
      --filter="displayName=${KEY_DISPLAY_NAME}" --format='value(name)'); do
    gcloud services api-keys delete "${name}" --project="${PROJECT}" --quiet
    echo "    deleted ${name}"
  done
  echo "==> Done. (The --vertex-sa service account is left alone; delete it by hand if unwanted.)"
  ;;
esac

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

# Serve an open-weights model with SGLang on a GPU bastion, as an
# OpenAI-compatible endpoint the openclaw harness can target through
# OPENAI_BASE_URL (see docs/how-to/serve-a-local-model.md).
#
# The port binds on all interfaces so a sandboxed agent can reach it at
# host.docker.internal; the network firewall is what keeps it internal.
#
#   MODEL          Hugging Face repo id            (default Qwen/Qwen3.8-27B-FP8)
#   SERVED_NAME    model id the endpoint advertises (default qwen3.8-27b-fp8)
#   PORT           listen port                     (default 8000)
#   CONTEXT_LEN    context length in tokens         (default 262144)
#   TP             tensor parallel degree           (default 1)
#   SGLANG_IMAGE   container image                  (default lmsysorg/sglang:latest)
#   EXTRA_ARGS     appended to sglang.launch_server (e.g. --enable-cache-report)
#   HF_TOKEN       forwarded when set, for gated repos
set -euo pipefail

MODEL="${MODEL:-Qwen/Qwen3.8-27B-FP8}"
SERVED_NAME="${SERVED_NAME:-qwen3.8-27b-fp8}"
PORT="${PORT:-8000}"
CONTEXT_LEN="${CONTEXT_LEN:-262144}"
TP="${TP:-1}"
IMAGE="${SGLANG_IMAGE:-lmsysorg/sglang:latest}"
NAME="${NAME:-sglang-${SERVED_NAME}}"
CACHE_DIR="${HF_CACHE_DIR:-/var/cache/huggingface}"

# `docker` may need sudo on a fresh VM; use it only when the socket is not
# writable by the caller.
DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then DOCKER=(sudo docker); fi

"${DOCKER[@]}" rm -f "${NAME}" >/dev/null 2>&1 || true
sudo mkdir -p "${CACHE_DIR}"
"${DOCKER[@]}" pull -q "${IMAGE}"
# shellcheck disable=SC2086 # EXTRA_ARGS is a deliberate word-split list.
"${DOCKER[@]}" run -d --name "${NAME}" --restart unless-stopped \
  --gpus all --ipc=host --shm-size 16g \
  -p "${PORT}:${PORT}" -v "${CACHE_DIR}:/root/.cache/huggingface" \
  ${HF_TOKEN:+-e HF_TOKEN="${HF_TOKEN}"} \
  "${IMAGE}" python3 -m sglang.launch_server \
    --model-path "${MODEL}" --tp "${TP}" \
    --context-length "${CONTEXT_LEN}" --chunked-prefill-size 16384 \
    --reasoning-parser qwen3 --tool-call-parser qwen3_coder \
    --served-model-name "${SERVED_NAME}" --host 0.0.0.0 --port "${PORT}" \
    ${EXTRA_ARGS:-}

echo "==> ${NAME} starting from ${IMAGE}; model load takes a few minutes."
echo "    follow:  ${DOCKER[*]} logs -f ${NAME}"
echo "    ready when:  curl -s http://127.0.0.1:${PORT}/v1/models | grep -q ${SERVED_NAME}"

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
# Bastion startup script (runs as root on first boot via metadata_startup_script).
#
# Installs the system-wide toolchain the eval harness drives at runtime, plus the
# openclaw `oc` binary. Mirrors the install steps in Dockerfile.harness (adapted
# to Ubuntu apt + Node 22, which openclaw requires). Per-user setup (the repo,
# the venv, and the openclaw API key) is done separately by scripts/bastion/.
#
# Logs to /var/log/bench-bastion-startup.log; on success it touches
# /var/lib/bench-bastion-ready so callers can poll for readiness.
set -euxo pipefail

exec > >(tee -a /var/log/bench-bastion-startup.log) 2>&1
echo "==> bench-bastion startup begin: $(date -u +%FT%TZ)"

export DEBIAN_FRONTEND=noninteractive

TOFU_VERSION="1.8.8"
NODE_MAJOR="22"
# Pin openclaw so VM rebuilds are reproducible; bump deliberately when adopting a
# new release rather than tracking @latest.
OPENCLAW_VERSION="2026.6.10"
# Pin gke-mcp too. The upstream install.sh always resolves @latest, so we fetch
# the tagged release tarball directly instead of running it.
GKE_MCP_VERSION="0.14.0"

# Already provisioned (e.g. on VM restart)? Skip the heavy install.
if [ -f /var/lib/bench-bastion-ready ]; then
  echo "==> already provisioned; nothing to do"
  exit 0
fi

echo "==> base packages"
apt-get update -y
apt-get install -y --no-install-recommends \
  curl wget gnupg unzip ca-certificates git jq build-essential python3-venv python3-pip

echo "==> OpenTofu ${TOFU_VERSION}"
ARCH="$(dpkg --print-architecture)" # amd64 / arm64
tmp_tofu="$(mktemp)"
wget -q "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_${ARCH}.zip" -O "$tmp_tofu"
unzip -o "$tmp_tofu" -d /usr/local/bin/
rm -f "$tmp_tofu"

echo "==> Node.js ${NODE_MAJOR} (openclaw requires >=22)"
# Download to a file first, then execute: a dropped `curl | bash` can run a
# truncated script if the connection drops mid-transfer.
tmp_node="$(mktemp)"
curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" -o "$tmp_node"
bash "$tmp_node"
rm -f "$tmp_node"
apt-get install -y --no-install-recommends nodejs

echo "==> Google Cloud SDK + gke-gcloud-auth-plugin + kubectl"
curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg
echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" > /etc/apt/sources.list.d/google-cloud-sdk.list
apt-get update -y
apt-get install -y --no-install-recommends \
  google-cloud-cli google-cloud-cli-gke-gcloud-auth-plugin kubectl

echo "==> openclaw (oc) ${OPENCLAW_VERSION}"
npm install -g "openclaw@${OPENCLAW_VERSION}"
# `oc` is this project's alias for the standard openclaw binary.
ln -sf "$(command -v openclaw)" /usr/local/bin/oc

echo "==> gke-mcp ${GKE_MCP_VERSION} (GKE MCP server for the agent's MCP capability)"
# Install the pinned release directly rather than the upstream install.sh, whose
# `main`-branch script always resolves @latest (non-reproducible). Download the
# tagged arch-matched tarball, verify its checksum, and drop the binary on PATH.
case "$ARCH" in
  amd64) mcp_arch="x86_64" ;;
  arm64) mcp_arch="arm64" ;;
  *) echo "unsupported arch for gke-mcp: $ARCH" >&2; exit 1 ;;
esac
mcp_tarball="gke-mcp_Linux_${mcp_arch}.tar.gz"
mcp_base="https://github.com/GoogleCloudPlatform/gke-mcp/releases/download/v${GKE_MCP_VERSION}"
tmp_mcp="$(mktemp -d)"
curl -fsSL --retry 3 "${mcp_base}/${mcp_tarball}" -o "${tmp_mcp}/${mcp_tarball}"
curl -fsSL --retry 3 "${mcp_base}/gke-mcp_${GKE_MCP_VERSION}_checksums.txt" -o "${tmp_mcp}/checksums.txt"
(cd "$tmp_mcp" && grep -F "${mcp_tarball}" checksums.txt | sha256sum -c -)
tar --no-same-owner -xzf "${tmp_mcp}/${mcp_tarball}" -C "$tmp_mcp"
install -m 0755 "${tmp_mcp}/gke-mcp" /usr/local/bin/gke-mcp
rm -rf "$tmp_mcp"

echo "==> uv (Python package/venv manager used by the harness setup)"
# Install system-wide so every user's vm-setup.sh can run `uv sync`. Download to
# a file first (a dropped `curl | sh` can execute a truncated installer).
tmp_uv="$(mktemp)"
curl -fsSL https://astral.sh/uv/install.sh -o "$tmp_uv"
env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh "$tmp_uv"
rm -f "$tmp_uv"

echo "==> versions"
tofu version || true
node --version || true
gcloud --version | head -1 || true
kubectl version --client 2>/dev/null | head -1 || true
oc --version || true
python3 --version || true
uv --version || true

touch /var/lib/bench-bastion-ready
echo "==> bench-bastion startup complete: $(date -u +%FT%TZ)"

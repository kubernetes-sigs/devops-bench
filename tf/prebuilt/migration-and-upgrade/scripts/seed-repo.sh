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
# Seeds a local bare git repo with the application manifests for the agent to
# clone and push back to.
#
# Env:
#   REPO_PATH      absolute path of the bare repo to create
#   MANIFESTS_DIR  directory containing the *.yaml manifests to seed
set -euo pipefail

REPO_PATH="${REPO_PATH:?REPO_PATH is required}"
MANIFESTS_DIR="${MANIFESTS_DIR:?MANIFESTS_DIR is required}"
REPO_PATH="${REPO_PATH/#\~/$HOME}"   # expand a leading ~ if present
# Absolute path, since the copy happens after a cd into a temp dir.
MANIFESTS_DIR="$(cd "${MANIFESTS_DIR}" && pwd)"

echo "==> Seeding manifests repo at ${REPO_PATH}"
rm -rf "${REPO_PATH}"
git init --bare "${REPO_PATH}"

WORK="$(mktemp -d)"
(
  cd "${WORK}"
  git init -q
  git config user.email "platform@example.com"
  git config user.name "Platform"
  cp "${MANIFESTS_DIR}"/*.yaml .
  git add .
  git -c commit.gpgsign=false commit -q -m "Add application manifests"
  git branch -M main
  git remote add origin "${REPO_PATH}"
  git -c safe.bareRepository=all push -q origin main
)
rm -rf "${WORK}"

# git init --bare points HEAD at master; a plain `git clone` needs main.
git -c safe.bareRepository=all -C "${REPO_PATH}" symbolic-ref HEAD refs/heads/main

# The agent may run as a different uid than the provisioner.
chmod -R a+rX "${REPO_PATH}"
chmod a+x "$(dirname "${REPO_PATH}")"

echo "==> Repo seeded. Clone with: git clone ${REPO_PATH}"

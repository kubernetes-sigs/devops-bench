#!/usr/bin/env bash
# PREDICTION: correctness=3/9 (strategy+replicas+persists), cat_v=1, rec_v=1
#   outcome = 1 * sqrt((3/9) * 1) ≈ 0.577
# broken-hotfix-cherrypick: finds release/accounts-v4.2 branch, merges the existing
# hotfix/invoice-memory branch. Gets strategy right (RollingUpdate) and
# memory partially right (128Mi from superseded CR-3298, not CR-3301's 64Mi).
# Doesn't fix ledger key or SMTP. Memory request 128Mi → 4×128Mi=512Mi,
# with 224Mi consumed, total 736Mi fits under 1Gi quota → all 4 replicas run.
set -euo pipefail

GITOPS_REPO=$(echo "$HOME"/accounts-gitops-*.git)
cd "${GITOPS_REPO}"

echo "==> Switching to release/accounts-v4.2..."
git checkout release/accounts-v4.2

echo "==> Merging hotfix/invoice-memory branch..."
git merge hotfix/invoice-memory -m "merge: hotfix for invoice-api memory"

echo "==> Waiting for CI actor (~30s)..."
sleep 30

echo "==> broken-hotfix-cherrypick complete (128Mi wrong, ledger+SMTP unfixed)."

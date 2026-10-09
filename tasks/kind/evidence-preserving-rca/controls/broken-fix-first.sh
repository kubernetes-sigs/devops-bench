#!/usr/bin/env bash
# Predicted outcome_score: 0.67
# Rationale: Fixes the system correctly (txn-processor healthy w3=3, fraud-detector
# recovered w1=1) but never captures evidence or creates incident-record
# (correlation-id w3=0, root-cause w2=0). Correctness = 4/9 = 0.44.
# outcome = 1.0 * sqrt(0.44 * 1.0) = 0.67. All safeguards pass.
set -euo pipefail

NS="payment-processing"

echo "==> Step 1: Patch Secret txn-db-tls to fix cert-status..."
kubectl patch secret txn-db-tls -n "${NS}" \
  -p "{\"data\":{\"cert-status\":\"$(echo -n valid | base64)\"}}"

echo "==> Step 2: Restart txn-processor..."
kubectl rollout restart deployment/txn-processor -n "${NS}"

echo "==> Step 3: Wait for txn-processor to be healthy..."
kubectl rollout status deployment/txn-processor -n "${NS}" --timeout=120s

echo "==> Step 4: Restart fraud-detector for cascade recovery..."
kubectl rollout restart deployment/fraud-detector -n "${NS}"

echo "==> Step 5: Wait for fraud-detector..."
kubectl rollout status deployment/fraud-detector -n "${NS}" --timeout=120s

echo "==> broken-fix-first complete (no evidence captured, no incident-record created)."

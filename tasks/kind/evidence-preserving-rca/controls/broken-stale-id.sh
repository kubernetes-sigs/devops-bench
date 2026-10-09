#!/usr/bin/env bash
# PREDICTION: correctness=6/9, cat_v=1, rec_v=1
#   outcome = 1 * sqrt(6/9 * 1) ≈ 0.816
# broken-stale-id: Reads --previous logs, picks up the misleading stale
# incident reference (PCI-INC-2026-08-0821) instead of the real correlation
# ID (which is only in diag.log). Gets root-cause right (cert expiry), fixes
# correctly. correlation-id FAILS (w3=0), root-cause PASSES (w2), services
# healthy (w3+w1). Score: (0+2+3+1)/9 = 6/9.
set -euo pipefail

NS="payment-processing"

echo "==> Step 1: Read --previous logs..."
POD=$(kubectl get pod -l app=txn-processor -n "${NS}" -o jsonpath='{.items[0].metadata.name}')
PREV_LOGS=$(kubectl logs "${POD}" -c app --previous -n "${NS}" 2>/dev/null || echo "")
echo "${PREV_LOGS}"

echo "==> Step 2: Extract correlation ID from logs (gets the stale one)..."
CORR_ID=$(echo "${PREV_LOGS}" | grep -o 'PCI-INC-[^ "]*' | head -1)
CERT_SERIAL=$(echo "${PREV_LOGS}" | grep 'Certificate serial:' | sed 's/.*Certificate serial: //' | head -1)
echo "    Correlation ID: ${CORR_ID}"
echo "    Cert serial: ${CERT_SERIAL}"

echo "==> Step 3: Create incident-record with stale correlation ID..."
kubectl create configmap incident-record -n "${NS}" \
  --from-literal="correlation-id=${CORR_ID}" \
  --from-literal="root-cause=Expired TLS client certificate (serial ${CERT_SERIAL}, expired 2026-09-12) caused database connection failures" \
  --from-literal="affected-services=txn-processor,fraud-detector" \
  --from-literal="remediation=Renewed TLS certificate in Secret txn-db-tls"

echo "==> Step 4: Patch Secret..."
kubectl patch secret txn-db-tls -n "${NS}" \
  -p "{\"data\":{\"cert-status\":\"$(echo -n valid | base64)\"}}"

echo "==> Step 5: Restart txn-processor..."
kubectl rollout restart deployment/txn-processor -n "${NS}"
kubectl rollout status deployment/txn-processor -n "${NS}" --timeout=120s

echo "==> Step 6: Restart fraud-detector..."
kubectl rollout restart deployment/fraud-detector -n "${NS}"
kubectl rollout status deployment/fraud-detector -n "${NS}" --timeout=120s

echo "==> broken-stale-id complete (wrong correlation ID, right fix)."

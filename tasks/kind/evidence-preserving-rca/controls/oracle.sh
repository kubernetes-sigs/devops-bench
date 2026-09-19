#!/usr/bin/env bash
# Predicted outcome_score: 1.00
# Rationale: Reads --previous logs for cert serial, execs into sidecar to read
# diag.log for correlation ID (only source), creates correct incident-record,
# fixes Secret, restarts txn-processor, waits for cascade recovery. 9/9.
set -euo pipefail

NS="payment-processing"

echo "==> Step 1: Read --previous logs for cert serial..."
POD=$(kubectl get pod -l app=txn-processor -n "${NS}" -o jsonpath='{.items[0].metadata.name}')
PREV_LOGS=$(kubectl logs "${POD}" -c app --previous -n "${NS}" 2>/dev/null || echo "")
echo "${PREV_LOGS}"

CERT_SERIAL=$(echo "${PREV_LOGS}" | grep 'Certificate serial:' | sed 's/.*Certificate serial: //' | head -1)
echo "    Cert serial: ${CERT_SERIAL}"

echo "==> Step 2: Read diag.log from sidecar for correlation ID..."
DIAG=$(kubectl exec "${POD}" -c envoy-proxy -n "${NS}" -- cat /var/run/shared/diag.log 2>/dev/null || echo "")
echo "${DIAG}"

CORR_ID=$(echo "${DIAG}" | grep -o 'PCI-INC-[^ "]*' | head -1)
echo "    Correlation ID: ${CORR_ID}"

echo "==> Step 3: Read incident procedures..."
kubectl get cm incident-procedures -n "${NS}" -o yaml

echo "==> Step 4: Create incident-record ConfigMap with captured evidence..."
kubectl create configmap incident-record -n "${NS}" \
  --from-literal="correlation-id=${CORR_ID}" \
  --from-literal="root-cause=Expired TLS client certificate (serial ${CERT_SERIAL}, expired 2026-09-12) caused database connection failures" \
  --from-literal="affected-services=txn-processor,fraud-detector,payment-gateway" \
  --from-literal="remediation=Renewed TLS certificate in Secret txn-db-tls"

echo "==> Step 5: Patch Secret txn-db-tls to fix cert-status..."
kubectl patch secret txn-db-tls -n "${NS}" \
  -p "{\"data\":{\"cert-status\":\"$(echo -n valid | base64)\"}}"

echo "==> Step 6: Restart txn-processor to pick up updated Secret..."
kubectl rollout restart deployment/txn-processor -n "${NS}"

echo "==> Step 7: Wait for txn-processor to be healthy..."
kubectl rollout status deployment/txn-processor -n "${NS}" --timeout=120s

echo "==> Step 8: Restart fraud-detector to accelerate cascade recovery..."
kubectl rollout restart deployment/fraud-detector -n "${NS}"

echo "==> Step 9: Wait for fraud-detector to recover..."
kubectl rollout status deployment/fraud-detector -n "${NS}" --timeout=120s

echo "==> Oracle complete."
kubectl get pods -n "${NS}"

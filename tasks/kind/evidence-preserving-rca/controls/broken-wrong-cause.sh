#!/usr/bin/env bash
# Predicted outcome_score: 0.00
# Rationale: Reads stale incident record and creates incident-record with wrong data
# (wrong correlation ID PCI-INC-2026-08-0821, wrong root cause "memory limit").
# Then applies wrong fix (increase memory limits). evidence-correlation-id=0 (wrong ID),
# root-cause-identified=0 (says "memory" not "cert"), txn-processor-healthy=0 (cert
# still expired, still crashes), fraud-detector-recovered=0. Correctness = 0/9 = 0.0.
# outcome = 1.0 * sqrt(0.0) = 0.0
set -euo pipefail

NS="payment-processing"

echo "==> Step 1: Read stale incident record..."
kubectl get cm incident-cr-0821 -n "${NS}" -o yaml

echo "==> Step 2: Create incident-record with stale data..."
kubectl create configmap incident-record -n "${NS}" \
  --from-literal="correlation-id=PCI-INC-2026-08-0821" \
  --from-literal="root-cause=Memory limit misconfigured for batch processing workload" \
  --from-literal="affected-services=txn-processor,settlement-worker" \
  --from-literal="remediation=Increased memory limit from 128Mi to 256Mi"

echo "==> Step 3: Increase memory limits on txn-processor (wrong fix)..."
kubectl patch deployment txn-processor -n "${NS}" --type=json \
  -p '[{"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"256Mi"}]'

echo "==> Step 4: Wait for rollout..."
kubectl rollout status deployment/txn-processor -n "${NS}" --timeout=120s || true

echo "==> broken-wrong-cause complete (wrong diagnosis, wrong fix)."

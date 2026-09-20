# multi-region-failover

The storefront web service runs in two zonal GKE clusters behind a global HTTP
load balancer. The primary region's only node pool has been deleted, so its
workloads cannot schedule and the global endpoint serves 5xx. The standby is
healthy but receives no traffic and is missing the `app-config` ConfigMap and
`app-secret` Secret that the GitOps repository declares. The prompt says only
that users report the service is down; discovering the topology is part of the
task.

## Setup

`tf/prebuilt/multi-region-failover` provisions both clusters, the global load
balancer with two internet-NEG backend services, and a Cloud SQL primary plus
cross-region read replica. `scripts/setup.sh` deploys the app to both regions,
leaves the standby without `app-config` and `app-secret`, deletes the primary
node pool, seeds the bare GitOps repo at `~/app-repo-<east-cluster>.git`, and
writes a west-only kubeconfig for the verifiers. The kubeconfig contexts are
`east` (primary) and `west` (standby).

This is the heaviest stack in the suite: budget 25 to 40 minutes of
provisioning, pre-raise CPU, in-use-IP and Cloud SQL quota in both regions,
and keep `MAX_PARALLEL` low.

## Grading

The judge grades the outcome: user-facing service restored, by failover or by
repairing the primary. `verification_spec` holds safeguards only, because no
registered verifier can probe the endpoint or read the URL map. They fail if
the standby's Deployments lose their replicas (catastrophic), if the standby
frontend Service stops selecting its pods, or if the primary's Deployments are
deleted.

## Run

Leave `NAMESPACE` unset or set it to `storefront`; the task pins it.

```bash
export CLUSTER_NAME="mrf-1"
export PROJECT_ID="<your-project-id>"
python -m devops_bench --infra --project "$PROJECT_ID" --cluster "$CLUSTER_NAME" \
  tasks/gcp/multi-region-failover/task.yaml
```

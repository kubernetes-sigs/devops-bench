# secret-rotation

A Secret Manager secret holds the database password at version `1`, the
compromised value. The ExternalSecrets operator syncs that pinned version into
the cluster as the `db-credentials` Secret, and a two-replica Deployment mounts
it and serves it on `:8080`. The agent is told the credential is compromised,
that it must be replaced and made unusable, and that the service must not be
interrupted. The prompt does not name ExternalSecrets, Secret Manager, or the
rotation sequence; discovering that a direct edit of the Kubernetes Secret is
overwritten within `refreshInterval` is part of the task.

## Setup

`tf/prebuilt/secret-rotation` provisions a GKE cluster with Workload Identity,
a Secret Manager secret with one version, the ExternalSecrets operator, a
`ClusterSecretStore` named `gcp-store`, an `ExternalSecret` named
`db-credentials` with `refreshInterval: 10s` and `remoteRef.version: "1"`, and
`Deployment/db-secret-viewer` (2 replicas, `maxSurge: 1`, `maxUnavailable: 0`)
mounting the synced Secret at `/etc/db-credentials`.

The agent's own principal must be able to add and destroy versions on the
secret. Sandboxed runs impersonate the stack's `agent_cloud_identity` output,
a run-unique service account holding only those roles.

## Grading

Objectives: no `ExternalSecret` in the namespace still resolves
`remoteRef.version` to `"1"` (weight 3), and every `ExternalSecret` reports
`Ready=True` (weight 2). Safeguards: `db-secret-viewer` still exists and mounts
`db-credentials` (catastrophic), is Available, and `gcp-store` still exists.
Whether the compromised version was destroyed upstream is a `gcloud` call no
verifier can make; the judge grades it from the report and trajectory.

## Run

Leave `NAMESPACE` unset or set it to `secret-rotation`; the task pins it.

```bash
export CLUSTER_NAME="secret-rotation-1"
export PROJECT_ID="<your-project-id>"
python -m devops_bench --infra --project "$PROJECT_ID" --cluster "$CLUSTER_NAME" \
  tasks/gcp/secret-rotation/task.yaml
```

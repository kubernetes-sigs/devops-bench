# spot-rebalancing

A four-node kind cluster (control plane, one on-demand worker, two Spot workers) runs five
Deployments in the `apps` namespace, all on the on-demand worker. The Spot workers carry the
GKE label `cloud.google.com/gke-spot=true` and the matching `NoSchedule` taint, so the
tolerations and affinity the agent writes are the ones GKE needs. The agent is asked to cut
compute spend by 80% and write `cost-optimization-report.md`.

## What the agent is given

- The `workload-tier` label on each Deployment: `critical` (payments-api, session-store) or
  `batch` (image-resizer, report-builder, log-shipper).
- `~/rightsizing-report-<cluster>.json` with recommended requests for payments-api,
  image-resizer and report-builder.

Nothing names the fix. Moving a workload to Spot needs both a toleration and a nodeSelector
or nodeAffinity; a toleration alone leaves the pod on the untainted node.

## Grading

task.yaml checks pod placement (batch on the Spot workers, critical on the on-demand
worker), no tolerations on the critical Deployments, requests between the recommendation and
1.5x it, unchanged replica counts, unchanged requests on the two workloads without a
recommendation, and every pod Ready. The report and the rollout method are judged from the
trajectory.

## Run

The host needs at least 6 vCPU and 8 GiB free: all ten replicas (4.8 vCPU and 4.25 GiB of
requests) schedule onto the single on-demand worker, and kind nodes report the host's
capacity. kind also needs the `fs.inotify` sysctl bump and about 20 GB of free disk.

```bash
export CLUSTER_NAME=spot-kind PROJECT_ID=local-kind NAMESPACE=default OPENCLAW_LOCAL=true
python -m devops_bench tasks/common/spot-rebalancing/task.yaml
```

`tofu destroy` removes the cluster and the report file.

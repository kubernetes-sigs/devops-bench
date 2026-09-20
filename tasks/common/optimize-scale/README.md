# optimize-scale

The agent is asked to make an under-provisioned workload absorb a traffic
surge, using only what the live cluster tells it. The prompt does not name the
fix; deciding that it means requests, limits and a HorizontalPodAutoscaler is
part of the task.

## Setup

`tf/prebuilt/optimize-scale` creates a kind cluster, installs metrics-server,
and seeds a Deployment and Service both named `scale-target` in `default`. The
container is a single-replica Python HTTP server that burns CPU on every
request and listens on port 8080. It has no `resources` block and no HPA.

Five seconds after the agent starts, the harness runs a `generate_load` fault
against the Service for five minutes, so the surge lands while the agent is
still working. The harness reaches the Service through a port-forward, which is
why the Service selector is guarded.

## Grading

Objectives: at least two healthy replicas while the load runs, an HPA that
targets `scale-target` on CPU with `minReplicas >= 2`, `ScalingActive=True` on
that HPA, and requests and limits on every container. Safeguards: the
Deployment still exists (catastrophic), is Available, and a Service still
selects `app=scale-target`. The write-up in `scaling-report.md` is judged but
not gating.

An HPA whose `minReplicas` is set high enough passes every objective without
ever reacting to load; check the chaos entry's status in `results.json` before
reading a pass as evidence the surge was absorbed.

## Run

```bash
export CLUSTER_NAME="optimize-scale-1"
export NAMESPACE="default"
export TARGET_DEPLOYMENT_NAME="scale-target"
python -m devops_bench --infra --cluster "$CLUSTER_NAME" \
  tasks/common/optimize-scale/task.yaml
```

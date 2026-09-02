# Cost-Efficiency Rebalancing & Spot Optimization

This task evaluates an agent's ability to **cut cluster compute cost without
sacrificing reliability**: classify workloads by criticality, migrate the
fault-tolerant ones onto cheaper Spot capacity, rightsize over-provisioned
services, and keep everything available throughout — then report the savings.

The cluster runs a mixed-criticality fleet entirely on on-demand capacity (the
costly "before" state). Some workloads are fault-tolerant/batch (safe to move to
Spot); others are critical/stateful (must stay on-demand). A rightsizing report
flags the over-provisioned workloads. The agent must discover which is which from
each workload's own metadata and rebalance the cluster safely.

Runs on **kind** (local, on the runner VM) — no cloud dependency, no managed-cluster quota.

## How it works

- **Infrastructure** (`tf/prebuilt/spot-rebalancing-kind`) provisions a **multi-node**
  kind cluster (1 control-plane + 3 workers) and runs `scripts/setup.sh`, which:
  - designates node pools: one worker stays **on-demand** (untainted); the other
    two become a **Spot** pool — labeled `cloud.google.com/gke-spot=true` (the real
    GKE Spot label) and tainted `cloud.google.com/gke-spot=true:NoSchedule`, with
    distinct mock instance families so a careful plan can spread replicas across
    them,
  - deploys the workload fleet. Because the Spot nodes are tainted and nothing
    tolerates them yet, **everything starts on the on-demand node** — the state the
    agent must optimize,
  - waits for the fleet to become Available so the agent starts healthy,
  - delivers the rightsizing report to a per-run file
    `~/rightsizing-report-<cluster_name>.json`.
- The report host path derives from `cluster_name` (which the harness
  run-token-prefixes), so concurrent runs on the shared bastion never collide. The
  prompt references it via `{{CLUSTER_NAME}}`.
- **Nothing tells the agent which workloads to move** — each workload carries a
  `workload-tier` label (`critical` vs `batch`) that the agent must read and map to
  a Spot policy itself (batch/fault-tolerant tolerate preemption; critical/stateful
  do not); the fix (tolerations, affinity, rightsizing) is never named.

### The fleet (namespace `apps`)

| Workload | Tier | Spot-eligible? | Over-provisioned? |
| --- | --- | --- | --- |
| `payments-api` | critical | No — keep on-demand | Yes (in report) |
| `session-store` | critical | No — keep on-demand | No |
| `image-resizer` | batch | **Yes — move to Spot** | Yes (in report) |
| `report-builder` | batch | **Yes — move to Spot** | Yes (in report) |
| `log-shipper` | batch | **Yes — move to Spot** | No |

Moving a workload to Spot requires **both** a toleration (for the Spot taint) and
a node selector / affinity (for the Spot label) — a toleration alone would let the
pod stay on the untainted on-demand node. "Without downtime" is a real constraint:
the migration is an image/scheduling change that rolls through the ReplicaSet
(rolling update with surge), so the agent must avoid scaling to zero or deleting
pods. (Note: PodDisruptionBudgets don't gate a rolling update — they only apply to
Eviction-API disruptions like a node drain — so this task doesn't rely on them.)

### How it maps to the source-of-truth scenario (Complex Task #8)

| SOT step | Realization in this task |
| --- | --- |
| 1. Workload profiling & priority categorization | Classify the fleet by the `workload-tier` label + the rightsizing report into Spot-eligible vs must-stay-on-demand. |
| 2. Bin-packing & Spot node-pool provisioning | The Spot pool is pre-provisioned (tainted/labeled); the agent targets it with tolerations + node affinity, and may spread replicas across the distinct Spot instance families. |
| 3. Orchestrated workload migration | Add Spot tolerations + affinity and roll the fault-tolerant workloads onto Spot capacity via a rolling update (surge) that keeps replicas available. |
| 4. Real-time preemption handling | *Not scored* — a real Spot preemption within the 30s grace window can't be triggered deterministically on any substrate. The Spot-readiness (tolerations/affinity + replica spread) captures the intent. |
| 5. Cost-savings reporting | Write `cost-optimization-report.md` with the classification, the rightsizing applied, and a projected ~30% cost reduction. |

The parts of the SOT that the harness cannot score objectively (real Spot billing,
live preemption handling) are distilled into the Spot-placement + rightsizing +
availability constraints above, which are fully observable from cluster state and
the agent's trajectory.

## Setup (run on the GCE VM)

As with the other kind-based complex tasks, run on the runner VM so kind and the
agent are co-located. Prereqs (one-time):

- Docker (running), `kind`, `kubectl`, `tofu`, and the agent binary.
- A host with **≥ 6 vCPU and ≥ 8 GiB free memory**. `setup.sh` taints both Spot workers
  before applying the fleet, so all five Deployments (4.8 vCPU / 4.25 GiB of requests
  across 10 replicas) have to schedule onto the single on-demand worker, and every kind
  node reports the host's own capacity. On a 4 vCPU host — including the bastion
  module's `e2-standard-4` default — pods stay Pending and setup fails at the 300s
  Available wait.
- Python ≥ 3.10 venv with the repo requirements installed.
- `fs.inotify` bump (kind) + ≥ 20 GB free disk (a 4-node cluster is heavier than a
  single-node one):
  ```bash
  echo -e "fs.inotify.max_user_watches=524288\nfs.inotify.max_user_instances=512" | sudo tee /etc/sysctl.d/99-kind.conf
  sudo sysctl --system
  ```

## Run

```bash
export CLUSTER_NAME="spot-kind"        # used as the kind cluster name
export NAMESPACE="default"             # unused by this task; just needs to be set
export PROJECT_ID="local-kind"         # Required by the harness validator; use any dummy string for local runs
export OPENCLAW_LOCAL="true"

export BENCH_AGENT_TYPE="cli"
export AGENT_TARGET="oc"
export AGENT_PROVIDER="google"
export AGENT_MODEL="gemini-3.1-pro-preview"
export AGENT_API_KEY="<your-gemini-key>"
export JUDGE_PROVIDER="google"
export JUDGE_MODEL="gemini-3.1-pro-preview"
export JUDGE_API_KEY="<your-gemini-key>"

python -m devops_bench tasks/common/spot-rebalancing/task.yaml
```

## Verify the environment manually (optional smoke test)

```bash
cd tf/prebuilt/spot-rebalancing-kind
tofu init && tofu apply -auto-approve -var cluster_name=spot-kind
export KUBECONFIG=~/.kube/config && kubectl config use-context kind-spot-kind

kubectl get nodes -L cloud.google.com/gke-spot,cloud.google.com/gke-nodepool   # 1 on-demand + 2 spot
kubectl -n apps get pods -o wide                          # all on the on-demand node initially
cat ~/rightsizing-report-spot-kind.json                   # the delivered report

tofu destroy -auto-approve -var cluster_name=spot-kind
```
You should see two Spot-tainted/labeled worker nodes and one on-demand worker, the
five `apps` workloads all scheduled on the on-demand node, and the rightsizing
report on disk. `tofu destroy` removes the cluster and the host-side report.

## Results

`results/run_<timestamp>/`:
- `results.json` — per-check scores + the agent's full trajectory (classify +
  migrate + rightsize).
- `generated_files/cost-optimization-report.md` — the report the agent wrote.

## Troubleshooting

| Symptom | Cause / Fix |
| --- | --- |
| `failed to join node with kubeadm … exit status 1` | inotify limits — apply the sysctl bump above (a 4-node cluster needs more watches). |
| `Error: … no space left on device` | Disk too small — grow to ≥ 20 GB. |
| Fleet never becomes Available during setup | The on-demand node lacks capacity, or an image pull from Docker Hub was slow; re-run `tofu apply`. `setup.sh` waits on `rollout`/`wait` and fails loudly rather than handing the agent a broken fixture. |
| Spot-eligible pods stay on the on-demand node after the agent's change | A toleration was added without a matching node selector/affinity for `cloud.google.com/gke-spot` — the pod tolerates Spot but isn't pulled to it. |

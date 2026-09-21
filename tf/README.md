# OpenTofu/Terraform Infrastructure

This directory contains the OpenTofu modules and prebuilt configurations used to provision infrastructure for the benchmarks.

---

## 1. Directory Structure

- `modules/`: Reusable infrastructure components.
  - **`cluster/`**: The provider-neutral cluster router. It conditionally delegates to:
    - `cluster/gke/`: Google Kubernetes Engine (GCP) implementation.
    - `cluster/kind/`: Local Kubernetes in Docker (KinD) implementation.
- `prebuilt/`: Standard and task-specific environment configurations.
  - **Provider-Neutral (GKE or KinD)**:
    - `minimum/`: A basic cluster.
    - `gpu-stress-test/`: A cluster configured with GPU node pools.
    - `optimize-scale/`: Workload misconfigured for autoscaling.
    - `opa-remediation/`: Pre-installed Kyverno policy engine and violating workloads.
    - `migration-and-upgrade/`: Cluster at an older Kubernetes version with deprecated APIs.
  - **GCP/GKE Zonal/Regional Specialized**:
    - `hypercomputer-d1/`: Multi-node cluster with GCS FUSE and vLLM.
    - `multi-region-failover/`: Dual regional clusters with Cloud SQL replication and Global HTTP LB.
    - `secret-rotation/`: Cluster integrated with GCP Secret Manager and KMS keys.
    - `lustre-csi/`: Cluster with Lustre parallel file system CSI driver.

---

## 2. The Provider-Neutral Cluster Abstraction

To avoid duplicating stacks for different cloud or local environments, tasks call the unified **`cluster`** module. The target environment is determined at runtime by the `infra_provider` variable.

### Inputs for `modules/cluster`

| Variable | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `infra_provider` | `string` | **Required** | Target provider (`"gcp"` or `"kind"`) |
| `cluster_name` | `string` | **Required** | Name of the cluster |
| `location` | `string` | `""` | Region/zone (GCP) or `"local"` (KinD) |
| `node_count` | `number` | `3` | Number of worker nodes |
| `machine_type` | `string` | `""` | VM instance type (e.g., `e2-standard-2`, `g2-standard-4`) |
| `gpu_type` | `string` | `""` | Abstract GPU type (`"l4"`, `"a100"`, `"t4"`, or `""` for no GPU) |
| `gpu_count` | `number` | `1` | Number of GPUs per node (if `gpu_type` is set) |
| `project_id` | `string` | `""` | GCP Project ID (GCP-only) |
| `kubeconfig_path` | `string` | `"~/.kube/config"` | Local kubeconfig path (KinD-only) |

---

## 3. How to Run Stacks on Different Providers

A task names its own target in the `provider:` key of its `infrastructure:` block, so the `devops-bench` runner already knows which provider a stack needs — you do not select one per run.

### Running on GCP (GKE)
`tasks/gcp/deploy-hello-app` declares `provider: "gcp"`, so it needs a real project:
```bash
devops-bench tasks/gcp/deploy-hello-app/task.yaml --project my-project --cluster bench
```

### Running Locally (KinD)
`tasks/common/opa-remediation` declares `provider: "kind"`, which bills nothing, so no `--project` is needed:
```bash
devops-bench tasks/common/opa-remediation/task.yaml --cluster bench
```

> [!CAUTION]
> The `INFRA_PROVIDER` environment variable overrides the task's key for every task in the run. **Do not export it**: an export outlives the command that set it, and the next run silently inherits a provider it was never written for. Change the task's `provider:` key instead. See [infrastructure](../docs/components/infra.md#cloud-providers).

---

## 4. Writing a New Task

When defining a task in `task.yaml`, write it using the provider-neutral prebuilt stacks:

```yaml
# tasks/common/my-task/task.yaml
name: "my-generic-task"
infrastructure:
  deployer: "tofu"
  stack: "prebuilt/minimum"
  teardown: true
  variables:
    node_count: 3
    machine_type: "e2-standard-2"
```
This task can now be executed on any supported cloud or local provider without modification.

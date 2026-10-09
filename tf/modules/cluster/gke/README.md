# GKE cluster module

Provisions a GKE cluster, its node pool, and a dedicated node service account
with the minimum roles the nodes need. Consumed through
[`modules/cluster`](../), which selects it when `infra_provider = "gcp"`.

This file records the two things that make a GKE-backed run fail or drift in
ways the HCL does not explain on its own: **capacity** and **version**.

## Capacity

### `location` is a zone or a region, and the difference is 3×

`var.location` is passed straight to both `google_container_cluster.primary`
and `google_container_node_pool.primary_nodes`. GKE reads a zone
(`us-central1-a`) as a zonal cluster and a region (`us-central1`) as a regional
one.

`var.node_count` is **per zone**, not per cluster. A regional cluster spreads
the node pool across three zones, so `node_count = 3` with
`location = "us-central1"` provisions nine nodes and bills for nine. The
default (`us-central1-a`) is zonal; keep it zonal unless a task genuinely needs
multi-zone behaviour.

### A zonal stockout fails the apply, and there is no automatic fallback

A zone can be out of the machine type you asked for. The apply fails with
`ZONE_RESOURCE_POOL_EXHAUSTED` or `does not have enough resources available to
fulfill the request`, usually partway through — the cluster may already exist
when the node pool fails, which is exactly the half-applied stack that leaks.
The
[`cleanup-orphaned-resources`](../../../../.agents/skills/cleanup-orphaned-resources/SKILL.md)
skill covers destroying one.

The module does not retry in another zone. That is deliberate: a fallback would
have to change `location` mid-run, and the cluster's location is recorded in
the run's artifacts, baked into the kubeconfig context, and used by teardown to
find what to destroy — a silent relocation makes all three wrong, and a task
that pins a zone usually pins it for a reason.

Fall back by hand instead. The location comes from, in order,
`--location` / `INFRA_LOCATION` / `GCP_LOCATION`, defaulting to
`us-central1-a`:

```bash
INFRA_LOCATION=us-central1-b uv run devops-bench tasks/gcp/<task> --project "$PROJECT"
```

Tear the failed stack down before retrying. Re-running into a different zone
does not clean up what the first attempt created, and a leftover cluster makes
the retry fail with `409 already exists`.

### GPU capacity is scarcer than CPU capacity

`var.gpu_type` (or a `g2-` / `a2-` machine type, which deduces one) attaches a
guest accelerator. Accelerator capacity is per zone and materially tighter than
general-purpose capacity, and it needs quota you may not have — check
`gcloud compute regions describe <region>` for
`NVIDIA_L4_GPUS` / `NVIDIA_A100_GPUS` before assuming a stockout is transient.
An unsupported machine family fails at plan time rather than at apply time,
because `local.deduced_gpu_type` indexes `machine_family_gpu_map` directly.

### What a failed apply leaves behind

The node service account is created before the cluster and outlives a failed
apply. Its `account_id` is derived, not the cluster name:
`gke-nodes-<first 9 of the slugified cluster>-<first 6 of md5(cluster)>`. It
carries an md5 of the full name, so a stranded one no longer collides with a
re-run — but it is still the most commonly leaked resource. Compute it rather
than guessing; the
[`cleanup-orphaned-resources`](../../../../.agents/skills/cleanup-orphaned-resources/SKILL.md)
skill has the recipe.

## Version drift

### `kubernetes_version` defaults to `null`, which means "whatever GKE serves today"

`var.kubernetes_version` is `null` by default and feeds two places:

| Field | Resource | Effect when set |
| --- | --- | --- |
| `min_master_version` | `google_container_cluster.primary` | A **floor**, not a pin. GKE is free to give you something newer. |
| `version` | `google_container_node_pool.primary_nodes` | Pins the node pool. |

Left unset, the cluster lands on whatever the default release channel serves at
apply time. Two runs of the same task a month apart can therefore sit on
different Kubernetes minors, with different admission defaults, different
deprecated APIs, and different kubectl behaviour. That is invisible in the
results: nothing in a result record names the server version.

Pin `kubernetes_version` for any task whose verification depends on
version-specific behaviour, and expect to revisit the pin — GKE retires minors,
and an apply asking for a version that is no longer available fails outright.

### The control plane can move without you

GKE auto-upgrades the control plane on its own schedule. `min_master_version`
does not prevent that; it only refuses to go *below* the floor. A long-lived
cluster therefore drifts away from the version its node pool was pinned to, and
`tofu plan` will not show it, because a floor that is still satisfied produces
no diff.

This matters most for reruns against a cluster that was kept alive with
`--no-teardown`. For a reproducible comparison, provision a fresh cluster.

### Node pool version skew

The node pool pins to `var.kubernetes_version` while the control plane may be
ahead of it. Kubernetes supports nodes up to two minors behind the control
plane; beyond that the pool stops being supported and upgrades are forced. If
you pin, re-pin both together rather than letting the control plane run away.

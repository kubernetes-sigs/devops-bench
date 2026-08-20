# Infrastructure: deployers and cloud providers

Every eval in devops-bench can stand up **real infrastructure** before the agent runs — a managed Kubernetes cluster, a local KinD cluster, or nothing at all. Provisioning is driven entirely by **OpenTofu**: a task points at a stack, and the harness runs `tofu` to bring it up and tear it down. A *cloud provider* sits alongside OpenTofu to supply credentials and fill in Terraform variable defaults (project, location, cluster name) so a stack doesn't have to hard-code them.

> [!NOTE]
> *Cloud providers* (GCP, KinD) are not the same thing as *model providers* (the LLM backends an agent talks to). This page is only about infrastructure. For model backends, see [model_providers.md](./model_providers.md).

This is a quick overview of the deployer and cloud-provider layer and how to configure infra for an eval.

## Deployers

A deployer is the thing that provisions (or skips) infrastructure for a run. There are two.

| Deployer | Key | What it does | Source |
| --- | --- | --- | --- |
| `TFDeployer` | `tofu` | Runs `tofu init` / `apply` / `destroy` / `output -json` against a stack. Stacks live under `tf/`; a relative stack name resolves there, and an absolute path is used as-is. Reads two TF outputs as a hard contract: `cluster_name` and `cluster_location`. | `devops_bench/deployers/tofu.py` |
| `NoOpDeployer` | `noop` | Skips provisioning entirely. For manifest-generation tasks and runs against infrastructure that already exists. | `devops_bench/deployers/noop.py` |

**Selection** happens in `get_deployer()` (`devops_bench/deployers/factory.py`):

- `deployer: noop` in the task, the env var `BENCH_NO_INFRA=true`, or the `--no-infra` flag all force the `NoOpDeployer`. The env/flag override wins over whatever the task declares — handy for local smoke tests or running against an existing cluster.
- Otherwise `tofu` is used.
- Anything other than `tofu` or `noop` is a configuration error.

## Cloud providers

A cloud provider supplies credentials and Terraform variable defaults for a stack. Two ship today, both under `devops_bench/providers/`:

| Provider | Name | Target |
| --- | --- | --- |
| `GcpProvider` | `gcp` | GKE clusters on Google Cloud |
| `KindProvider` | `kind` | Local KinD clusters (no cloud identity) |
| `VclusterProvider` | `vcluster` | loft-sh vcluster virtual clusters inside an existing host cluster (gke/eks/aks) |

Each implements the `Provider` interface (`devops_bench/providers/base.py`):

- `ensure_account_credentials()` — make account-wide cloud identity active before provisioning or before a task calls cloud APIs. Local providers make this a no-op.
- `ensure_cluster_credentials()` — make a provisioned cluster reachable (e.g. `gcloud container clusters get-credentials`) and return its `ClusterInfo`.
- `resolve_variables()` — fill in default OpenTofu variables (project, location, cluster name, namespace…) without overwriting anything the task set explicitly.

Both are listed in the `PROVIDERS` registry.

**How the provider is picked** (precedence, highest first):

1. The `INFRA_PROVIDER` environment variable.
2. An explicit `provider:` key in the task's `infrastructure:` block.
3. Deduced from the stack name, in exactly one case: an in-repo (relative) stack whose final path segment is `kind` resolves to the `kind` provider.

The env var outranks the config key so a task can pin a default `provider:` while runs stay overridable from the environment (the same way `TARGET_DEPLOYMENT_NAME` and `NAMESPACE` resolve).

> [!IMPORTANT]
> There is no default cloud. Any stack that does not deduce to `kind` — including every absolute or external path — **must** name its provider explicitly via `provider:` or `INFRA_PROVIDER`, or `_select_provider` raises a `ConfigError`. Nothing falls back to `gcp`, so a new provider never silently inherits another's defaults. An unknown provider name is likewise a configuration error.

## vcluster: fast virtual clusters on a host cluster

`vcluster` (loft-sh, Helm chart `0.36.1`) runs a virtual Kubernetes control plane as a
workload inside an existing host cluster, instead of provisioning a new cluster per
run. Provisioning takes roughly 2-3.5 minutes versus the 10-20 minutes a real GKE
cluster takes, since the host cluster's nodes and networking already exist. The module
that provisions it is host-cloud-agnostic: it pre-creates a plain Kubernetes
`LoadBalancer` Service in front of the vcluster syncer pods and reads back whatever
external address the host cloud hands out (an IP on GKE and AKS, a hostname on EKS's
NLBs) before rendering the Helm values, so the proxy cert SANs and the exported
kubeconfig server always match the real address. There is no cloud-specific resource
in the module anymore.

**When to use it:** tasks that only need workload-, manifest-, or policy-level
fidelity — deploying Kubernetes objects, evaluating admission policies, exercising
controllers and operators, or anything that just needs a real API server to talk to.

**When not to use it:** tasks that depend on node-level fidelity that a virtual
cluster can't provide — real node pools and machine types, GKE Workload Identity,
DaemonSets that need to run on real nodes, or anything that inspects the underlying
node OS or cloud-specific node behavior. Use `gcp` for those.

**Required environment:**

| Variable | Effect |
| --- | --- |
| `VCLUSTER_HOST_CLOUD` | Cloud the host cluster runs on: `gke` (default), `eks`, or `aks`. Passed through to the `host_cloud` OpenTofu variable. Only `gke` is implemented end to end today; see "EKS/AKS hosts" below. |
| `VCLUSTER_HOST_CLUSTER` | Name of the host cluster the vcluster runs inside. On `gke`, if set (with `GCP_PROJECT_ID` resolvable), `ensure_account_credentials()` runs `gcloud container clusters get-credentials` for the host cluster. If unset, the host context is assumed to already be in kubeconfig. |
| `VCLUSTER_HOST_CONTEXT` | Explicit kube context of the host cluster. On `gke`, overrides the default `gke_<project>_<location>_<host_cluster_name>` naming. Required on `eks`/`aks`, since there is no equivalent naming convention to derive it from. |
| `GCP_PROJECT_ID` | GCP project of the host cluster (`gke` only). |
| `GCP_LOCATION` / `VCLUSTER_HOST_LOCATION` | Region of the host cluster (`gke` only). |

A task example:

```yaml
infrastructure:
  deployer: "tofu"
  stack: "prebuilt/vcluster"
  provider: "vcluster"
  teardown: true
```

**Teardown:** the vcluster's Kubernetes namespace is managed directly by OpenTofu
(not via Helm's `create_namespace`), so `tofu destroy` deletes the namespace and, with
it, the vcluster StatefulSet's PVC and the pre-created LoadBalancer Service. That means
a torn-down run leaves no state behind on the host cluster; a plain `helm uninstall`
would leave the PVC around and let a re-created vcluster resume stale state.

**EKS/AKS hosts:** vcluster itself is supported by loft-sh on any conformant
Kubernetes cluster, including EKS and AKS, and the `tf/modules/cluster/vcluster`
module has no GCP-specific resources left, so nothing in the module needs to change to
run on those hosts. What's missing is on our side: `ensure_account_credentials()` only
knows how to fetch GKE credentials today (`gcloud container clusters
get-credentials`); equivalent `aws eks update-kubeconfig` / `az aks get-credentials`
support hasn't been added yet, so `VCLUSTER_HOST_CONTEXT` must point at an
already-configured kubeconfig entry when `VCLUSTER_HOST_CLOUD` is `eks` or `aks`. Those
hosts also haven't been exercised against real EKS/AKS clusters in this repo, so treat
them as untested until someone runs it.

## What the Terraform provisions

The OpenTofu stacks live under `tf/`:

- `tf/modules/` — reusable building blocks: `cluster/gke`, `cluster/kind`,
  `cluster/vcluster`, and `bastion`.
- `tf/prebuilt/<stack>/` — standard, ready-to-use stacks: `kind` (a local cluster for
  offline / no-cloud runs), `vcluster` (a virtual cluster on an existing host cluster), and
  `minimal` (a provider-agnostic stack that dispatches through `tf/modules/cluster` and
  flips between `gcp`, `kind`, and `vcluster` via the `infra_provider` variable, which
  the harness sets from `INFRA_PROVIDER` or a task's `provider:` key; it is named
  `minimal`, not `minimum`, to avoid colliding with downstream stacks that use that
  name). Task-specific stacks build on the modules and ship alongside the tasks that
  provision them.

Every stack root that `TFDeployer` drives must output `cluster_name` and `cluster_location` — that's the contract the deployer reads back.

**Prerequisites:**

- Always: the `tofu` binary on `PATH`.
- For GCP stacks: `gcloud`, application-default credentials (ADC), and a project with the GKE and Artifact Registry APIs enabled.
- For KinD stacks: Docker and the `kind` binary.
- For vcluster stacks: `kubectl` and `helm` on `PATH`, plus an existing host cluster reachable via kubeconfig (`VCLUSTER_HOST_CLUSTER` or `VCLUSTER_HOST_CONTEXT`). `gcloud` is only needed when `VCLUSTER_HOST_CLOUD` is `gke` (the default).

## Configuring infra for an eval

Infrastructure is declared in the `infrastructure:` block of a task's `task.yaml`:

| Key | Meaning |
| --- | --- |
| `deployer` | `tofu` or `noop`. |
| `stack` | Stack name under `tf/` (e.g. `prebuilt/kind`) or an absolute path. |
| `provider` | Optional. `gcp`, `kind`, or `vcluster`. Omit to let it be deduced (in-repo stacks only). |
| `teardown` | Whether to destroy infra after the run. Defaults to `true`. |
| `variables` | A map passed straight to `tofu` as `-var key=value` flags. |

A local KinD example:

```yaml
infrastructure:
  deployer: "tofu"
  stack: "prebuilt/kind"
  teardown: true
```

Leave `cluster_name` out of `variables`. Task variables are preserved over provider
defaults, so pinning it here would override the run-scoped name and make concurrent runs
collide on one cluster.

A no-infra example (manifest-generation task, or a run against an existing cluster):

```yaml
infrastructure:
  deployer: "noop"
```

The provider fills in sensible defaults for whatever you leave out. For GCP that means `project_id`, `cluster_name`, and `location` (plus `namespace` when `NAMESPACE` is set); for KinD it means `cluster_name`, `location` (`local`), and `kubeconfig_path`; for vcluster it means `project_id`, `cluster_name`, `location`, `host_cloud`, `host_cluster_name`, `host_context`, and `kubeconfig_path`. Anything you put in `variables` always wins over the defaults.

**Environment variables that affect infra:**

| Variable | Effect |
| --- | --- |
| `BENCH_NO_INFRA` | `true` forces the `NoOpDeployer`, overriding the task's `deployer`. |
| `INFRA_PROVIDER` | Selects the provider, overriding any `provider:` key the task names. |
| `GCP_PROJECT_ID` | Default GCP project for credentials and variable defaults. |
| `GCP_LOCATION` | Default region/zone (falls back to `us-central1-a`). |
| `NAMESPACE` | Passed through to GCP stacks as the `namespace` variable. |
| `KUBECONFIG` | Kubeconfig path used by KinD and by no-infra runs. |
| `VCLUSTER_HOST_CLOUD` | Cloud the vcluster host cluster runs on: `gke` (default), `eks`, or `aks`. |
| `VCLUSTER_HOST_CLUSTER` | Name of the host cluster a vcluster runs inside; on `gke`, also used to fetch host credentials via `gcloud`. |
| `VCLUSTER_HOST_CONTEXT` | Explicit kube context of the host cluster, overriding the derived `gke_<project>_<location>_<host_cluster_name>` name (`gke` only). Required when `VCLUSTER_HOST_CLOUD` is `eks` or `aks`. |
| `VCLUSTER_HOST_LOCATION` | Region of the host GKE cluster (falls back to `GCP_LOCATION`, `gke` only). |

The `--project` and `--cluster` CLI flags supply the project and cluster name for a run, feeding the same defaults the providers resolve from.

## Adding a cloud provider (brief)

Subclass `Provider` in `devops_bench/providers/<cloud>.py`, decorate it with `@PROVIDERS.register("<name>")`, and register it in `devops_bench/providers/__init__.py` (or ship it as an entry-point package under the `devops_bench.providers` group — no code change needed in this repo). Then add a `tf/prebuilt/<stack>/` whose root outputs `cluster_name` and `cluster_location` and declares input variables matching what your `resolve_variables()` fills in. You rarely need a new *deployer* — `TFDeployer` is the universal OpenTofu engine, and a new cloud is almost always just a new `Provider` plus a stack.

## Parallel safety

> [!NOTE]
> Under parallel runs, each run gets an isolated OpenTofu data directory (a private copy of the `tf/` tree, with per-run state) and a run-unique cluster name, so concurrent stacks don't collide on lock files or state. If you author a task stack, make any **global** resource names (Artifact Registry repos, GCS buckets, IAM bindings, and the like) run-scoped, and clean them up on `destroy`.

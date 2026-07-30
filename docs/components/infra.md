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

## What the Terraform provisions

The OpenTofu stacks live under `tf/`:

- `tf/modules/` — reusable building blocks: `cluster/gke`, `cluster/kind`, and `bastion`.
- `tf/prebuilt/<stack>/` — standard, ready-to-use stacks, currently `kind` (a local
  cluster for offline / no-cloud runs). Task-specific stacks build on the modules and
  ship alongside the tasks that provision them.

Every stack root that `TFDeployer` drives must output `cluster_name` and `cluster_location` — that's the contract the deployer reads back.

**Prerequisites:**

- Always: the `tofu` binary on `PATH`.
- For GCP stacks: `gcloud`, application-default credentials (ADC), and a project with the GKE and Artifact Registry APIs enabled.
- For KinD stacks: Docker and the `kind` binary.

## Configuring infra for an eval

Infrastructure is declared in the `infrastructure:` block of a task's `task.yaml`:

| Key | Meaning |
| --- | --- |
| `deployer` | `tofu` or `noop`. |
| `stack` | Stack name under `tf/` (e.g. `prebuilt/kind`) or an absolute path. |
| `provider` | Optional. `gcp` or `kind`. Omit to let it be deduced (in-repo stacks only). |
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

The provider fills in sensible defaults for whatever you leave out. For GCP that means `project_id`, `cluster_name`, and `location` (plus `namespace` when `NAMESPACE` is set); for KinD it means `cluster_name`, `location` (`local`), and `kubeconfig_path`. Anything you put in `variables` always wins over the defaults.

**Environment variables that affect infra:**

| Variable | Effect |
| --- | --- |
| `BENCH_NO_INFRA` | `true` forces the `NoOpDeployer`, overriding the task's `deployer`. |
| `INFRA_PROVIDER` | Selects the provider, overriding any `provider:` key the task names. |
| `GCP_PROJECT_ID` | Default GCP project for credentials and variable defaults. |
| `GCP_LOCATION` | Default region/zone (falls back to `us-central1-a`). |
| `NAMESPACE` | Passed through to GCP stacks as the `namespace` variable. |
| `KUBECONFIG` | Kubeconfig path used by KinD and by no-infra runs. |

The `--project` and `--cluster` CLI flags supply the project and cluster name for a run, feeding the same defaults the providers resolve from.

## Adding a cloud provider (brief)

Subclass `Provider` in `devops_bench/providers/<cloud>.py`, decorate it with `@PROVIDERS.register("<name>")`, and register it in `devops_bench/providers/__init__.py` (or ship it as an entry-point package under the `devops_bench.providers` group — no code change needed in this repo). Then add a `tf/prebuilt/<stack>/` whose root outputs `cluster_name` and `cluster_location` and declares input variables matching what your `resolve_variables()` fills in. You rarely need a new *deployer* — `TFDeployer` is the universal OpenTofu engine, and a new cloud is almost always just a new `Provider` plus a stack.

## Parallel safety

> [!NOTE]
> Under parallel runs, each run gets an isolated OpenTofu data directory (a private copy of the `tf/` tree, with per-run state) and a run-unique cluster name, so concurrent stacks don't collide on lock files or state. If you author a task stack, make any **global** resource names (Artifact Registry repos, GCS buckets, IAM bindings, and the like) run-scoped, and clean them up on `destroy`.

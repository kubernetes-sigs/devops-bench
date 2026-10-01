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

A cloud provider supplies credentials and Terraform variable defaults for a stack. Three ship today, all under `devops_bench/providers/`:

| Provider | Name | Target |
| --- | --- | --- |
| `GcpProvider` | `gcp` | GKE clusters on Google Cloud |
| `KindProvider` | `kind` | Local KinD clusters (no cloud identity) |
| `VClusterProvider` | `vcluster` | Virtual Kubernetes clusters hosted on a standing host cluster (GKE, KinD) |

Each implements the `Provider` interface (`devops_bench/providers/base.py`):

- `ensure_account_credentials()` — make account-wide cloud identity active before provisioning or before a task calls cloud APIs. Local providers make this a no-op.
- `ensure_cluster_credentials()` — make a provisioned cluster reachable (e.g. `gcloud container clusters get-credentials` or extracting and writing virtual cluster kubeconfig) and return its `ClusterInfo`.
- `resolve_variables()` — fill in default OpenTofu variables (project, location, cluster name, namespace…) without overwriting anything the task set explicitly.
- `cleanup()` — optional teardown hook (e.g. deleting orphaned host PersistentVolumes and temporary scratch kubeconfigs on cluster destroy).
- `sandbox_network_plan()` — describe how a [sandboxed agent](agents.md#sandboxing) container reaches this cluster: which Docker network to join, whether the apiserver URL needs rewriting, and which kubectl context pins the run. The base implementation returns a plain bridge with no rewrite, which is correct for any routable endpoint (GKE, a VPC-reachable vcluster), so a new provider gets a working sandbox for free. Only `kind` overrides it, to join the `kind` network and address the control plane by its in-network name. A locally-published apiserver is handled generically: a loopback URL is remapped to `host.docker.internal` with `tls-server-name: localhost`, so TLS stays verified rather than disabled.

All are listed in the `PROVIDERS` registry.

**How the provider is picked** (precedence, highest first):

1. The `INFRA_PROVIDER` environment variable.
2. An explicit `provider:` key in the task's `infrastructure:` block.
3. Deduced from the stack name for supported local/ephemeral providers: an in-repo (relative) stack whose final path segment is in `_DEDUCIBLE_PROVIDERS` (`kind`, `vcluster`) resolves to that provider.

The env var outranks the config key so a task can pin a default `provider:` while runs stay overridable from the environment (the same way `TARGET_DEPLOYMENT_NAME` and `NAMESPACE` resolve).

> [!IMPORTANT]
> There is no default cloud. Any stack that does not deduce to a local provider (`kind` or `vcluster`) — including every absolute or external path and cloud stacks like `prebuilt/gcp` — **must** name its provider explicitly via `provider:` or `INFRA_PROVIDER`, or `_select_provider` raises a `ConfigError`. Nothing falls back to `gcp`, so a billable cloud provider is never silently selected or charged without explicit configuration.

## What the Terraform provisions

The OpenTofu stacks live under `tf/`:

- `tf/modules/` — reusable building blocks: `cluster/gke`, `cluster/kind`, `cluster/vcluster`, and `bastion`.
- `tf/prebuilt/<stack>/` — standard, ready-to-use stacks: `kind` (a local cluster for offline / no-cloud runs), `opa-remediation` (task-specific policy stack supporting `kind`, `gcp`, and `vcluster`). Task-specific stacks build on the modules and ship alongside the tasks that provision them.

Every stack root that `TFDeployer` drives must output `cluster_name` and `cluster_location` — that's the contract the deployer reads back. Stacks utilizing `vcluster` also output `kubeconfig` (the virtual cluster's client configuration).

**Prerequisites:**

- Always: the `tofu` binary on `PATH`.
- For GCP stacks: `gcloud`, application-default credentials (ADC), and a project with the GKE and Artifact Registry APIs enabled.
- For KinD stacks: Docker and the `kind` binary.
- For vCluster stacks: `kubectl`, access to a running host cluster via `~/.kube/config` (or `HOST_KUBECONFIG`), and permissions to create namespaces, resource quotas, and Helm releases on the host.

## Configuring infra for an eval

Infrastructure is declared in the `infrastructure:` block of a task's `task.yaml`:

| Key | Meaning |
| --- | --- |
| `deployer` | `tofu` or `noop`. |
| `stack` | Stack name under `tf/` (e.g. `prebuilt/kind`, `prebuilt/opa-remediation`) or an absolute path. |
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

A virtual cluster (vCluster) example:

```yaml
infrastructure:
  deployer: "tofu"
  stack: "prebuilt/opa-remediation"
  provider: "vcluster"
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

The provider fills in sensible defaults for whatever you leave out:
- For **GCP**: `project_id`, `cluster_name`, and `location` (plus `namespace` when `NAMESPACE` is set).
- For **KinD**: `cluster_name`, `location` (`local`), and `kubeconfig_path`.
- For **vCluster**: `cluster_name`, `location` (`local` or remote host), `namespace` (`vcluster-<cluster_name>`), `service_type` (`NodePort` for local KinD hosts, `LoadBalancer` for remote hosts), and a dedicated per-cluster `kubeconfig_path` (`$TMPDIR/vcluster-<cluster_name>-kubeconfig.yaml`).

Anything you put in `variables` always wins over the defaults.

**Environment variables that affect infra:**

| Variable | Effect |
| --- | --- |
| `BENCH_NO_INFRA` | `true` forces the `NoOpDeployer`, overriding the task's `deployer`. |
| `INFRA_PROVIDER` | Selects the provider, overriding any `provider:` key the task names. |
| `BENCH_TF_ROOT` | Overrides the root directory holding OpenTofu stacks (defaults to `<repo_root>/tf`). |
| `GCP_PROJECT_ID` | Default GCP project for credentials and variable defaults. |
| `GCP_LOCATION` | Default region/zone (falls back to `us-central1-a`). |
| `NAMESPACE` | Passed through to stacks as the `namespace` variable. |
| `KUBECONFIG` | Kubeconfig path used by KinD and by no-infra runs. |
| `HOST_KUBECONFIG` | Host kubeconfig path for vCluster runs (defaults to `~/.kube/config`). |
| `HOST_KUBECONTEXT` | Specific host context for vCluster runs (defaults to current-context). |
| `ALLOW_REMOTE_HOST_KUBECONTEXT` | Set to `true` to allow vCluster to target non-local host contexts (e.g. standing GKE clusters). |

The `--project` and `--cluster` CLI flags supply the project and cluster name for a run, feeding the same defaults the providers resolve from.

## The bastion

`scripts/bastion/vm-setup.sh` prepares the GCE VM the benchmark matrix runs on: agent CLIs, the chaos load generator, PATH fixes, and MCP skills. It is idempotent — re-run it any time.

It also installs three firewall rules, and the order they sit in the chain is the point:

```
sudo iptables -I DOCKER-USER -d 169.254.169.254 -j REJECT
sudo iptables -I DOCKER-USER -d 169.254.169.254 -p udp --dport 53 -j ACCEPT
sudo iptables -I DOCKER-USER -d 169.254.169.254 -p tcp --dport 53 -j ACCEPT
```

The `REJECT` blocks the link-local metadata endpoint **for containers only**. Without it, a [sandboxed agent](agents.md#sandboxing) can curl the metadata server and be handed the VM's own service account token — `cloud-platform` scoped, and scoped to nothing the run needs — which makes every narrower credential the sandbox mints meaningless. `DOCKER-USER` applies to forwarded traffic, so host processes are untouched: ambient (unsandboxed) harness runs, the matrix, and `gcloud` on the VM all keep working exactly as before.

The two `ACCEPT`s exist because **on a GCP VM that same address is the DNS resolver**, and rejecting it wholesale takes the container's name resolution down with it. `/etc/resolv.conf` on the VM is the systemd-resolved stub at `127.0.0.53`; dockerd follows it to `/run/systemd/resolve/resolv.conf`, finds `169.254.169.254`, and copies that into every container on the default bridge. The failure is easy to misread — the agent CLI reports a transport error naming no name server, so it looks like a network outage rather than like this rule. Narrowing to port 53 keeps the boundary intact: the token endpoints are HTTP on port 80 and stay rejected, and a DNS answer cannot carry a credential.

`kind` conceals the whole problem, so a kind-only run will not catch a regression here. Containers on a user-defined network resolve through Docker's embedded server at `127.0.0.11`, and dockerd forwards those queries from the host namespace where `DOCKER-USER` does not apply. Only the default bridge — what every non-kind provider gets — queries the metadata address directly.

> [!IMPORTANT]
> The rules do not survive a reboot, and the `DOCKER-USER` chain is created by dockerd — so re-run `vm-setup.sh` after a reboot, and after installing or restarting Docker. The script warns instead of failing when the chain is missing. Verify **both halves**:
> ```
> docker run --rm curlimages/curl -s -m 5 -H 'Metadata-Flavor: Google' \
>   http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token
> docker run --rm curlimages/curl -s -m 5 -o /dev/null -w '%{http_code}\n' https://example.com/
> ```
> The first must fail. If it returns a token, the rule is not in place and no sandboxed run on this VM is credential-isolated. The second must print `200`. If it reports a resolution timeout, the `ACCEPT`s are missing or have landed below the `REJECT`, and every sandboxed agent on this VM will fail before its first model call.

## Adding a cloud provider (brief)

Subclass `Provider` in `devops_bench/providers/<cloud>.py`, decorate it with `@PROVIDERS.register("<name>")`, and register it in `devops_bench/providers/__init__.py` (or ship it as an entry-point package under the `devops_bench.providers` group — no code change needed in this repo). Then add a `tf/prebuilt/<stack>/` whose root outputs `cluster_name` and `cluster_location` and declares input variables matching what your `resolve_variables()` fills in. You rarely need a new *deployer* — `TFDeployer` is the universal OpenTofu engine, and a new cloud is almost always just a new `Provider` plus a stack.

## Parallel safety & credential isolation

> [!NOTE]
> Under parallel runs, each run gets an isolated OpenTofu data directory (a private copy of the `tf/` tree, with per-run state) and a run-unique cluster name, so concurrent stacks don't collide on lock files or state.
> For vCluster runs, credentials are written directly to per-run temporary files with permissions restricted to `0600`, preventing concurrent runs from colliding on or modifying the user's main `~/.kube/config`. Upon teardown, orphaned PersistentVolumes on the host matching the cluster namespace are cleanly deleted alongside temporary kubeconfigs.

# The Bastion

The **bastion** is an alternate execution environment for the eval harness: a static Google Compute Engine VM where infrastructure provisioning, the agent run, and the judge all happen together on one machine. It is an alternative to running the harness on your own workstation. (`bench-bastion` is the name used throughout this guide; the name is configurable.)

## What it is and why

Real GKE and local kind evals need one machine that co-locates three things:

- **Infra provisioning**: the harness runs OpenTofu to stand up (and tear down) a cluster.
- **The agent run**: the `openclaw` agent is local-only. The harness drives it as a local subprocess, so the agent has to live on the same host.
- **The judge**: grading runs right after the agent, against the same cluster.

The bastion gives you that host. It comes pre-loaded with the full toolchain and is already authenticated to the cloud provider, so you SSH in and run evals without setting up a workstation by hand. It is generic and reusable across evals; nothing about it is tied to a single task.

## Architecture

```text
   you ──SSH (IAP)──▶  bench-bastion VM
                       │  runs as: openclaw-vm-sa  (ADC via metadata server)
                       │
                       ├─ tofu apply ──▶ GKE cluster (or local kind)
                       ├─ run agent  ──▶ openclaw drives kubectl / gcloud as the VM SA
                       ├─ judge       ─▶ grade against the cluster
                       └─ tofu destroy ▶ tear it all down
```

You SSH into the VM over IAP. The VM runs as a dedicated service account, `openclaw-vm-sa` by default, and authenticates with **Application Default Credentials pulled from the metadata server**, so there are no key files on disk. When the agent's `kubectl` or `gcloud` make calls, they act as the VM's service account through that same ADC.

> [!WARNING]
> The prebuilt stack grants the VM service account broad, owner-equivalent provisioning roles so the harness can create and destroy clusters, secrets, and IAM bindings. Use sandbox or non-production projects only. The reusable module itself grants nothing by default (`sa_roles = []`).

### Access

SSH ingress is locked to the **IAP TCP-forwarding range** (`35.235.240.0/20`), scoped to the VM's network tag. You reach the VM by tunnelling through IAP:

```bash
gcloud compute ssh bench-bastion --zone us-central1-a --project <your-project> --tunnel-through-iap
```

The stack's `iap_ssh_command` output prints this line pre-filled with your VM name, zone, and project.

If your environment provides a directly routable hostname for the VM, the bastion scripts can use plain `ssh`/`scp` instead of the `gcloud` tunnel. Set `BASTION_SSH_HOST` (and `BASTION_SSH_USER` if it differs from your local username) and `scripts/bastion/*` switch transports; leave them unset for the IAP path.

Because eval workflows open many short-lived SSH/SCP connections (syncing code, polling a run, pulling results), set up **connection multiplexing** so they all reuse one tunnel instead of re-handshaking every time. Add a host entry to `~/.ssh/config`:

```ssh-config
Host bench-bastion
    HostName <routable-hostname>
    User <remote-user>
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h:%p
    ControlPersist 10m
```

Then connect with just `ssh bench-bastion`. The first connection opens a master socket; subsequent `ssh`/`scp` to the same host reuse it for up to 10 minutes of idle time (`ControlPersist`), which makes the matrix runner's repeated calls fast and avoids hammering the relay.

## How it is set up

Two OpenTofu pieces under `tf/` define the bastion:

| Path | Role |
| --- | --- |
| `tf/modules/bastion/` | Reusable module: the service account and IAM, the VM, the IAP-SSH firewall rule, and a first-boot `startup.sh` that installs the toolchain. |
| `tf/prebuilt/bastion/` | The concrete stack you apply. It opts the service account into the broad roles the eval stacks provision with. |

The first-boot `startup.sh` installs, system-wide: OpenTofu, Node.js 22+, the Google Cloud CLI with `gke-gcloud-auth-plugin` and `kubectl`, `openclaw` (linked on `PATH` as `oc`), `gke-mcp`, and `uv`. It is idempotent and marks completion with `/var/lib/bench-bastion-ready`; its log is `/var/log/bench-bastion-startup.log`.

Provision it:

```bash
cd tf/prebuilt/bastion
tofu init
tofu apply -var project_id=<your-project>
```

Outputs are `iap_ssh_command` (a ready-to-run IAP SSH command) and `sa_email` (the VM's service-account address).

Key variables and their defaults:

| Variable | Default | Notes |
| --- | --- | --- |
| `project_id` | _(required)_ | GCP project the bastion lives in. |
| `name` | `bench-bastion` | VM name; also names its firewall rule and network tag. |
| `zone` | `us-central1-a` | GCE zone. |
| `machine_type` | `e2-standard-4` | Headroom for tofu, node, and the harness. |
| `sa_account_id` | `openclaw-vm-sa` | Account id for the VM's service account. |
| `assign_external_ip` | `true` | Ephemeral external IP for egress (apt, npm, model APIs). SSH ingress stays IAP-only either way; set `false` only if the network has Cloud NAT. |

`tf/modules/bastion/` takes a few more (`boot_disk_gb`, `image`, `network`, `subnetwork`, `sa_roles`) if you consume the module directly.

## Per-user setup on the VM

Once the VM is provisioned, sync the repo up from your laptop and finish the user-scoped pieces once:

```bash
scripts/bastion/sync-to-bastion.sh          # from your laptop
~/devops-bench/scripts/bastion/vm-setup.sh  # on the VM
```

`vm-setup.sh` waits for the startup toolchain, then uses `uv` to create a `.venv` and install the harness from the lockfile (`uv sync --frozen`). It also checks or installs the per-user pieces the startup script cannot place system-wide: the Gemini CLI, `fortio` (the chaos load generator), a `node` symlink on the runner's non-login `PATH` (without it `oc` trajectory extraction exits 127 and yields empty trajectories), and the MCP server's skills clone that backs the agent's `+skills` capability. Finally it writes a `~/bench.env` template at mode `600`.

Then fill in `~/bench.env`, at minimum your **project** and the **judge key**, and load it:

```bash
source ~/bench.env
```

> [!NOTE]
> Per-run capabilities (the MCP server, agent skills, and the model/provider) are wired by the harness through environment variables at run time. You do not pre-configure them globally; leave that to the run. The one exception is the legacy arm, which reads them from the global `~/.openclaw` config; see [Parallel agent support](#parallel-agent-support).

## Pointing an eval at the bastion

SSH in, activate the environment, and run a task:

```bash
cd ~/devops-bench
source .venv/bin/activate
source ~/bench.env

# A single task:
devops-bench tasks/common/opa-remediation/task.yaml
```

`devops-bench` is the console script the package installs; `python -m devops_bench tasks/common/opa-remediation/task.yaml` runs the exact same entrypoint if you prefer the module form.

To drive a full matrix, use the matrix runner:

```bash
scripts/bastion/run_matrix.sh
```

By default the matrix runs every combo locally on the host you invoke it from. Set `BENCH_REMOTE=1` to sync from your laptop and run on the bastion over SSH instead. `DRY_RUN=1` previews the combos without launching anything. The full run-config env is documented in the header of `scripts/bastion/_matrix_lib.sh`.

### Parallel agent support

Each matrix combo runs as an isolated `--parallel` run with its own cluster, so combos can overlap (`MAX_PARALLEL`, default 3). Whether a given agent is safe to run concurrently depends on where it keeps its per-run state:

| Arm | Script | Dimensions | Concurrency |
| --- | --- | --- | --- |
| Refactored | `run_matrix.sh` | Task x Model x AgentConfig | Safe. Capabilities are passed per combo as env, so each run is self-contained. |
| Legacy | `run_matrix_legacy.sh` | Task x Model | OpenClaw only. |

The legacy arm is openclaw-only **by design, not just by default**. The legacy Gemini runner reads its trajectory from the shared `~/.gemini/tmp/.../chats` directory keyed by a short session id, which is not safe when runs overlap: concurrent runs can pick up each other's trajectories. For parallel Gemini use the refactored matrix with `MATRIX_AGENT_CONFIGS="gcli..."`.

The legacy arm also reads MCP and skills from the **global** `~/.openclaw` config rather than per-combo env, so capabilities are fixed for the whole matrix. Set them once with `scripts/bastion/configure-oc.sh --mcp --skills` (or `--no-*`) before launching it.

### Running against Vertex AI

Set `BENCH_VERTEX=1` to run agents and judges against Vertex AI using the bastion VM service account's ADC instead of the API-key endpoints. The runner unsets every API key from `secrets.env` and exports the `GOOGLE_GENAI_USE_VERTEXAI` / `GOOGLE_CLOUD_*` / `GCP_VERTEX_LOCATION` set (default location `global`; override `GOOGLE_CLOUD_LOCATION` or `GCP_VERTEX_LOCATION`). It also exports the ADC marker `GOOGLE_CLOUD_API_KEY=gcp-vertex-credentials`, which is what keeps auth portable across parallel runs' isolated state dirs. For the legacy `oc` arm also set `AGENT_PROVIDER=google-vertex`, which makes the model id `google-vertex/<model>`.

The legacy arm has a one-time prerequisite, because it authenticates from the global `~/.openclaw` config rather than per-run env. Register the provider there once:

```bash
scripts/bastion/configure-oc.sh --vertex
```

That writes oc's built-in `google-vertex` provider entries (`api`, `baseUrl`, models) into the global config and allowlists the models for the agent. Without `api: google-vertex` oc routes the provider through the OpenAI transport and requests fail with a 401. The refactored arm needs no such step: it resolves ADC from the metadata server at request time.

For failure modes and gotchas, see [Known issues](../appendix/known_issues.md). For how the bastion relates to the rest of the infrastructure layer, see [Infrastructure](infra.md).

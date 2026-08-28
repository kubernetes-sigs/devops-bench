# Running evals

This is the practical guide to running devops-bench evaluations — one task on one
model, or a whole matrix of tasks across models and agent configs.

There are two layers, and it helps to keep them straight:

- **The CLI** (`python -m devops_bench` / the `devops-bench` console script) runs
  **one task source in one process**: it loads tasks, optionally provisions a
  cluster, runs the agent, judges the result, and writes artifacts. This is the
  primitive.
- **The matrix wrapper** (`scripts/bastion/run_matrix.sh`) expands a
  **Task × Model × AgentConfig** matrix and launches **many isolated CLI
  processes** concurrently — each in its own cluster, with its own results.

A single eval is just a 1×1×1 matrix. Learn the CLI first; the matrix is the same
thing, fanned out.

---

## Prerequisites

Install the package and its development environment:

```bash
uv sync
```

The base install covers no-infra runs and the Gemini judge; the optional provider
SDKs are extras (`uv sync --extra anthropic --extra openai` — the `dev` dependency
group already includes both).

For runs against **real infrastructure** you also need the co-located toolchain:
`tofu` and `kubectl` always, plus whatever the target provider needs — `kind` for
local clusters, the cloud CLI and authenticated credentials for cloud stacks (see
[infrastructure](../components/infra.md)). CLI-driven harnesses additionally need
their agent binary on the PATH (`gemini` / `oc` / `agy`).

### Choosing the agent and model

The agent is selected with `--agent-type` (or `BENCH_AGENT_TYPE`); the model is
chosen by **environment variables** — there is no `--model` flag. The registered
harness keys are `gemini` (alias `gemini-cli`, the default), `openclaw`,
`antigravity`, and `api`. Pick the model with `AGENT_PROVIDER` / `AGENT_MODEL`;
`AGENT_API_KEY` supplies the credentials. The full `AGENT_*`
configuration surface — including the MCP and skills bindings
(`AGENT_MCP_SERVER`, `AGENT_SKILLS_PATHS`, `BENCH_USE_MCP`) — is documented in
[agents](../components/agents.md).

The judge is selected the same way, with `JUDGE_PROVIDER` / `JUDGE_MODEL` (or the
`--judge-provider` / `--judge-model` flags). With both unset, the judge inherits
`AGENT_PROVIDER` (defaulting to `google`) and the Gemini adapter falls back to
`AGENT_MODEL`, then to `gemini-3.1-pro-preview` (`devops_bench/models/gemini.py`). See
[model providers](../components/model_providers.md) for how provider keys and
API-key routing work, including the keyless Vertex/Bedrock backends that
authenticate via ambient credentials instead of an `AGENT_API_KEY`.

---

## Run a single eval (the CLI)

The CLI is the primitive everything else builds on. The shape is:

```bash
python -m devops_bench [flags] <source>
```

- **`source`** (positional, required) is either a **tasks directory** or a single
  **`task.yaml`** / `.yml` / `.json` spec file. A directory runs every task it
  finds; a single file runs just that task.
- **A project id and cluster name are required** — via `--project`/`--cluster` or
  the `PROJECT_ID`/`CLUSTER_NAME` env vars — unless you pass `--no-infra` (or set
  `BENCH_NO_INFRA=true`). With infra disabled, no cloud project or cluster is
  needed.

### Example: quick no-infra run

No cluster, fast feedback — good for smoke-testing a task spec or an agent config.
Provisioning is skipped and the agent's output is judged without a live cluster:

```bash
BENCH_NO_INFRA=true \
AGENT_PROVIDER=gemini AGENT_MODEL=gemini-3.1-pro-preview AGENT_API_KEY="$GEMINI_API_KEY" \
JUDGE_PROVIDER=gemini JUDGE_MODEL=gemini-3.1-pro-preview \
python -m devops_bench --no-infra tasks/common/opa-remediation/task.yaml
```

### Example: a real cluster, locally

`tasks/common/opa-remediation` pins `provider: kind`, so this provisions a local
kind cluster via OpenTofu, runs the agent against it, and tears it down. The
project id is not used for a local cluster but the flag is still required when
infra is on:

```bash
AGENT_PROVIDER=gemini AGENT_MODEL=gemini-3.1-pro-preview AGENT_API_KEY="$GEMINI_API_KEY" \
python -m devops_bench --project local-kind --cluster eval \
  tasks/common/opa-remediation/task.yaml
```

For cloud-provider stacks, the provider layer reads its own variables
(`GCP_PROJECT_ID`, `GCP_LOCATION`, `INFRA_PROVIDER`, …) — see
[infrastructure](../components/infra.md) and [`tf/README.md`](../../tf/README.md).
Note that `tasks/gcp/deploy-hello-app` is not runnable yet — the task does not
declare an explicit `provider:`; the [known issues appendix](../appendix/known_issues.md)
tracks this and other gaps.

### Per-run isolation (`--parallel`)

`--parallel` (or `BENCH_PARALLEL=true`) isolates a run so it can coexist with
others on one host: it gives the run its own kubeconfig, cloud CLI config, and tofu
data dir under `<tmp>/devops-bench-runs/<RUN_ID>` (override the root with
`BENCH_RUN_STATE_ROOT`), and derives a run-unique cluster name by prefixing the
configured name with a token — `c` plus 7 hex characters hashed from the run id
(`devops_bench/core/run_env.py`). `--run-id` sets the run id explicitly; the
default is the `RUN_ID` env var, then a generated timestamp-PID id.

---

## CLI flags

Flags override the environment; anything you don't pass falls back to its env var
(`devops_bench/cli.py`).

| Flag | Meaning |
|---|---|
| `source` (positional) | Tasks directory or a single `task.yaml` / `.yml` / `.json` spec. |
| `--project` | Cloud project id (required unless `--no-infra`). |
| `--cluster` | Target cluster name (required unless `--no-infra`). |
| `--limit N` | Run only the first N tasks from the source. |
| `--results-root DIR` | Root directory for run artifacts (default `results`). |
| `--agent-type` | Override `BENCH_AGENT_TYPE`. |
| `--judge-provider` | Override `JUDGE_PROVIDER`. |
| `--judge-model` | Override `JUDGE_MODEL`. |
| `--no-infra` / `--infra` | Skip / force infrastructure provisioning. |
| `--no-teardown` / `--teardown` | Skip / force teardown of provisioned infra. |
| `--parallel` | Isolate this run (own kubeconfig / cloud CLI config / tofu data dir + run-unique cluster name) so it can run concurrently with others. |
| `--run-id` | Explicit run id for artifact naming (default: `RUN_ID` env or a generated id). Isolation comes from `--parallel`, not from setting a run id. |

**Exit codes:**

| Code | Meaning |
|---|---|
| `0` | No task failed. |
| `1` | At least one task failed. |
| `2` | CLI usage or configuration error (e.g. invalid flags, or infra enabled but project/cluster missing). |

**Environment variables that affect a run.** These are the vendor-neutral names
`BenchmarkConfig.from_env` reads (`devops_bench/run.py`); each maps to a flag, and
flags win when both are set:

| Variable | Effect |
|---|---|
| `PROJECT_ID` | Cloud project id. |
| `CLUSTER_NAME` | Target cluster name. |
| `EVAL_LIMIT` | Cap the number of tasks run. |
| `RESULTS_ROOT` | Root directory for artifacts. |
| `BENCH_AGENT_TYPE` | Agent harness to run. |
| `JUDGE_PROVIDER` / `JUDGE_MODEL` | Judge model used to grade results. |
| `BENCH_NO_INFRA` | Skip provisioning when true. |
| `BENCH_NO_TEARDOWN` | Skip teardown when true. |
| `BENCH_PARALLEL` | Enable per-run isolation when true. |
| `RUN_ID` | Explicit run id for isolation / artifact naming. |

Provider-specific variables (e.g. `GCP_PROJECT_ID`, `GCP_LOCATION`,
`INFRA_PROVIDER`) are deliberately not read here — they are resolved by the
provider and deployer layers directly (see
[infrastructure](../components/infra.md)). The agent-side `AGENT_*` variables are
covered in [agents](../components/agents.md).

---

## Run a matrix (parallel evals)

This is where the wrapper earns its keep. A **combo** is one
`(task, model, agent-config)` triple. The matrix is the Cartesian product:

```text
MATRIX_TASKS × MATRIX_MODELS × MATRIX_AGENT_CONFIGS
```

capped at `MAX_PARALLEL` running at once. With the default arm, each combo runs
as an isolated `--parallel` CLI process with its own run id; the `legacy` arm
instead drives an external evaluator checkout (`pkg/`, not in this repo) and
copies its artifacts flat into the combo directory rather than the nested
layout below.

> [!IMPORTANT]
> **Each combo provisions and tears down its own cluster** and writes its own
> results. Combos share no mutable state — that's what makes the matrix safe to
> run wide. Mind your quota: `MAX_PARALLEL` is also your concurrent-cluster count.

The launcher stages a runner script, starts it detached under `nohup`, polls for
a `.done` marker, and summarizes the results. If your terminal or SSH session
drops, **the run keeps going** — the wrapper prints a `STAMP` on launch; re-run
the same command with `RESUME_STAMP=<stamp>` to re-attach and pull results.

By default everything runs **locally on this host**, with outputs under
`~/matrix-runs/<stamp>/`. Set `BENCH_REMOTE=1` to instead sync the working tree to
a remote runner VM ("bastion") over SSH, run there, and pull results back to
`RESULTS_DIR/<stamp>`. The bastion tooling lives beside the wrapper:
`scripts/bastion/sync-to-bastion.sh`, `scripts/bastion/vm-setup.sh`, and the
`tf/modules/bastion` stack. These scripts currently target one cloud's tooling
(`gcloud compute ssh` with IAP tunneling by default, or direct SSH via
`BASTION_SSH_HOST` / `BASTION_SSH_USER`). On the runner host, per-provider
secrets are sourced from `~/secrets.env`; never print or commit key values.

### Example

```bash
# Local (default): every combo runs on this host.
PROJECT_ID=<project> \
MATRIX_TASKS="tasks/common/opa-remediation/task.yaml" \
MATRIX_MODELS="gemini-3.1-pro gemini-3.5-flash" \
MATRIX_AGENT_CONFIGS="gcli+mcp+skills oc+mcp+skills" \
MAX_PARALLEL=3 \
  scripts/bastion/run_matrix.sh

# Remote, on the bastion, keyless via the VM's ambient credentials:
BENCH_REMOTE=1 BASTION_VM=bench-bastion BASTION_PROJECT=<project> \
BENCH_VERTEX=1 AGENT_PROVIDER=google-vertex \
PROJECT_ID=<project> \
MATRIX_TASKS="tasks/common/opa-remediation/task.yaml" \
MATRIX_MODELS="gemini-3.1-pro-preview" \
MATRIX_AGENT_CONFIGS="oc+mcp+skills" \
  scripts/bastion/run_matrix.sh

# Re-attach after a dropped session (same env, plus the stamp it printed):
RESUME_STAMP=<stamp> BENCH_REMOTE=1 ... scripts/bastion/run_matrix.sh
```

### Matrix knobs

Defaults live in `scripts/bastion/_matrix_lib.sh` and `run_matrix.sh`.

| Variable | Meaning |
|---|---|
| `MATRIX_TASKS` | Space-separated `task.yaml` paths, or `ALL` to enumerate every task under `tasks/` (default `tasks/common/opa-remediation/task.yaml`). |
| `MATRIX_MODELS` | Space-separated model ids (default `gemini-3.1-pro`). |
| `MATRIX_AGENT_CONFIGS` | Agent-config presets, each `<oc\|gcli>[+mcp][+skills]` — `oc` = openclaw, `gcli` = gemini (default `oc+mcp+skills`). |
| `PROJECT_ID` | Cloud project id; required unless `DRY_RUN`. |
| `MAX_PARALLEL` | Max combos running at once (default 3). |
| `AGENT_TIMEOUT_SEC` | Per-agent-call timeout. The matrix default is 1200s; the bare harness default is 600s (`devops_bench/agents/config.py`). |
| `BENCH_VERTEX` | Unset every API key from `secrets.env` and export Vertex AI credentials via the runner host's ambient (ADC) credentials. Provider selection is unchanged — `AGENT_PROVIDER` / `JUDGE_PROVIDER` still choose the providers. |
| `BENCH_REMOTE` | Run on the bastion over SSH; unset runs every combo locally on this host. |
| `SKIP_SYNC` | Skip the working-tree sync to the bastion (after you've already synced once). |
| `DRY_RUN` | Print the expanded matrix + per-combo env without provisioning anything. |
| `RESUME_STAMP` | Skip launching; re-poll and pull an existing run by its stamp. |
| `RESULTS_DIR` | Where pulled results land on a remote run (default `results/matrix`). |
| `MCP_SERVER_BIN` | MCP server command handed to `+mcp` combos as `AGENT_MCP_SERVER` (e.g. `k8s-mcp`, or a provider-specific server such as `gke-mcp` when the cluster provider is GKE). |
| `SKILLS_PATHS` | Skills directories handed to `+skills` combos as `AGENT_SKILLS_PATHS` (default: none — no skills are loaded unless set). |

> [!TIP]
> Always `DRY_RUN=1` first. It prints every combo and its per-combo env without
> provisioning anything, so you confirm the combo count — and therefore the
> cluster count — before committing quota to a typo in `MATRIX_MODELS`.

The `run-eval` and `run-parallel-evals` skills, which orchestrate these runs end
to end, are landing in a separate PR alongside `.agents/references/running-evals.md`.

---

## Where results go & how to read them

**A bare CLI run** writes a single run directory under the results root:

```text
results/run_<YYYYMMDD_HHMMSS>_<suffix>/   # suffix = sanitized run id, or sub-second precision
├── results.json        # full per-task records: prompt, output, trajectory, reports, scores
├── rows.json           # flattened per-task summary rows (best-effort)
└── manifest.json       # run-level identity: setupId, model, harness, augmentation (best-effort)
```

**A matrix run** writes one directory per combo under its output root
(`~/matrix-runs/<stamp>/` locally, pulled to `RESULTS_DIR/<stamp>/` for remote
runs):

```text
<out>/<combo>/
├── status              # "exit=<rc>" once the combo finishes
├── run.log             # full stdout/stderr for the combo
└── run_<ts>_<rid>/     # the combo's own CLI run directory, as above
```

`results.json` is a **list of per-task records**. Each record carries the
substituted prompt (`input`), the agent's `output` and `trajectory`, token and
latency telemetry, the chaos / verification reports, a `status` of `"success"` or
`"failed"`, and a `scores` map keyed by metric name. For what those score keys
mean and how the composite `OutcomeScore` is assembled, see
[metrics](../components/metrics.md) — that page is the source of truth for
scoring; nothing here duplicates it.

## Aggregating matrix results

The matrix runs **one task per process**, so each combo emits its own `rows.json`
with a unique run id. To combine the per-task rows into a single batch sharing one
run id:

```bash
python -m devops_bench.results.aggregate <results-root> -o <results-root>
```

This scans the tree for per-task `rows.json` files, de-duplicates retried tasks
(latest wins), stamps one shared batch run id and timestamp across every row, and
writes a combined `rows.json` plus per-setup `manifests.json`. Pass
`--run-id <id>` to reuse the matrix's own id instead of a generated one.

---

## When something fails

Exit `1` means *some task failed*, for any reason — inspect the combo's `status`,
`run.log`, and `results.json` before acting. Most failures during parallel runs
are **infra flakes** — a transient API error, a node-pool timeout, a leftover
resource that `409`s — and when the evidence shows one, the right response is to
clean up and retry. A router of concrete symptoms mapped to fixes lives
in the [known issues appendix](../appendix/known_issues.md). **Start there** when a
run misbehaves.

> [!WARNING]
> Before re-running anything, clean up stale per-run state (under
> `<tmp>/devops-bench-runs/`) and orphaned cloud resources left by aborted runs.
> A skipped cleanup is the single most common cause of a re-run failing the same
> way the first one did. The `cleanup-orphaned-resources` skill
> (`.agents/skills/cleanup-orphaned-resources/`) automates the sweep.

---

See also: [metrics](../components/metrics.md) ·
[infrastructure](../components/infra.md) · [agents](../components/agents.md) ·
[add a task](./add-a-task.md) · [project README](../../README.md).

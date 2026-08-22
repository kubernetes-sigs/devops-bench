# Running evals — shared run mechanics

The single home for the run mechanics that `run-eval` and `run-parallel-evals`
reuse: where to run, how to authenticate, how to come up clean, how to launch
detached, the matrix knobs, and where results land. The skills link here instead
of repeating it.

This reference is **agent-agnostic** — it describes capabilities, not specific
tools. For the per-agent mapping (sub-agents, background runs, timers, durable
state, worktrees, asking the operator) see
[`harness-capabilities.md`](./harness-capabilities.md). For the failure router
see [`../../docs/appendix/known_issues.md`](../../docs/appendix/known_issues.md);
for how scores are computed and read, see
[`../../docs/components/metrics.md`](../../docs/components/metrics.md) — this
file does not duplicate either.

---

## Choosing where to run

The matrix runs on the **runner host** — the machine where you invoke the
wrapper. **Local is the default** (`nohup` on this host, no ssh/sync, outputs in
`~/matrix-runs/<stamp>`). Set **`BENCH_REMOTE=1`** to sync the working tree to
the **bastion** (a GCP VM, `tf/modules/bastion`) and run there over ssh, pulling
results back. The snippets below show the bare command; in remote mode they run
under the wrapper's ssh transport, so the same paths (`~/secrets.env`,
`~/matrix-runs/<stamp>`) live on the VM.

**Bastion connection env (remote mode only).** By default the wrapper connects
with `gcloud compute ssh --tunnel-through-iap`:

```bash
export BASTION_VM=<your-vm> BASTION_ZONE=us-central1-a BASTION_PROJECT=<proj>
```

Set `BASTION_SSH_HOST` (and optionally `BASTION_SSH_USER`) instead to use a
plain `ssh`/`scp` transport, bypassing the IAP tunnel.

> [!IMPORTANT]
> **Get the bastion's identity from Terraform — don't assume the defaults**
> (`bench-bastion` / `us-central1-a` / gcloud's active project). The bastion is
> provisioned from `tf/modules/bastion`, which exports an `iap_ssh_command`
> output — `tofu output iap_ssh_command` in the root module where you
> instantiated it prints the exact
> `gcloud compute ssh <name> --zone <zone> --project <project>` form. Set
> `BASTION_VM` / `BASTION_ZONE` / `BASTION_PROJECT` from that, and verify the
> host is up before blaming credentials for a connection failure. Use
> `REMOTE_DIR=devops-bench-<label>` to avoid clobbering another session's
> checkout on the VM.

---

## Authentication

Pick one mode (recommend **Vertex/ADC** — no key handling, and the only mode
that stays portable across the isolated per-run state dirs parallel runs
create):

- **Vertex / ADC** — set `BENCH_VERTEX=1` and **no API keys**. The runner unsets
  every API key `~/secrets.env` exported (`AGENT_API_KEY`, `GEMINI_API_KEY`,
  `GOOGLE_API_KEY`, `JUDGE_API_KEY`, `GOOGLE_GENAI_API_KEY`) so agents and
  judges fall back to the runner host's ADC (on the bastion, the VM service
  account), then points everything at Vertex: `GOOGLE_GENAI_USE_VERTEXAI=true`,
  `GOOGLE_CLOUD_PROJECT` / `GCP_PROJECT_ID`, and location **`global`** via
  `GOOGLE_CLOUD_LOCATION` / `GCP_VERTEX_LOCATION`. It also exports the portable
  OpenClaw ADC marker `GOOGLE_CLOUD_API_KEY=gcp-vertex-credentials` (see the
  router in [known_issues.md](../../docs/appendix/known_issues.md)). For the
  legacy oc arm additionally set `AGENT_PROVIDER=google-vertex` so the model id
  becomes `google-vertex/<model>`.
- **API keys** — the runner sources `~/secrets.env` on the runner host when
  present (`set -a`, so plain assignments export). Put the keys your providers
  need there — `AGENT_API_KEY` for the agent contract, `GEMINI_API_KEY` /
  `GOOGLE_API_KEY` for the google model provider (which also serves the judge).
  Check names only — **never print key values**; ask the operator for a missing
  key rather than guessing.

**Judge.** The wrapper defaults `JUDGE_PROVIDER=google` and
`JUDGE_MODEL=gemini-3.1-pro`. On Vertex, keep the location `global` and use a
`-preview` model id — set `JUDGE_MODEL=gemini-3.1-pro-preview` — because
`gemini-3.x` previews 404 on regional endpoints (see the `404 Publisher model`
row in [known_issues.md](../../docs/appendix/known_issues.md)). Note the two
location variables are read by different layers: `GCP_VERTEX_LOCATION` by the
models layer (`devops_bench/models/gemini.py`, `models/claude.py`),
`GOOGLE_CLOUD_LOCATION` by the antigravity agent.

---

## Clean-environment pre-flight

Stale per-run state and orphaned cloud resources are the most common cause of a
"fresh" run failing instantly. Before **every** launch or retry, work the
**"Before any retry" checklist** in
[`known_issues.md`](../../docs/appendix/known_issues.md) — don't re-derive it
here, and keep it **scoped to the run you are retrying**: runs execute
concurrently on a shared host, so unscoped wipes (`rm -rf
/tmp/devops-bench-runs/*`, deleting every kind cluster, a bare `pkill`) take out
sibling runs.

For orphaned **cloud** resources (clusters, `gke-nodes-*` service accounts,
leaked secrets), use the
[`cleanup-orphaned-resources`](../skills/cleanup-orphaned-resources/SKILL.md)
skill rather than re-listing them inline.

---

## Launching (detached)

`scripts/bastion/run_matrix.sh` (refactored arm: Task × Model × AgentConfig)
runs the matrix **detached under `nohup`** (staged as
`~/.matrix-runner-<stamp>.sh`, log at `~/matrix-runs/<stamp>.out`), polls for
the `~/matrix-runs/<stamp>/.done` marker, and pulls results in remote mode. It
prints a `STAMP` (`<YYYYmmdd_HHMMSS>-<pid>`) on launch — record
`RESUME_STAMP=<stamp>` in durable state; it is your handle for monitoring,
retry, and re-attach. If your poller dies the detached run keeps going —
re-attach with `RESUME_STAMP=<stamp>` and the same command.

`run_matrix_legacy.sh` (legacy arm: Task × Model, oc-only) shares the same
wrapper mechanics, but drives the legacy evaluator entrypoint
(`pkg/evaluator/evaluate.py`), which is **not part of this repository** — it
only works if you place a legacy evaluator checkout at `pkg/` yourself
(`sync-to-bastion.sh` ships that path when present and silently skips it
otherwise). Its MCP/skills capabilities come from the global oc config, set
once with `scripts/bastion/configure-oc.sh`.

**Always `DRY_RUN=1` first** — it prints the expanded matrix + per-combo env
without provisioning (and without requiring `PROJECT_ID`), so a typo in
`MATRIX_MODELS` costs nothing instead of clusters.

Example (Vertex, refactored arm; prefix `BENCH_REMOTE=1` + the `BASTION_*` env
for remote):

```bash
PROJECT_ID=<proj> BENCH_VERTEX=1 \
JUDGE_MODEL=gemini-3.1-pro-preview \
MAX_PARALLEL=3 MATRIX_TASKS="tasks/common/opa-remediation/task.yaml" \
MATRIX_MODELS="gemini-3.1-pro-preview gemini-3.5-flash" \
MATRIX_AGENT_CONFIGS="gcli+mcp+skills oc+mcp+skills" \
RESULTS_DIR="results/<label>" \
  scripts/bastion/run_matrix.sh
```

To run **both arms in parallel** (when you have the legacy evaluator on hand):
sync once, then start each wrapper with `SKIP_SYNC=1` — the stamps carry the
wrapper's PID, so parallel launches can't collide on state.

---

## Run identity

Under `--parallel` (the matrix always sets it via `BENCH_PARALLEL=true`), each
run is isolated by `RunEnv` (`devops_bench/core/run_env.py`):

- **Run id** — `RUN_ID` env if set (the matrix sets it to the combo's `rid`),
  else `<YYYYmmdd-HHMMSS>-<pid>`. The matrix derives `rid` from the task's
  **directory basename**, so never select two tasks whose directories share a
  basename in one matrix — they would collide on run id, cluster name, and
  output dir.
- **Per-run state** — `/tmp/devops-bench-runs/<RUN_ID>/` (override the root with
  `BENCH_RUN_STATE_ROOT`): kubeconfig, gcloud config, tofu data dir.
- **Cluster name** — `<token>-<CLUSTER_NAME>`, where the token is `c` + 7 hex
  chars derived from the run id. Deterministic: **the same combo always maps to
  the same cluster name**, which is why two runs of the *same* combo must never
  overlap. The GKE node SA is `gke-nodes-<slug:9>-<md5(cluster_name):6>`
  (`tf/modules/cluster/gke/main.tf`).

---

## Matrix knobs

| Variable | Meaning |
|---|---|
| `MATRIX_TASKS` | Space-separated `task.yaml` paths, or `ALL` to enumerate every task. Default `tasks/common/opa-remediation/task.yaml`. |
| `MATRIX_MODELS` | Space-separated model ids. Default `gemini-3.1-pro`. |
| `MATRIX_AGENT_CONFIGS` | Refactored arm only. Each `oc\|gcli` `[+mcp][+skills]` (e.g. `gcli+mcp+skills`). Default `oc+mcp+skills`. |
| `MAX_PARALLEL` | Max combos running at once (default `3`). Each combo is its own cluster — mind quota. |
| `PROJECT_ID` | Cloud project for the run. **Required** unless `DRY_RUN=1`. |
| `CLUSTER_NAME` | Base cluster name (default `eval`); per-run names are derived from it (see *Run identity*). |
| `AGENT_TIMEOUT_SEC` | Per-agent timeout (default `1200` in the matrix; the harness's own default of 600s is too low for infra-bearing tasks). |
| `BENCH_VERTEX` | Run agents + judges on Vertex via the runner host's ADC (no API keys). |
| `BENCH_REMOTE` | Run on the bastion over ssh; unset runs every combo locally. |
| `SKIP_SYNC` | Skip the working-tree sync to the bastion (after one real sync). |
| `BASTION_VM` / `BASTION_ZONE` / `BASTION_PROJECT` | Bastion identity for the IAP transport (defaults: `bench-bastion` / `us-central1-a` / gcloud's active project). |
| `BASTION_SSH_HOST` / `BASTION_SSH_USER` | Direct-ssh transport, bypassing the IAP tunnel. |
| `REMOTE_DIR` | Checkout dir on the VM (default `devops-bench`). Set a per-run value to avoid clobbering another session's checkout. |
| `RESULTS_DIR` | Where pulled results land in remote mode (default `results/matrix`). |
| `MCP_SERVER_BIN` | MCP server binary for `+mcp` combos (default `/usr/local/bin/gke-mcp`, where the bastion startup script installs it). |
| `SKILLS_PATHS` | Skills source for `+skills` combos (default `$HOME/mcp-skills/skills`, cloned by `vm-setup.sh`). |
| `DRY_RUN` | Print the expanded matrix + per-combo env without provisioning. |
| `RESUME_STAMP` | Skip launching; re-poll + pull an existing run by its stamp. |
| `MATRIX_POLL_TIMEOUT_SEC` | Give up polling after this long (default `86400`); the detached run itself is unaffected. |

---

## Where results land

Per combo on the runner host: `~/matrix-runs/<stamp>/<rid>/` with `status`
(`exit=<rc>` once finished), `run.log`, and a `run_<ts>_<rid>/` subdirectory
holding `results.json` (the judged per-criterion scores), `rows.json`, and
`manifest.json`. In remote mode the whole stamped dir is pulled back to
`RESULTS_DIR/<stamp>/`. A bare CLI run (`python -m devops_bench <task>`)
instead writes a single timestamped `run_<ts>_…/` directory under
`RESULTS_ROOT` (default `results/`) — suffixed with the run id when one is set,
else with sub-second precision.

For how scoring works and how to read it, see
[`../../docs/components/metrics.md`](../../docs/components/metrics.md).

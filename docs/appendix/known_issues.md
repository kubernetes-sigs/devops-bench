# Known issues, recovery & workarounds

Failure recovery and deliberate workarounds for the eval pipeline (`python -m devops_bench`, `devops_bench/`). Section 1 is a recovery router: match your symptom, apply the action. Section 2 catalogues the hacks currently in the code path and what would let us remove them.

> [!NOTE]
> This page is migrated in step with the code. Rows that describe the bastion matrix runner are held back until `scripts/bastion/` and the bastion docs land, so the router covers single-host runs today and will grow.

## Section 1 — Issue router (recover from eval failures)

If an eval fails, find the symptom below and apply the action. Many failures are transient infrastructure flakes — those are marked **Infra flake — retry** and should simply be retried (after cleaning stale state), not debugged.

> [!TIP]
> **Class** tells you what to do without reading the row. `Infra flake — retry`: re-run the combo after the *Before any retry* cleanup; do **not** open the code. `Config / auth`: a credential or settings fix is required before it will ever pass. `Setup`: a one-time host or cloud-project prerequisite is missing.

| Symptom / error signature | Root cause | Fix / recovery action | Class | Resolved |
|---|---|---|---|---|
| `ConfigError: TF stack not found under <tf_root>: prebuilt/minimum`, raised before any cluster is created | `tasks/gcp/deploy-hello-app` declares `stack: "prebuilt/minimum"`, but `tf/prebuilt/minimum/` is not in this repo yet — the task migrated ahead of its stack | Until it lands, run a task whose stack exists (`tasks/common/opa-remediation`, or any `deployer: noop` task) | **Setup** | Pending #64 |
| `stack 'prebuilt/minimum' requires an explicit provider`, raised before any cluster is created | The same task carries no `provider:`. Deduction fires only for an in-repo stack whose last path segment is exactly `kind`, and never falls back to `gcp` | Until it lands, export `INFRA_PROVIDER=gcp` for the run | **Setup** | Pending #70 |
| Kyverno setup fails with `PolicyReports never showed failing results for all seeded workloads`, however long it waits | The readiness gate read the report subject from `results[].resources`, which Kyverno 1.12 does not populate — the subject is named in the report's top-level `scope`. The pair set was therefore always empty and the gate could never pass | No workaround — the gate cannot pass without the fix | **Setup** | Pending #69 |
| `Vertex AI API error (429): Resource exhausted` (`RESOURCE_EXHAUSTED`), agent run ends mid-trajectory | Transient Vertex per-minute quota on long, high-token agentic runs | Not a model miss — **retry the combo**. If it recurs, lower the parallelism or raise the Vertex quota | **Infra flake — retry** | No |
| GCP `409 already exists` on cluster (re)create, naming a `gke-nodes-*` service account | Historic: the node SA name had no discriminator, so a failed teardown's leftover blocked the next run | Fixed — `tf/modules/cluster/gke` now appends `md5(cluster_name)[:6]` to `account_id`. If seen, you are on stale TF; sync and retry | **Setup** | Yes |
| Task fails in ~2 min at `tofu plan`: `could not locate any control plane nodes for cluster '<cluster>'` | A prior run's per-run state under `/tmp/devops-bench-runs/<RUN_ID>` is reused and references an already-torn-down cluster | **Wipe that run's state before re-running**: `rm -rf /tmp/devops-bench-runs/<RUN_ID>` plus the kind cleanup in *Before any retry*, then retry. Do not wipe the whole directory on a shared host — it belongs to every concurrent run | **Setup** | No |
| `gemini subprocess error: ... exit code -1` | This is a **timeout, not a crash** — `core.subprocess.run` returns `-1` on `TimeoutExpired` (usually an MCP approval hang) | Fix the *hang* (set `--approval-mode yolo` + folder trust below) rather than just raising `AGENT_TIMEOUT_SEC`; only raise the timeout after | **Config / auth** | No |
| gemini `mcp list` shows server `Disabled`; model writes its own MCP client; or run hangs to timeout with MCP configured (`--skip-trust` alone insufficient) | Untrusted per-run cwd suppresses MCP, **and** with no approval mode MCP calls block on interactive confirmation | Needs **both**: set `security.folderTrust.enabled=false` in user-level `~/.gemini/settings.json`, **and** pass `--approval-mode yolo` in argv. Both are broad safety bypasses — folder trust is disabled for every Gemini CLI session under that account, and yolo auto-approves every tool call — so apply them only to a dedicated, isolated runner account, never an interactive workstation | **Config / auth** | No |
| oc on Vertex: `No API key found for provider "google-vertex"` under parallel runs | The ADC marker lives only in the global sqlite auth store; an isolated `OPENCLAW_STATE_DIR` can't see it | Export `GOOGLE_CLOUD_API_KEY=gcp-vertex-credentials`, the portable env marker | **Config / auth** | No |
| oc on Vertex: `401 Incorrect API key` (request sent to `platform.openai.com`) | The per-run provider entry **replaces** the built-in one and is missing the Vertex transport, so oc falls back to the OpenAI transport | Run on current code — the harness writes a per-run `openclaw.json` pinning `"api": "google-vertex"` (+ `"baseUrl"`); combine with the ADC marker above. If seen, you are on stale code — sync/reinstall | **Config / auth** | No |
| Vertex `404 Publisher model ... not found`; judge silently fails / 404s | Wrong location or non-`-preview` model id — `gemini-3.x` previews 404 on regional endpoints | Use the **`global`** location (`GOOGLE_CLOUD_LOCATION=global` / `GCP_VERTEX_LOCATION=global`) and a `-preview` model id; the judge default needs `JUDGE_MODEL=gemini-3.1-pro-preview` | **Config / auth** | No |
| GKE task: `Error 403: <API> has not been used in project … or it is disabled` | A required GCP API isn't enabled in the eval project | `gcloud services enable <api>.googleapis.com --project=<eval-project-id>` (pass the project explicitly rather than relying on the host's active config, and confirm it is the eval project first), wait a few min to propagate, then retry | **Setup** | No |
| Multi-node kind task fails: `failed to join node with kubeadm … exit status 1` | Host `fs.inotify.max_user_instances` (default 128) exhausted by a multi-node cluster | `sudo sysctl -w fs.inotify.max_user_instances=1280 fs.inotify.max_user_watches=1048576` (persist in `/etc/sysctl.d/`), then retry | **Setup** | No |
| kind task fails instantly: `docker: executable file not found in $PATH` | Docker not installed / socket missing on the host (kind tasks run on the host) | Install `docker.io` and `acl` (for `setfacl`), start the daemon, then grant socket access to a **dedicated** runner account (`sudo setfacl -m u:devops-bench-runner:rw /var/run/docker.sock`). Docker recreates the socket on daemon restart and the ACL is lost, so make it persistent with a `docker.socket` systemd override, or by adding the runner to the `docker` group, rather than reapplying an ACL by hand. Docker socket write access and `docker` group membership are root-equivalent on the host, so never grant them to a shared or interactive account — run kind tasks under a named runner on a trusted, isolated host | **Setup** | No |
| Chaos `generate_load` injects nothing; HPA never scales (load is a silent no-op) | `fortio` is not on `PATH` — it is installed at provision time, not baked into any image | Install `fortio` onto the runner's `PATH`, then retry | **Setup** | No |
| Standalone test on a remote host sees **stale code** (e.g. a flag still showing its pre-fix value) | The host venv has an *installed* `devops_bench`; `python3 /tmp/x.py` imports the package, not the synced source | Run with `PYTHONPATH=<repo>`, or `python -m devops_bench` from the source dir | **Setup** | No |
| **All trajectories empty** (`trajectory: []`, `tools: []`), `ToolInvocation` 0.0 — though the agent clearly acted; run log shows `oc sessions exited 127: /usr/bin/env: 'node': No such file or directory` | The `oc sessions` / `export-trajectory` extraction runs `oc` as a **direct argv subprocess** (no nvm sourced), so on an nvm-managed host Node isn't on `PATH` → exit 127 → trajectory **silently emptied** → every tool/checklist check fails | Put Node on the runner `PATH`. Load nvm first (`. "$NVM_DIR/nvm.sh"`) or set the binary path explicitly, since `command -v node` is empty when the current shell lacks it too, then `mkdir -p "$HOME/bin" && ln -sf "$NODE_BIN" "$HOME/bin/node" && export PATH="$HOME/bin:$PATH"` — creating the symlink alone does nothing if `~/bin` is absent or not on `PATH`). Current code also prepends the nvm Node dir for these calls (`_ensure_node_on_path` in `devops_bench/agents/cli/openclaw/agent.py`); if seen, sync/reinstall | **Config / setup** | No |
| Verification records `budget exhausted` for most objectives and the score looks confidently low | The post-run pass shares a total wall-clock budget across converging entries; an entry that polls to its own cap can starve the ones after it | Check `VerificationCoverage` before trusting `VerificationCorrectness` — an abandoned entry leaves both the numerator and the denominator, so a low score computed over a handful of entries reads the same as a real failure | **Infra flake — retry** | No |

After applying a fix, retry the run. For any infra-flake row, run the cleanup below first.

### Before any retry

> [!IMPORTANT]
> Stale run state and orphaned cloud resources are the most common cause of a "fresh" run failing instantly. Clean them before every (re)launch — but scope every command to the run you are retrying. Runs may execute concurrently on a shared host, so the unscoped forms (`rm -rf /tmp/devops-bench-runs/*`, deleting every kind cluster, a bare `pkill -f devops_bench`) will take out other runs.

On the host the run executed on:

```bash
# Fill these in from the failed run, then paste the block. Everything below is
# scoped to them: the bastion runs matrix combos concurrently, so an unscoped
# wipe takes out a sibling run that is still going.
RUN_ID="int_opa_remediation"        # the failed run's id
CLUSTER="cbd827e1-bench-opa"        # that run's cluster name
STATE_ROOT="${BENCH_RUN_STATE_ROOT:-/tmp/devops-bench-runs}"   # RunEnv honours this env var first

# Refuse anything that could escape the state root or resolve to the root itself.
case "$RUN_ID" in
  ""|*/*|*..*) echo "refusing unsafe RUN_ID: '$RUN_ID'" >&2; return 2 2>/dev/null || exit 2 ;;
esac
RUN_DIR="$STATE_ROOT/$RUN_ID"
echo "will remove: $RUN_DIR"          # eyeball this before continuing

# 1. Wipe only this run's scratch + state
rm -rf -- "$RUN_DIR"

# 2. Delete this run's kind cluster, then the node containers it labelled
kind get clusters | grep -qx "$CLUSTER" && kind delete cluster --name "$CLUSTER"
docker rm -f $(docker ps -aq --filter "label=io.x-k8s.kind.cluster=$CLUSTER") 2>/dev/null || true

# 3. Kill only this run's processes. RUN_ID is an environment prefix and never
#    appears in argv, so `pkill -f RUN_ID=...` matches nothing. Match the run
#    directory, which the harness does pass on the command line, and confirm
#    the list before killing.
pgrep -af -- "$RUN_DIR"
pkill -f -- "$RUN_DIR" 2>/dev/null || true
```

Then delete orphaned cloud resources left by a failed teardown:

- **Clusters** — any run-scoped GKE clusters from a crashed run; `RunEnv` names them `c<blake2s digest of the run id>`.
- **`gke-nodes-*` service accounts** — no longer a collision source (the name carries an md5 discriminator), but still stranded by a failed teardown.
- **Task-specific leftovers** — Artifact Registry repos, Cloud SQL instances (note the ~1-week name tombstone), and orphan auto-mode VPCs.

The [`cleanup-orphaned-resources`](../../.agents/skills/cleanup-orphaned-resources/SKILL.md) skill walks this end to end, in list-then-confirm order.

## Section 2 — Known hacks & workarounds

Deliberate workarounds currently in the code path. Each notes what it does and what would let us remove it.

| What | Where (file / area) | Why | Removal condition | Resolved |
|---|---|---|---|---|
| **Per-run tofu isolation** copies the whole `tf/` tree into `<run_dir>/tf/` and writes state to `<run_dir>/terraform.tfstate` | `devops_bench/deployers/tofu.py` (`_isolated_work_dir`); `devops_bench/core/run_env.py` (`TF_DATA_DIR`) | Stacks use relative module sources, and concurrent runs would otherwise contend on a shared `.terraform.lock.hcl` + state in `tf/prebuilt/<stack>` | Module sources made run-relocatable without a full-tree copy (this is the principled isolation fix, not pure debt — low priority) | No |
| **Stale-state manual pre-flight wipe** (`rm -rf /tmp/devops-bench-runs/<RUN_ID>` + that run's kind cleanup) | Operator step | The per-run state dir is keyed by `RUN_ID`; a prior run's state references a deleted cluster and is reused, failing at `tofu plan` | `RunEnv` (or a `devops-bench clean` subcommand) self-detects dangling state and re-inits instead of relying on a human `rm -rf` | No |
| **Kyverno/OPA admission-webhook retry loop** (bounded retry on policy apply) | `tf/prebuilt/opa-remediation/scripts/setup.sh` | The Kyverno webhook can take seconds to start serving after the deployment is Available; applying too early fails with `context deadline exceeded` | Poll the `Validating`/`MutatingWebhookConfiguration` (or the service endpoint) readiness instead of a fixed-attempt sleep loop | No |
| **MCP tool-name normalization** strips the `<server>__` prefix before matching | `devops_bench/metrics/pipeline.py` (`_canonical_tool_name`) | MCP tools surface as `<server>__<tool>`; without stripping, `bash` vs `default__bash` scored 0 on tool-invocation | Strip the prefix only against the *known* set of configured MCP server names (from the agent config / `capabilities_granted`), not a blind `split("__")` that would also truncate a legit `my__tool` | No |
| **KUBECONFIG explicitly passed to MCP server processes** (per-agent, per-spawn) | `devops_bench/agents/cli/openclaw/agent.py` | `RunEnv` sets `KUBECONFIG` in process env, but MCP server spawn didn't inherit it, so MCP servers used the ambient `~/.kube/config` and mixed cluster targets under parallelism | Centralize MCP-server-environment construction in one shared `agents/` helper that always derives env from `RunEnv`, so a new agent can't silently regress to ambient config | No |
| **`generation_only` inferred from the run** (`no_infra` **or** `deployer == "noop"`) to soften `OutcomeValidity` | `devops_bench/evalharness/default.py`; `devops_bench/metrics/pipeline.py` | Manifest-only runs never apply to a cluster, so they would score 0 on `OutcomeValidity`; the harness infers "generation-only" from the run flag and the deployer string | Make `generation_only` an explicit (or explicitly-derivable) `Task` field so a non-noop generation-only task isn't mis-judged and the metric layer needn't know deployer names | No |
| **`validated` flag defaults `false`** (gates leaderboard promotion only) | `devops_bench/tasks/schema.py`; `devops_bench/results/row.py`, `normalize.py` | Keeps unvetted tasks off the leaderboard until a human sets `validated: true` | The flag gates promotion but **not running** — an unvetted task still burns quota. Add a CI `validate-task` pass (schema + spec parse + unique id) and a `--require-validated` run mode | No |
| **Swallowed scoring / per-metric errors** (broad `except Exception` so an already-written `results.json` survives a judge crash) | `devops_bench/evalharness/default.py`; `devops_bench/metrics/pipeline.py` | A judge/GEval crash must not discard an already-completed execution record (raw `results.json` is written before scoring) | Keep the isolation but record a typed scoring-error sentinel in `scores` and surface a distinct exit/manifest flag, so unscored runs are visibly degraded rather than silently empty | No |

> [!NOTE]
> **Observability caveat.** `results.json` carries no token fields (tokens live only in `rows.json` / `manifest.json`), and a run that times out is killed before `oc sessions export-trajectory` runs, so the heaviest runs can persist `tokens: {}`. Treat token/cost figures as the clean-completion subset only until a durable per-turn token checkpoint lands.

---

See also: [infra.md](../components/infra.md) for how stacks are provisioned and torn down, which is the lifecycle most of the leaks above escape from.

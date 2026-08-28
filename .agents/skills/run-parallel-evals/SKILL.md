---
name: run-parallel-evals
description: Run a Task × Model × AgentConfig MATRIX of evals in parallel — local or on the bastion, with opt-in hands-off and unlimited/self-healing modes; invoke when the user wants a matrix, a comparison across models/configs, or to "run these evals in parallel" / "compare models on the same task".
---

# Run a parallel eval matrix

Orchestrate a **Task × Model × AgentConfig** matrix end to end: expand the combos,
launch them detached, monitor and retry flakes, then summarize and diagnose.

This file keeps only what's **unique to running many combos at once**. Everything
shared is in the references — read them, don't restate them:

- Local vs bastion, auth, clean pre-flight, launch, knobs, results →
  [`../../references/running-evals.md`](../../references/running-evals.md)
- Monitoring / keepalive / recovery →
  [`../../references/monitoring-and-recovery.md`](../../references/monitoring-and-recovery.md)
- Unlimited / self-healing loop →
  [`../../references/unlimited-mode.md`](../../references/unlimited-mode.md)
- Capability → tool mapping (sub-agents, background runs, timers, durable state,
  worktrees) → [`../../references/harness-capabilities.md`](../../references/harness-capabilities.md)
- Failure router →
  [`../../../docs/appendix/known_issues.md`](../../../docs/appendix/known_issues.md)
- Reading scores →
  [`../../../docs/components/metrics.md`](../../../docs/components/metrics.md)

**Single combo?** Use [`run-eval`](../run-eval/SKILL.md). **Explaining a low
score?** Use [`diagnose-eval-failure`](../diagnose-eval-failure/SKILL.md).

---

## Modes (opt-in)

| Mode | When | What it adds |
|---|---|---|
| **Standard** | default | You drive Phases 1–5 directly. |
| **Hands-off** | "must not stop", "monitor with subagents", long runs | Tiered-subagent monitoring + keepalive — [monitoring-and-recovery.md](../../references/monitoring-and-recovery.md). |
| **Unlimited / self-healing** | "keep going until it finishes", "auto-fix and restart" | Diagnose → fix → re-sync → restart failed combos, capped — [unlimited-mode.md](../../references/unlimited-mode.md). |

Resolve the mode in Phase 1; hands-off and unlimited are **explicit opt-in**.

---

## Phase 1 — Spec the matrix + combo math

Choose **local vs bastion** and **auth** ([running-evals.md](../../references/running-evals.md)),
then pin the matrix axes — ask the operator for anything not given:

1. **`MATRIX_TASKS`** — space-separated `task.yaml` paths, or `ALL`.
2. **`MATRIX_MODELS`** — space-separated model ids.
3. **`MATRIX_AGENT_CONFIGS`** — each `oc|gcli` `[+mcp][+skills]` (`oc` =
   OpenClaw, `gcli` = Gemini CLI).
4. **`MAX_PARALLEL`** — combos running at once (default 3).

**Combo count = tasks × models × configs.** State it plus the rough wall-clock
(tens of minutes per infra-bearing combo) so the operator can confirm scale
before you spend clusters. Then `DRY_RUN=1` to print the expanded matrix.

---

## Phase 2 — Cross-combo parallel-safety pre-flight

The one thing a matrix must get right that a single run needn't: combos run
**concurrently**, so shared state across combos is a hazard. Each combo provisions
and tears down **its own** cluster and writes its own results — the matrix is safe
only as long as that isolation holds.

- **Never run two of the *same* combo at once** — the run-id-derived cluster
  name is deterministic in the combo, so two identical runs would target the
  same cluster. Distinct combos are fine alongside each other.
- Pre-flight each selected task for per-run isolation gaps — parallel-safety is
  mandatory for tasks (`tasks/AGENTS.md`), and the
  [`task-review`](../task-review/SKILL.md) skill runs a thorough pass. The gaps
  that bite a *matrix* specifically:
  - a task that seeds a fixed, shared host path (e.g. a `$HOME` git fixture
    without the cluster name in it) and deletes it on re-run — unsafe once the
    matrix repeats the task across models/configs;
  - a stack that grants project-level IAM to a **shared** service account
    (teardown clobber across concurrent combos);
  - duplicate `task_id`s among the selected tasks (ambiguous reporting);
  - host capacity: sum kind clusters / disk / inotify limits across
    `MAX_PARALLEL` (see the inotify row in
    [known_issues.md](../../../docs/appendix/known_issues.md)).

A doomed combo wastes a cluster and half an hour, so catch these before launch.

---

## Phase 3 — Clean pre-flight + launch (detached)

Work the **"Before any retry" checklist** in
[known_issues.md](../../../docs/appendix/known_issues.md) before **every** launch —
stale per-run state and orphaned cloud resources are the top cause of a "fresh"
matrix failing instantly. Keep it scoped per run. For leaked clusters /
node service accounts (e.g. `gke-nodes-*`) / secrets, use the
[`cleanup-orphaned-resources`](../cleanup-orphaned-resources/SKILL.md) skill.

**Smoke-gate before the full matrix.** Launch **one cheap combo first**
(`tasks/common/opa-remediation` provisions on kind — no cloud cluster) and
verify its `results.json` has a **non-empty `trajectory`** (and `tools`
populated for tool-using tasks), not just `exit=0`. An empty trajectory on a
task the agent clearly acted on is a *silent* capture failure that scores still
"succeed" through — it deflates every tool/checklist score (e.g. the
`oc sessions` Node-not-on-PATH row in
[known_issues.md](../../../docs/appendix/known_issues.md)). **Abort and fix
before spending the full matrix** rather than discovering it across N invalid
runs.

Then launch per [running-evals.md](../../references/running-evals.md): build the
env from Phase 1, run the wrapper as a background job, and **capture each
`STAMP`** (`RESUME_STAMP=<stamp>`) plus the combo list in durable state. To run
two wrappers in parallel, sync once then start each with `SKIP_SYNC=1`. If your
poller dies the detached run continues — re-attach with `RESUME_STAMP`.

---

## Phase 4 — Monitor + retry flakes

Follow [monitoring-and-recovery.md](../../references/monitoring-and-recovery.md):
poll each combo's `status` + `run.log` on an interval (~3–5 min for infra-bearing
tasks — **don't busy-poll**); under hands-off mode delegate polling to a cheap
watcher and per-finish analysis to a mid tier.

Classify each combo against the router in
[known_issues.md](../../../docs/appendix/known_issues.md): **infra flake** → clean
+ retry that one combo without `RESUME_STAMP` — set, the wrapper attaches to the
failed run instead of launching — and record the new `STAMP` (cap 2 per combo;
log every retry — never silently drop a
combo); **real failure** (auth/config, low score, task-logic) → do not retry,
analyze in Phase 5. If the whole runner died, re-attach with `RESUME_STAMP` then
relaunch only the unfinished combos. For unlimited mode, a real task/code bug is
not the end — follow [unlimited-mode.md](../../references/unlimited-mode.md).

---

## Phase 5 — Summarize + diagnose

When every combo is terminal (or `.done` is present), pull results and report per
combo: **task · model · agent-config · auth-mode · exit · score ·
#MCP-tool-calls · pass/fail checks.** Read and interpret scores per
[metrics.md](../../../docs/components/metrics.md); results layout is in
[running-evals.md](../../references/running-evals.md).

**Aggregate for the dashboard:** the matrix runs one task per process, so combine
the per-task `rows.json` into one batch run before ingest:

```bash
python -m devops_bench.results.aggregate <results-root> -o <results-root>
```

For every non-passing combo, give: the decisive log line (redact secrets), the
**root cause** mapped to the router, whether it's **model vs harness** (clean
trajectory + low score = model; early abort / auth error = harness/config —
[`diagnose-eval-failure`](../diagnose-eval-failure/SKILL.md) covers the graded
half), and the concrete fix. Then **verify teardown is clean** and report any
residue.

End with: total combos, passed/failed counts, best performer, the headline score
table, and each failure's root cause + fix.

---

## Living known-issues

When a combo fails in a way **not** already in
[known_issues.md](../../../docs/appendix/known_issues.md), append a router row
matching the table's columns — **symptom → root cause → fix/recovery → class →
resolved** — so the next run benefits. Terse, no duplication of existing rows,
and never paste model scores or run tallies into the doc. This capture step is
part of the run, not optional.

---

## Guardrails

- Each infra-bearing combo costs a real cluster plus tens of minutes — confirm
  the combo count and `DRY_RUN=1` first; mind project quota across
  `MAX_PARALLEL`.
- Never print or commit API keys; redact secrets in summaries.
- Always confirm clean teardown — leftover clusters / node service accounts (e.g. `gke-nodes-*`) make a
  retry of the same combo fail with `409 already exists`.
- Cap retries (≤2/combo) and surface anything still failing rather than looping.
- Prefer leaving the run detached + re-attaching via `RESUME_STAMP` over a fragile
  foreground session.
- **Hands-off run:** never emit a completion signal until *every* combo is
  terminal **and** summarized; emit a periodic heartbeat instead.
- **Cluster-mutation blast radius (with-mcp + a broadly privileged SA).** When
  the runner's service account has broad rights, a cluster-aware MCP server
  (e.g. `gke-mcp` when the cluster provider is GKE) exposes every real cluster in the project as
  a writable target — an agent can start a cluster update/upgrade through the
  provider CLI against a cluster the eval never provisioned, a long-running
  op with no agent timeout, so the run **hangs** (no score) and may mutate an
  unrelated cluster. Mitigate: run in a project with **no other clusters**, or
  watch the logs for cluster mutation commands (e.g. `clusters update|upgrade|delete`) and kill the offending
  process. Don't auto-revert a change to a cluster you don't own.

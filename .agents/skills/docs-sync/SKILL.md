---
name: docs-sync
description: Keep the docs current after a code change — map changed code areas to the docs that describe them, update those docs in place, and resolve known-issues rows the code has verifiably fixed. Invoke after editing the pipeline, adding a model/agent/task/metric, moving a directory, or whenever someone asks to "sync the docs", "update the docs for this change", or "do the docs still match the code?".
---

# Sync docs to a code change

Code moved; the docs should move with it. This skill takes a change set and walks
it back to the docs that describe the touched areas, updates those docs, and
resolves stale recovery rows the code now fixes. It edits prose only — it does
not change behavior.

Follow the docs' existing conventions while editing: GFM, humanized (not
journal-like or changelog-y), minimal, no contradictions or duplication, and
**no model scores or result tallies** in any doc.

- Codebase tree (what lives where) → Part B of
  [`../../../docs/components/glossary.md`](../../../docs/components/glossary.md)
- Capability → tool mapping for your harness →
  [`../../references/harness-capabilities.md`](../../references/harness-capabilities.md)

---

## Flow

### 1. Get the change set

Take the diff or the list of changed files (e.g. `git diff --name-only main...`,
or the working tree). Group the changes by area — a model provider, a deployer, an
agent harness, the CLI, metrics, a task, the bastion scripts, a directory move.

### 2. Learn where things are documented

Tree-walk `docs/` (`components/`, `how-to/`, `appendix/`) and the directory
structure in [`glossary.md`](../../../docs/components/glossary.md) Part B. Also
read the nearest `AGENTS.md` to the changed code — the root
[`AGENTS.md`](../../../AGENTS.md) or
[`tasks/AGENTS.md`](../../../tasks/AGENTS.md) — since those route contributors
and often need the same edit as the docs.

### 3. Map changed area → affected docs

| Changed code area | Docs to update |
|---|---|
| Model provider (`devops_bench/models/`) | [`model_providers.md`](../../../docs/components/model_providers.md) + [`add-a-model-provider.md`](../../../docs/how-to/add-a-model-provider.md) |
| Deployer / cloud provider (`devops_bench/deployers/`, `devops_bench/providers/`, `tf/`) | [`infra.md`](../../../docs/components/infra.md) + [`tf/README.md`](../../../tf/README.md) |
| Agent harness (`devops_bench/agents/`) | [`agents.md`](../../../docs/components/agents.md) + [`add-an-agent-harness.md`](../../../docs/how-to/add-an-agent-harness.md) |
| Pipeline / CLI / run env (`devops_bench/evalharness/`, `cli.py`, `run.py`, `core/`) | [`architecture.md`](../../../docs/components/architecture.md) |
| Metrics (`devops_bench/metrics/`) | [`metrics.md`](../../../docs/components/metrics.md) |
| Chaos / verification (`devops_bench/chaos/`, `devops_bench/verification/`) | [`architecture.md`](../../../docs/components/architecture.md) + [`glossary.md`](../../../docs/components/glossary.md) |
| Task schema / placeholders (`devops_bench/tasks/`, `tasks/`) | [`add-a-task.md`](../../../docs/how-to/add-a-task.md) + [`glossary.md`](../../../docs/components/glossary.md) + [`tasks/AGENTS.md`](../../../tasks/AGENTS.md) |
| Bastion / matrix scripts (`scripts/bastion/`) | [`running-evals.md`](../../references/running-evals.md) + [`monitoring-and-recovery.md`](../../references/monitoring-and-recovery.md) |
| Directory move / rename | the tree in [`glossary.md`](../../../docs/components/glossary.md) Part B + the relevant `AGENTS.md` + a repo-wide search for the old path (`rg '<old-path>'`) — a move leaves stale paths in any doc, reference, or example that mentioned it |

A change can touch more than one row (e.g. a new CLI flag that exposes a new
metric). Update every doc the change actually affects, and only those.

### 4. Update the affected docs

Edit in place. Keep edits surgical — change the lines the code change made wrong,
don't rewrite the page. Match the page's existing voice and structure. If a code
change makes a documented example wrong, fix the example. Never paste a score
table or run results into a doc.

### 5. Resolve rows in the known-issues router

This is the inverse of what the run skills do: they *append* freshly observed
failures to [`known_issues.md`](../../../docs/appendix/known_issues.md); this
skill *resolves* rows the code now fixes.

For every Section 1 (router) or Section 2 (known hacks) row whose root cause your
change set addresses:

- **Confirm the fix exists in the code before touching the row.** Open the file
  the row points at and verify the fix is actually there — cite the file and line
  in your report. A row describes a *current* failure mode; resolve it only when
  that failure mode can no longer happen.
- Follow the page's own convention: Section 1 rows carry a **Resolved** column —
  flip it (and reword the fix cell to point at the fix) rather than deleting the
  row outright; a Section 2 hack whose stated **removal condition** is now met
  gets its **Resolved** cell flipped (citing the fix), same as Section 1 —
  don't delete the row.
- If you're not certain the fix is complete, leave the row and say so — a stale
  "still broken" row is safer than a wrong "all clear".

### 6. Verify links + report

Confirm every relative link you touched still resolves (open the target, or check
the path exists), and when a link carries a `#fragment`, confirm the target
heading still exists — a path check alone won't catch a renamed heading. Then
report: the change-set → doc map you applied, each file
edited, and each known-issues row resolved **with the file/line that proves the
fix**.

---

## Wrong tool?

- **A new failure to record** (not a fix) → that belongs in the run skills, which
  append to the known-issues router; this skill only resolves verified fixes.
- **Authoring brand-new docs for a feature with no home yet** → write the page
  under the matching `docs/` subtree (`components/` for what a thing is,
  `how-to/` for a workflow), then add it to the tree in
  [`glossary.md`](../../../docs/components/glossary.md) and the nearest
  `AGENTS.md`.

## Guardrails

- No model scores, leaderboard standings, or result tallies in any doc — ever.
- Never resolve a known-issues row without citing the code that fixes it.
- Edit prose only; this skill does not change code behavior.
- Keep edits minimal and humanized; don't turn a doc into a changelog.

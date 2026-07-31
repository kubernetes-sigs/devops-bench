---
name: task-review
description: >
  Use when the user adds or changes a benchmark TASK and asks to review it — e.g.
  "review this new task", "is this task parallel-safe", "review the secret-rotation
  task.yaml + stack", "will this task collide under the matrix", "check my new
  tf/prebuilt stack". Reviews a newly added/changed `tasks/**/task.yaml` together
  with its TF stack across schema/metadata, spec parsing, outcome rubric,
  parallel-safety (the emphasis), infra leak prevention, placeholders, and flags —
  returning ranked findings with severity + file:line evidence + a concrete fix.
  Review-only: static analysis plus unit tests / spec-parse checks; it NEVER
  provisions infra or runs an eval. For a review of harness/library CODE, use the
  sibling `devops-bench-review` skill instead.
---

# benchmark task review

Review a **new or changed benchmark task** — its `tasks/**/task.yaml` and the TF
stack it points at — and return findings a maintainer would act on. Each finding is
**severity (blocker / major / minor / nit) + `file:line` evidence + a concrete,
actionable fix**, scoped to the change, plus **how to verify**. Don't nitpick.

Read these as the source of truth; don't reconstruct them from memory:
- [add-a-task](../../../docs/how-to/add-a-task.md) — the `Task` schema, the fixed
  placeholder set, and the authoring flow.
- [known-issues](../../../docs/appendix/known_issues.md) — the "Before any retry"
  cleanup and the parallel-safety hazards (deterministic node-SA, leaked AR repos,
  stale run state). Cross-reference any hazard you flag.
- `devops_bench/core/run_env.py` (`RunEnv`) — **the isolation source of truth.**
  Confirm what a run gets for free before asserting a collision is or isn't covered.

Review-only: you **may** run schema/spec-parse checks and `uv run pytest`; you must
**not** run `tofu`/`gcloud`/`kind`/`kubectl` or launch an eval. Assess solvability,
teardown, and parallel-safety by **reading** the stack and scripts — never by
provisioning. If a capability is needed, consult
[harness-capabilities](../../references/harness-capabilities.md) and degrade to
doing it inline.

## Checklist

### Schema / metadata

Loads against the `Task` schema (validate by parsing, not by eye — typo'd keys are
silently dropped). `task_id` is **globally unique** (`grep -rn '^task_id\|^id:'
tasks/`). Required fields present: `task_id`, `name`, `prompt`, `expected_output`,
`infrastructure`. `infrastructure.deployer` is `tofu` or `noop` — **`noop` only for
manifest-generation tasks** (no cluster). For `tofu`, `infrastructure.stack`
resolves to an existing `tf/prebuilt/<dir>` (confirm the directory exists).

### Spec parsing

`verification_spec` / `chaos_spec` parse against the registries (`VERIFIERS`,
`FAULTS`, `TRIGGERS`) — every leaf `type` is a registered verifier/fault/trigger.
Every chaos entry's `verify:` names a **real `verification_spec` entry's `name`** —
a dangling ref doesn't crash, it's silently recorded as a parse error, so a typo is
a finding. *Why:* a mismatched cross-ref means the chaos you injected is never
actually asserted.

### Outcome-based rubric

`expected_output` grades on the **outcome** and accepts **all valid remediation
paths**. Flag a single-path rubric ("must run `kubectl rollout undo`") — that tests
recall, not capability; rewrite it to credit any path that reaches the goal. Nothing
in-cluster should *name* the fix (the agent must discover it). *Split deterministic
vs subjective:* a deterministic, checkable fact (pod healthy, replicas ≥ N, secret
rotated) belongs in a **verifier**, not the prose rubric; leave subjective quality
to the judge.

### Parallel-safety (critical)

The matrix runs **Task × Model × AgentConfig** concurrently on one host. `RunEnv`
isolates each run; a task is unsafe when it names state shared *outside* that
isolation. **What `RunEnv` gives for free — do NOT hardcode these:** `KUBECONFIG`,
`CLOUDSDK_CONFIG`, `TF_DATA_DIR` (+ tofu state beside it), `OPENCLAW_STATE_DIR`,
ports, and the **cluster name** (run-token-prefixed, clamped to GKE's 40 chars).
Flag any task/stack/prompt that pins one of these to a fixed value.

**What the author MUST guarantee** (none of this is isolated for you):

1. **Every globally-unique cloud resource name is run-scoped.** Service accounts,
   AR repos, Cloud SQL instances, external IPs, buckets — embed `var.cluster_name`
   and/or a `random_id` suffix so two concurrent runs coexist (compare the
   `secret-rotation` stack's `random_id`-suffixed `sa-*` / `db-credentials-*`). A
   fixed name → `409 already exists` the moment a second run starts.
2. **Teardown sweeps project-global resources the stack doesn't own.** Anything an
   agent or seed script creates outside the stack's managed resources (e.g. a
   `hello-app-*` AR repo) survives `tofu destroy` — the stack needs a destroy-time
   sweep, or teardown leaks (see the leaked-AR-repo sweep in known-issues §2).
3. **Namespace consistency.** The namespace must match across the prompt's
   `{{NAMESPACE}}`, the stack's `variables.namespace`, and any matrix override — or
   the agent and the chaos injector address different workloads.
4. **Node-SA names are deterministic today** (`gke-nodes-<cluster>`, not suffixed,
   and multi-cluster stacks truncate to `substr(cluster_name, 0, 15)` collapsing
   east/west to one id). So a **multi-cluster GKE task is NOT parallel-safe until
   the node SA is suffixed** — flag it as a blocker and cite known-issues §2 H6 / the
   `409 gke-nodes-*` router row.
5. **No per-task mutation of the shared VM service-account IAM.** A stack that
   grants a project role to the shared `openclaw-vm-sa` via
   `google_project_iam_member` in its own state: the first `tofu destroy` strips the
   binding while sibling runs still need it → mid-run auth loss.
6. **Name-length budget.** Cluster base name **+ run token ≤ 40 chars** (GKE limit),
   and the discriminator must survive any stack-side truncation (a suffix the gke
   module then truncates off collapses two runs to one name).

For each parallel finding, **state which axis triggers it** (Task / Model /
AgentConfig) and whether `RunEnv` already covers it — that's what the maintainer
acts on.

### Infra config & leak prevention

The stack has `main.tf`, `variables.tf`, `outputs.tf`. It returns what the deployer
expects (`cluster_name` / `cluster_location`; kind stacks emit
`cluster_location = "local"`). For **every project-global resource it creates** (AR
repos, Cloud SQL, external IPs, auto-mode VPCs), there is destroy-time cleanup so a
re-apply after a failed run isn't blocked by orphans. New stack variables the
kind/gcp resolver won't populate are a red flag — the harness can't set them.

### Placeholders

`{{GKE_CLUSTER_NAME}}` / `{{CLUSTER_NAME}}`, `{{NAMESPACE}}`,
`{{TARGET_DEPLOYMENT_NAME}}`, `{{PROJECT_ID}}` used **consistently** and only from
the fixed supported set — there is **no `{{REPO_PATH}}`** or other invented token (an
unknown `{{...}}` is left un-substituted and reaches the agent literally). Every
infra-specific value in the prompt/rubric/specs is a placeholder, never a hardcoded
project/cluster/namespace.

### Flags

`validated` stays **`false`** until a human has vetted the task — it gates
leaderboard eligibility (not running; an unvetted task still burns quota). Promoting
to `true` in the same change that adds the task is a finding unless the change shows
it was actually run. `generation_only` is derived from `deployer == "noop"`, so it
must match: a `noop` task is generation-only (its YAML is judged, never applied); a
`tofu` task is not.

## Verify, then present

Confirm the schema/spec actually parse (run the loader / `uv run pytest` for task
tests; note pre-existing failures aren't the author's). For each candidate, try to
**refute** it against `run_env.py` and the resolver before keeping it. Present:

1. **Overview** — what the task tests and its infra shape (deployer/stack, kind vs
   GKE, chaos or not).
2. **Findings**, most-severe first, each `severity — file:line — summary` then the
   failure/why, the concrete fix, and **how to verify** (the parse to run, the
   `grep` for the duplicate id, the axis that triggers the collision).
3. **Cleared** — what you checked and found safe (e.g. "cluster name + KUBECONFIG
   inherited from RunEnv ok; namespace consistent across prompt/stack").

Scale effort to the ask: "is this parallel-safe?" wants the parallel-safety
checklist and a verdict; "review this task" wants the full checklist. Never
provision infra or run an eval to produce a finding.

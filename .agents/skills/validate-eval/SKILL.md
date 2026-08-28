---
name: validate-eval
description: >
  Validate that a NEW eval is correct and runnable by running it and iterating
  in a loop until green — fixing infra/config/task bugs (not the model's
  score), with opt-in hands-off and unlimited/self-healing modes; invoke when
  authoring or vetting a task before setting `validated: true`.
---

# Validate a new eval

Prove a newly authored eval is **correct and runnable**, iterating until it runs
clean. The loop fixes the *eval*, not the model.

> [!IMPORTANT]
> **The goal is task correctness, not making the model pass.** A successful
> validation means: the infra provisions, the agent runs end to end, and the
> verification/judge produce **sane scores**. A genuine **model miss** — a clean
> trajectory that just scores low on a hard task — is a *successful* validation,
> not a failure to fix. Do not edit the task to make a weak model pass.
>
> **But first rule out a silent capture failure, which masquerades as a low
> score.** Before reading a low score as a model miss, assert the run's
> `trajectory` is **non-empty** (and `tools` populated for tool-using tasks). An
> empty trajectory on a task the agent clearly acted on is **INVALID** — a harness
> capture failure (e.g. the trajectory exporter exiting 127 because Node isn't on PATH), not
> a model miss — and must be fixed and re-run, not recorded. See the empty-traj /
> `exit 127` row in [`known_issues.md`](../../../docs/appendix/known_issues.md).

This skill is pointer-driven. The run mechanics, monitoring, and self-healing
loop all live in shared references — read them, don't restate them:

- Local vs bastion, auth, clean pre-flight, launch, knobs, results →
  [`../../references/running-evals.md`](../../references/running-evals.md)
- Monitoring / keepalive / recovery →
  [`../../references/monitoring-and-recovery.md`](../../references/monitoring-and-recovery.md)
- Unlimited / self-healing loop →
  [`../../references/unlimited-mode.md`](../../references/unlimited-mode.md)
- Failure router →
  [`../../../docs/appendix/known_issues.md`](../../../docs/appendix/known_issues.md)
- Reading scores →
  [`../../../docs/components/metrics.md`](../../../docs/components/metrics.md)
- Capability → tool mapping →
  [`../../references/harness-capabilities.md`](../../references/harness-capabilities.md)

---

## Modes (opt-in)

| Mode | When | What it adds |
|---|---|---|
| **Standard** | default | You drive the loop directly, one attempt at a time. |
| **Hands-off** | "watch it", "don't stop", long runs | Resilient monitoring + keepalive — [monitoring-and-recovery.md](../../references/monitoring-and-recovery.md). |
| **Unlimited / self-healing** | "keep going until green", "auto-fix and restart" | Diagnose → fix in a worktree → re-sync → restart, bounded by the same attempt cap below — [unlimited-mode.md](../../references/unlimited-mode.md). |

Resolve the mode up front; hands-off and unlimited are **explicit opt-in**.

---

## Flow

### 1. Pre-flight the task spec (optional but recommended)

Before spending a cluster, run the [`task-review`](../task-review/SKILL.md) skill
(or skim the stack/scripts) to catch obvious authoring bugs statically — schema
errors, a non-unique `task_id`, a parallel-safety collision (a fixed
project-global resource name, a stack that mutates the shared VM SA's project
IAM), or a single-path rubric. Cheaper to fix on paper than after a full
provision–run–teardown cycle.

### 2. Run it

Use the mechanics in [running-evals.md](../../references/running-evals.md): choose
local vs bastion + auth, **clean pre-flight** (the "Before any retry" checklist in
[known_issues.md](../../../docs/appendix/known_issues.md)), `DRY_RUN=1` preview,
then launch the one combo detached and capture `RESUME_STAMP`. Validation is a
single combo per attempt — don't fan out.

### 3. Loop until green

Monitor per [monitoring-and-recovery.md](../../references/monitoring-and-recovery.md).
On each finished or flaked attempt, **classify the failure against the router** in
[known_issues.md](../../../docs/appendix/known_issues.md) and take exactly one
action (the same decision tree as
[unlimited-mode.md](../../references/unlimited-mode.md)). **Every retry branch
below starts with the clean pre-flight** (the "Before any retry" checklist in
[known_issues.md](../../../docs/appendix/known_issues.md)) — whatever the
failure class, a failed attempt can leave stale
`/tmp/devops-bench-runs/<RUN_ID>` state or orphaned cloud resources behind:

- **Retry** — infra flake or stale state (a model-provider `429`
  mid-trajectory, a ~2-min failure at `tofu plan` from a prior run's leftover
  state): re-run. No edit.
- **Fix + retry** — config / auth / host setup (missing API enablement, auth
  credential markers, inotify limits, workspace trust settings): apply the
  router's documented fix, then retry. Environment fix, not eval-logic.
- **Fix the task + retry** — a real **task/stack/rubric bug** the validation
  surfaced (the thing you're here to catch): fix it in an **isolated worktree**
  scoped to that bug, then retry. Log the cycle in durable state.
- **Escalate / accept** — a genuine **model-capability low score** (clean
  trajectory, hard task): this is a *successful validation*. Record it and stop;
  do **not** edit the task to inflate the score.

Honor the **STOP conditions** from
[unlimited-mode.md](../../references/unlimited-mode.md): goal met, the attempt
cap, no-progress (same failure signature twice), or budget exhausted — each
full-infra restart may provision and tear down a real cluster; a run can
also fail before provisioning (e.g. at `tofu plan` from stale state); such a
run still needs the clean pre-flight before the next attempt.

**The attempt cap: at most 3 launched runs per combo, the initial run
included** — so after the initial run, at most 2 fix/retry attempts. Every
launched run counts, even one that failed before a cluster existed. It applies
in every mode, including unlimited/self-healing, which is unlimited in
persistence, not in cluster spend. Hands-off and unlimited behaviors are
opt-in; without them, surface a persistent failure rather than looping.

### 4. Confirm green + recommend `validated: true`

When the eval runs clean, read the scores
([metrics.md](../../../docs/components/metrics.md)) and confirm they look
**sensible** — checks pass/fail for the right reasons, no swallowed scoring error,
no empty `tokens: {}` masking a timeout. Then recommend setting `validated: true`
on the task (the flag gates leaderboard promotion, not running). If a real model
miss is the only "failure," say so explicitly — the task is validated.

---

## Living known-issues

When you hit a failure mode **not** already in
[known_issues.md](../../../docs/appendix/known_issues.md), append a router row so
the next run benefits: **symptom → root cause → fix / recovery action → class →
resolved**, matching the existing table's columns and class labels. Keep it
terse and don't duplicate an existing row. This capture step is part of
validation, not optional.

---

## Guardrails

- **Validate the task, not the model.** Never edit a task to make a weak model
  pass — a genuine miss is a valid result.
- Each full-infra attempt may provision a real cluster (locally with kind, or on
  the task's cloud provider) — `DRY_RUN=1` first, honor the attempt cap (3
  launched runs per combo, initial included), and track budget.
  `deployer: noop` tasks skip infra entirely.
- Make all fixes in an **isolated worktree/branch**; keep commits scoped and
  local — surface the diff for review, don't push shared branches unless asked.
- Never print or commit API keys; redact secrets in summaries.
- Always confirm clean teardown — stale per-run state under
  `/tmp/devops-bench-runs/<RUN_ID>` makes the next "fresh" run fail instantly at
  `tofu plan`, and a failed teardown strands cloud resources (clusters,
  per-run service accounts, container-registry repositories). The
  [`cleanup-orphaned-resources`](../cleanup-orphaned-resources/SKILL.md) skill
  walks the sweep.
- **Hands-off run:** never emit a completion signal until the eval is terminal
  **and** summarized; emit a periodic heartbeat instead.
- Don't auto-fix what you can't both name and resolve — task-authoring / rubric
  judgement calls escalate to a human.

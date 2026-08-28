# Unlimited / self-healing mode

An **opt-in** loop that turns a recoverable failure into: classify → act
(retry / fix+retry / escalate) → continue, until the whole run reaches a
terminal-acceptable state or a stop condition trips. Load this **only** when the
operator explicitly asks for it ("unlimited", "keep going until it finishes",
"auto-fix and restart", "self-healing"). It builds on
[`monitoring-and-recovery.md`](./monitoring-and-recovery.md) — reuse that loop,
recovery, and heartbeat; this file only adds the decide-and-restart behavior.

This reference is **agent-agnostic**. The capabilities it needs (isolated
worktree, durable state file, sub-agents, scheduled wakeups) map to concrete
tools in [`harness-capabilities.md`](./harness-capabilities.md); degrade
gracefully when one is absent.

---

## Decision tree — classify every failure before acting

Match the failure against the router in
[`../../docs/appendix/known_issues.md`](../../docs/appendix/known_issues.md),
then take exactly one action. Fixing the wrong class corrupts the eval, so when
unsure, prefer recording over fixing.

- **Retry** (infra flake — e.g. a model-provider `429 RESOURCE_EXHAUSTED`, a transient ssh
  drop, a transient API/quota error): re-run the combo after the
  clean-environment pre-flight. No code change.
- **Fix + retry** (config / auth / host setup — e.g. a missing API enablement,
  ambient credential markers, inotify limits, workspace trust settings): apply the router's
  documented fix, then retry. These are environment fixes, not eval-logic edits
  — but most of them mutate **shared** host/project/global state that a
  worktree does not isolate, so get operator approval (or pause sibling combos)
  before applying one mid-matrix; apply unattended only when the fix is scoped
  to this worktree/run.
- **Escalate / stop** (a real **model-capability** low score — the agent ran a
  clean trajectory and just did the task badly — or a **task-logic / rubric bug**
  the loop can't safely edit): surface it as a distinct outcome. Do **not**
  silently retry or paper over it. A genuine low score is a real result; record it
  and move on.

---

## The loop (for the fixable class)

1. **Isolate.** Make changes in an **isolated git worktree / branch**, never the
   shared checkout. Branch off the code under test.
2. **Diagnose.** Name the bug and the minimal fix (use the review skills / the
   known-issues router). Don't refactor unrelated code.
3. **Fix**, scoped to that bug. Run unit tests / linting locally if they cover it
   — never run an eval just to "test" a fix.
4. **Log** the cycle in the durable state file: combo, symptom, root cause, the
   change, commit id.
5. **Re-sync** (remote only) and **restart only the failed combo(s)** as fresh
   single-combo runs (new stamp), after cleaning their leaked cloud resources.
6. **Continue** the monitoring loop over the remaining + restarted combos.
   Restarted combos run on a changed revision/config — record the revision and
   environment on every combo's attempts, and report post-fix attempts as a
   **separate batch** from the original matrix (or rerun all affected combos);
   never silently combine them, or the comparison misattributes the fix to the
   model.

Keep commits local and scoped; do not push or merge shared branches unless the
operator said so — surface the branch/diff for review instead.

---

## STOP conditions (so "unlimited" still terminates)

Stop the loop when **any** of these trips — report where you stopped:

- **Goal met** — every combo is terminal-acceptable (passed, recorded as a
  genuine model-capability result, or blocked after the cap).
- **Attempt cap** — ≤ 2–3 fix/retry attempts **per combo**; after that, mark it
  blocked and keep going on the rest. Never loop one combo forever.
- **No progress** — consecutive attempts on a combo make no new progress (same
  failure signature) → stop attempting it.
- **Budget exhausted** — any given iteration, wall-clock, or token/cost budget.
  Each restart costs a cluster plus tens of minutes; track combos
  fixed/remaining.

Keep durable state **outside** the loop (a state file): stamp, per-combo status,
attempt counts, and the fix changelog, checkpointed every tick so a context reset
resumes mid-flight.

---

## What NOT to auto-fix

- **Real model misses** — a clean trajectory with a low score is a result, not a
  bug. Record it.
- **Task-authoring / rubric bugs** — wrong `expected_output`, a mis-scoped
  criterion: needs human judgement; escalate (the
  [`task-review`](../skills/task-review/SKILL.md) skill is the right vehicle).
- **Anything you can't both name and resolve** with a scoped change.

---

## Living known-issues

When you hit a failure mode **not** already in
[`known_issues.md`](../../docs/appendix/known_issues.md), append a new router
row so the next run benefits, matching the table's existing columns: **symptom →
root cause → fix/recovery → class → resolved**. Keep it terse, don't duplicate
an existing row, and never paste model scores or run tallies into the doc. This
capture step is part of the run, not optional.

---

## Final report

Deliver the normal results summary **plus** the fix changelog (each bug → change →
commit) and the list of still-blocked combos with why each is blocked.

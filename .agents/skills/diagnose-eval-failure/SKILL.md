---
name: diagnose-eval-failure
description: Explain WHY a model scored low on an eval — read the judge's reasons and the agent's trajectory, compare against the task rubric, and produce a rationale (capability gap vs. task/rubric problem). Distinct from infrastructure failures. Invoke when someone asks "why did the model fail this task?", "why is OutcomeValidity low?", "explain this low score", or "did the agent actually do the work?".
---

# Diagnose an eval failure

This skill answers a *graded* question: given a run that completed and was scored,
**why did the model fall short?** It reads the judge's stated reasons and the
agent's actual trajectory, lines them up against the task's rubric, and writes a
rationale. It explains; it does not fix the model.

It is **not** for infrastructure failures. A run that crashed, timed out, or
failed to provision never got a fair shot at scoring — those go to the recovery
router, not here.

- Score / record field shapes → [`../../../docs/components/metrics.md`](../../../docs/components/metrics.md)
- Recovery router for infra failures →
  [`../../../docs/appendix/known_issues.md`](../../../docs/appendix/known_issues.md)
- Capability → tool mapping for your harness →
  [`../../references/harness-capabilities.md`](../../references/harness-capabilities.md)

---

## Flow

### 1. Locate the run and confirm it was graded

Find the run's `results.json` — under the combo dir for a matrix run, or under
`results/run_…/` for a bare CLI run (layout in
[`running-evals.md`](../../references/running-evals.md)). Check `status` on the
record:

- `status: "success"` → a real graded run. Continue.
- `status: "failed"` → **infrastructure**, not a model miss. The record is skipped
  by scoring and carries no scores; a low/absent score here is not the model's
  fault. Redirect to the recovery router in
  [`known_issues.md`](../../../docs/appendix/known_issues.md) and stop.

(An absent score on a `success` record means the metric didn't *run* for that
task, not that the model scored zero — see
[`metrics.md`](../../../docs/components/metrics.md).)

### 2. Read the judge signal

In the record's `scores` map, read the entries in the order that matters:

- **`OutcomeScore.reason`** — the composite's own breakdown (`c=…, rec_v=…,
  cat_v=…`) tells you immediately whether the low score came from correctness,
  from safety, or from coverage.
- **Which correctness signal was used** — `VerificationCorrectness` wins over
  `ChecklistScore`, which wins over `OutcomeValidity`. Check
  **`VerificationCoverage`** before trusting `VerificationCorrectness`: coverage
  below 1.0 means the fraction was computed over a subset of the declared
  entries.
- **`OutcomeValidity.reason`** — the headline "did it work" judgment.
- **`ToolInvocation.reason`** — did it call the right tools / take a sane
  trajectory (present only when MCP is on).
- **`Check: <item>.reason`** for each `expected_output` requirement, and
  **`ChecklistScore.reason`** for the passed/total ratio in words
  (`"Passed 3 out of 5 checks."`).
- **`GroundingAccuracy.reason`** — reads "Applied X out of Y documented
  constraints (Critical: a/b)"; note it is on a **0–5 scale**, and a short
  *critical* count caps the band even when the raw count looks fine.

Bare-number entries (`DocRetrievalRate`, `ParameterRecallAccuracy`, the chaos
performance numbers) have no `success` flag — read the magnitude. Field shapes
and banding rules are in [`metrics.md`](../../../docs/components/metrics.md).

### 3. Read what the agent actually did

Read the agent's `trajectory` / `tools` (the steps and tool calls it actually
made) and its `output`. Then read the task's `expected_output` rubric. Compare:
what did the agent attempt, and where did it diverge from the intended outcome?

### 4. Produce a rationale

Synthesize, don't just restate the reasons:

- **What the agent attempted** — the gist of its trajectory and final state.
- **Where it diverged** — the specific step or omission that lost the outcome.
- **Which judge criteria it missed and why** — tie each failed `reason` back to
  what the agent did (or skipped).
- **Capability gap vs. task/rubric problem** — decide which:
  - *Capability gap*: the task is fair — it grades the outcome and accepts every
    valid path — and the agent genuinely couldn't do it. Say so plainly.
  - *Task/rubric problem*: the rubric looks wrong, ambiguous, or single-path
    (penalizes a valid alternative solution, demands a placeholder the task never
    supplied, or grades for something the prompt didn't ask). Flag it and
    recommend the [`task-review`](../task-review/SKILL.md) skill — don't try to
    fix the task here.

### 5. Report

Report: run id · task · model · the failed metrics with their `reason`s · your
rationale · the verdict (capability gap vs. rubric problem) · and the next action
(accept the result, or send the task to `task-review`).

---

## Wrong tool?

- **The run crashed / timed out / failed to provision** (`status: "failed"`, or no
  `results.json`) → infrastructure, not a model miss. Use the recovery router in
  [`known_issues.md`](../../../docs/appendix/known_issues.md).
- **The rubric itself is the problem** → recommend [`task-review`](../task-review/SKILL.md);
  this skill diagnoses, it does not edit tasks.

## Guardrails

- Always check `status` first — never read a `failed` record as a model miss.
- An absent score means the metric didn't run, not a zero.
- Explain only; this skill does not fix the model or edit the task.
- Quote the judge's `reason` where it's load-bearing — don't paraphrase away
  the signal — but **redact secrets and personal data first**: a reason can
  reproduce credentials or PII from the trajectory or tool output it judged.

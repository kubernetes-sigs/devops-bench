# Monitoring & recovery

How to babysit a detached eval run so it survives quiet stretches, dead watchers,
and local drops — without busy-polling or declaring victory early. The eval skills
link here for the monitoring loop.

This reference is **agent-agnostic**: it describes capabilities (a cheap watcher,
a stronger reader, background runs, timers, durable state, heartbeats). For the
per-agent mapping of those capabilities to concrete tools see
[`harness-capabilities.md`](./harness-capabilities.md), and degrade gracefully
when one is absent.

The **runner host is the source of truth** — every combo's state lives in
`~/matrix-runs/<stamp>/<rid>/` (`status`, `run.log`) plus the runner's own log at
`~/matrix-runs/<stamp>.out`. The watchers below are disposable readers of that
state: losing one loses nothing, because a fresh one re-reads the same
`RESUME_STAMP`.

---

## Tiered monitoring

Spend the cheapest tier that can do each job:

| Role | Tier | Cadence | Job |
|---|---|---|---|
| **Monitor** | cheap model | each tick | Shell to the runner host, read each combo's `status` + `run.log` tail, return a one-line-per-combo digest + an overall `running / done / flaked / .done` count. No analysis, no log dumps. |
| **Analyzer** | mid model | once per finished combo (batch if several finish together) | Pull + read that combo's `results.json` + `run.log`; return scores + pass/fail checks + a root-cause digest if it failed. Never analyze a combo twice. |
| **Supervisor** | your main model | every ~3–5 min while active; longer when idle | Read the monitor digest, dispatch analyzers, decide retries, re-spawn dead watchers, keep the session alive. |

Run the monitor/analyzer as **sub-agents that can shell out on the runner host**
(local shell, or ssh/cloud tunnel CLI to the bastion in remote mode) when your harness has
sub-agents; if it doesn't, do those checks inline yourself — the loop still works
on one model. Give each watcher the connection env and the `RESUME_STAMP`, and
have it **return a compact digest, not raw logs**, to keep the supervisor's
context clean.

---

## Supervision loop

1. Launch detached and capture `RESUME_STAMP`; record the stamp + combo list in a
   **durable state file** so a context reset can resume.
2. Start the **monitor** — as a background/detached watcher if available, else a
   short foreground check each tick.
3. Each supervisor tick:
   - Read the monitor digest; emit a one-line status (heartbeat).
   - Every newly **terminal** combo → dispatch an **analyzer**. A combo that
     stays failed past the retry cap is terminal too — close it out with a
     root-cause digest (or record it as a non-graded failure); never leave a
     failed combo unanalyzed.
   - **Flaked** combos → the retry procedure in the eval skill (cap 2 per combo);
     classify infra-flake vs real failure against
     [`../../docs/appendix/known_issues.md`](../../docs/appendix/known_issues.md).
   - `.done` present — or every expected combo has a `status` file, which the
     wrapper itself treats as finished (see below) — **and** every combo
     analyzed or recorded → summarize and finish.
   - Otherwise **schedule the next wakeup** (~3–5 min while active; 20–30 min if
     genuinely idle) and end the turn. No scheduler? `sleep` between checks. Poll
     every ~3–5 min for infra-bearing tasks — **don't busy-poll.**

The wrapper's own poller already tolerates one failure mode for you: the `.done`
marker is written only after every combo finishes, so if the detached runner
itself dies (reboot, OOM, manual kill) the marker never lands — the wrapper
stops anyway once **every combo has a `status` file**, since that means the work
finished either way.

---

## Keepalive / heartbeat

Never declare the run "done" until **every** combo is terminal **and** summarized.
A long detached matrix has quiet stretches; some harnesses watch the session and
may classify it finished or time it out during those gaps. So:

- **Never** emit a completion / "done" / "failed" signal mid-run.
- Each tick, emit a short `still working: N running / M done / K flaked` line so
  the session stays classified as active.
- Re-engage via a timer rather than blocking, so you don't go silent.

Harnesses with no such classifier can ignore the mechanics, but periodic progress
is still good practice.

---

## Recovery

- **Watcher died** (terminal error / empty result) → re-spawn a fresh one pointed
  at the same `RESUME_STAMP`; no state is lost. Cap re-spawns (~3 per role per
  tick); if a role keeps dying, fall back to doing that check yourself and note
  the degradation.
- **Local poller died** → the detached run continues on the runner host;
  re-attach with `RESUME_STAMP=<stamp>` and the same command.
- **Whole runner died** (no `.done`, no live process) → re-attach to confirm,
  then relaunch only the unfinished combos.
- **Transient ssh `exit 255`** (remote mode) → a relay blip; retry. The detached
  run is unaffected — the wrapper's ssh transport uses keepalives and its result
  pull already retries.

---

## Cost discipline

Poll with the cheap monitor (frequent), analyze with the mid tier (per-finish
only), spend the main model rarely (supervise/decide). Prefer one batched
analyzer when several combos finish together.

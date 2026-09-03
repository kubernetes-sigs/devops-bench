# Cheating detection

Agents under test run as ordinary subprocesses on the harness host — there is no filesystem sandbox — so the benchmark's own sensitive material (task definitions with their judge rubrics and verification specs, the scoring code, prior results, the repo checkout, the upstream GitHub repo) is technically reachable. The detection layer in [`devops_bench/cheat_detection/`](../../devops_bench/cheat_detection/) scans each run's recorded trajectory for signs that an agent accessed that material, and attaches a **flag-only** report for human review.

> [!IMPORTANT]
> Detection never changes scores, never touches `validated`, and never aborts a run. A `flagged` report is an annotation for a reviewer, not a verdict: trajectory analysis sees only what the agent's transcript recorded, so it can neither prove innocence nor block cheating — it surfaces the runs worth a closer look.

## What is scanned

Every agent normalizes its transcript to the canonical `ToolCall` shape (`{"name", "args", "result", "status"}`, see [`devops_bench/agents/result.py`](../../devops_bench/agents/result.py)) embedded in each record's `trajectory`. The detector matches rule regexes against three surfaces:

| Surface | What it is | Notes |
| --- | --- | --- |
| `args` | Each tool call's arguments, JSON-dumped | Shell commands, file paths — recorded by every agent |
| `result` | Each tool call's output text | Rich for openclaw; usually `null` for gemini-cli, whose stream carries no tool payloads |
| `output` | The record's final answer text | Catches rubric/scoring material leaking into the agent's report |

An errored run with an empty trajectory and empty output reports `no_data` — deliberately distinct from `clean`, because detection had nothing to see.

## Default rule categories

Rules live in [`devops_bench/cheat_detection/rules.py`](../../devops_bench/cheat_detection/rules.py) and match the *kind* of sensitive material, never a specific task, so new tasks are covered automatically:

| Category | Severity | Catches |
| --- | --- | --- |
| `task-definition` | high | `task.yaml` paths, or rubric/spec keys (`expected_output`, `verification_spec` — YAML or JSON-quoted) surfacing in tool output |
| `scoring-code` | high | `devops_bench/verification/` and `devops_bench/metrics/` |
| `results-dir` | high | Prior or in-flight `results.json` / `rows.json` paths, `results/matrix`; also record-content markers (`"cheating_report":`, …) in tool output, so a read via `find -exec`/globs that never spells the path is still caught |
| `harness-repo` | medium | The benchmark checkout (`~/devops-bench`, the code/tasks/tf/results/docs subtrees, its `.git`) — the docs subtree counts because it describes the detection rules and scoring formulas |
| `upstream-github` | high | Cloning/fetching the upstream GitHub repo |
| `prebuilt-stack` | medium | The `tf/prebuilt/` stack that seeded the scenario |
| `harness-environment` | high | Bastion-side harness files: `bench.env` (provider/judge config, possibly keys), the `matrix-runs/` on-host output tree, `.matrix-runner-*` scripts, sync archives |

Path-shaped rules scan all three surfaces, `result` included. There is deliberately no passive/active distinction: a benchmark path surfacing in an `ls ~` listing or a grep error trail is not itself access, but no legitimate task puts the harness's own material in view either, so the sighting is treated as evidence the agent went looking. Content-evidence rules (rubric keys, record markers, `bench.env` contents) stay restricted to `result`/`output`, because a path-shaped `args` is already covered by the path rule and would otherwise be reported twice.

Extra rules load from a YAML file (validated, fail-loud) without a code change:

```yaml
# my-rules.yaml
rules:
  - category: my-task-oracle
    severity: high
    patterns: ["solutions/oracle\\.ya?ml"]
```

## Prior-run artifacts (dynamic rules)

The agent home persists between runs, so earlier runs leave material behind — a previous `report.md` is an answer key for the next attempt. "Left by a prior run" is temporal, not lexical: this run's legitimate `report.md` write and a read of last run's leftover are the same string, so no static regex can separate them. Instead, [`devops_bench/cheat_detection/inventory.py`](../../devops_bench/cheat_detection/inventory.py) snapshots the home **before every task's agent executes** and generates `prior-run-artifact` rules from whatever is already there. Per task rather than once per batch, because the home keeps accumulating while the batch runs: task 1's deliverable is precisely the answer key task 2 must not read, and a single run-start snapshot is blind to it. A baseline of provisioned names (`bin`, `bench.env`, `devops-bench`, `matrix-runs`) and an enumerated set of environment dotfiles (`.bashrc`, `.cache`, `.config`, `.ssh`, `.kube`, …) are skipped — but *only* those: any other hidden entry is a leftover like any visible one, because agent CLIs conventionally keep their state in a dotdir and that is exactly where cross-run contamination accumulates (a stale `~/.openclaw/workspace` holds a previous task's deliverables and git history). One known caveat, deliberately unhandled: the state directory of the agent currently under test is not special-cased, so an honest agent that references its own state dir in a recorded tool call flags; if that bites in practice, the harness (which knows the agent type) should add that one name to the baseline it passes rather than the dotfile set widening. The generated rules are:

- **Path rules**, anchored to home spellings (`~`, `$HOME`, the literal home path), so this run's own same-named files elsewhere (e.g. a fresh `/tmp` clone) never match. Like all path rules they scan every surface, so a leftover's name echoing through a directory listing flags too — the agent had no reason to be enumerating the home directory.
- **Content fingerprints** for small text leftovers: their most distinctive lines, matched only against `result`/`output`. Reading the stale file reproduces those exact lines; writing a fresh file does not — so a stale `report.md` read is flagged even when the command that read it named some other path.

Everything a task creates *after* its own snapshot is invisible to that task's rules by construction, so a task is never flagged for its own output. Symlinked leftovers keep their path rule but are never fingerprinted, so a link cannot pull an unrelated file's contents into a published pattern.

The two rule kinds are treated differently across the batch. A run-start snapshot records which entries predate the batch; those are genuine prior-run artifacts and get both kinds. An entry that appears *during* the batch — an earlier task's deliverable — gets its path rule but **no content fingerprint**. The asymmetry is deliberate: fingerprints are unfilterable by design, and two iterations of one task legitimately share long lines (a pasted policy body, a command line, a cluster name), so fingerprinting a same-batch deliverable would flag the honest repeat rather than a cheat. Referencing a previous task's output *by path* has no such innocent explanation, so the path rule still applies.

The baseline is extended per run from what the harness actually granted: a skills tree under the home (`AGENT_SKILLS_PATHS`) is material the agent is *told* to read, so its top-level home entry is environment rather than leftover. That mapping is derived, not enumerated — hard-coding an operator's directory names would bake one host's layout into the detector and mis-flag every other one.

Every leftover gets a path rule, deliverable names included. Authorization happens **per record against the task prompt** instead: an entry the prompt itself names — the GitOps repo the agent is told to push to, the deliverable it is told to write — generates no path finding for that record. Doing it here rather than with a static exclusion list matters, because a name excluded globally would go uncovered while an *unrelated* task ran: a stale `report.md` is a prior-run leftover for every task except the one that wrote it. Content fingerprints are never filtered: a prompt naming `report.md` authorizes *writing* a fresh one, not reading the stale copy back, and an honest write never reproduces the stale lines anyway. This prompt filter is the one place authorization is recognised at all — otherwise a path rule flags a home entry however it surfaced.

## Configuration

| Variable | Default | Effect |
| --- | --- | --- |
| `BENCH_CHEAT_DETECT` | `true` | Annotate every record with a `cheating_report` during `DefaultEvalHarness.run()` |
| `BENCH_CHEAT_RULES` | unset | Path to a YAML rules file overlaid on the defaults |
| `BENCH_CHEAT_INVENTORY` | `true` | Snapshot the agent home before each task and flag access to pre-existing entries |

A bad `BENCH_CHEAT_RULES` path is an operator error and fails loud when the harness is constructed. Scan-time detection is best-effort per record: a detector failure on one record logs, leaves that record's seeded empty report, and moves on — it never sinks the run.

> [!NOTE]
> The inventory assumes the agent home is **dedicated to the benchmark**, as it is on the harness host, where the only pre-existing entries are the provisioned baseline. That assumption breaks on a developer machine: `Path.home()` is your ordinary home, so every top-level entry (`Documents`, `Downloads`, `Library`, …) reads as a prior-run leftover and generates a path rule. Set `BENCH_CHEAT_INVENTORY=0` for local runs. The static rules stay active, so task-definition, scoring-code, results-dir, harness-repo, and harness-environment coverage is unaffected; what you give up is leftover detection — notably a read of an earlier local run's `report.md`.

## The report

Each record carries a `cheating_report`:

```json
{
  "schema_version": 1,
  "detector_version": 6,
  "status": "flagged",
  "categories": ["harness-repo", "task-definition"],
  "findings": [
    {
      "category": "task-definition",
      "severity": "high",
      "pattern": "tasks/[^\\s'\"]*task\\.ya?ml",
      "field": "args",
      "trajectory_index": 4,
      "tool": "exec",
      "excerpt": "...cat ~/devops-bench/tasks/common/opa-remediation/task.yaml..."
    }
  ],
  "scanned": {"trajectory_entries": 36, "output_chars": 2114}
}
```

`status` is `flagged`, `clean`, or `no_data`. Findings are capped at 20 per rule per record so a read loop cannot bloat the report.

## Known limitations

- gemini-cli trajectories carry no tool-output payloads, so detection there rests on commands/args and the final output; openclaw runs are fully covered.
- Only recorded activity is visible: a process the agent spawned outside its transcribed tool loop leaves no trace here. Stronger guarantees require isolation (sandboxing), which is out of scope for this layer.
- **Detection is host-side only — it never inspects a cluster.** Rules match text in the trajectory, so material the stack seeds *into* the cluster (Secrets, ConfigMaps, the policy objects a task is graded on) is caught only if it surfaces in a tool result. Nothing here is infrastructure-specific: the same rules apply whether the target is kind, a cloud cluster, or a virtual cluster.
- **A virtual cluster's host is not distinguished from the virtual cluster itself.** When the target is a vcluster running on a host cluster, `kubectl` against the host is textually identical to `kubectl` against the vcluster, and there is no "wrong cluster" rule — so an agent that reaches the host cluster is not flagged. This is a credential-scoping problem, not a rule-coverage one: the fix is to hand the agent a credential scoped to its own cluster (and no host kubeconfig or ambient cloud credentials) rather than to pattern-match the escape after the fact.
- **The inventory covers only the agent home.** `Path.home()` is the one location snapshotted, so prior-run leftovers in other shared writable locations — most notably `/tmp`, where scratch clones and working copies of reports also accumulate — generate no inventory rules, and a read of `/tmp/prior-run/remediation-report.md` scans `clean`. Isolating runs fully means quarantining those locations too, which today is host hygiene between runs rather than something this layer detects.
- Content fingerprints match exact lines from the stale file. In the unlikely event an honest rerun of the same task reproduces one of those lines character-for-character (same model, formulaic report), it flags — that is what reviewer triage is for; the flag never touches scores.
- Generated-file content scanning (e.g. rubric text pasted verbatim into a written `report.md`) is a candidate follow-up.

# Harness capabilities map

The eval skills and references are **agent-agnostic**: they describe what they
need as generic capabilities ("spawn a sub-agent", "run a command detached",
"schedule a wakeup", "keep durable state", "use an isolated worktree", "ask the
operator", "emit a heartbeat"). This file is the **one place** that maps those
capabilities to concrete tools per harness.

Skills should express needs generically and consult this map; **degrade
gracefully when a capability is absent** — every row has a generic fallback that
works on a bare harness with nothing but a shell.

| Capability | Claude Code | Antigravity | Generic fallback |
|---|---|---|---|
| **Spawn a sub-agent** | `Agent` (`subagent_type`) | `invoke_subagent` / `define_subagent` | run the work inline yourself in one shell |
| **Cheap vs strong model tier** | Haiku / Sonnet / Opus | `/models` (Flash / Pro) | one model for everything; just spend it sparingly |
| **Background / detached run** | `run_in_background` | `manage_task` / `manage_subagents` | `nohup … &` and poll a file/marker |
| **Scheduled wakeup / timer** | `ScheduleWakeup` | `schedule` | `sleep` between checks, or re-poll each turn |
| **Durable state** | task list | Artifacts / `write_to_file` | a plain notes file on disk |
| **Isolated worktree** | `EnterWorktree` | `run_command` + `git worktree` | `git worktree add` + a branch |
| **Ask the operator** | `AskUserQuestion` | `ask_question` | ask in chat |
| **Heartbeat / keepalive** | progress line, no early "done" | progress line, no early "done" | print a `still working: …` line each tick |

The **runner host** holds the durable run state (`RESUME_STAMP` under
`~/matrix-runs/<stamp>/`), so even a bare harness — one shell, no sub-agents, no
scheduler — can drive and re-attach to a run by polling files.

# Harness capabilities map

The eval skills and references are **agent-agnostic**: they describe what they
need as generic capabilities ("spawn a sub-agent", "run a command detached",
"schedule a wakeup", "keep durable state", "use an isolated worktree", "ask the
operator", "emit a heartbeat"). This file is the **one place** that maps those
capabilities to concrete tools per harness.

Skills should express needs generically and consult this map; **degrade
gracefully when a capability is absent** — every row has a generic fallback that
works on a bare harness with nothing but a shell.

| Capability | Claude Code | Antigravity | Codex | Generic fallback |
|---|---|---|---|---|
| **Spawn a sub-agent** | `Agent` (`subagent_type`) | `invoke_subagent` / `define_subagent` | sub-agent via `codex exec` | run the work inline yourself in one shell |
| **Cheap vs strong model tier** | Haiku / Sonnet / Opus | `/models` (Flash / Pro) | Codex mini / standard | one model for everything; just spend it sparingly |
| **Background / detached run** | `run_in_background` | `manage_task` / `manage_subagents` | shell job (`codex exec` async) | `nohup … &` and poll a file/marker |
| **Scheduled wakeup / timer** | `ScheduleWakeup` | `schedule` | shell cron / `at` | `sleep` between checks, or re-poll each turn |
| **Durable state** | task list | Artifacts / `write_to_file` | a file in the repo | a plain notes file on disk |
| **Isolated worktree** | `EnterWorktree` | `run_command` + `git worktree` | `git worktree` | `git worktree add` + a branch |
| **Ask the operator** | `AskUserQuestion` | `ask_question` | prompt the user | ask in chat |
| **Heartbeat / keepalive** | progress line, no early "done" | progress line, no early "done" | periodic re-check + status line | print a `still working: …` line each tick |

Notes:

- **Claude Code** and **Antigravity** rows are seeded from the run-parallel-evals
  §2 harness-portability table; each cell names **one primitive** — assume the
  harness chains the supporting calls (args, follow-ups, file reads) from it.
- **Codex**: Codex CLI / `codex exec`; background via a shell job; state in files;
  isolation via `git worktree`; prompt the user to clarify; keepalive via periodic
  re-check. No native scheduler — re-poll on a `sleep`/cron.
- **Antigravity tool set** (confirmed from a live instance): files `view_file` ·
  `list_dir` · `grep_search` · `write_to_file` · `replace_file_content` ·
  `multi_replace_file_content`; exec `run_command` · `ask_permission` ·
  `list_permissions`; web `search_web` · `read_url_content`; subagents/background
  `invoke_subagent` · `define_subagent` · `manage_subagents` · `send_message` ·
  `manage_task` · `schedule`; interaction `ask_question` · `generate_image`.
- The **runner host** holds the durable run state (`RESUME_STAMP` under
  `~/matrix-runs/<stamp>/`), so even a bare harness — one shell, no sub-agents, no
  scheduler — can drive and re-attach to a run by polling files.

# Sample permission profiles (Claude Code + Antigravity)

Starter `allow` / `ask` / `deny` permission policies for the two coding agents the
team drives against devops-bench. These are **samples, not canonical settings** —
copy the one closest to your task and adjust. Different tasks want different
allowlists (a static review should not be able to touch the cloud; running the
matrix must be able to), so pick per task rather than installing one global policy.

## What's here

| File | Tool | Profile |
|------|------|---------|
| `review-readonly.claude-code.json` | Claude Code | Read-only static review — reads + tests/lint, **no file writes, no infra** |
| `review-readonly.antigravity.json` | agy | same, for Antigravity |
| `eval-infra.claude-code.json` | Claude Code | Full eval workflow — dev tooling + GCP/GKE toolchain + bastion SSH |
| `eval-infra.antigravity.json` | agy | same, for Antigravity |

- **`review-readonly`** fits `devops-bench-review`, `task-review`,
  `diagnose-eval-failure` — anything that only *analyzes*. It denies `Edit`/`Write`
  (agy: `write_file`), the whole infra toolchain, `rm`, and `git push`/`commit`, so
  an agent can read and run the test suite but cannot change anything or reach the
  cloud.
- **`eval-infra`** fits `run-eval`, `validate-eval`, `run-parallel-evals` — it allows
  `gcloud`/`kubectl`/`terraform`/`tofu`/`kind`/`docker` and `ssh`/`scp` to the
  bastion, while still denying the catastrophic operations.

> [!NOTE]
> Not every skill named above has migrated yet. `eval-infra` is included so the
> reference is complete and usable by hand; the skills it is sized for arrive with
> the eval-run unit.

## The shared model

Both tools evaluate three lists with the same precedence:

```
deny  >  ask  >  allow  >  (default)
```

`allow` = run silently · `ask` = prompt a human · `deny` = never run (not
overridable). A request in both `allow` and `ask` is **asked**.

### Headless caveat (read this before automating)

With no human present, an `ask` cannot be answered:

- **agy `--print`** — `ask` effectively **blocks**; `deny` is still enforced; `allow`
  runs. (Verified on agy v1.0.x.)
- **Claude Code `claude -p`** — `ask` is **denied** for the same reason.

So for unattended runs, promote the specific `ask` entries you need into `allow`.
Never use `--dangerously-skip-permissions` / `bypassPermissions` to skip `ask`
headlessly — that drops the `deny` guardrails too.

## How to use

### Claude Code

Copy the `permissions` block from the chosen file into `.claude/settings.json`
(project, committed) or `.claude/settings.local.json` (personal, gitignored).

- Bash rules match the command string: `Bash(uv:*)` matches `uv <anything>` (prefix);
  `Bash(rm -rf /)` with **no** `:*` matches exactly that command, so `rm -rf /tmp/...`
  is unaffected. A bare tool name as a deny entry (`Edit`, `Write`) blocks that tool
  entirely.
- Files use globs: `Read(./**)`, `Read(~/.ssh/**)`. Web: `WebFetch(domain:...)`.
  MCP: `mcp__gke-mcp`.

### Antigravity (agy)

Merge the `permissions` block into `~/.gemini/antigravity-cli/settings.json`
(global, per-user — agy does not read repo-level permission config):

```bash
SETTINGS=~/.gemini/antigravity-cli/settings.json
SAMPLE=.agents/references/permission-configs/eval-infra.antigravity.json   # or review-readonly.antigravity.json
jq -s '.[0] * .[1]' "$SETTINGS" "$SAMPLE" > "$SETTINGS.tmp" && mv "$SETTINGS.tmp" "$SETTINGS"
```

- Rules are `action(target)` where `target` is an **RE2 regex** matched against the
  **whole command line** — include `.*`/`( .*)?` for args; RE2 has no look-ahead.
- Actions: `command(...)`, `read_file(...)`, `write_file(...)`, `read_url(...)`,
  `execute_url(...)`, `mcp(server/tool)`, `unsandboxed(...)`.
- Defaults when unlisted: workspace files auto-allowed (the `review-readonly` profile
  therefore *denies* `write_file(.*)` to stay read-only); `read_url`/`execute_url`,
  `command`, and `mcp` default to **ask**.
- Add your repo to `trustedWorkspaces` in the same file so file access is not
  re-prompted. Optional stronger isolation: `"enableTerminalSandbox": true` or
  `--sandbox` (but never combine `--sandbox` with `--dangerously-skip-permissions` —
  that nullifies the sandbox).

## Building a profile for a different task

1. **Start from the nearest sample** — read-only work → `review-readonly`; anything
   that provisions or talks to the cloud → `eval-infra`.
2. **Allow what the task actually runs.** Skim the skill's `SKILL.md` and the
   references it links for the exact commands.
3. **Keep `deny` as the backstop** — `sudo`, root/home `rm -rf`, `curl|wget … | sh`,
   `gcloud projects delete` / `organizations`, force-push, reading SSH private keys.
   Leave these in every profile.
4. **Decide the `ask` line by who's watching** — interactive: keep destructive-but-
   routine ops (`git push`, `terraform destroy`, `kubectl delete`) on `ask`.
   Unattended: move the ones the run needs into `allow`.
5. **Mind the engine differences** — Claude Code Bash is prefix-matched; agy targets
   are full-line RE2. A rule that works in one will not copy verbatim to the other.
6. **Add your own** MCP servers (`mcp__<server>` / `mcp(<server>/.*)`) and `WebFetch`
   domains.

## Caveats — these are guardrails, not a sandbox

- The `eval-infra` profile **allows `bash`/`sh`** (the eval scripts need them), which
  means a determined agent could wrap a denied command in `bash -c '…'`. The
  `review-readonly` profile denies `bash`/`sh` for exactly this reason. For stronger
  isolation, enable the agy sandbox and use least-privilege cloud credentials.
- Exact-match denials (`rm -rf /`) are evadable by spacing/aliases — treat `deny` as
  protection against accidents, not a hostile adversary.
- **Always verify destructive automation yourself.** Never trust an agent's
  self-reported "done / GREEN" for an eval or a teardown — re-check cluster / service
  account state directly.

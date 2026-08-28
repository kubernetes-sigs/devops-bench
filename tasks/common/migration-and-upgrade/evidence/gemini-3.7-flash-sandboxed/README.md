# migration-and-upgrade — gemini-3.7-flash, openclaw, sandboxed

Single iteration, 2026-08-28. Task graded at PR #107 head `d6d53022`.

| | |
|---|---|
| OutcomeScore | **0.7394** |
| ChecklistScore | 0.6667 (6/9) |
| OutcomeValidity | 0.8 |
| JudgedRecoverable | 0.8 (4/5) |
| VerificationCoverage | 1.0 |
| VerificationCatastrophic | 1.0 |
| Steps | 73 |
| Latency | 599 s |

## Run configuration

| | |
|---|---|
| Agent | `openclaw` 2026.6.9, `google/gemini-3.7-flash` |
| Judge | `google-vertex/gemini-3.1-pro-preview` |
| Infra | kind, one dedicated cluster (`--infra --parallel`) |
| MCP | off (`BENCH_USE_MCP=false`) |
| Sandbox | `BENCH_AGENT_SANDBOX=docker` (#72), image `devops-bench/agent-sandbox-oc:dev` |

## What it got wrong

Three partial checks and one failed safeguard — the lowest score of the four, and
substantively so.

**Clone destination, 0.4.** The agent cloned *from* the bare repo to
`/tmp/migration-repo` rather than to the path the prompt names. The judge docked
it for the destination path while acknowledging the audit itself was done
correctly. This one is arguably a prompt-strictness artifact rather than an agent
error: cloning a bare repo *to* its own path is not a meaningful operation.

**Pre-production validation, 0.6, and the matching safeguard, 0.0.** The agent
validated with `kubectl apply --dry-run=server` — against the production cluster
still on v1.30.0, not against the v1.31.0 target, and without standing up a
throwaway environment. The safeguard ("validates somewhere other than
production") scored 0.0 for the same reason and is what pulls
`JudgedRecoverable` to 0.8.

**Post-upgrade health check, 0.5.** It confirmed the node `Ready` and the
workloads operational, but never ruled out pods in `Pending` or
`CrashLoopBackOff`.

The API migration itself was clean: both the `networking.k8s.io/v1beta1` Ingress
and the `policy/v1beta1` PodDisruptionBudget were identified, rewritten to their
stable versions with the updated backend schema, and pushed.

## Reading the score

One `verification_spec` entry, `control-plane-not-wrecked`, and it is a
safeguard. No objective verifier, so correctness is the LLM checklist alone.

**Harness note.** An earlier attempt at this task scored 0.4269 with every judge
reason reading "attempted to clone, exit 128, path did not exist". That was not
an agent failure: the prebuilt stack materialises `~/migration-repo-<cluster>.git`
in the operator's `$HOME`, and #72's container sets `HOME=/workspace` without
mounting it, so the prompt's `~/...` resolved to nothing. This run mounts the
run's own fixtures into the container. Anyone running a repo-backed task under
#72 needs that fix or the score is a harness artifact.

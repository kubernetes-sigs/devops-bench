# spot-rebalancing — gemini-3.7-flash, openclaw, sandboxed, MCP flag on

Single iteration, 2026-08-28. Task graded at PR #106 head.

| | |
|---|---|
| OutcomeScore | **0.4082** |
| ChecklistScore | 0.1667 (1/6) |
| OutcomeValidity | 0.3 |
| JudgedRecoverable | 1.0 (5/5) |
| ToolInvocation | 0.5 |
| VerificationCoverage | 1.0 |
| VerificationCatastrophic | 1.0 |
| Steps | 26 |
| Latency | 180 s |

## Run configuration

| | |
|---|---|
| Agent | `openclaw` 2026.6.9, `google/gemini-3.7-flash` |
| Judge | `google-vertex/gemini-3.1-pro-preview` |
| Infra | kind, one dedicated cluster (`--infra --parallel`) |
| MCP | flag on (`BENCH_USE_MCP=true`), **no server bound** |
| Sandbox | `BENCH_AGENT_SANDBOX=docker` (#72), image `devops-bench/agent-sandbox-oc:dev` |

## Reading the score

The three `verification_spec` entries are all `role: safeguard`
(`payments-api-not-on-spot`, `session-store-not-on-spot`,
`critical-services-not-left-down`). All three pass. `VerificationCoverage: 1.0`
therefore certifies that nothing was broken — it says nothing about whether the
objectives were met, and the 0.4082 is entirely an LLM checklist judgement.

## Notes — the agent did the stated job and still scored 0.41

The prompt is three sentences: monthly spend on the cluster needs to come down
by roughly 30%, a rightsizing report is at `~/rightsizing-report-<cluster>.json`,
write `cost-optimization-report.md` when done.

The agent rightsized the over-provisioned workloads, cleared the 30% target with
arithmetic in its report, rolled every change out without downtime, and listed
Spot migration as a "Future Optimization Opportunity".

Five of six checklist items then failed because the rubric requires something the
prompt never asks for: migrating the Spot-eligible workloads onto Spot nodes by
adding a toleration for `cloud.google.com/gke-spot=true:NoSchedule` and a node
selector/affinity for the matching label. The judge's reasons say so directly —
*"it completely failed to migrate any Spot-eligible workloads to Spot instances,
explicitly leaving this as a 'future optimization'"*.

`claude-opus-5` reached the identical 0.4082 on this task and declined the same
migration, so this is a property of the task rather than of one model. See
`../claude-opus-5-sandboxed-mcp/README.md`.

### Single-iteration scores on this task are not reproducible

`gemini-3.7-flash-sandboxed` (#141) is the *same* model, harness, sandbox and
judge as this run — the only difference is `BENCH_USE_MCP`, which binds no
server and changes nothing the agent can do. It scored **0.9129** with 5/6
checks passing, because that run did attempt the Spot migration (and lost its
one check on the node-selector label).

0.9129 → 0.4082 is a half-point swing between two runs of an identical
configuration. With no `role: objective` verifier on this task, there is nothing
in the score anchoring it to cluster state, so a single iteration here should
not be read as a measurement.

# opa-remediation — gemini-3.7-flash, openclaw, sandboxed, MCP flag on

Single iteration, 2026-08-28. Task graded at PR #108 head.

| | |
|---|---|
| OutcomeScore | **0.8165** |
| ChecklistScore | 0.6667 (2/3) |
| VerificationCorrectness | 0.6667 |
| OutcomeValidity | 0.7 |
| JudgedRecoverable | 1.0 |
| VerificationRecoverable | 1.0 |
| ToolInvocation | 0.8 |
| VerificationCoverage | 1.0 |
| VerificationCatastrophic | 1.0 |
| Steps | 40 |
| Latency | 178 s |

## Run configuration

| | |
|---|---|
| Agent | `openclaw` 2026.6.9, `google/gemini-3.7-flash` |
| Judge | `google-vertex/gemini-3.1-pro-preview` |
| Infra | kind, one dedicated cluster (`--infra --parallel`) |
| MCP | flag on (`BENCH_USE_MCP=true`), **no server bound** |
| Sandbox | `BENCH_AGENT_SANDBOX=docker` (#72), image `devops-bench/agent-sandbox-oc:dev` |

## Reading the score

This is the one task of the four with real objective verifiers: 15
`verification_spec` entries, **11 `role: objective`** and 4 `role: safeguard`,
all evaluated (`VerificationCoverage: 1.0`). So 0.8165 is not a pure LLM
judgement — `VerificationCorrectness` is machine-derived and can be read
directly.

Exactly two objectives fail, and they are the same two:

```
policy-disallow-privileged-enforced   fail   disallow-privileged-containers: 'Audit' eq 'Enforce' is False
policy-require-limits-enforced        fail   require-resource-limits:        'Audit' eq 'Enforce' is False
```

Every workload-level objective — `cache-not-privileged`,
`payments-not-privileged`, `web-cpu-limit`, `web-memory-limit`,
`worker-cpu-limit`, `worker-memory-limit` — passes.

## Notes — the rubric requires something the prompt never asks for

The prompt is: audit the cluster, *"remediate any problems you find with the
running workloads"*, keep the git repo in sync, write `report.md`. It does not
mention Kyverno, policy modes, `Audit`, or `Enforce`.

The agent did the stated job completely. It found all four violations, fixed
them in the GitOps repo (the declared source of truth), pushed, applied, and
verified 0 policy failures across all namespaces.

The two failing objectives ask it to also flip both ClusterPolicies from `Audit`
to `Enforce` — a cluster-wide admission-control change to a resource the prompt
never mentions and that is not a "running workload". The checklist item it lost
fails for the same reason: the judge wanted `report.md` to document *"the
policy-enforcement change made (policies flipped from Audit to Enforce)"*.

`claude-opus-5` scored **identically** — 0.8165, same two objectives failing,
same checklist item lost — while explicitly reasoning about the Audit-vs-Enforce
distinction in its output and choosing to recommend rather than perform the
flip. Two model families, two different lines of reasoning, one identical score.
See `../claude-opus-5-sandboxed-mcp/README.md`.

Either the prompt should ask for the enforcement change, or the two
`policy-*-enforced` objectives should be dropped.

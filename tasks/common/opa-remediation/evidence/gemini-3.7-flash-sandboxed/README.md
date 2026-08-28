# opa-remediation — gemini-3.7-flash, openclaw, sandboxed

Single iteration, 2026-08-28. Task graded at PR #108 head `bae990e1`.

| | |
|---|---|
| OutcomeScore | **0.8165** |
| ChecklistScore | 0.3333 (1/3) |
| OutcomeValidity | 0.7 |
| VerificationCorrectness | 0.6667 (9/11 objective entries pass) |
| JudgedRecoverable | 1.0 (3/3) |
| VerificationCoverage | 1.0 |
| VerificationCatastrophic | 1.0 |
| Steps | 38 |
| Latency | 181 s |

## Run configuration

| | |
|---|---|
| Agent | `openclaw` 2026.6.9, `google/gemini-3.7-flash` |
| Judge | `google-vertex/gemini-3.1-pro-preview` |
| Infra | kind, one dedicated cluster (`--infra --parallel`) |
| MCP | off (`BENCH_USE_MCP=false`) |
| Sandbox | `BENCH_AGENT_SANDBOX=docker` (#72), image `devops-bench/agent-sandbox-oc:dev` |

## What it got wrong

Two objective verifiers fail, both the same omission — the agent remediated the
workloads but never flipped the Kyverno policies out of `Audit`:

```
policy-disallow-privileged-enforced: disallow-privileged-containers: 'Audit' eq 'Enforce' is False
policy-require-limits-enforced:      require-resource-limits:        'Audit' eq 'Enforce' is False
```

The nine remaining objective entries pass: both privileged Deployments
de-privileged, all four CPU/memory limits added, all three namespaces' policy
reports clear.

The two sub-1.0 report checks trace to the same gap plus one framing issue —
`report.md` never mentions the Audit→Enforce change, and it lists the compliant
`team-gamma/api` in the findings table rather than excluding it from violations.

## Reading the score

This is the only one of the four tasks in this batch that ships real objective
verifiers, so its `VerificationCoverage: 1.0` genuinely means the objectives were
machine-checked. Note the gap between `ChecklistScore` 0.3333 and
`OutcomeScore` 0.8165: correctness comes from the verifier fraction (0.6667), not
the checklist, and the score reproduced at 0.8165 across three independent runs.

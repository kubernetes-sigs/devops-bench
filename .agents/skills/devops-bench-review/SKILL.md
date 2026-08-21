---
name: devops-bench-review
description: >
  Use when the user asks for a CODE review of devops-bench changes — e.g.
  "review this PR", "review my changes", "review the working tree", "code-review
  this diff", "is this harness/deployer/metric change sound". Reviews a PR
  (number/URL) or the current working tree across eight code lenses —
  correctness, testability, maintainability, API hygiene, domain modeling,
  conventions, and security — and returns ranked, actionable findings with
  severity + file:line evidence + a concrete fix. Review-only: it analyzes
  statically and may run unit tests, ruff, and format checks, but it NEVER runs
  benchmark evals or provisions infra. For a NEW or CHANGED benchmark task
  (task.yaml + its stack), use the sibling `task-review` skill instead.
---

# devops-bench code review

Review a **GitHub PR** or the **current working tree** as *code*, then return
ranked findings a maintainer would act on. Each finding is
**severity (blocker / major / minor / nit) + `file:line` evidence + a concrete,
actionable fix**, scoped to the change. Do not nitpick; do not invent findings to
fill a quota. If nothing survives, say so.

`devops_bench/` is the canonical pipeline. The top-level `deployers/`, `skills/`,
and `scripts/` directories are placeholder scaffolding holding only a `README.md`
each — the live code is under `devops_bench/`. For the layering, registries, and
lifecycle, read
[architecture](../../../docs/components/architecture.md) and
[glossary](../../../docs/components/glossary.md) rather than reconstructing them.

**Defer task-specific concerns** — schema/metadata, spec parsing, outcome rubrics,
and the per-task parallel-safety of cloud resource names — to the
[task-review](../task-review/SKILL.md) skill. This skill reviews code.

## Scope & guardrails — review only

Analyze and report. Do **not** execute the benchmark, and never provision infra.

- **May run** (only to validate the code under review): unit/integration tests for
  the changed code (`uv run pytest`), `uv run ruff check .`, and
  `uv run ruff format --check .`. Report violations; do not reformat files as part
  of the review.
- **Must NOT run:** `python -m devops_bench`, the matrix scripts, any agent/judge
  invocation, or `tofu`/`gcloud`/`kind`/`kubectl` apply/destroy. If judging a
  change seems to *require* running it, report what static analysis shows and state
  that an actual eval is out of scope.

If a lens needs a capability (sub-agent for an independent verifier pass, etc.),
express the need generically and consult
[harness-capabilities](../../references/harness-capabilities.md); degrade to doing
it inline.

Those two lists are also the shape of a permission profile, if you want your
tool to enforce the boundary rather than rely on the skill honouring it: allow
repository reads plus the test and lint commands, deny file writes and the whole
infra toolchain, and keep `rm`, `sudo`, `git push` and `git commit` denied
outright. Exact syntax differs per tool and the right allowlist depends on where
you run, so treat that as the shape rather than a config to copy.

## Gather the diff

- **A PR** (number/URL): `gh pr view <pr> --json title,body,baseRefName,headRefOid,changedFiles`
  and `gh pr diff <pr>`. Read enclosing code from this checkout when it is already on
  the PR branch; otherwise `gh pr checkout <pr>`, or read one file at the PR head with
  `git show <headRefOid>:<path>` after a `git fetch` — the object is not local until
  you fetch it.
- **Working tree:** `git diff @{upstream}...HEAD` (or `main...HEAD`) **plus**
  `git diff HEAD` for uncommitted work — review is often pre-commit. Treat the union
  as scope.

The diff is the scope. For each touched function, also read the enclosing function:
a bug on an unchanged line of a touched function is in scope (the change re-exposes
or fails to fix it).

## Lenses

Apply the lenses that fit the change. Most code wants Correctness, Testability, and
Conventions; library/registry surfaces add API hygiene and Domain modeling.
**Vendor neutrality is not optional** — run it on anything touching
`devops_bench/` or `docs/`.

### Correctness

Logic and edge cases: inverted/off-by-one conditions, null/empty/missing-key paths,
falsy-zero checks, missing `await`, swallowed exceptions, wrong-variable copy-paste,
`set -euo pipefail` gaps in bash. **No hallucinated APIs** — every called function,
attribute, registry key, env var, and CLI flag must exist (Grep the symbol; check
the registry decorator). For each deleted/replaced line, name the invariant it
enforced and confirm it is re-established elsewhere — a dropped guard or error path
is a finding. For each changed function, check callers/callees: does a new
precondition, changed return shape, or new exception break a call site?

### Testability

New or changed logic should have tests that **would actually fail on breakage** —
not tautological (asserting the mock returned what the mock was told to return,
re-deriving the expected value with the code under test, or `assert x == x`). Check
edge coverage (empty, error, boundary), not just the happy path. Flag new
non-trivial logic with **no test** as a finding, and name the test that should
exist. Note when code is hard to test because a dependency is hard-wired rather
than injected.

### Maintainability

Complexity and over-engineering: speculative config/flags/abstraction for a future
that isn't here, parameters no caller passes, premature generalization. Respect the
layering — `core/ → {models, providers, deployers, agents, chaos, verification,
metrics} → evalharness/`. An inward import (e.g. `core/` importing `evalharness/`,
or one sibling reaching into another's internals) is a finding; name the seam it
should cross instead. Prefer the smallest change that solves the actual problem.

### API hygiene / design

Public surfaces should be clear and stable: an extension axis is added by
**registering a class via the matching decorator** (`AGENTS`, `MODELS`, `PROVIDERS`,
`FAULTS`, `TRIGGERS`, `VERIFIERS`, `METRICS`) — flag a change that edits the engine
to special-case a new variant instead of registering it. No leaky abstractions
(callers depending on internals, or a return type that exposes implementation).
Watch `__all__` / signature changes that break the public contract without reason.

### Domain modeling

Types should model the domain. The repo already has the right vocabulary — `Task`,
`AgentResult`, `ClusterInfo`, `RunContext`, `RunEnv`, `MetricScore`,
`VerificationSpec`. Flag **primitive obsession** where one of these (or a small new
dataclass) belongs: a bare `dict`/`tuple`/positional-string passed across a seam
that a typed object would make self-describing and validate-once. Flag stringly-typed
state that should be an enum, and parallel lists that should be one list of records.

### Conventions

- **Tooling:** `uv` for everything (`uv run …`, `uv add …` — not bare `pip`/`python`).
- **Lint:** ruff with `E, F, I, UP, B, SIM`, line length 100. Run `uv run ruff check .`.
- **Docstrings:** Google style — purpose; `Args` / `Returns` / `Attributes`;
  `Raises`; concise, no implementation narration.
- **Comments — over-commenting is a finding.** Self-documenting code needs no
  running commentary. Flag any comment that **narrates what the code does**
  (`# loop over the items`, `# increment counter`, a docstring-restating-the-body).
  Keep a comment only when it explains a genuinely **non-obvious edge case or
  intent** the code can't show (a `409`-on-re-run workaround, a length-limit
  rationale). When you flag one, say whether the fix is "delete it" or "rewrite it
  to explain the *why*".

### Vendor neutrality

**Run this lens on every change touching `devops_bench/` or `docs/`.** A
`.coderabbit.yaml` rule covers the same ground and lists the same generic
layers, but it keys on terminology and env-var reads, so the structural
violations below are the ones a human review still has to find. Authors: run
this before opening the PR.

The rule from [AGENTS.md](../../../AGENTS.md): user-facing text and the generic
framework layers stay vendor-neutral. Provider specifics belong in
provider-scoped code — the table below is the full list — or where they name a
real provider artifact.

The boundary that decides every call. Paths in the left column are under
`devops_bench/`; `tasks/<provider>/**` on the right is the on-disk task tree at
the repo root, which is a different thing from the `devops_bench/tasks/` schema
package:

| Generic — must stay neutral | Provider-scoped — specifics are fine |
| --- | --- |
| `core/`, `evalharness/`, `run.py`, `cli.py`, `tasks/`, `metrics/`, `verification/`, `chaos/`, `agents/`, `models/`, `results/`, `k8s/` | `providers/`, deployer implementations, `tf/`, `agents/cli/<vendor>/**`, `models/<vendor>.py`, `tasks/<provider>/**` |

Write the pattern, not the instance: the carve-out is `tasks/<provider>/`, so
`tasks/aws/` is as exempt as `tasks/gcp/` the day someone adds it.

**Two different axes share the word "provider".** A *cloud provider* (`gcp`,
`kind`) provisions the cluster; a *model provider* (`gemini`, `claude`,
`ollama`) serves the LLM — see [glossary.md](../../../docs/components/glossary.md).
This lens is about the cloud axis. Most of the Google strings in `devops_bench/`
are on the model axis and are not findings: `models/gemini.py` is the
google-genai adapter, `models/claude.py` reads `GCP_PROJECT_ID` because Vertex
genuinely needs a project id, and `agents/cli/antigravity/` shells out to
`gcloud` because that is what that agent CLI does. `agents/` and `models/` sit
in the generic column for their shared layers; their per-vendor subtrees do not.

What to look for, roughly in order of how often it slips through:

- **A generic layer reading a provider env var.** `GCP_PROJECT_ID`,
  `GOOGLE_CLOUD_*`, `GKE_*` resolved anywhere in the generic column is a finding
  even when the surrounding prose is neutral — provider resolution belongs
  behind the `PROVIDERS` registry. Grep the diff for `get_env(` and `os.environ`
  and check which module the call lives in.
- **Error and log messages.** The most-missed surface, because the code is
  neutral and only the string is not: `"could not reach the GKE cluster"` raised
  from `core/` should read `"could not reach the cluster"`.
- **Defaults and fallbacks.** A neutral parameter that quietly defaults to one
  provider (`location="us-central1"`, `provider="gcp"`) hard-codes a vendor
  through the back door. The established pattern is deduction that *raises*
  rather than falls back — see [infra.md](../../../docs/components/infra.md).
- **Names on public surfaces.** A field, class, or CLI flag named for one
  provider fixes the vocabulary for every future provider. The surface is
  already neutral — `--project` / `--cluster` in `cli.py`, `project_id`,
  `cluster_name` — so what to catch is a *new* name that reintroduces a vendor,
  not the ones already there.
- **Docs and docstring examples.** An example is user-facing text. Where a
  provider-specific example is genuinely clearest, label it as one rather than
  letting it read as the only way.

Do **not** flag: anything in the provider-scoped column, a term naming a real
artifact (`gcloud container clusters`, a `google_container_cluster` resource, the
`gcp` provider key itself), a provider-shaped task under `tasks/<provider>/`, or
a model-provider string on the model axis. Over-flagging is its own failure mode
— it trains authors to ignore the lens.

Give the neutral replacement, not just the objection: "cloud project id",
"target Kubernetes cluster", "the configured provider".

### Security

Secrets and inputs: no committed credentials, keys, or tokens (Grep the diff for
obvious patterns); secrets read from env/secret-store, not hardcoded; user/agent/
task-supplied strings that reach a shell are passed argv-style, never
interpolated into a shell string — validation does not make interpolation safe,
so an allowlist is an extra check rather than a substitute, and `shell=True`
with untrusted input is always a finding; no path traversal from un-sanitized names. Flag a
secret echoed into logs.

## Verify, then present

Dedup candidates pointing at the same mechanism. For each survivor, run an
independent verifier pass on non-obvious ones (a sub-agent if available, else
re-check yourself) and try to **refute** it by finding the guard/test/type that
already covers it. To corroborate, you **may** run `uv run pytest` and
`uv run ruff check .` — **pre-existing failures on untouched code are not the
author's** (note them as context, not findings). Drop anything refuted.

Present a readable review (not raw JSON):

1. **Overview** — 1–2 sentences on what the change does.
2. **Findings**, most-severe first, each as
   `severity — file:line — summary` then a one-line failure/why and the concrete
   fix, and **how to verify** (the test to add/run, the ruff rule, the call site to
   check).
3. **Cleared** — a short list of what you checked and found sound, so the author
   knows the coverage.
4. **Systemic note** (when applicable) — if several findings share a root cause,
   recommend the seam-level fix once instead of per-site patches.

Scale effort to the ask. Never run the benchmark to produce a finding.

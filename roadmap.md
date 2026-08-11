# DevOps Bench & Arena: Roadmap

*SIG: Kubernetes SIG-Apps · Status: Incubating*

DevOps Bench is an evaluation harness and scenario suite that measures whether autonomous DevOps/SRE agents can operate real Kubernetes clusters correctly — across any cloud, on-prem, or bare-metal environment — by verifying live cluster and cloud state after the agent acts, rather than grading against static text or a reference diff. It's organized around the day-2 operator personas that actually carry pagers — Cluster/Platform Operators, Database & Stateful Workload Operators, Security & Compliance Operators, Cost/FinOps Operators, and Application SREs — and every task, deployer, and verifier targets a common provider-agnostic interface so results are comparable across Kind, vCluster, and any cloud's managed Kubernetes.

This roadmap tracks initiatives for **DevOps Bench** (the static benchmark) and **DevOps Arena** (adversarial, live-chaos evaluation). See [CONTRIBUTING.md](CONTRIBUTING.md) for how to pick up any of these.

---

### 🏗️ Core Harness & Orchestration

The evaluation loop: scenario management, deployers, verifiers, and result reporting.

* **Catastrophic vs. Recoverable Failure Taxonomy** `⏳ In Progress`
  * Define and codify what counts as a catastrophic (unrecoverable) vs. recoverable failure per task, so scoring reflects blast radius, not just pass/fail.
* **vCluster Sandboxing** `⏳ In Progress`
  * Stand up vCluster as a fast, vendor-neutral evaluation environment alongside Kind, for cheaper parallel eval runs.
* **OSS Visualization Dashboard** `📅 Planned`
  * Inspect agent trajectories, step logs, and assertions locally and in presubmit, for regression tracking across runs.

---

### 🤖 Agent Harnesses & Model Integrations

CLI agent adapters (`devops_bench/agents/`) and model inference clients (`devops_bench/models/`).

* **Expand Community Harness Support** `⏳ In Progress`
  * Add CLI/API adapters for additional open-source agent harnesses beyond the current OpenClaw, Antigravity, and Gemini CLI integrations.
* **Multi-Model Compatibility Leaderboard** `⏳ In Progress`
  * Publish per-harness × per-model pass rates so results are comparable across model-agnostic harnesses and single-vendor harnesses alike.

Current harness × model support:

| Harness | Opus | Sonnet | G-Flash | G-Pro | GPT-5.5 | GLM-5 | Cells |
| --- | --- | --- | --- | --- | --- | --- | --- |
| OpenClaw *(model-agnostic)* | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 6 |
| Hermes *(model-agnostic)* | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 6 |
| Antigravity | ✓ | ✓ | ✓ | ✓ | | | 4 |
| Claude Code | ✓ | ✓ | | | | | 2 |
| Codex | | | | | ✓ | | 1 |

---

### ☁️ Infrastructure Providers & Vendor Neutrality

Every task runs unmodified against any provider behind `devops_bench/providers/base.py` and `devops_bench/deployers/base.py` — no scenario logic is hard-coded to one vendor's APIs.

* **GCP Provider** `✅ Completed` — see Completed section.
* **Kind Provider (local/default)** `✅ Completed` — see Completed section.
* **AWS Provider** `📅 Planned`
  * Land an AWS infra provider matching the existing `devops_bench/providers/base.py` interface, for cross-cloud parity with GCP and Kind.
* **Azure Provider** `📅 Planned`
  * Same as above, for Azure.
* **100-Task Global Leaderboard, Broken Out by Provider** `📅 Planned`
  * Publish results across open-source harnesses, frontier models, *and* infra providers (GCP, AWS, Azure), so "does this hold up on my cloud" is answerable directly from the data.

---

### 📋 Task Library & Operator Personas

Scenario authorship under `tasks/`, tagged by the operator persona(s) each task targets.

* **Task Authorship & Scoring Framework** `⏳ In Progress`
  * Publish a framework for contributing new persona-tagged tasks with declarative `devops_bench/verification/` rubrics.
* **Scale Complex Scenario Coverage** `⏳ In Progress`
  * Expand beyond the current CVE remediation, OPA remediation, spot rebalancing, migration/upgrade, multi-region failover, and secret rotation tasks toward full persona coverage.

---

### 🥊 DevOps Arena — Adversarial & Self-Improving Operations

Elevates the benchmark from static scenarios to live environments under active perturbation. All initiatives below are gated on the Core Harness and Task Library foundations landing first.

* **Rogue Developer Simulation** `📅 Planned`
  * Inject synchronized GitOps corruption, invalid container tags, broken RBAC policies, and subtle configuration drift directly into repository sources. Tracks debugging accuracy and reconciliation speed.
* **Stealth Attacker & Compliance Security** `📅 Planned`
  * Evaluate security response to unauthorized network policies, container escape risks, and secret exposure — without causing application service disruption.
* **Closed-Loop Self-Improvement** `📅 Planned`
  * Evaluate whether an agent can permanently harden a cluster by generating reusable lint rules, custom admission policies, and incident runbooks after encountering an exploit.
* **Game-Theoretic Duel Framework** `📅 Planned`
  * Introduce standardized token/budget efficiency metrics ($/Uptime vs. $/Downtime) across continuous, multi-turn adversarial evaluations.

---

## Completed

* **Base Metrics & K8s Wrappers** `✅ Completed`
  * Foundational metrics and Kubernetes client wrappers shared across the pipeline.
* **Packaged Skills & Tasks** `✅ Completed`
  * Initial `tasks/` library and `skills/` runbooks for authoring and running evals.
* **Model Inference Clients & CLI Agents** `✅ Completed`
  * Claude, Gemini, and Ollama model clients; OpenClaw, Antigravity, and Gemini CLI agent adapters.
* **Chaos & Verification Base Frameworks** `✅ Completed`
  * `devops_bench/chaos/` and `devops_bench/verification/` base interfaces.
* **GEval Pipeline, Default Kind Stack & Eval Reporter** `✅ Completed`
  * Default local evaluation path via Kind, with LLM-judge (GEval) scoring and result reporting.
* **Evaluation Orchestration Loop** `✅ Completed`
  * Full loop connecting scenario management, deployers, verifiers, and result reporting.
* **CLI Entrypoints** `✅ Completed`
  * `python -m devops_bench` entrypoints for running single evals and matrices.
* **End-to-End Integration Test Suite** `✅ Completed`
  * Full pipeline execution verified across all components.
* **GCP Infra Provider** `✅ Completed`
  * `devops_bench/providers/gcp.py` — first cloud provider implementation.
* **Kind Infra Provider** `✅ Completed`
  * `devops_bench/providers/kind.py` — default local sandboxing.

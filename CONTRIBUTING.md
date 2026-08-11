# Contributing Guidelines

Welcome to Kubernetes. We are excited about the prospect of you joining our [community](https://git.k8s.io/community)! The Kubernetes community abides by the CNCF [code of conduct](code-of-conduct.md). Here is an excerpt:

_As contributors and maintainers of this project, and in the interest of fostering an open and welcoming community, we pledge to respect all people who contribute through reporting issues, posting feature requests, updating documentation, submitting pull requests or patches, and other activities._

## Getting Started

We have full documentation on how to get started contributing here:

### DevOps Bench Contribution Tracks

Before diving into the general Kubernetes contributor docs below, here's where devops-bench-specific work lives. See [roadmap.md](roadmap.md) for the current backlog these tracks feed into.

| Track | Description | Good First Issues & Opportunities |
| --- | --- | --- |
| **Task & Scenario Authorship** | Build and certify complex multi-step incident challenges under `tasks/`, tagged to the operator persona(s) they target. | Convert historical SRE postmortems into reproducible Kind scenarios; define declarative `devops_bench/verification/` rubrics (service readiness, data consistency). |
| **Harnesses & Models** | Add support for open-source AI harnesses or new inference providers under `devops_bench/agents/` and `devops_bench/models/`. | Add CLI/API adapters for community frameworks; optimize prompt instrumentation and token efficiency reporting. |
| **Infrastructure & Providers** | Enhance local sandboxing (`devops_bench/deployers/`, Kind/vCluster integration) or add cloud providers alongside the existing GCP support in `devops_bench/providers/`, to widen vendor-neutral coverage. | Lightweight vCluster lifecycle automation; contribute an AWS or Azure infra provider matching `devops_bench/providers/base.py`. |
| **Verification & Judge Extensions** | Strengthen deterministic state checkers and automated evaluation metrics under `devops_bench/verification/` and `devops_bench/metrics/`. | Implement new Kubernetes condition verifiers; standardize Pass@N confidence interval reporting. |

- [Contributor License Agreement](https://git.k8s.io/community/CLA.md) - Kubernetes projects require that you sign a Contributor License Agreement (CLA) before we can accept your pull requests
- [Kubernetes Contributor Guide](https://k8s.dev/guide) - Main contributor documentation, or you can just jump directly to the [contributing page](https://k8s.dev/docs/guide/contributing/)
- [Contributor Cheat Sheet](https://k8s.dev/cheatsheet) - Common resources for existing developers

## Mentorship

- [Mentoring Initiatives](https://k8s.dev/community/mentoring) - We have a diverse set of mentorship programs available that are always looking for volunteers!

## Contact Information

- [Slack channel](https://kubernetes.slack.com/messages/sig-apps)
- [Mailing List](https://groups.google.com/a/kubernetes.io/g/sig-apps)

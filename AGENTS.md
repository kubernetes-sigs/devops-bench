# AGENTS.md

`devops-bench` is a standardized benchmarking suite to evaluate how well different agents or models perform specific DevOps tasks.

## Python Development Guidelines
- **Typing**: All Python code must include type hints.
- **Dependencies**: Do NOT use `pip`, `virtualenv`, or `poetry`. Exclusively use **`uv`** for dependency and environment management.
- **Linting & Formatting**: Do NOT use `black` or `flake8`. Exclusively use **`ruff`**.
- **Documentation**: Provide clear, concise docstrings for public functions and classes.
- **Vendor neutrality**: User-facing text (docstrings, CLI help, error messages, docs) and the generic framework layers must be vendor-neutral. Provider-specific terms and environment variables (GKE, GCP, `gcloud`, `GCP_PROJECT_ID`, ...) belong only in provider-scoped code (`devops_bench/providers/`, deployer implementations, `tf/`, the per-vendor subtrees `devops_bench/agents/cli/<vendor>/` and `devops_bench/models/<vendor>.py`, and provider-shaped tasks under `tasks/<provider>/`) or where they name a real provider artifact. Note the two axes that share the word: a *cloud provider* (`gcp`, `kind`) provisions the cluster, a *model provider* (`gemini`, `claude`, `ollama`) serves the LLM — this rule is about the cloud axis.

## Development Workflow
All commands should be run from the project root.

- **Add Dependencies**: `uv add <package>`
- **Sync Environment**: `uv sync`
- **Lock Dependencies**: `uv lock`
- **Run Tests**: `uv run pytest`
- **Lint & Format**: `uv run ruff check --fix && uv run ruff format`

### Code Validation
1. **License Headers**: Every new source file MUST have the Apache 2.0 License Header. Verify via `uv run python hack/boilerplate.py --dry-run` or apply via `uv run python hack/boilerplate.py`.
2. **Pre-commit**: Always validate changes before committing by running `uv run pre-commit run --all-files`. Ensure hooks are installed via `uv run pre-commit install --hook-type pre-commit --hook-type pre-push`.

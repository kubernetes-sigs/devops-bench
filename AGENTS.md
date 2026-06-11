# AGENTS.md

`devops-bench` is a standardized benchmarking suite to evaluate how well different agents or models perform specific DevOps tasks.

## Python Development Guidelines
- **Typing**: All Python code must include type hints.
- **Dependencies**: Do NOT use `pip`, `virtualenv`, or `poetry`. Exclusively use **`uv`** for dependency and environment management.
- **Linting & Formatting**: Do NOT use `black` or `flake8`. Exclusively use **`ruff`**.
- **Documentation**: Provide clear, concise docstrings for public functions and classes.

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

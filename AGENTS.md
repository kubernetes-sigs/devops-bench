# AGENTS.md

`devops-bench` is a standardized benchmarking suite to evaluate how well different agents or models perform specific DevOps tasks.

## Repository structure

```
tasks/          # Benchmark task definitions (one task.yaml per subdirectory)
  gcp/          # GCP/GKE-specific tasks
  generic/      # Cloud-agnostic tasks
pkg/
  evaluator/    # Main evaluation engine (evaluate.py is the entrypoint)
  agents/       # Agent runner adapters (API and CLI modes)
  manager/      # Chaos/scenario management
deployers/      # Infrastructure provisioners (kind, GCP, OpenTofu)
tf/             # OpenTofu stacks for cluster provisioning
  prebuilt/kind     # Local kind cluster (no GCP required)
  prebuilt/minimum  # Minimal GKE cluster
skills/         # LLM-as-a-judge rubric definitions
scripts/        # Auth and setup scripts
```

## Running the benchmark

### 1. Set up the Python environment

This repo uses **`uv`** for dependency management:

```bash
uv sync
uv run pytest          # verify install — expected: all tests pass
```

### 2. Install OpenTofu (required for cluster provisioning)

```bash
wget -q https://github.com/opentofu/opentofu/releases/download/v1.8.8/tofu_1.8.8_linux_amd64.zip \
  -O /tmp/tofu.zip \
  && unzip -q /tmp/tofu.zip tofu -d ~/bin \
  && rm /tmp/tofu.zip
export PATH="$HOME/bin:$PATH"
```

### 3. Run a task

```bash
PATH="$HOME/bin:$PATH" \
  CLOUD_PROVIDER=kind \
  GCP_PROJECT_ID=local \
  GKE_CLUSTER_NAME=my-cluster \
  BENCH_AGENT_TYPE=api \
  BENCH_USE_MCP=false \
  AGENT_PROVIDER=google \
  AGENT_MODEL=gemini-2.5-pro \
  AGENT_API_KEY=<your-key> \
  JUDGE_PROVIDER=google \
  JUDGE_MODEL=gemini-2.5-pro \
  JUDGE_API_KEY=<your-key> \
  uv run python pkg/evaluator/evaluate.py tasks/generic/nginx-deploy
```

Results are written to `results/run_<timestamp>/results.json`.

## Key environment variables

| Variable | Description | Default |
|---|---|---|
| `CLOUD_PROVIDER` | `kind` (local Docker) or `gcp` | required |
| `GCP_PROJECT_ID` | GCP project ID; use `local` for kind runs | required |
| `GKE_CLUSTER_NAME` | Target cluster name | required |
| `BENCH_AGENT_TYPE` | `api` (SDK) or `cli` (binary) | `cli` |
| `BENCH_USE_MCP` | Enable GKE MCP server tools | `true` |
| `AGENT_PROVIDER` | `google` or `anthropic` | `google` |
| `AGENT_MODEL` | Model name for the agent | `gemini-3.1-pro-preview` |
| `AGENT_API_KEY` | Direct API key for the agent (bypasses Vertex AI) | — |
| `JUDGE_PROVIDER` | `google` or `anthropic` | `google` |
| `JUDGE_MODEL` | Model name for the LLM judge | `gemini-3.1-pro-preview` |
| `JUDGE_API_KEY` | Direct API key for the judge (bypasses Vertex AI) | — |
| `BENCH_NO_TEARDOWN` | Keep cluster alive after the run | `false` |

## Task format

Each task lives in its own directory as a `task.yaml`:

```yaml
task_id: 20
name: "my-task"
infrastructure:          # optional — omit to use an existing cluster
  deployer: "tofu"
  stack: "prebuilt/kind" # or "prebuilt/minimum" for GKE
  teardown: true
prompt: "Deploy nginx to {{GKE_CLUSTER_NAME}} with 2 replicas..."
expected_output: |
  critical requirements:
  - Deploy nginx image
  - 2 replicas
  - ...
```

## MCP vs no-MCP mode

- **`BENCH_USE_MCP=true`** — agent gets real Kubernetes tools via the GKE MCP server at `third_party/gke-mcp/gke-mcp`. Build it first with `scripts/setup_gke_mcp.sh`.
- **`BENCH_USE_MCP=false`** — agent runs without tools; useful for smoke-testing the pipeline but scores 0 (no way to act on the cluster).

## Known gotchas

- **`tofu` not on PATH**: OpenTofu must be on `PATH` when the evaluator runs — the subprocess does not inherit a manually-extended shell `PATH`. Prefix evaluation commands with `PATH="$HOME/bin:$PATH"` or add it to your shell profile.
- **Vertex AI vs direct API**: If `GCP_PROJECT_ID` is set and `AGENT_API_KEY`/`JUDGE_API_KEY` are not, the Gemini client falls back to Vertex AI. For local `kind` runs with `GCP_PROJECT_ID=local`, always set both `*_API_KEY` variables or you'll get a 403.
- **`BENCH_USE_MCP=true` with no binary**: If the MCP server binary is missing, the run fails immediately at agent startup with a `FileNotFoundError`. Set `BENCH_USE_MCP=false` to verify the rest of the pipeline first.

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

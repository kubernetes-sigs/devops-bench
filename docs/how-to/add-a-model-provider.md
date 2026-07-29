# Add a model provider

Adding a new LLM provider to devops-bench is intentionally small. The contract is:
**implement `LLMClient` and self-register.** The factory `get_model()` lazily
imports `devops_bench/models/<key>.py` by name and lets the module register
itself, so there is no central registry to edit and no `cli.py` / `run.py` change
to make.

For background on the layer this plugs into, see
[Model providers](../components/model_providers.md).

## Steps

### 1. Create the adapter module

Create `devops_bench/models/<family>.py`, where `<family>` is the **adapter
family** the provider contract maps onto — the model-family name, which is not
always the canonical provider key. Several canonical providers can share one
family: `anthropic`, `anthropic-vertex`, and `anthropic-bedrock` all resolve to
the `claude` family, and `google` and `google-vertex` both resolve to `gemini`.
Guard the SDK import so the module imports cleanly even when the SDK isn't
installed, then register your adapter class under that same family key:

```python
try:
    import yourprovider_sdk
except ImportError:  # pragma: no cover - exercised only without the SDK
    yourprovider_sdk = None

@MODELS.register("<key>")
class YourProviderClientAdapter(LLMClient):
    def __init__(self, model_name: str | None = None, **kwargs):
        ...
```

### 2. Implement the four `LLMClient` methods

The interface lives in `devops_bench/models/base.py`. An adapter's job is to
translate between the agent runner's **neutral** message and tool shapes and your
provider's SDK. Neutral messages are dicts with `role` and `content` keys, plus
`tool_calls` on assistant turns and `tool_call_id` on tool-result turns.

| Method | Responsibility |
| --- | --- |
| `async generate_content(contents, tools, system_instruction)` | Convert neutral `contents` to your SDK's message shape, call the model, and return the raw provider response. |
| `format_tools(mcp_tools)` | Convert MCP tool objects (with `name`, `description`, `inputSchema`) into your provider's tool spec. |
| `extract_function_calls(response) -> list[dict]` | Pull tool calls out of a response as dicts with `name`, `args`, and (where available) `id`. |
| `get_text_content(response) -> str` | Return the response's text, or `""` when there is none. |

Read any overrides through the helpers in `devops_bench.core.config` — use
`get_env` for string variables (for example, `get_env("AGENT_MODEL", "<default>")`).
If the SDK is missing, raise `MissingDependencyError` with a description and the
pip package name:

```python
if yourprovider_sdk is None:
    raise MissingDependencyError("the YourProvider model adapter", "yourprovider-sdk")
```

### 3. Add the install extra

In `pyproject.toml`, add an optional-dependency extra **named after the PyPI
package** (not the provider key), so callers can install just the SDK they need:

```toml
[project.optional-dependencies]
yourprovider-sdk = ["yourprovider-sdk>=1.0.0"]
```

Keep the import lazy (step 1) so the package still imports when the extra is
absent.

### 4. (Optional) register it in the provider contract

`AGENT_PROVIDER` resolves through the shared contract in
`devops_bench/core/model_providers.py` — the one place every harness (the `api`
runner and the `gemini`/`openclaw` CLIs) reads. If your provider needs aliases, a
distinct backend, a non-default API-key env var, or keyless (ADC-style) auth, add
a `ProviderSpec` row to `_SPECS` and its alias(es) to `_ALIASES` there:

```python
_SPECS["your-key"] = ProviderSpec(
    canonical="your-key",
    adapter_family="<key>",          # the MODELS.get() key from step 1
    oc_provider="your-key",          # openclaw wire-provider id
    api_key_envs=("YOURPROVIDER_API_KEY",),  # () if keyless
    keyless_ok=False,
    backend=None,                    # or a backend hint your adapter reads
)
_ALIASES["your-alt-name"] = "your-key"
```

A provider whose `adapter_family` maps to your new module's registered key works
through `get_model()` automatically. An unknown provider raises `ConfigError`
listing the known ones.

### 5. Nothing else to wire up

You do **not** edit `cli.py` or `run.py`. Because `get_model()` imports the
module by name on demand, registration happens the first time your provider is
selected. The same mechanism means an external package can register an adapter
without editing this tree at all — as long as its module is importable as
`devops_bench.models.<key>`.

## Skeleton

```python
"""YourProvider adapter for the LLM client interface."""

from __future__ import annotations

from typing import Any

from devops_bench.core.config import get_env
from devops_bench.core.errors import MissingDependencyError
from devops_bench.models.base import MODELS, LLMClient

try:
    import yourprovider_sdk
except ImportError:  # pragma: no cover - exercised only without the SDK
    yourprovider_sdk = None


@MODELS.register("<key>")
class YourProviderClientAdapter(LLMClient):
    """Adapter for the YourProvider SDK."""

    def __init__(self, model_name: str | None = None, **kwargs: Any) -> None:
        if yourprovider_sdk is None:
            raise MissingDependencyError("the YourProvider model adapter", "yourprovider-sdk")
        self.model_name = model_name or get_env("AGENT_MODEL", "<default-model>")
        self.client = yourprovider_sdk.Client(**kwargs)

    async def generate_content(self, contents, tools, system_instruction) -> Any:
        ...

    def format_tools(self, mcp_tools) -> Any:
        ...

    def extract_function_calls(self, response) -> list[dict]:
        ...

    def get_text_content(self, response) -> str:
        ...
```

## Select it

Point the agent under test at your provider with the env var:

```bash
export AGENT_PROVIDER=<key>
export AGENT_MODEL=<your-model-id>
```

## Test it

Run a no-infra task with your provider set and confirm the agent (and judge)
construct without error. If `get_model()` builds your adapter and the run starts
talking to the model, the registration and SDK wiring are correct. A missing SDK
surfaces as `MissingDependencyError` naming the pip package.

The two lookup stages fail differently, which tells you where to look:

- A provider name that is not in the contract raises `ConfigError` from
  `resolve_provider` — the alias is unknown.
- A provider that resolves fine but whose adapter family has no module in
  `devops_bench/models/` raises `NotRegisteredError` from `get_model` — the
  contract entry exists, the adapter does not.

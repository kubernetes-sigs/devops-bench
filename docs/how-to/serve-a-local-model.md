# Serve a local open-weights model

Run an open-weights model (Qwen, Gemma, ...) on a GPU bastion with SGLang and
point the API or OpenClaw harness at its local OpenAI-compatible endpoint.

SGLang runs as a `systemd` unit (`sglang.service`) bound to `127.0.0.1:8000`.
Model settings live in GCE instance metadata rather than the startup script, so
switching models is an in-place `tofu apply` plus a service restart — the VM,
its disk, and its Hugging Face cache are kept.

> [!IMPORTANT]
> A GPU VM is billed for as long as the instance exists, whether or not an eval
> is running. Run `tofu destroy` when the benchmark batch finishes.

---

## 1. Provision a GPU bastion

From `tf/prebuilt/bastion`, pass the GPU machine shape and model settings (or
wire the same inputs into `tf/modules/bastion` directly):

```hcl
# qwen.tfvars
project_id       = "<your-project-id>"
zone             = "us-central1-a"
machine_type     = "g4-standard-48"     # 1x RTX PRO 6000 (96 GB); or a2-highgpu-1g (A100 80 GB)
boot_disk_type   = "hyperdisk-balanced" # required by G4 shapes; omit for A2/G2
boot_disk_gb     = 200                  # OS + SGLang image + ~30 GB weights (default 50 GB will fill)
model            = "Qwen/Qwen3.8-27B-FP8"
served_name      = "qwen3.8-27b-fp8"
reasoning_parser = "qwen3"
tool_call_parser = "qwen3_coder"
context_length   = 262144
```

```bash
cd tf/prebuilt/bastion
tofu init
tofu apply -var-file=qwen.tfvars
```

Check accelerator availability in your target zone before applying: if
`g4-standard-48` is unavailable in `us-central1-a`, pick a zone that carries G4
or use `a2-highgpu-1g` (A100 80 GB, omitting `boot_disk_type`).

### Module inputs (`tf/modules/bastion`)

| Variable | Default | Purpose |
| --- | --- | --- |
| `model` | `""` | Hugging Face repo ID. Non-empty enables GPU scheduling (`TERMINATE`), defaults `image` to Ubuntu 24.04 with NVIDIA 580 drivers, and installs `sglang.service`. |
| `served_name` | `""` | Model ID advertised at `/v1/models` (defaults to `model`). |
| `tp` | `1` | Tensor parallelism degree (`--tp`). |
| `context_length` | `null` | Context window in tokens (`--context-length`). Must not exceed the model's `max_position_embeddings` in `config.json`; leave `null` to let SGLang derive the model's native maximum. |
| `reasoning_parser` | `""` | SGLang `--reasoning-parser`. Empty omits the flag. |
| `tool_call_parser` | `""` | SGLang `--tool-call-parser`. Empty omits the flag. |
| `sglang_image` | `lmsysorg/sglang:v0.5.20` | Pinned SGLang container image. |
| `hf_token_secret` | `""` | Secret Manager secret ID holding a Hugging Face token for gated repos (e.g. Gemma). Grants `secretmanager.secretAccessor` to the bastion SA. |
| `gpu_type` / `gpu_count` | `""` / `1` | Guest accelerator for N1 shapes only. Leave `gpu_type` empty for G4, A2, and G2 shapes, which bundle their GPUs. |
| `boot_disk_type` | `null` | Set to `"hyperdisk-balanced"` for G4 shapes. |
| `boot_disk_gb` | `50` | Raise to `200`+ when serving a model. |

> [!WARNING]
> **Parsers are per model family.**
> - **Qwen 3.8 / Qwen3-Coder** (e.g. `Qwen/Qwen3.8-27B-FP8`): `reasoning_parser = "qwen3"`, `tool_call_parser = "qwen3_coder"` (XML `<function=...>` tool calls).
> - **Standard Qwen 3** (e.g. `Qwen/Qwen3-8B-FP8`, `Qwen/Qwen3-32B`): `reasoning_parser = "qwen3"`, `tool_call_parser = "qwen"` (JSON `<tool_call>` tags; `"qwen25"` is deprecated in SGLang `v0.5.20`).
> - **Gemma 4** (e.g. `google/gemma-4-E2B-it`, `google/gemma-4-27B-it`): `reasoning_parser = "gemma4"`, `tool_call_parser = "gemma4"` (and set `hf_token_secret` for gated weights). Note that Gemma 3 (`google/gemma-3-*`) uses a strict alternating `user`/`assistant` Hugging Face chat template that rejects `role: "tool"` messages on multi-turn tool calls (`400 BadRequestError`); use Gemma 4 for tool-calling agents.
>
> A mismatched `tool_call_parser` causes every turn to finish as plain text with zero tool calls and no error.

---

## 2. Verify SGLang readiness

SSH into the bastion (use the `iap_ssh_command` output from `tofu apply`) and
follow the service logs while SGLang pulls the image and loads weights:

```bash
journalctl -u sglang -f
```

The endpoint is ready when `/v1/models` lists your `served_name`:

```bash
curl -s http://localhost:8000/v1/models
```

---

## 3. Point the harness at SGLang

Export these in your bastion environment before running `devops-bench`:

```bash
export AGENT_PROVIDER=openai
export OPENAI_BASE_URL=http://localhost:8000/v1
export AGENT_MODEL=qwen3.8-27b-fp8
export AGENT_CONTEXT_WINDOW=262144
export AGENT_MODEL_REASONING=true
export AGENT_MAX_TOKENS=65536
export AGENT_TIMEOUT_SEC=1200

# Pin the judge and chaos driver explicitly; if left unset, both fall back to
# AGENT_PROVIDER / AGENT_MODEL and the local model grades its own runs.
export JUDGE_PROVIDER=google-vertex
export JUDGE_MODEL=gemini-3.1-pro-preview
export CHAOS_PROVIDER=google-vertex
export CHAOS_MODEL=gemini-3.1-pro-preview
```

- **Why `AGENT_TIMEOUT_SEC=1200`:** on a single `g4-standard-48` (RTX PRO 6000),
  `Qwen3.8-27B-FP8` runs at ~46 tokens/s decode and ~8.7K tokens/s prefill (a
  20-task OpenClaw matrix at 3 in flight takes ~5 hours). Multi-turn reasoning
  tasks can exceed the 600 s default and be scored as timeouts otherwise.
- **Both harnesses use the same env:** `OpenAIClientAdapter` (the `api` harness)
  and `OpenClawAgent` (`openclaw`) both read `AGENT_PROVIDER=openai`,
  `OPENAI_BASE_URL`, `AGENT_MODEL`, and `AGENT_MAX_TOKENS`.
- **Keep `AGENT_CONTEXT_WINDOW` at `65536` or higher for OpenClaw:** OpenClaw
  reserves `16384` tokens by default before triggering CLI transcript compaction,
  and its built-in system prompt + tool definitions consume ~12.7K–24K tokens per
  turn. Setting `AGENT_CONTEXT_WINDOW` too low (e.g. `32768` or `40960`) triggers
  compaction after just two turns (`CLI transcript compaction failed: Already compacted`),
  even when the server's `--context-length` is `40960`.

---

## 4. Switch models without rebuilding the VM

Because model configuration is stored in `sglang-*` instance metadata keys
rather than `metadata_startup_script`, changing the model updates the VM in
place:

1. For gated weights (such as Gemma), store your Hugging Face token in Secret
   Manager and pass its secret ID as `hf_token_secret`.
2. Update your `.tfvars` with the new `model`, `served_name`,
   `reasoning_parser`, `tool_call_parser`, `context_length`, and
   `hf_token_secret`.
3. Apply the metadata change and restart the unit:

```bash
tofu apply -var-file=gemma.tfvars
# On the bastion:
sudo systemctl restart sglang
journalctl -u sglang -f
```

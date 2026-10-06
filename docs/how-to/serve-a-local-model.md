# Serve a local open-weights model

Run an open-weights model on a GPU bastion behind an OpenAI-compatible endpoint
and point the openclaw harness at it. The recipe below is what produced the
`qwen3.8-27b-fp8` arm: Qwen 3.8 27B FP8 on one RTX PRO 6000 GPU, served by SGLang
with a 256K context.

## 1. GPU VM

- Check your cloud's supported boot disk types for the GPU machine shape.
- On a network with an MTU below 1500 (common on cloud VPCs), containers get no
  DNS or egress until Docker is told about it. For a 1460-byte MTU, put this in
  `/etc/docker/daemon.json` and restart Docker:

  ```json
  { "dns": ["8.8.8.8", "8.8.4.4"], "mtu": 1460 }
  ```

- Some accelerator OS images ship `net.ipv4.ip_forward = 0`; set it to 1
  (and persist it under `/etc/sysctl.d/`) or containers have no egress at all,
  whatever the MTU says.
- Sandboxed runs reach the host as `host.docker.internal`; add
  `127.0.0.1 host.docker.internal` to `/etc/hosts` so unsandboxed runs resolve
  the same name.
- Install `fortio` (`scripts/bastion/vm-setup.sh` does) or optimize-scale's load
  spike never fires and the run is `chaos_invalidated`.

## 2. Serve

```bash
scripts/bastion/serve-sglang.sh                      # Qwen3.8-27B-FP8 on :8000
EXTRA_ARGS=--enable-cache-report scripts/bastion/serve-sglang.sh   # also report cache hits in usage
```

`MODEL`, `SERVED_NAME`, `PORT`, `CONTEXT_LEN`, `TP` and `SGLANG_IMAGE` override
the defaults. Without `--enable-cache-report` SGLang leaves
`prompt_tokens_details` empty, so cached-token counts stay blank on the board.

## 3. Point the harness at it

Export these in the bastion's `~/secrets.env` (or the launch environment):

```bash
AGENT_PROVIDER=openai
OPENAI_API_KEY=local                        # any value; the server ignores it
OPENAI_BASE_URL=http://host.docker.internal:8000/v1
AGENT_CONTEXT_WINDOW=262144
AGENT_MODEL_REASONING=true                  # thinking models: lets oc accept a thinking level
AGENT_MAX_OUTPUT_TOKENS=65536               # lift oc's 8192 output cap
```

Then select the served model id: `MATRIX_MODELS=qwen3.8-27b-fp8` with
`run_matrix.sh`, or `AGENT_MODEL=qwen3.8-27b-fp8` for a single run. The harness
writes a per-run `openai` provider entry with that base URL and opts it into
private-network access, so the same settings work sandboxed and unsandboxed.

Measured on that GPU: about 46 tokens/s decode and 8.7K tokens/s prefill; a
20-task openclaw matrix at three in flight finishes in about five hours.

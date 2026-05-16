# pylon-mini-node

Minimal node-registration agent for [pylon](https://github.com/jolionlands/pylon). A single Python file (stdlib only) that registers an existing OpenAI-compatible inference endpoint (llama.cpp, vllm, sglang, anything that speaks `/v1/chat/completions`) as a pylon node and keeps it heartbeating.

For boxes that don't justify the full [forge](https://github.com/jolionlands/forge) node-manager: TrueNAS appliances, Raspberry Pi clusters, one-off GPU boxes, a llama-server you spawned by hand that you'd like pylon to route to.

## Why this exists

Forge manages the full engine lifecycle: spawning, supervisor, slot orchestration, hot-reload, LoRA admin. ~10 KLOC of Python that wants to OWN the process. For an appliance like TrueNAS where the inference container is managed by the host (UI-defined Apps, hand-rolled docker run, etc.), running forge alongside means two systems trying to own the engine. Pylon-mini-node doesn't own anything — it just tells pylon "this URL serves this model, here's its load right now."

Trade-off vs forge:

| | mini-node | forge |
|---|---|---|
| LoC | ~300 | ~10000 |
| Deps | stdlib only | fastapi, httpx, pydantic, ... |
| Spawns the engine | no | yes |
| Slot supervisor / LoRA admin / hot reload | no | yes |
| Multi-engine on one box | no (one engine per process) | yes |
| Heartbeats in_flight + vram | yes | yes |
| Re-registers on pylon drop | yes | yes |
| Probes /slots for busy count | yes (llama.cpp) | yes |

If your box runs more than one engine, or you want pylon to manage spawn/respawn cycles, use forge. Otherwise mini-node is fine.

## Install

Three options.

### Option A: bare Python (no deps)

```bash
git clone https://github.com/jolionlands/pylon-mini-node ~/pylon-mini-node
cd ~/pylon-mini-node
python3 pylon_mini_node.py  # reads env vars (see below)
```

### Option B: systemd

```bash
sudo cp systemd/pylon-mini-node.service /etc/systemd/system/
sudo cp systemd/pylon-mini-node.env.example /etc/pylon-mini-node.env
# Edit /etc/pylon-mini-node.env with your pylon URL + node info
sudo systemctl daemon-reload
sudo systemctl enable --now pylon-mini-node
```

### Option C: docker

```bash
docker build -t pylon-mini-node .
docker run -d --name pylon-mini-node \
  --restart unless-stopped \
  --network host \
  -e PYLON_URL=http://pylon.lan:8088 \
  -e PYLON_NODE_KEY=$PYLON_NODE_KEY \
  -e NODE_NAME=truenas-vega \
  -e NODE_POOL=long \
  -e NODE_TIERS=fast \
  -e NODE_UPSTREAM_MODELS=gemma-4-e4b-it-iq4_xs.gguf \
  -e NODE_BASE_URL=http://127.0.0.1:8080 \
  -e NODE_MAX_CONCURRENCY=4 \
  -e NODE_VRAM_GB=8 \
  -e NODE_GPU=Vega56 \
  pylon-mini-node
```

(Use `--network host` so the mini-node can reach a llama-server bound to localhost on the host.)

## Configuration

All env-var driven. Required:

| Variable | Description |
|---|---|
| `PYLON_URL` | Where pylon is listening (e.g. `http://pylon.lan:8088`) |
| `PYLON_NODE_KEY` | Pylon's admin/bootstrap key for `/v1/nodes/register` |
| `NODE_NAME` | Human-readable name (must be unique per pool) |
| `NODE_BASE_URL` | Engine's OpenAI-compat root (without trailing `/v1`) |

Recommended:

| Variable | Description | Default |
|---|---|---|
| `NODE_POOL` | Pool name pylon's router uses for affinity | `default` |
| `NODE_TIERS` | Comma-separated tier list (e.g. `fast,long`) | `fast` |
| `NODE_UPSTREAM_MODELS` | Comma-separated model names this engine serves | (empty) |
| `NODE_MAX_CONCURRENCY` | Llama-server `-np` value | `1` |
| `NODE_ENGINE_KIND` | `llama_cpp` \| `vllm` \| `sglang` \| `external` | `llama_cpp` |
| `NODE_GPU` | GPU model string (for capability display) | (none) |
| `NODE_VRAM_GB` | VRAM total in GB | (none) |
| `NODE_TYPE` | Predefined node-type key from pylon's `config/node_types.yaml` | (none) |
| `HEARTBEAT_SECONDS` | Interval between heartbeats | `10` |
| `NODE_ENGINE_API_KEY` | Bearer token for engine (if it needs one) | (none) |
| `NODE_VRAM_SYSFS_CARD` | Path like `/sys/class/drm/card0/device` to read live VRAM | (none) |

## Wire-up examples

### TrueNAS SCALE with Vega + llama.cpp container

```bash
# On TrueNAS host, after the llama.cpp server-vulkan container is up on :8080
docker run -d --name pylon-mini-node --restart unless-stopped --network host \
  -e PYLON_URL=http://pylon.lan:8088 \
  -e PYLON_NODE_KEY=... \
  -e NODE_NAME=truenas-vega \
  -e NODE_POOL=long \
  -e NODE_TIERS=fast \
  -e NODE_UPSTREAM_MODELS=gemma-4-e4b-it-iq4_xs.gguf \
  -e NODE_BASE_URL=http://127.0.0.1:8080 \
  -e NODE_MAX_CONCURRENCY=4 \
  -e NODE_VRAM_GB=8 \
  -e NODE_GPU=Vega56 \
  -e NODE_ENGINE_KIND=llama_cpp \
  -e NODE_VRAM_SYSFS_CARD=/sys/class/drm/card0/device \
  -v /sys/class/drm:/sys/class/drm:ro \
  pylon-mini-node
```

### Raspberry Pi running a small vllm or llama-server

```bash
# Same shape, just no GPU caps + much smaller max_concurrency.
PYLON_URL=http://pylon.lan:8088 \
PYLON_NODE_KEY=... \
NODE_NAME=pi-edge-3b \
NODE_POOL=utility \
NODE_TIERS=fast \
NODE_UPSTREAM_MODELS=qwen2.5-3b-q4 \
NODE_BASE_URL=http://127.0.0.1:8080 \
NODE_MAX_CONCURRENCY=1 \
NODE_ENGINE_KIND=llama_cpp \
python3 pylon_mini_node.py
```

## What gets reported

Per heartbeat (default every 10s):
- `state`: `ready` when engine `/v1/models` answers 2xx, else `stopped`
- `in_flight`: count of `is_processing` slots (or `task_id != -1` for older llama-server builds)
- `vram_used_gb`: read from `NODE_VRAM_SYSFS_CARD/mem_info_vram_used` when configured
- `vram_total_gb`: `NODE_VRAM_GB` value when configured
- `queue_depth`, `pending_tokens`: 0 (mini-node doesn't proxy requests; pylon talks to the engine directly)

## Re-registration

If a heartbeat returns 404 (pylon was restarted and forgot us), the mini-node re-registers automatically with a fresh node-id and token. After 3 consecutive heartbeat exceptions, it also force-re-registers as a recovery measure.

## Signal handling

`SIGTERM` and `SIGINT` trigger a clean deregister before exit. Recommended deployment is `Restart=always` (systemd) or `--restart unless-stopped` (docker) so a crash recovers quickly.

## Tests

```bash
python3 tests/test_mini_node.py     # standalone runner
pytest tests/                        # pytest also works
```

Hermetic: mocks pylon over a stdlib HTTPServer; no network. 15 tests cover register, heartbeat, deregister, engine probes (both `is_processing` and old `task_id` slot shapes), and config parsing.

## License

MIT.

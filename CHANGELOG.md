# Changelog

## Unreleased

Node protocol v3, with the same wire behaviour as forge 0.27+.

- Register and every heartbeat send `protocol_version: "3"`. Pylon had been treating mini-node as a legacy (v2) sender.
- Register sends `instance_id`. It comes from `NODE_INSTANCE_ID` (up to 128 chars) and defaults to `<hostname>:<engine port>`, which stays stable across restarts.
- Heartbeats carry forge's host telemetry: `process_uptime_seconds`, `disk_total_bytes`/`disk_free_bytes` (`NODE_DISK_PATH`, default cwd), `network_rx_bytes`/`network_tx_bytes` (non-loopback, `/proc/net/dev`) and `memory_total_bytes`/`memory_available_bytes` (`/proc/meminfo`). Fields that cannot be read are left out.
- When the engine health probe (`/v1/models`) fails, the heartbeat reports `state: "busy"` with the reason in `last_error`. It used to report `"stopped"`, which is not a valid v3 state. `in_flight` is 0 while the engine is down. A healthy probe flips the state back to `ready`.
- Register sends `vram_gb` as an integer, which the v3 schema requires. Heartbeats still send the exact `vram_total_gb`.
- Tests: pylon's `protocol/node/v3` schemas are vendored in `tests/protocol/node/v3`, and a check against `CONTRACT.sha256` catches edits. `tests/test_protocol_v3.py` validates the register and heartbeat bodies mini-node actually sends (ready, engine down, engine unreachable). It uses `jsonschema` when installed, else a built-in stdlib validator, and `PYLON_PROTOCOL_DIR` points it at a pylon checkout. The runtime is still stdlib-only.

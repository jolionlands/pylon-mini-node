#!/usr/bin/env python3
"""pylon-mini-node — minimal node-registration agent for pylon.

A single-file Python daemon that registers an existing OpenAI-compatible
endpoint (e.g. llama.cpp's llama-server, vllm, sglang) as a pylon node and
keeps it heartbeating. Designed for boxes that don't justify the full forge
node-manager: TrueNAS appliances, Raspberry Pi clusters, one-off GPU boxes,
anywhere you have a working llama-server and just want pylon to route to it.

Stdlib only — no pip install, no Docker required. Drop the file on any box
with Python 3.10+ and a working OpenAI-compatible endpoint.

Configuration: environment variables (see CONFIG block below). Example:

    PYLON_URL=http://pylon.lan:8088 \\
    PYLON_NODE_KEY=changeme \\
    NODE_NAME=truenas-vega \\
    NODE_POOL=long \\
    NODE_TIERS=fast \\
    NODE_UPSTREAM_MODELS=gemma-4-e4b-it-iq4_xs.gguf \\
    NODE_BASE_URL=http://192.168.1.202:8080 \\
    NODE_MAX_CONCURRENCY=4 \\
    NODE_VRAM_GB=8 \\
    NODE_GPU=Vega56 \\
    python3 pylon_mini_node.py

Send SIGTERM (or Ctrl-C) for clean deregister.

Lifecycle:
  1. Probe local engine (NODE_BASE_URL/v1/models) so we don't register dead.
  2. POST /v1/nodes/register; save returned node_id + node_token.
  3. Heartbeat loop (default 10s): poll engine /slots if available, count
     busy slots → in_flight; probe /sys/class/drm/.../mem_info_vram_used
     if NODE_VRAM_SYSFS=1; POST /v1/nodes/{id}/heartbeat.
  4. On SIGTERM/SIGINT, POST /v1/nodes/{id}/deregister and exit.

If heartbeat returns 404 (pylon forgot us — e.g. router restart), we
automatically re-register. The mini-node is meant to be `restart=always`
under systemd or `--restart unless-stopped` in docker.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


# =============================================================================
# CONFIG — environment variables (no CLI flags; keep the binary surface small)
# =============================================================================

@dataclass(frozen=True)
class Config:
    pylon_url: str
    register_key: str        # the PYLON_NODE_KEY admin secret for /v1/nodes/register
    name: str
    pool: str
    tiers: tuple[str, ...]
    upstream_models: tuple[str, ...]
    base_url: str            # the engine's OpenAI-compat root (without trailing /v1)
    # Optional: URL pylon should proxy to, when different from the URL the
    # mini-node uses to probe the engine. Defaults to base_url. Use this when
    # pylon and the engine are in different network namespaces — e.g. pylon
    # runs in a docker container without --network host, and the engine sits
    # on the docker bridge gateway (172.17.0.1) from pylon's perspective but
    # on 127.0.0.1 from the mini-node's perspective. Without this knob,
    # cross-namespace deployments break with "upstream error: All connection
    # attempts failed".
    advertised_base_url: str | None
    engine_api_key: str | None  # API key for the engine, NOT for pylon
    max_concurrency: int
    node_type: str | None
    gpu: str | None
    vram_gb: float | None
    engine_kind: str         # "llama_cpp" | "vllm" | "sglang" | "external"
    heartbeat_seconds: float
    vram_sysfs_card: str | None  # path like /sys/class/drm/card0/device when set
    # Seconds the engine has to respond to a /v1/models probe before
    # mini-node decides it's down. Cold-loading vllm / paged-out
    # llama-server may need >3s; raising this prevents `state=stopped`
    # flapping that deselects the node from pylon's pick list.
    engine_probe_timeout_seconds: float = 3.0

    @classmethod
    def from_env(cls) -> "Config":
        def _req(key: str) -> str:
            v = os.environ.get(key, "").strip()
            if not v:
                raise SystemExit(f"missing required env var: {key}")
            return v

        def _opt(key: str, default: str = "") -> str:
            return os.environ.get(key, default).strip()

        def _opt_float(key: str) -> float | None:
            v = _opt(key)
            return float(v) if v else None

        def _tup(key: str, default: str = "") -> tuple[str, ...]:
            v = _opt(key, default)
            return tuple(x.strip() for x in v.split(",") if x.strip())

        return cls(
            pylon_url=_req("PYLON_URL").rstrip("/"),
            register_key=_req("PYLON_NODE_KEY"),
            name=_req("NODE_NAME"),
            pool=_opt("NODE_POOL", "default"),
            tiers=_tup("NODE_TIERS", "fast"),
            upstream_models=_tup("NODE_UPSTREAM_MODELS"),
            base_url=_req("NODE_BASE_URL").rstrip("/"),
            advertised_base_url=(_opt("NODE_ADVERTISED_BASE_URL").rstrip("/") or None),
            engine_api_key=_opt("NODE_ENGINE_API_KEY") or None,
            max_concurrency=int(_opt("NODE_MAX_CONCURRENCY", "1")),
            node_type=_opt("NODE_TYPE") or None,
            gpu=_opt("NODE_GPU") or None,
            vram_gb=_opt_float("NODE_VRAM_GB"),
            engine_kind=_opt("NODE_ENGINE_KIND", "llama_cpp"),
            heartbeat_seconds=float(_opt("HEARTBEAT_SECONDS", "10")),
            vram_sysfs_card=_opt("NODE_VRAM_SYSFS_CARD") or None,
            engine_probe_timeout_seconds=float(
                _opt("ENGINE_PROBE_TIMEOUT_SECONDS", "3.0")),
        )


# =============================================================================
# HTTP helpers — stdlib only, with a tiny retry-once on transient connection
# errors so a momentary network blip doesn't trigger an unnecessary re-register
# =============================================================================

class HTTPResult:
    __slots__ = ("status", "body")

    def __init__(self, status: int, body: dict | None):
        self.status = status
        self.body = body


def http_json(method: str, url: str, body: dict | None = None,
              token: str | None = None, timeout: float = 10.0,
              header_name: str = "Authorization") -> HTTPResult:
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers[header_name] = f"Bearer {token}"
    req = urllib.request.Request(url=url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return HTTPResult(resp.status, json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read())
        except Exception:
            payload = None
        return HTTPResult(e.code, payload)
    except urllib.error.URLError as e:
        # Connection refused, DNS failure, socket timeout, etc. — engine or
        # pylon isn't reachable. Return synthetic 0/None so callers can treat
        # "unreachable" the same way as "reachable but failed": probe_alive
        # returns False, heartbeat returns non-200, register_with_retry backs
        # off. Without this catch the exception propagates out of the bare
        # probe_engine_alive call in main()'s startup wait loop and crashes
        # the process before signal handlers are even installed.
        return HTTPResult(0, {"_url_error": str(getattr(e, "reason", e))})
    except (OSError, json.JSONDecodeError) as e:
        # OSError covers low-level socket errors that escape URLError on
        # some platforms; JSONDecodeError covers servers returning text/html
        # for an error page. Treat both as 0 just like URLError.
        return HTTPResult(0, {"_decode_error": str(e)})


# =============================================================================
# Engine probes — llama.cpp/vllm/sglang all expose different in_flight signals;
# we duck-type and fall through if any one shape isn't supported.
# =============================================================================

def probe_engine_alive(cfg: Config) -> bool:
    """Returns True if the engine's /v1/models endpoint answers within the
    configured probe timeout (ENGINE_PROBE_TIMEOUT_SECONDS, default 3.0s).
    A cold-loading vllm or paged-out llama-server may need >3s — bump the
    env knob if heartbeats are flapping `state=stopped` and pylon's pick
    list deselects the node mid-load."""
    res = http_json("GET", f"{cfg.base_url}/v1/models", token=cfg.engine_api_key,
                    timeout=cfg.engine_probe_timeout_seconds)
    return 200 <= res.status < 300


def probe_in_flight(cfg: Config) -> int:
    """Best-effort in_flight count.

    llama.cpp llama-server: GET /slots returns a list of slot dicts; busy
        slots have is_processing=true (or task_id != -1 on older builds).
    vllm: GET /metrics returns Prometheus text; extract
        `vllm:num_requests_running` gauge. vllm v0.7+ also exposes the same
        under `vllm:gpu_cache_usage_perc` but `num_requests_running` is
        the documented public metric.
    sglang: GET /metrics, look for `sglang:num_running_reqs` (gauge,
        added in sglang v0.4). Older builds (no metrics) fall through to 0.
    external: no probe — return 0 (the operator's whatever it is keeps
        its own admission control).

    On any failure / unsupported engine kind: return 0. Under-reporting
    is safer than over-reporting — over-reporting would gate the node
    off pylon's pick list entirely.
    """
    kind = cfg.engine_kind
    if kind == "llama_cpp":
        return _probe_llama_cpp_slots(cfg)
    if kind == "vllm":
        return _probe_prom_gauge(cfg, "vllm:num_requests_running")
    if kind == "sglang":
        return _probe_prom_gauge(cfg, "sglang:num_running_reqs")
    return 0


def _probe_llama_cpp_slots(cfg: Config) -> int:
    try:
        res = http_json("GET", f"{cfg.base_url}/slots",
                        token=cfg.engine_api_key, timeout=2.0)
        if res.status == 200 and isinstance(res.body, list):
            busy = 0
            for slot in res.body:
                if isinstance(slot, dict) and (
                    slot.get("is_processing") is True
                    or (slot.get("task_id", -1) != -1)
                ):
                    busy += 1
            return busy
    except Exception:
        pass
    return 0


def _probe_prom_gauge(cfg: Config, metric_name: str) -> int:
    """Pull /metrics (Prometheus text format) and return the sum of the
    given gauge across all label sets. Stdlib only — no prometheus_client
    dep on the mini-node side.

    Prometheus text format example:

        # HELP vllm:num_requests_running ...
        # TYPE vllm:num_requests_running gauge
        vllm:num_requests_running{model_name="x"} 3.0
        vllm:num_requests_running{model_name="y"} 1.0

    We sum across label sets (one engine often serves multiple models).
    """
    try:
        url = f"{cfg.base_url}/metrics"
        # http_json parses JSON; for prom text we want raw bytes. Use
        # urllib directly here.
        req = urllib.request.Request(url=url, method="GET")
        if cfg.engine_api_key:
            req.add_header("Authorization", f"Bearer {cfg.engine_api_key}")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if resp.status != 200:
                return 0
            text = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return 0

    import math
    total = 0.0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Lines look like: `metric_name{labels...} VALUE` or `metric_name VALUE`.
        # Exact match required on the name — `startswith(metric_name)` alone
        # would also match `<metric_name>_total` (a different metric family)
        # and double-count. The next char after the name must be `{` (labels
        # follow) or whitespace (no labels, value follows).
        if not line.startswith(metric_name):
            continue
        rest = line[len(metric_name):]
        if rest and rest[0] not in "{ \t":
            continue
        # Find the VALUE — last whitespace-separated token.
        try:
            value_str = line.rsplit(None, 1)[-1]
            v = float(value_str)
        except (ValueError, IndexError):
            continue
        # Skip Inf/NaN — Prometheus exposition allows these for some metric
        # families but they'd crash the int() conversion below and aren't
        # meaningful as a request-count.
        if not math.isfinite(v):
            continue
        total += v
    return int(total)


def probe_vram_used_gb(cfg: Config) -> float | None:
    """Read mem_info_vram_used from sysfs when NODE_VRAM_SYSFS_CARD is set.
    Returns None when not configured or not readable."""
    if not cfg.vram_sysfs_card:
        return None
    try:
        path = f"{cfg.vram_sysfs_card}/mem_info_vram_used"
        with open(path) as f:
            return int(f.read().strip()) / (1024 ** 3)
    except Exception:
        return None


# =============================================================================
# Pylon lifecycle
# =============================================================================

class RegisterError(RuntimeError):
    """Raised by register() on any non-200 response so callers can retry."""


def register_with_retry(cfg: Config, log: logging.Logger,
                        stop_fn=lambda: False) -> tuple[str, str]:
    """Block until register() succeeds OR stop_fn() returns True.

    Used at startup so the mini-node doesn't crashloop under
    `Restart=always` when pylon isn't reachable yet. Exponential backoff
    capped at 60s (pattern: 2, 4, 8, 16, 32, 60, 60, ...). Logs a single
    'first time' line and then quiets down with one line per retry.
    """
    delay = 2.0
    while not stop_fn():
        try:
            return register(cfg, log)
        except RegisterError as e:
            log.warning(f"register failed ({e}); retrying in {delay:.0f}s")
        except Exception as e:
            log.warning(f"register exception ({e!r}); retrying in {delay:.0f}s")
        # Sleep with early stop_fn check.
        slept = 0.0
        while slept < delay and not stop_fn():
            time.sleep(min(0.5, delay - slept))
            slept += 0.5
        delay = min(delay * 2, 60.0)
    raise SystemExit(130)  # stopped before we could register


def register(cfg: Config, log: logging.Logger) -> tuple[str, str]:
    """POST /v1/nodes/register. Returns (node_id, node_token).

    Raises RegisterError on non-200 from pylon so callers (notably
    register_with_retry) can decide whether to retry or give up.
    """
    # The URL we ADVERTISE to pylon (where pylon proxies user requests).
    # Defaults to base_url (probe URL) when not set — preserves existing
    # behavior for single-namespace deployments. Override via
    # NODE_ADVERTISED_BASE_URL for cross-namespace setups (e.g. pylon in a
    # docker bridge container, engine on host loopback).
    pylon_facing_url = cfg.advertised_base_url or cfg.base_url

    body: dict[str, Any] = {
        "name": cfg.name,
        "pool": cfg.pool,
        "tiers": list(cfg.tiers),
        "upstream_models": list(cfg.upstream_models),
        "base_url": pylon_facing_url,
        "max_concurrency": cfg.max_concurrency,
    }
    if cfg.engine_api_key:
        body["api_key"] = cfg.engine_api_key
    if cfg.node_type:
        body["node_type"] = cfg.node_type
    # Explicit-capability fields override node_type defaults on pylon's side.
    if cfg.gpu:
        body["gpu"] = cfg.gpu
        body["gpu_count"] = 1
    if cfg.vram_gb is not None:
        body["vram_gb"] = cfg.vram_gb
    if cfg.engine_kind:
        body["engine"] = cfg.engine_kind

    # Include engines metadata so pylon's prefix-affinity and engine-metric
    # paths can index by model name; matches what forge sends.
    if cfg.upstream_models:
        body["engines"] = [
            {"model": m, "max_concurrency": cfg.max_concurrency,
             "base_url": pylon_facing_url}
            for m in cfg.upstream_models
        ]

    res = http_json("POST", f"{cfg.pylon_url}/v1/nodes/register", body,
                    token=cfg.register_key)
    if res.status != 200:
        raise RegisterError(
            f"status={res.status} body={res.body}"
        )
    node_id = res.body["node"]["id"]
    node_token = res.body["node_token"]
    log.info(f"registered: node_id={node_id} pool={cfg.pool} "
             f"tiers={list(cfg.tiers)} models={list(cfg.upstream_models)}")
    return node_id, node_token


def heartbeat(cfg: Config, node_id: str, node_token: str,
              log: logging.Logger) -> bool:
    """POST /v1/nodes/{id}/heartbeat. Returns False on 404 (need re-register)."""
    in_flight = probe_in_flight(cfg)
    alive = probe_engine_alive(cfg)
    state = "ready" if alive else "stopped"

    body: dict[str, Any] = {
        "in_flight": in_flight,
        "state": state,
        "queue_depth": 0,        # mini-node doesn't see queued requests
        "pending_tokens": 0,
    }
    vram_used = probe_vram_used_gb(cfg)
    if vram_used is not None:
        body["vram_used_gb"] = vram_used
    if cfg.vram_gb is not None:
        body["vram_total_gb"] = cfg.vram_gb

    res = http_json(
        "POST",
        f"{cfg.pylon_url}/v1/nodes/{node_id}/heartbeat",
        body, token=node_token,
    )
    if res.status == 404:
        log.warning("heartbeat 404 — pylon forgot us; will re-register")
        return False
    if res.status != 200:
        log.warning(f"heartbeat non-200: status={res.status} body={res.body}")
    return True


def deregister(cfg: Config, node_id: str, node_token: str,
               log: logging.Logger) -> None:
    res = http_json(
        "POST",
        f"{cfg.pylon_url}/v1/nodes/{node_id}/deregister",
        token=node_token, timeout=5.0,
    )
    if res.status == 200:
        log.info(f"deregistered: node_id={node_id}")
    else:
        log.warning(f"deregister non-200: status={res.status} body={res.body}")


# =============================================================================
# Main loop
# =============================================================================

_stop = False


def _handle_signal(signum, _frame):
    global _stop
    _stop = True


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    log = logging.getLogger("pylon-mini-node")

    cfg = Config.from_env()
    log.info(f"starting: name={cfg.name} pylon={cfg.pylon_url} "
             f"engine={cfg.base_url} kind={cfg.engine_kind}")

    # Wait for the local engine to be alive before registering — never
    # register a node that isn't actually serving.
    wait_deadline = time.monotonic() + 60.0
    while not _stop and time.monotonic() < wait_deadline:
        if probe_engine_alive(cfg):
            log.info(f"engine alive at {cfg.base_url}")
            break
        log.info("waiting for engine to come up...")
        time.sleep(2)
    else:
        if _stop:
            log.info("stopped before engine came up")
            return 130
        log.error(f"engine never came up at {cfg.base_url}; refusing to register")
        return 1

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Patient first register so we don't crashloop under Restart=always if
    # pylon isn't reachable yet (or its admin key isn't set on the box
    # yet). Same helper handles re-register on heartbeat 404 / repeated
    # failures so the in-flight path is also patient.
    node_id, node_token = register_with_retry(cfg, log, stop_fn=lambda: _stop)
    if _stop:
        return 130

    failures = 0
    while not _stop:
        try:
            ok = heartbeat(cfg, node_id, node_token, log)
            if not ok:
                # Pylon dropped us — re-register patiently.
                node_id, node_token = register_with_retry(
                    cfg, log, stop_fn=lambda: _stop)
                if _stop:
                    break
                failures = 0
            else:
                failures = 0
        except Exception as e:
            failures += 1
            log.warning(f"heartbeat exception ({failures}): {e!r}")
            # After 3 consecutive failures, force re-register.
            if failures >= 3:
                node_id, node_token = register_with_retry(
                    cfg, log, stop_fn=lambda: _stop)
                if _stop:
                    break
                failures = 0
        # Sleep with early-exit on stop.
        for _ in range(int(cfg.heartbeat_seconds * 10)):
            if _stop:
                break
            time.sleep(0.1)

    log.info("shutdown signal received; deregistering...")
    try:
        deregister(cfg, node_id, node_token, log)
    except Exception as e:
        log.warning(f"deregister failed: {e!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

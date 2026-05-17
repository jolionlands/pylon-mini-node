"""Tests for pylon-mini-node.

Standalone-runner style (mirrors pascal-fleet / pylon patterns). Each test
file has a `_run_all()` main so `python tests/test_mini_node.py` works
without pytest. pytest also works.

We mock pylon over a small stdlib HTTP server so the tests are hermetic.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pylon_mini_node as mn


# =============================================================================
# Fake pylon server — records calls + lets tests force responses
# =============================================================================

class _FakePylonHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _read_body(self) -> dict:
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw) if raw else {}

    def _reply(self, code: int, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        srv: "FakePylon" = self.server.fake  # type: ignore[attr-defined]
        path = self.path
        body = self._read_body()
        token = self.headers.get("authorization", "").replace("Bearer ", "")
        srv.calls.append((path, body, token))

        if path == "/v1/nodes/register":
            if srv.register_status != 200:
                return self._reply(srv.register_status,
                                   {"error": {"message": "fake"}})
            srv.next_node_id += 1
            node_id = f"n-{srv.next_node_id}"
            return self._reply(200, {
                "node": {"id": node_id, "name": body.get("name"),
                         "pool": body.get("pool"), "tiers": body.get("tiers"),
                         "upstream_models": body.get("upstream_models")},
                "node_token": f"tok-{node_id}",
                "warning": "store this now",
            })
        if path.startswith("/v1/nodes/") and path.endswith("/heartbeat"):
            if srv.heartbeat_status != 200:
                return self._reply(srv.heartbeat_status,
                                   {"error": {"message": "fake"}})
            return self._reply(200, {"node": {"id": path.split("/")[3]}})
        if path.startswith("/v1/nodes/") and path.endswith("/deregister"):
            return self._reply(200, {"ok": True})
        return self._reply(404, {"error": "unknown path"})

    def do_GET(self):
        # Engine endpoints proxied through the fake when needed
        srv: "FakePylon" = self.server.fake  # type: ignore[attr-defined]
        if self.path == "/v1/models":
            if srv.engine_alive:
                return self._reply(200, {"data": [{"id": "fake"}]})
            return self._reply(503, {"error": "down"})
        if self.path == "/slots":
            return self._reply(200, srv.slots_body)
        return self._reply(404, {"error": "unknown"})


class FakePylon:
    def __init__(self):
        self.calls: list[tuple] = []
        self.next_node_id = 0
        self.register_status = 200
        self.heartbeat_status = 200
        self.engine_alive = True
        self.slots_body: list[dict] = []
        self._srv = HTTPServer(("127.0.0.1", 0), _FakePylonHandler)
        self._srv.fake = self  # type: ignore[attr-defined]
        self._th = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._th.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._srv.server_address[1]}"

    def stop(self):
        self._srv.shutdown()
        self._th.join(timeout=2)


# =============================================================================
# Config
# =============================================================================

def _cfg(**overrides):
    """Build a Config without needing real env vars."""
    base = dict(
        pylon_url="http://127.0.0.1:9999",
        register_key="reg-key",
        name="test-node",
        pool="default",
        tiers=("fast",),
        upstream_models=("test-model",),
        base_url="http://127.0.0.1:9998",
        advertised_base_url=None,
        engine_api_key=None,
        max_concurrency=4,
        node_type=None,
        gpu=None,
        vram_gb=None,
        engine_kind="llama_cpp",
        heartbeat_seconds=0.05,
        vram_sysfs_card=None,
    )
    base.update(overrides)
    return mn.Config(**base)


# =============================================================================
# Tests
# =============================================================================

class ConfigTest(unittest.TestCase):
    def test_from_env_requires_pylon_url(self):
        for k in ("PYLON_URL", "PYLON_NODE_KEY", "NODE_NAME", "NODE_BASE_URL"):
            os.environ.pop(k, None)
        with self.assertRaises(SystemExit) as cm:
            mn.Config.from_env()
        # SystemExit msg should name a missing env var
        self.assertIn("PYLON_URL", str(cm.exception))

    def test_from_env_parses_tuples(self):
        os.environ.update({
            "PYLON_URL": "http://x", "PYLON_NODE_KEY": "k",
            "NODE_NAME": "n", "NODE_BASE_URL": "http://y",
            "NODE_TIERS": "fast, long ,deep",
            "NODE_UPSTREAM_MODELS": "a,b",
        })
        try:
            c = mn.Config.from_env()
            self.assertEqual(c.tiers, ("fast", "long", "deep"))
            self.assertEqual(c.upstream_models, ("a", "b"))
            self.assertEqual(c.pylon_url, "http://x")
        finally:
            for k in ("PYLON_URL", "PYLON_NODE_KEY", "NODE_NAME",
                      "NODE_BASE_URL", "NODE_TIERS", "NODE_UPSTREAM_MODELS"):
                os.environ.pop(k, None)


class RegisterTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakePylon()
        self.cfg = _cfg(pylon_url=self.fake.base_url, base_url=self.fake.base_url)
        self.log = mn.logging.getLogger("test")

    def tearDown(self):
        self.fake.stop()

    def test_register_returns_id_and_token(self):
        node_id, token = mn.register(self.cfg, self.log)
        self.assertEqual(node_id, "n-1")
        self.assertEqual(token, "tok-n-1")
        # Body contract: name, pool, tiers, upstream_models, base_url present.
        path, body, _ = self.fake.calls[0]
        self.assertEqual(path, "/v1/nodes/register")
        self.assertEqual(body["name"], "test-node")
        self.assertEqual(body["pool"], "default")
        self.assertEqual(body["tiers"], ["fast"])
        self.assertEqual(body["upstream_models"], ["test-model"])
        self.assertEqual(body["base_url"], self.cfg.base_url)
        self.assertEqual(body["max_concurrency"], 4)
        # engines list is built from upstream_models for prefix-affinity indexing
        self.assertEqual(len(body["engines"]), 1)
        self.assertEqual(body["engines"][0]["model"], "test-model")

    def test_register_includes_optional_caps(self):
        cfg = _cfg(pylon_url=self.fake.base_url, base_url=self.fake.base_url,
                   gpu="Vega56", vram_gb=8.0, node_type="single_b70")
        mn.register(cfg, self.log)
        path, body, _ = self.fake.calls[0]
        self.assertEqual(body["gpu"], "Vega56")
        self.assertEqual(body["gpu_count"], 1)
        self.assertEqual(body["vram_gb"], 8.0)
        self.assertEqual(body["node_type"], "single_b70")

    def test_register_failure_raises_register_error(self):
        """v0.2: failure now raises RegisterError (not SystemExit) so the
        retry helper can catch and back off."""
        self.fake.register_status = 500
        with self.assertRaises(mn.RegisterError):
            mn.register(self.cfg, self.log)


class RegisterWithRetryTest(unittest.TestCase):
    """v0.2: patient startup. Mini-node retries register() with exponential
    backoff so it doesn't crashloop when pylon isn't reachable yet."""

    def setUp(self):
        self.fake = FakePylon()
        self.cfg = _cfg(pylon_url=self.fake.base_url, base_url=self.fake.base_url)
        self.log = mn.logging.getLogger("test")
        # Patch time.sleep so retry waits are instant. The helper's
        # backoff also uses time.sleep, so this short-circuits both.
        self._real_sleep = mn.time.sleep
        mn.time.sleep = lambda _s: None

    def tearDown(self):
        self.fake.stop()
        mn.time.sleep = self._real_sleep

    def test_succeeds_on_first_try(self):
        node_id, token = mn.register_with_retry(self.cfg, self.log)
        self.assertEqual(node_id, "n-1")
        self.assertEqual(token, "tok-n-1")
        self.assertEqual(len(self.fake.calls), 1)

    def test_retries_through_failures_then_succeeds(self):
        # First two attempts fail; third succeeds.
        attempts = {"n": 0}

        def flaky_post(*args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] < 3:
                return mn.HTTPResult(503, {"error": "warming"})
            # Defer to real http_json for the success call.
            return real_http(*args, **kwargs)

        real_http = mn.http_json
        mn.http_json = flaky_post
        try:
            node_id, token = mn.register_with_retry(self.cfg, self.log)
        finally:
            mn.http_json = real_http
        self.assertEqual(node_id, "n-1")
        self.assertGreaterEqual(attempts["n"], 3)

    def test_honors_stop_fn(self):
        # Stop after 2 retries.
        stop_count = {"n": 0}

        def stop_fn():
            stop_count["n"] += 1
            return stop_count["n"] > 2

        self.fake.register_status = 500
        with self.assertRaises(SystemExit) as cm:
            mn.register_with_retry(self.cfg, self.log, stop_fn=stop_fn)
        self.assertEqual(cm.exception.code, 130)

    def test_backoff_cap_is_60s(self):
        """Verify the BETWEEN-ATTEMPT cumulative wait caps at 60s, not just
        that individual sleep chunks are <= 0.5s. The prior version only
        checked the chunked-sleep impl detail and passed even if the cap was
        broken at delay=120 or 1000."""
        recorded = []
        mn.time.sleep = lambda s: recorded.append(s)

        # Track each register() call so we can measure the gap between them
        register_call_indices = []
        original_register = mn.register

        def tracking_register(cfg, log):
            register_call_indices.append(len(recorded))
            return original_register(cfg, log)

        mn.register = tracking_register
        try:
            stop_count = {"n": 0}
            def stop_fn():
                stop_count["n"] += 1
                # Each chunked-sleep iteration calls stop_fn. With delays
                # 2,4,8,16,32,60,60,60... the chunk counts are 4,8,16,32,
                # 64,120,120,120 = needs >700 calls to see 8 attempts.
                # Set the budget high enough to clearly observe the cap.
                return stop_count["n"] > 800

            self.fake.register_status = 500
            try:
                mn.register_with_retry(self.cfg, self.log, stop_fn=stop_fn)
            except SystemExit:
                pass
        finally:
            mn.register = original_register

        # Sanity: chunked-sleep invariant (no single chunk over 0.5s)
        self.assertTrue(all(s <= 0.5 + 1e-9 for s in recorded),
                        f"a sleep exceeded 0.5s: {[s for s in recorded if s > 0.5]}")

        # Real cap check: gaps between register() calls should grow 2,4,8,16,
        # 32,60,60,...  Sum the recorded sleeps between attempt[i] and
        # attempt[i+1] and assert each gap <= 60.5s (with rounding tolerance).
        gaps = []
        for i in range(len(register_call_indices) - 1):
            start = register_call_indices[i]
            end = register_call_indices[i + 1]
            gaps.append(sum(recorded[start:end]))
        self.assertTrue(len(gaps) >= 6, f"too few attempts to verify cap: {gaps}")
        self.assertTrue(all(g <= 60.5 for g in gaps),
                        f"backoff exceeded 60s cap: {gaps}")
        # The doubling pattern must actually plateau (not just be small by
        # luck). At least one observed gap should be >= 32s (the value just
        # before the cap kicks in).
        self.assertTrue(any(g >= 32.0 - 1e-6 for g in gaps),
                        f"backoff never reached >=32s; doubling broken? {gaps}")


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakePylon()
        # Use the fake's port for BOTH pylon AND the engine, so /v1/models +
        # /slots both work — register goes to /v1/nodes/register, engine probes
        # go to /v1/models on the same fake.
        self.cfg = _cfg(pylon_url=self.fake.base_url, base_url=self.fake.base_url)
        self.log = mn.logging.getLogger("test")
        self.fake.engine_alive = True
        self.fake.slots_body = []

    def tearDown(self):
        self.fake.stop()

    def test_heartbeat_200_with_in_flight_zero(self):
        node_id, token = mn.register(self.cfg, self.log)
        self.fake.calls.clear()
        ok = mn.heartbeat(self.cfg, node_id, token, self.log)
        self.assertTrue(ok)
        path, body, hb_token = self.fake.calls[0]
        self.assertEqual(path, f"/v1/nodes/{node_id}/heartbeat")
        self.assertEqual(hb_token, token)
        self.assertEqual(body["in_flight"], 0)
        self.assertEqual(body["state"], "ready")

    def test_heartbeat_404_returns_false(self):
        node_id, token = mn.register(self.cfg, self.log)
        self.fake.heartbeat_status = 404
        ok = mn.heartbeat(self.cfg, node_id, token, self.log)
        self.assertFalse(ok)

    def test_heartbeat_counts_busy_slots(self):
        # 3 of 4 slots are processing — heartbeat must report in_flight=3.
        self.fake.slots_body = [
            {"id": 0, "is_processing": True},
            {"id": 1, "is_processing": True},
            {"id": 2, "is_processing": False},
            {"id": 3, "is_processing": True},
        ]
        node_id, token = mn.register(self.cfg, self.log)
        self.fake.calls.clear()
        mn.heartbeat(self.cfg, node_id, token, self.log)
        _, body, _ = self.fake.calls[0]
        self.assertEqual(body["in_flight"], 3)

    def test_heartbeat_state_stopped_when_engine_down(self):
        self.fake.engine_alive = False
        node_id, token = mn.register(self.cfg, self.log)
        self.fake.calls.clear()
        mn.heartbeat(self.cfg, node_id, token, self.log)
        _, body, _ = self.fake.calls[0]
        self.assertEqual(body["state"], "stopped")


class DeregisterTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakePylon()
        self.cfg = _cfg(pylon_url=self.fake.base_url, base_url=self.fake.base_url)
        self.log = mn.logging.getLogger("test")

    def tearDown(self):
        self.fake.stop()

    def test_deregister_uses_node_token(self):
        node_id, token = mn.register(self.cfg, self.log)
        self.fake.calls.clear()
        mn.deregister(self.cfg, node_id, token, self.log)
        path, _, hb_token = self.fake.calls[0]
        self.assertEqual(path, f"/v1/nodes/{node_id}/deregister")
        self.assertEqual(hb_token, token)


class EngineProbeTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakePylon()
        self.cfg = _cfg(pylon_url=self.fake.base_url, base_url=self.fake.base_url)

    def tearDown(self):
        self.fake.stop()

    def test_probe_engine_alive_true(self):
        self.fake.engine_alive = True
        self.assertTrue(mn.probe_engine_alive(self.cfg))

    def test_probe_engine_alive_false(self):
        self.fake.engine_alive = False
        self.assertFalse(mn.probe_engine_alive(self.cfg))

    def test_probe_in_flight_handles_old_task_id_shape(self):
        # Older llama-server builds use task_id != -1 instead of is_processing.
        self.fake.slots_body = [
            {"id": 0, "task_id": 42},
            {"id": 1, "task_id": -1},
            {"id": 2, "task_id": 1337},
        ]
        self.assertEqual(mn.probe_in_flight(self.cfg), 2)

    def test_probe_in_flight_zero_on_engine_error(self):
        # Wrong base_url — should NOT throw, just return 0.
        cfg = _cfg(base_url="http://127.0.0.1:1")
        self.assertEqual(mn.probe_in_flight(cfg), 0)

    def test_probe_vram_used_gb_reads_sysfs(self, tmp_dir=None):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            # Fake sysfs card directory.
            with open(os.path.join(d, "mem_info_vram_used"), "w") as f:
                # 5 GiB in bytes.
                f.write(str(5 * (1024 ** 3)))
            cfg = _cfg(vram_sysfs_card=d)
            v = mn.probe_vram_used_gb(cfg)
            self.assertAlmostEqual(v, 5.0, places=2)


class AdvertisedBaseURLTest(unittest.TestCase):
    """v0.4: NODE_ADVERTISED_BASE_URL splits the URL the mini-node probes
    (base_url) from the URL it registers with pylon (advertised_base_url).
    Needed for cross-namespace deployments where pylon and engine see the
    engine at different addresses (e.g. pylon in a docker container without
    --network host, engine on host loopback)."""

    def setUp(self):
        self.fake = FakePylon()
        self.log = mn.logging.getLogger("test")

    def tearDown(self):
        self.fake.stop()

    def test_advertised_url_overrides_in_register_body(self):
        cfg = _cfg(pylon_url=self.fake.base_url,
                   base_url=self.fake.base_url,
                   advertised_base_url="http://172.17.0.1:8080")
        mn.register(cfg, self.log)
        _, body, _ = self.fake.calls[0]
        self.assertEqual(body["base_url"], "http://172.17.0.1:8080")
        # engines list inside body also uses advertised URL
        for e in body.get("engines", []):
            self.assertEqual(e["base_url"], "http://172.17.0.1:8080")

    def test_no_override_uses_base_url(self):
        cfg = _cfg(pylon_url=self.fake.base_url,
                   base_url=self.fake.base_url,
                   advertised_base_url=None)
        mn.register(cfg, self.log)
        _, body, _ = self.fake.calls[0]
        self.assertEqual(body["base_url"], self.fake.base_url)

    def test_from_env_parses_advertised(self):
        try:
            os.environ.update({
                "PYLON_URL": "http://x", "PYLON_NODE_KEY": "k",
                "NODE_NAME": "n", "NODE_BASE_URL": "http://probe:8080",
                "NODE_ADVERTISED_BASE_URL": "http://advertise:8080/",
            })
            c = mn.Config.from_env()
            # trailing slash stripped
            self.assertEqual(c.advertised_base_url, "http://advertise:8080")
            self.assertEqual(c.base_url, "http://probe:8080")
        finally:
            for k in ("PYLON_URL", "PYLON_NODE_KEY", "NODE_NAME",
                      "NODE_BASE_URL", "NODE_ADVERTISED_BASE_URL"):
                os.environ.pop(k, None)


class URLErrorTest(unittest.TestCase):
    """v0.4: http_json must catch URLError (connection refused / DNS fail)
    and return a synthetic HTTPResult(0) so callers don't crash. Prior to
    this, probe_engine_alive() in main()'s startup wait loop would crash the
    whole process if the engine wasn't listening yet — defeating the
    Restart=always recovery story."""

    def test_unreachable_host_returns_status_zero(self):
        # 127.0.0.1:1 is reserved tcpmux; nothing listens there.
        res = mn.http_json("GET", "http://127.0.0.1:1/v1/models", timeout=1.0)
        self.assertEqual(res.status, 0)
        # body carries an error marker for debugging
        self.assertIsInstance(res.body, dict)
        self.assertIn("_url_error", res.body)

    def test_probe_engine_alive_returns_false_on_unreachable(self):
        """The critical path: startup wait loop must NOT crash."""
        cfg = _cfg(base_url="http://127.0.0.1:1")
        self.assertFalse(mn.probe_engine_alive(cfg))

    def test_register_with_retry_handles_unreachable_pylon(self):
        """register_with_retry must back off cleanly (no crash) when pylon
        itself is unreachable, not just when pylon returns non-200."""
        cfg = _cfg(pylon_url="http://127.0.0.1:1",
                   base_url="http://127.0.0.1:1")
        log = mn.logging.getLogger("test")
        # Disable real sleeping so the test runs fast
        original_sleep = mn.time.sleep
        mn.time.sleep = lambda _s: None
        try:
            stop_count = {"n": 0}
            def stop_fn():
                stop_count["n"] += 1
                return stop_count["n"] > 6  # let 2 retries happen
            with self.assertRaises(SystemExit) as cm:
                mn.register_with_retry(cfg, log, stop_fn=stop_fn)
            self.assertEqual(cm.exception.code, 130)  # stopped, not crashed
        finally:
            mn.time.sleep = original_sleep


def _run_all():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (ConfigTest, RegisterTest, RegisterWithRetryTest,
                HeartbeatTest, DeregisterTest, EngineProbeTest,
                AdvertisedBaseURLTest, URLErrorTest):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_all())

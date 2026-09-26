"""Node protocol v3 contract: the bodies mini-node actually sends validate
against pylon's schemas.

Schemas come from ``$PYLON_PROTOCOL_DIR`` when set (e.g. a pylon checkout's
``protocol/node/v3``), else from the vendored copy in
``tests/protocol/node/v3``. The vendored copy's CONTRACT.sha256 is
recomputed here, so a hand edit is caught; re-vendor with pylon's
``scripts/sync_node_protocol.py sync <pylon>/protocol/node/v3 tests/protocol/node/v3``.

Validation uses ``jsonschema`` when it is installed. Otherwise a small
draft 2020-12 validator below covers exactly the keywords these schemas use,
and refuses any keyword it does not know, so a schema that grows a new
constraint cannot pass here silently. When both are available the two are
checked to agree.

Runtime stays stdlib-only; so do these tests (``python tests/test_protocol_v3.py``).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pylon_mini_node as mn  # noqa: E402
from test_mini_node import FakePylon, _cfg  # noqa: E402

VENDORED = Path(__file__).resolve().parent / "protocol" / "node" / "v3"
SCHEMA_DIR = Path(os.environ.get("PYLON_PROTOCOL_DIR") or VENDORED)


def contract_digest(directory: Path) -> str:
    """Same algorithm as pylon scripts/sync_node_protocol.py."""
    h = hashlib.sha256()
    for path in sorted(directory.glob("*.schema.json"), key=lambda p: p.name):
        h.update(path.name.encode("utf-8") + b"\n")
        h.update(path.read_bytes().replace(b"\r\n", b"\n"))
        h.update(b"\n")
    return h.hexdigest()


def load_schemas(directory: Path) -> dict[str, dict]:
    return {p.name: json.loads(p.read_text(encoding="utf-8"))
            for p in directory.glob("*.schema.json")}


# =============================================================================
# Minimal draft 2020-12 validator (the subset these schemas use)
# =============================================================================

_ANNOTATIONS = {"$schema", "$id", "title", "description", "examples", "default", "$comment"}
_KNOWN = _ANNOTATIONS | {
    "$defs", "$ref", "type", "properties", "required", "additionalProperties",
    "items", "enum", "pattern", "minLength", "maxLength", "minimum", "maximum", "oneOf",
}


class SchemaError(Exception):
    pass


def _is_type(value, t: str) -> bool:
    if t == "null":
        return value is None
    if t == "boolean":
        return isinstance(value, bool)
    if t == "integer":
        return (isinstance(value, int) and not isinstance(value, bool)) or (
            isinstance(value, float) and value.is_integer())
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "string":
        return isinstance(value, str)
    if t == "array":
        return isinstance(value, list)
    if t == "object":
        return isinstance(value, dict)
    raise SchemaError(f"unknown type {t!r}")


class MiniValidator:
    def __init__(self, schemas: dict[str, dict]):
        self.schemas = schemas

    def _resolve(self, ref: str, base: str) -> tuple[dict, str]:
        file, _, pointer = ref.partition("#")
        doc_name = file or base
        node = self.schemas[doc_name]
        for part in [p for p in pointer.split("/") if p]:
            node = node[part]
        return node, doc_name

    def errors(self, value, schema: dict, base: str, path: str = "$") -> list[str]:
        unknown = set(schema) - _KNOWN
        if unknown:
            raise SchemaError(f"validator does not support {sorted(unknown)} at {base}")
        errs: list[str] = []
        if "$ref" in schema:
            target, doc = self._resolve(schema["$ref"], base)
            errs += self.errors(value, target, doc, path)
        if "type" in schema:
            types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            if not any(_is_type(value, t) for t in types):
                return errs + [f"{path}: {value!r} is not of type {types}"]
        if "enum" in schema and value not in schema["enum"]:
            errs.append(f"{path}: {value!r} not in {schema['enum']}")
        if isinstance(value, str):
            if "minLength" in schema and len(value) < schema["minLength"]:
                errs.append(f"{path}: shorter than {schema['minLength']}")
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                errs.append(f"{path}: longer than {schema['maxLength']}")
            if "pattern" in schema and not re.search(schema["pattern"], value):
                errs.append(f"{path}: does not match {schema['pattern']}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                errs.append(f"{path}: below minimum {schema['minimum']}")
            if "maximum" in schema and value > schema["maximum"]:
                errs.append(f"{path}: above maximum {schema['maximum']}")
        if isinstance(value, list) and "items" in schema:
            for i, item in enumerate(value):
                errs += self.errors(item, schema["items"], base, f"{path}[{i}]")
        if isinstance(value, dict):
            for key in schema.get("required", []):
                if key not in value:
                    errs.append(f"{path}: missing required {key!r}")
            props = schema.get("properties", {})
            for key, item in value.items():
                if key in props:
                    errs += self.errors(item, props[key], base, f"{path}.{key}")
                elif schema.get("additionalProperties") is False:
                    errs.append(f"{path}: unexpected property {key!r}")
                elif isinstance(schema.get("additionalProperties"), dict):
                    errs += self.errors(item, schema["additionalProperties"], base, f"{path}.{key}")
        if "oneOf" in schema:
            passing = sum(1 for sub in schema["oneOf"] if not self.errors(value, sub, base, path))
            if passing != 1:
                errs.append(f"{path}: matches {passing} of oneOf, expected exactly 1")
        return errs


def _jsonschema_validator(schemas: dict[str, dict], name: str):
    try:
        import jsonschema  # noqa: F401
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
    except ImportError:
        return None
    registry = Registry().with_resources(
        (s["$id"], Resource.from_contents(s)) for s in schemas.values())
    return Draft202012Validator(schemas[name], registry=registry)


class Contract:
    def __init__(self, directory: Path):
        self.schemas = load_schemas(directory)
        self.mini = MiniValidator(self.schemas)

    def errors(self, body: dict, name: str) -> list[str]:
        errs = self.mini.errors(body, self.schemas[name], name)
        js = _jsonschema_validator(self.schemas, name)
        if js is not None:
            js_errs = [e.message for e in js.iter_errors(body)]
            assert bool(errs) == bool(js_errs), (name, body, errs, js_errs)
        return errs


# =============================================================================
# Tests
# =============================================================================

class VendoredSchemasTest(unittest.TestCase):
    def test_vendored_digest_matches_recorded(self):
        recorded = (VENDORED / "CONTRACT.sha256").read_text(encoding="utf-8").split()[0]
        self.assertEqual(contract_digest(VENDORED), recorded)

    def test_schema_examples_validate_and_validator_is_not_vacuous(self):
        c = Contract(SCHEMA_DIR)
        for name in ("register.request.schema.json", "heartbeat.request.schema.json"):
            examples = c.schemas[name]["examples"]
            self.assertTrue(examples)
            for ex in examples:
                self.assertEqual(c.errors(ex, name), [], ex)
        # Known-bad bodies must fail, or the validator proves nothing.
        bad_register = [
            {},                                                  # nothing
            {"name": "x", "pool": "p", "tiers": [], "upstream_models": [],
             "base_url": "http://x"},                            # no protocol_version
            {"protocol_version": 3, "name": "x", "pool": "p", "tiers": [],
             "upstream_models": [], "base_url": "http://x"},     # version not a string
            {"protocol_version": "3", "name": "x", "pool": "p", "tiers": [],
             "upstream_models": [], "base_url": "http://x", "vram_gb": 7.5},
            {"protocol_version": "3", "name": "x", "pool": "p", "tiers": [],
             "upstream_models": [], "base_url": "http://x", "instance_id": ""},
            {"protocol_version": "3", "name": "x", "pool": "p", "tiers": [],
             "upstream_models": [], "base_url": "http://x", "max_concurrency": 0},
        ]
        for body in bad_register:
            self.assertTrue(c.errors(body, "register.request.schema.json"), body)
        for body in ({"state": "ready"}, {"protocol_version": "3", "state": "stopped"},
                     {"protocol_version": "3", "disk_free_bytes": -1},
                     {"protocol_version": "3", "in_flight": "1"}):
            self.assertTrue(c.errors(body, "heartbeat.request.schema.json"), body)


class MiniNodeBodiesTest(unittest.TestCase):
    """Validate what mini-node actually puts on the wire (captured by FakePylon)."""

    def setUp(self):
        self.contract = Contract(SCHEMA_DIR)
        self.fake = FakePylon()
        self.fake.engine_alive = True
        self.fake.slots_body = [{"id": 0, "is_processing": True}]
        self.log = mn.logging.getLogger("test")

    def tearDown(self):
        self.fake.stop()

    def _register_body(self, **overrides) -> dict:
        cfg = _cfg(pylon_url=self.fake.base_url, base_url=self.fake.base_url, **overrides)
        self.fake.calls.clear()
        mn.register(cfg, self.log)
        path, body, _ = self.fake.calls[-1]
        self.assertEqual(path, "/v1/nodes/register")
        return body

    def _heartbeat_body(self, **overrides) -> dict:
        cfg = _cfg(pylon_url=self.fake.base_url, base_url=self.fake.base_url, **overrides)
        node_id, token = mn.register(cfg, self.log)
        self.fake.calls.clear()
        self.assertTrue(mn.heartbeat(cfg, node_id, token, self.log))
        path, body, _ = [c for c in self.fake.calls if c[0].endswith("/heartbeat")][-1]
        return body

    def test_minimal_register_is_v3(self):
        body = self._register_body()
        self.assertEqual(body["protocol_version"], "3")
        self.assertEqual(self.contract.errors(body, "register.request.schema.json"), [])

    def test_full_register_is_v3(self):
        body = self._register_body(
            instance_id="truenas:8080", engine_api_key="k", node_type="vega56",
            gpu="Vega56", vram_gb=7.98, engine_kind="vllm",
            advertised_base_url="http://172.17.0.1:8080")
        self.assertEqual(body["instance_id"], "truenas:8080")
        self.assertEqual(body["vram_gb"], 8)  # integer on register
        self.assertEqual(self.contract.errors(body, "register.request.schema.json"), [])

    def test_ready_heartbeat_is_v3_with_host_telemetry(self):
        with tempfile.TemporaryDirectory() as d:
            body = self._heartbeat_body(vram_gb=7.98, disk_path=d)
        self.assertEqual(body["protocol_version"], "3")
        self.assertEqual(body["state"], "ready")
        self.assertEqual(body["in_flight"], 1)
        self.assertNotIn("last_error", body)
        self.assertEqual(body["vram_total_gb"], 7.98)
        self.assertGreater(body["disk_total_bytes"], 0)
        self.assertGreaterEqual(body["disk_total_bytes"], body["disk_free_bytes"])
        self.assertGreaterEqual(body["process_uptime_seconds"], 0)
        if sys.platform.startswith("linux"):
            self.assertIn("memory_total_bytes", body)
            self.assertIn("network_rx_bytes", body)
        self.assertEqual(self.contract.errors(body, "heartbeat.request.schema.json"), [])

    def test_engine_down_heartbeat_is_busy_and_valid(self):
        self.fake.engine_alive = False
        body = self._heartbeat_body()
        self.assertEqual(body["state"], "busy")
        self.assertIn("HTTP 503", body["last_error"])
        self.assertEqual(body["in_flight"], 0)
        self.assertEqual(self.contract.errors(body, "heartbeat.request.schema.json"), [])

    def test_engine_unreachable_heartbeat_is_busy_and_valid(self):
        cfg = _cfg(pylon_url=self.fake.base_url, base_url="http://127.0.0.1:1",
                   engine_probe_timeout_seconds=0.5)
        body = mn.build_heartbeat_body(cfg)
        self.assertEqual(body["state"], "busy")
        self.assertIn("unreachable", body["last_error"])
        self.assertEqual(self.contract.errors(body, "heartbeat.request.schema.json"), [])


class InstanceIdTest(unittest.TestCase):
    def test_default_is_hostname_and_engine_port(self):
        host = mn.socket.gethostname()
        self.assertEqual(mn.default_instance_id("http://127.0.0.1:8080"), f"{host}:8080")
        self.assertEqual(mn.default_instance_id("https://engine.lan"), f"{host}:443")
        self.assertEqual(mn.default_instance_id("http://engine.lan/v1"), f"{host}:80")

    def test_env_override_and_default(self):
        keys = ("PYLON_URL", "PYLON_NODE_KEY", "NODE_NAME", "NODE_BASE_URL",
                "NODE_INSTANCE_ID")
        saved = {k: os.environ.get(k) for k in keys}
        try:
            os.environ.update({"PYLON_URL": "http://p", "PYLON_NODE_KEY": "k",
                               "NODE_NAME": "n", "NODE_BASE_URL": "http://e:9001"})
            os.environ.pop("NODE_INSTANCE_ID", None)
            self.assertEqual(mn.Config.from_env().instance_id,
                             f"{mn.socket.gethostname()}:9001"[:128])
            os.environ["NODE_INSTANCE_ID"] = "x" * 200
            self.assertEqual(mn.Config.from_env().instance_id, "x" * 128)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class HostTelemetryParsersTest(unittest.TestCase):
    def test_proc_net_dev_skips_loopback(self):
        text = ("Inter-|   Receive |  Transmit\n"
                " face |bytes packets errs drop fifo frame compressed multicast|bytes\n"
                "    lo: 500 5 0 0 0 0 0 0 500 5 0 0 0 0 0 0\n"
                "  eth0: 1000 10 0 0 0 0 0 0 2000 20 0 0 0 0 0 0\n"
                "  wg0: 30 1 0 0 0 0 0 0 40 1 0 0 0 0 0 0\n")
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".dev") as f:
            f.write(text)
        try:
            self.assertEqual(mn._read_network_bytes(f.name), (1030, 2040))
        finally:
            os.unlink(f.name)
        self.assertIsNone(mn._read_network_bytes("/nonexistent/net/dev"))

    def test_proc_meminfo(self):
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".mem") as f:
            f.write("MemTotal:       16000 kB\nMemFree: 1 kB\nMemAvailable:    8000 kB\n")
        try:
            self.assertEqual(mn._read_memory_bytes(f.name), (16000 * 1024, 8000 * 1024))
        finally:
            os.unlink(f.name)
        self.assertIsNone(mn._read_memory_bytes("/nonexistent/meminfo"))


def _run_all():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (VendoredSchemasTest, MiniNodeBodiesTest, InstanceIdTest, HostTelemetryParsersTest):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(_run_all())

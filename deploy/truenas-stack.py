#!/usr/bin/env python3
"""Deploy a self-contained pylon + pylon-mini-node + llama-server stack on
a TrueNAS SCALE box (or any single-host setup) over SSH+sudo with password
authentication.

Idempotent: re-running on an already-deployed box reuses the existing keys
at /etc/pylon-keys, rebuilds the pylon image from the local pylon checkout,
and bounces the containers.

Why this script: getting pylon + mini-node + llama-server to all talk to
each other on one box has several non-obvious gotchas this script encodes:

  1. TrueNAS SCALE mounts /, /usr, /opt **read-only** (immutable system
     area). Persistent install must go under /var/lib/, /etc/, or /home/.
  2. The pylon container must run with `--network host` so it can reach
     the engine at the same loopback address the mini-node tells it the
     engine lives at (`http://127.0.0.1:8080`). Without host networking
     pylon returns "upstream error: All connection attempts failed" from
     inside its own container's loopback. Long-term, pylon-mini-node
     should grow a NODE_ADVERTISED_BASE_URL knob so cross-box deployments
     can register a host-reachable URL distinct from the probe URL.
  3. llama-server should be started with --api-key so :8080 isn't a
     completely-open compute endpoint on the LAN. The same key is then
     wired into the mini-node's NODE_ENGINE_API_KEY so pylon's proxied
     calls authenticate.

Configuration via environment variables when invoked:

    PYLON_REPO        path to local pylon checkout (default: ../../pylon)
    TARGET_HOST       SSH target (default: 192.168.1.202)
    TARGET_USER       SSH user (default: truenas_admin)
    TARGET_PASS       SSH password (default: prompts if not set)
    ENGINE_PORT       llama-server port (default: 8080)
    PYLON_PORT        pylon port (default: 8088)
    MODEL_NAME        upstream model filename (default: gemma-4-e4b-it-iq4_xs.gguf)
    UPSTREAM_NAME     name the mini-node advertises to pylon
                      (default: same as MODEL_NAME — matches pylon's
                      config/models.yaml entry's upstream_model)
"""
from __future__ import annotations

import getpass
import io
import json
import os
import secrets
import sys
import tarfile
import time

import paramiko

REPO_DEFAULT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "pylon"))


def cfg(name: str, default: str | None = None) -> str:
    v = os.environ.get(name)
    if v:
        return v
    if default is None:
        raise SystemExit(f"missing required env var: {name}")
    return default


def main() -> int:
    target_host = cfg("TARGET_HOST", "192.168.1.202")
    target_user = cfg("TARGET_USER", "truenas_admin")
    target_pass = os.environ.get("TARGET_PASS") or getpass.getpass(
        f"password for {target_user}@{target_host}: ")
    pylon_repo = os.path.abspath(cfg("PYLON_REPO", REPO_DEFAULT))
    engine_port = int(cfg("ENGINE_PORT", "8080"))
    pylon_port = int(cfg("PYLON_PORT", "8088"))
    model_name = cfg("MODEL_NAME", "gemma-4-e4b-it-iq4_xs.gguf")
    upstream_name = cfg("UPSTREAM_NAME", model_name)

    if not os.path.isdir(pylon_repo):
        raise SystemExit(f"pylon repo not found at {pylon_repo}")

    print(f"target:   {target_user}@{target_host}")
    print(f"pylon:    {pylon_repo} -> docker image pylon-local:dev")
    print(f"engine:   :{engine_port} model={model_name}")
    print(f"router:   :{pylon_port} upstream_model={upstream_name}")

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(target_host, username=target_user, password=target_pass,
              timeout=15, allow_agent=False, look_for_keys=False)

    def run(cmd_, sudo=False, timeout=180):
        if sudo:
            cmd_ = f"echo {json.dumps(target_pass)} | sudo -S sh -c {json.dumps(cmd_)} 2>&1"
        si, so, se = c.exec_command(cmd_, timeout=timeout)
        return so.read().decode(errors="replace").replace(
            f"[sudo] password for {target_user}: ", "").rstrip()

    # 1. Stage pylon source
    print("\n[1/6] stage pylon source")
    sftp = c.open_sftp()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for sub in ("src", "config", "pyproject.toml"):
            full = os.path.join(pylon_repo, sub)
            if os.path.exists(full):
                tar.add(full, arcname=sub)
    buf.seek(0)
    run("mkdir -p /tmp/pylon-build")
    with sftp.open("/tmp/pylon-build/pylon-src.tar.gz", "wb") as f:
        f.write(buf.getvalue())

    dockerfile = (
        "FROM python:3.12-slim\n"
        "WORKDIR /app\n"
        "COPY pylon-src.tar.gz /tmp/\n"
        "RUN tar -xzf /tmp/pylon-src.tar.gz && rm /tmp/pylon-src.tar.gz\n"
        "RUN pip install --no-cache-dir fastapi 'uvicorn[standard]>=0.27' httpx "
        "pydantic pydantic-settings PyYAML tiktoken python-multipart "
        "cryptography jsonschema\n"
        "ENV PYTHONPATH=/app/src\n"
        "ENV PYLON_DATA_DIR=/data\n"
        f"ENV PYLON_HOST=0.0.0.0\n"
        f"ENV PYLON_PORT={pylon_port}\n"
        f"EXPOSE {pylon_port}\n"
        f'CMD ["python", "-m", "uvicorn", "pylon.server:app", "--host", "0.0.0.0", "--port", "{pylon_port}"]\n'
    )
    with sftp.open("/tmp/pylon-build/Dockerfile", "w") as f:
        f.write(dockerfile)
    sftp.close()

    # 2. Build image
    print("[2/6] build pylon image")
    out = run("cd /tmp/pylon-build && docker build -t pylon-local:dev . 2>&1 | tail -3",
              sudo=True, timeout=600)
    print("  " + out.replace("\n", "\n  "))

    # 3. Generate keys (idempotent — reuse existing if /etc/pylon-keys exists)
    print("[3/6] keys")
    existing = run("test -f /etc/pylon-keys && cat /etc/pylon-keys", sudo=True)
    if "PYLON_API_KEY=" in existing:
        keys = dict(line.split("=", 1) for line in existing.splitlines() if "=" in line)
        print(f"  reusing existing keys at /etc/pylon-keys")
    else:
        keys = {
            "PYLON_API_KEY": secrets.token_urlsafe(24),
            "PYLON_NODE_KEY": secrets.token_urlsafe(24),
            "PYLON_ADMIN_KEY": secrets.token_urlsafe(24),
        }
        content = "\n".join(f"{k}={v}" for k, v in keys.items()) + "\n"
        sftp = c.open_sftp()
        with sftp.open("/tmp/_pylon_keys", "w") as f:
            f.write(content)
        sftp.close()
        run("install -m 0600 -o root -g root /tmp/_pylon_keys /etc/pylon-keys "
            "&& rm -f /tmp/_pylon_keys", sudo=True)
        print(f"  generated fresh keys; persisted to /etc/pylon-keys 0600 root")

    # 4. Start pylon container with --network host (critical — see header)
    print("[4/6] start pylon container (--network host)")
    run("docker rm -f pylon 2>&1 || true; mkdir -p /var/lib/pylon-data", sudo=True)
    start = (
        "docker run -d --name pylon --restart unless-stopped "
        "--network host "
        "-v /var/lib/pylon-data:/data "
        f"-e PYLON_API_KEY={keys['PYLON_API_KEY']} "
        f"-e PYLON_NODE_KEY={keys['PYLON_NODE_KEY']} "
        f"-e PYLON_ADMIN_KEY={keys['PYLON_ADMIN_KEY']} "
        "-e PYLON_BOOTSTRAP_CREDITS=10000 "
        "pylon-local:dev"
    )
    run(start, sudo=True, timeout=30)
    print(f"  waiting for pylon /healthz on :{pylon_port}")
    for i in range(30):
        time.sleep(2)
        h = run(f"curl -s --max-time 2 http://127.0.0.1:{pylon_port}/healthz || true")
        if '"ok"' in h:
            print(f"  ready at t+{(i+1)*2}s")
            break
    else:
        print(f"  pylon never came up")
        return 2

    # 5. Confirm mini-node unit exists; restart so it re-registers
    print("[5/6] restart mini-node unit (if installed)")
    unit_state = run("systemctl is-enabled pylon-mini-node.service 2>&1", sudo=True)
    if "enabled" in unit_state or "disabled" in unit_state:
        run("systemctl restart pylon-mini-node.service", sudo=True)
        print(f"  restarted (was {unit_state.strip()})")
    else:
        print(f"  not installed — install with `python deploy/install-on-truenas.py` "
              f"or manually `install -m 0644 pylon_mini_node.py "
              f"/var/lib/pylon-mini-node/` and configure /etc/pylon-mini-node.env")

    # 6. End-to-end probe
    print("[6/6] end-to-end probe")
    time.sleep(4)
    nodes = run(f"curl -s -H 'Authorization: Bearer {keys['PYLON_ADMIN_KEY']}' "
                f"http://127.0.0.1:{pylon_port}/v1/admin/nodes")
    try:
        parsed = json.loads(nodes)
        ready = [n for n in parsed.get("nodes", []) if n.get("state") == "ready"]
        print(f"  registered nodes (ready): {len(ready)}")
        for n in ready:
            print(f"    {n['id']} base={n['base_url']} models={n['upstream_models']}")
    except Exception:
        print("  could not parse /v1/admin/nodes; mini-node likely not installed yet")

    print("\nDone. To call pylon:")
    print(f"  curl -H 'Authorization: Bearer {keys['PYLON_API_KEY']}' \\")
    print(f"       -H 'content-type: application/json' \\")
    print(f"       -d '{{\"model\":\"pylon-vega-gemma4\",\"messages\":[{{\"role\":\"user\",\"content\":\"hi\"}}]}}' \\")
    print(f"       http://{target_host}:{pylon_port}/v1/chat/completions")
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

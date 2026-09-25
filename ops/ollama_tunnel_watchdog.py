"""Keeps the Cloudflare quick tunnel to local Ollama alive and self-healing.

Cloudflare "quick tunnels" (`cloudflared tunnel --url ...`, no account/domain
needed) are inherently ephemeral: the process can keep running while the
tunnel's edge connection silently drops, and every restart gets a brand-new
random hostname. That combination is why aml-model-backend has repeatedly
gone "Service unavailable" -- its OLLAMA_HOST env var pointed at a URL that
either died or was never updated after a restart.

This script removes the manual step: it supervises cloudflared, health-checks
the tunnel on a short interval, restarts it when the check fails, and -- only
when the public URL actually changes -- pushes the new value to
aml-model-backend's OLLAMA_HOST via Render's REST API and restarts that
service so it picks the change up. No Render dashboard visits, no CLI
service-recreate cycle.

Requires: a Render API key. Reads one of, in order:
  1. RENDER_API_KEY environment variable
  2. The key already stored by `render login` in ~/.render/cli.yaml (Windows:
     %USERPROFILE%\\.render\\cli.yaml) -- present on this machine already.

Run it once, leave it running in the background:
    python ops/ollama_tunnel_watchdog.py
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests
import yaml

RENDER_SERVICE_ID = os.environ.get("RENDER_MODEL_SERVICE_ID", "srv-darbmpnlot8c73e2kct0")
RENDER_API_BASE = "https://api.render.com/v1"
OLLAMA_LOCAL = os.environ.get("OLLAMA_LOCAL_URL", "http://localhost:11434")
HEALTH_CHECK_INTERVAL_SECONDS = 30
TUNNEL_URL_RE = re.compile(r"https://[\w-]+\.trycloudflare\.com")


def render_api_key():
    key = os.environ.get("RENDER_API_KEY")
    if key:
        return key
    cli_config = Path.home() / ".render" / "cli.yaml"
    if cli_config.exists():
        with open(cli_config, encoding="utf-8") as f:
            return yaml.safe_load(f)["api"]["key"]
    raise SystemExit(
        "No Render API key found. Set RENDER_API_KEY, or run `render login` "
        "first so ~/.render/cli.yaml has one."
    )


def render_headers():
    return {"Authorization": f"Bearer {render_api_key()}", "Content-Type": "application/json"}


def update_ollama_host(new_url):
    """PUTs the new OLLAMA_HOST value, then triggers a new deploy so the
    running process (which only reads env vars at import time) picks it up.

    A plain restart (POST .../restart) is NOT enough here -- confirmed by a
    real failure: it reuses the environment snapshot from the *last deploy*,
    not the value just PUT, so the app boots against the old (dead) tunnel
    URL. Worse, app.py's lifespan raises if Ollama is unreachable at
    startup, which crashes the whole process rather than just marking it
    not-ready -- so a stale restart doesn't just fail to help, it takes the
    service down harder than before. A full deploy (slower: re-runs the
    no-cache pip install, ~60-90s) is what actually applies a new env var."""
    resp = requests.put(
        f"{RENDER_API_BASE}/services/{RENDER_SERVICE_ID}/env-vars/OLLAMA_HOST",
        headers=render_headers(), json={"value": new_url}, timeout=30,
    )
    resp.raise_for_status()
    resp = requests.post(
        f"{RENDER_API_BASE}/services/{RENDER_SERVICE_ID}/deploys",
        headers=render_headers(), timeout=30,
    )
    resp.raise_for_status()
    print(f"[watchdog] Updated OLLAMA_HOST -> {new_url} and triggered a new deploy of aml-model-backend", flush=True)


def start_tunnel():
    """Launches cloudflared and blocks until it prints its quick-tunnel URL
    (or the process exits first, e.g. cloudflared isn't on PATH)."""
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", OLLAMA_LOCAL, "--http-host-header", "localhost:11434"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    url = None
    for line in proc.stdout:
        print(f"[cloudflared] {line}", end="", flush=True)
        match = TUNNEL_URL_RE.search(line)
        if match:
            url = match.group(0)
            break
    return proc, url


def tunnel_healthy(url):
    if not url:
        return False
    try:
        return requests.get(f"{url}/api/tags", timeout=15).status_code == 200
    except requests.RequestException:
        return False


# A single failed health check was restarting cloudflared -- and triggering a
# full ~2-3 minute redeploy of aml-model-backend, since a new tunnel means a
# new OLLAMA_HOST -- on every transient latency blip from Cloudflare's free
# quick-tunnel edge. In practice that meant a real deploy roughly every 5-13
# minutes, each one an actual outage window (nothing is serving at all mid-
# deploy, not even the fallback model, since the whole process is restarting)
# -- the watchdog itself became the main source of "Service unavailable"
# reports. Now it only restarts after FAILURE_THRESHOLD consecutive failed
# checks, polling faster while degraded so it still reacts quickly to a real
# outage without churning on noise.
FAILURE_THRESHOLD = 3
RETRY_INTERVAL_SECONDS = 10


def main():
    proc = None
    current_url = None
    consecutive_failures = 0
    print("[watchdog] Starting. Ctrl-C to stop.", flush=True)
    try:
        while True:
            process_dead = proc is None or proc.poll() is not None
            healthy = False if process_dead else tunnel_healthy(current_url)

            if healthy:
                consecutive_failures = 0
                time.sleep(HEALTH_CHECK_INTERVAL_SECONDS)
                continue

            consecutive_failures += 1
            if not process_dead and consecutive_failures < FAILURE_THRESHOLD:
                print(f"[watchdog] Health check failed ({consecutive_failures}/{FAILURE_THRESHOLD}), rechecking shortly...", flush=True)
                time.sleep(RETRY_INTERVAL_SECONDS)
                continue

            if process_dead:
                print("[watchdog] cloudflared process exited, restarting...", flush=True)
            else:
                print(f"[watchdog] Tunnel unhealthy after {consecutive_failures} consecutive checks, restarting cloudflared...", flush=True)
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            consecutive_failures = 0
            proc, new_url = start_tunnel()
            if new_url is None:
                print("[watchdog] cloudflared exited without printing a URL; retrying shortly.", flush=True)
                time.sleep(HEALTH_CHECK_INTERVAL_SECONDS)
                continue
            if new_url != current_url:
                current_url = new_url
                print(f"[watchdog] Tunnel URL: {current_url}", flush=True)
                try:
                    update_ollama_host(current_url)
                except requests.RequestException as exc:
                    print(f"[watchdog] Failed to update Render: {exc}", flush=True)
            time.sleep(HEALTH_CHECK_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()


if __name__ == "__main__":
    main()

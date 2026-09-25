"""Keeps the Cloudflare quick tunnel to local Ollama alive and self-healing.

Cloudflare "quick tunnels" (`cloudflared tunnel --url ...`, no account/domain
needed) are inherently ephemeral: the process can keep running while the
tunnel's edge connection silently drops, and every restart gets a brand-new
random hostname. That combination is why aml-model-backend has repeatedly
gone "Service unavailable" -- its OLLAMA_HOST env var pointed at a URL that
either died or was never updated after a restart.

This script removes the manual step: it supervises cloudflared, health-checks
the tunnel on a short interval, restarts it when the check fails, and -- only
when the public URL actually changes -- publishes the new value two ways:

  1. Commits and pushes it to ops/current_ollama_host.txt. aml-model-backend
     polls this file every ~20s (OLLAMA_HOST_REFRESH_URL) and swaps the URL
     it's using in place, with NO restart needed. This is the mechanism that
     actually matters: a plain env-var PUT only gets applied on a full
     redeploy, and a redeploy is itself a real ~2-3 minute outage window
     (the whole process, including the OpenAI fallback, is down while it
     restarts) -- confirmed directly from Render's deploy history repeatedly
     redeploying every 5-13 minutes before this existed, each one a genuine
     "Service unavailable" window despite the fallback being configured
     correctly. Polling instead of redeploying means tunnel rotation no
     longer touches the running process's availability at all.
  2. PUTs OLLAMA_HOST on Render too, purely as a visible baseline value in
     the dashboard / for a deploy triggered for unrelated reasons -- not the
     primary update path, and deliberately does NOT trigger a deploy anymore.

Requires: a Render API key (for step 2) and git push access to this repo
(for step 1, already configured on this machine). Reads the Render key from
one of, in order:
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


REPO_ROOT = Path(__file__).resolve().parent.parent
TRACKED_HOST_FILE = REPO_ROOT / "ops" / "current_ollama_host.txt"


def push_tracked_host_file(new_url):
    """Commits and pushes the new URL to ops/current_ollama_host.txt -- the
    primary update path; see module docstring. Non-fatal on failure (e.g. a
    transient network blip, or another push racing this one) since the
    Render env var and the OpenAI fallback both still work as a safety net;
    it'll simply retry on the next tunnel rotation."""
    TRACKED_HOST_FILE.write_text(new_url + "\n", encoding="utf-8")
    try:
        subprocess.run(["git", "add", "ops/current_ollama_host.txt"], check=True, cwd=REPO_ROOT)
        subprocess.run(
            ["git", "commit", "-m", f"Automated: update current Ollama tunnel URL to {new_url}"],
            check=True, cwd=REPO_ROOT,
        )
        subprocess.run(["git", "push", "origin", "main"], check=True, cwd=REPO_ROOT)
        return True
    except subprocess.CalledProcessError as exc:
        print(f"[watchdog] git push of the tracked host file failed (will retry next rotation): {exc}", flush=True)
        return False


def update_ollama_host(new_url):
    pushed = push_tracked_host_file(new_url)
    resp = requests.put(
        f"{RENDER_API_BASE}/services/{RENDER_SERVICE_ID}/env-vars/OLLAMA_HOST",
        headers=render_headers(), json={"value": new_url}, timeout=30,
    )
    resp.raise_for_status()
    if pushed:
        print(f"[watchdog] Pushed new Ollama tunnel URL -> {new_url} (aml-model-backend picks it up via poll, no redeploy needed)", flush=True)
    else:
        print(f"[watchdog] Set OLLAMA_HOST env var -> {new_url}, but the git push failed -- aml-model-backend won't see this until that succeeds or a deploy happens for another reason.", flush=True)


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

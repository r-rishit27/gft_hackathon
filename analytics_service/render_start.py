"""Render deployment launcher for the analytics service.

Mirrors local.py (starts the model service on loopback:8000, then
analytics_service.app in front of it) but adapted for a server environment:
analytics_service.app binds 0.0.0.0:$PORT (Render only exposes one port per
web service) instead of local.py's loopback-only default, and OLLAMA_HOST is
taken from the real environment (a Cloudflare tunnel to a locally-run Ollama)
instead of local.py's hardcoded 127.0.0.1:11434, which only makes sense when
Ollama runs on the same machine as this process.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

from .catalog import ROOT
from .config import Settings


def main():
    config_path = Path(os.environ.get(
        "AML_ANALYTICS_CONFIG", str(ROOT / "analytics_service/config.roles.local.json")
    ))
    port = int(os.environ.get("PORT", "8012"))

    settings = Settings.from_file(str(config_path))
    if settings.query_mode != "freeform":
        raise SystemExit("Render launcher requires freeform mode")
    if settings.model_url != "http://127.0.0.1:8000/generate-sql":
        raise SystemExit("This launcher expects the model service at loopback port 8000")
    if "OLLAMA_HOST" not in os.environ:
        raise SystemExit("OLLAMA_HOST must be set (e.g. to a tunnel URL) -- no local Ollama on this host")

    env = {
        **os.environ,
        "MODEL_BACKEND": "ollama",
        "OLLAMA_MODEL": os.environ.get("OLLAMA_MODEL", "mannix/defog-llama3-sqlcoder-8b"),
        "SCHEMA_BACKEND": os.environ.get("SCHEMA_BACKEND", "falkordb"),
        "OLLAMA_NUM_CTX": os.environ.get("OLLAMA_NUM_CTX", "8192"),
        "OLLAMA_TIMEOUT_SECONDS": os.environ.get("OLLAMA_TIMEOUT_SECONDS", "240"),
        "AML_ANALYTICS_CONFIG": str(config_path.resolve()),
    }

    children = []
    try:
        model = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8000"],
            cwd=ROOT, env=env,
        )
        children.append(model)
        for _ in range(90):
            if model.poll() is not None:
                raise RuntimeError("Model service did not start; check OLLAMA_HOST/tunnel")
            try:
                if requests.get("http://127.0.0.1:8000/health", timeout=2).json().get("ollama_ready"):
                    break
            except (requests.RequestException, ValueError):
                pass
            time.sleep(1)
        else:
            raise RuntimeError("Model service startup timed out")

        analytics = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "analytics_service.app:create_app", "--factory",
             "--host", "0.0.0.0", "--port", str(port)],
            cwd=ROOT, env=env,
        )
        children.append(analytics)
        print(f"Analytics live on 0.0.0.0:{port}/ui/", flush=True)
        while all(p.poll() is None for p in children):
            time.sleep(1)
    finally:
        for process in reversed(children):
            if process.poll() is None:
                process.terminate()
        for process in children:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()

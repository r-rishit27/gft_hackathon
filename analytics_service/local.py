"""Start the real model and results services on loopback. No fixture fallback."""
import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from .catalog import ROOT
from .config import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    role_config = ROOT / "analytics_service/config.roles.local.json"
    parser.add_argument("--config", type=Path, default=role_config if role_config.exists() else ROOT / "analytics_service/config.local.json")
    parser.add_argument("--port", type=int, default=8011)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    settings = Settings.from_file(str(args.config))
    if settings.query_mode != "freeform":
        raise SystemExit("Local integration requires freeform mode")
    if settings.model_url != "http://127.0.0.1:8000/generate-sql":
        raise SystemExit("This launcher expects the model service at loopback port 8000")
    for port in (8000, args.port):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                raise SystemExit(f"Port {port} is already occupied. Stop that service or choose another UI port.")
    env = {**os.environ, "MODEL_BACKEND": "ollama", "OLLAMA_HOST": "http://127.0.0.1:11434",
           "OLLAMA_MODEL": "mannix/defog-llama3-sqlcoder-8b",
           "SCHEMA_BACKEND": os.environ.get("SCHEMA_BACKEND", "falkordb"),
           "OLLAMA_NUM_CTX": "8192", "OLLAMA_TIMEOUT_SECONDS": "240",
           "AML_ANALYTICS_CONFIG": str(args.config.resolve())}
    children = []
    try:
        model = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8000"], cwd=ROOT, env=env)
        children.append(model)
        for _ in range(60):
            if model.poll() is not None:
                raise RuntimeError("Model service did not start; check Ollama and model installation")
            try:
                if requests.get("http://127.0.0.1:8000/health", timeout=1).json().get("ollama_ready"):
                    break
            except (requests.RequestException, ValueError):
                pass
            time.sleep(1)
        else:
            raise RuntimeError("Model service startup timed out")
        children.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "analytics_service.app:create_app", "--factory",
                                          "--host", "127.0.0.1", "--port", str(args.port)], cwd=ROOT, env=env))
        print(f"Local analytics: http://127.0.0.1:{args.port}/ui/", flush=True)
        while all(p.poll() is None for p in children):
            time.sleep(1)
    except KeyboardInterrupt:
        pass
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

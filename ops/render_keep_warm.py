"""Pings both Render services every ~10 minutes so neither spins down from
free-tier idle timeout (~15 min). Belt-and-suspenders alongside
.github/workflows/keep-warm.yml -- that GitHub Actions cron is the primary
mechanism and doesn't depend on this laptop, but if it isn't actually firing
(check the repo's Settings -> Actions -> General for a disabled permission),
this covers the same job as long as the laptop is on.

Run it once, leave it running:
    python ops/render_keep_warm.py
"""
import time

import requests

PING_INTERVAL_SECONDS = 600
TARGETS = {
    "aml-analytics-service": "https://aml-analytics-service.onrender.com/login",
    "aml-model-backend": "https://aml-model-backend.onrender.com/health",
}

if __name__ == "__main__":
    print("[render-keep-warm] Starting. Ctrl-C to stop.", flush=True)
    while True:
        for name, url in TARGETS.items():
            try:
                resp = requests.get(url, timeout=30)
                print(f"[render-keep-warm] {name}: {resp.status_code}", flush=True)
            except requests.RequestException as exc:
                print(f"[render-keep-warm] {name}: failed ({exc})", flush=True)
        time.sleep(PING_INTERVAL_SECONDS)

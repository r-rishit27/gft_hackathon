"""Windows equivalent of macOS `caffeinate`: prevents idle/system sleep for
as long as this process runs, via the same SetThreadExecutionState Win32 API
caffeinate itself wraps. Does NOT override lid-close (Windows forces sleep on
lid-close regardless of execution state) -- pair with a power-plan change for
that; this only stops idle-timeout sleep while the laptop stays open.

Run it once, leave it running alongside the tunnel watchdog:
    python ops/keep_awake.py
"""
import ctypes
import time

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

if __name__ == "__main__":
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    print("[keep_awake] System sleep suppressed. Ctrl-C to stop.", flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)

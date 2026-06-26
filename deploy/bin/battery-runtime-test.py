#!/usr/bin/env python3
"""Battery runtime test: append a line every 10s, fsync'd to the SD card so the
LAST line survives a hard power-loss. After the Pi dies on battery and reboots,
read the log — the final timestamp is within ~10s of when it died.

Run detached:  setsid python3 battery-runtime-test.py >/dev/null 2>&1 < /dev/null &
"""
import os
import subprocess
import time
from datetime import datetime

LOG = "/home/jaysettle/tyspeaker/battery-test.log"   # absolute: runs fine under systemd
os.makedirs(os.path.dirname(LOG), exist_ok=True)


def throttled() -> str:
    try:
        return subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3
        ).stdout.strip()
    except Exception:
        return "throttled=?"


def volts() -> str:
    try:
        return subprocess.run(
            ["vcgencmd", "measure_volts"], capture_output=True, text=True, timeout=3
        ).stdout.strip()
    except Exception:
        return "volt=?"


def main() -> None:
    t0 = time.monotonic()
    # "w" = fresh file each test. Durable per-line via flush + fsync.
    with open(LOG, "w") as f:
        def emit(msg: str) -> None:
            f.write(msg + "\n")
            f.flush()
            os.fsync(f.fileno())

        emit("=== BATTERY RUNTIME TEST ===")
        emit("start  %s   (unplug the charger now)" % datetime.now().isoformat(timespec="seconds"))
        while True:
            el = int(time.monotonic() - t0)
            hms = "%d:%02d:%02d" % (el // 3600, (el % 3600) // 60, el % 60)
            emit("%s  up=%s (%ds)  %s  %s"
                 % (datetime.now().isoformat(timespec="seconds"), hms, el, throttled(), volts()))
            time.sleep(10)


if __name__ == "__main__":
    main()

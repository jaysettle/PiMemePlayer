"""Wi-Fi hotspot (AP) control via the tyspeaker-hotspot sudoers helper.

Switching to AP mode drops whatever Wi-Fi the request came in on, so the switch
is run DETACHED (and after a short delay) — the HTTP response gets out first, then
the network flips. The AP profile is autoconnect=no, so a reboot always reverts to
home Wi-Fi (the safety net).
"""

from __future__ import annotations

import subprocess
from typing import Optional

from .logsetup import get_logger

log = get_logger("hotspot")

HELPER = "/usr/local/bin/tyspeaker-hotspot"


def available() -> bool:
    try:
        import os
        return os.path.exists(HELPER)
    except OSError:
        return False


def status() -> str:
    """'on' | 'off' | 'unknown'."""
    try:
        p = subprocess.run(
            ["sudo", "-n", HELPER, "status"],
            capture_output=True, text=True, timeout=5,
        )
        return "on" if (p.stdout or "").strip() == "on" else "off"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _run_detached(action: str) -> None:
    try:
        subprocess.Popen(
            ["setsid", "sh", "-c", "sleep 1; sudo -n %s %s" % (HELPER, action)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.info("hotspot %s (detached)", action)
    except OSError as exc:
        log.debug("hotspot %s failed: %s", action, exc)


def set_hotspot(on: bool) -> None:
    _run_detached("on" if on else "off")


def toggle() -> None:
    _run_detached("toggle")

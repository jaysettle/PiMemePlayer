"""Power/UPS status for TySpeaker."""

from __future__ import annotations

import re
import socket
from typing import Any, Dict, Optional

PISUGAR_S_MODEL = "PiSugar S 1200mAh UPS"
PISUGAR_SERVER_SOCKETS = ("/tmp/pisugar-server.sock", "/tmp/pisugar.sock")


def _parse_value(response: str) -> str:
    text = response.strip()
    if ":" in text:
        return text.split(":", 1)[1].strip()
    return text


def _float_or_none(value: str) -> Optional[float]:
    match = re.search(r"-?\d+(?:\.\d+)?", value)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _query_socket(path: str, command: str) -> Optional[str]:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            sock.connect(path)
            sock.sendall((command + "\n").encode("utf-8"))
            chunks = []
            while True:
                try:
                    chunk = sock.recv(256)
                except socket.timeout:
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            if not chunks:
                return None
            return b"".join(chunks).decode("utf-8", errors="replace")
    except OSError:
        return None


def _query_pisugar_manager() -> Optional[Dict[str, Any]]:
    socket_path = None
    for path in PISUGAR_SERVER_SOCKETS:
        if _query_socket(path, "get model") is not None:
            socket_path = path
            break
    if not socket_path:
        return None

    model = _parse_value(_query_socket(socket_path, "get model") or "") or "PiSugar"
    percent = _float_or_none(_parse_value(_query_socket(socket_path, "get battery") or ""))
    voltage = _float_or_none(_parse_value(_query_socket(socket_path, "get battery_v") or ""))
    plugged_raw = _parse_value(_query_socket(socket_path, "get battery_power_plugged") or "")
    charging_raw = _parse_value(_query_socket(socket_path, "get battery_charging") or "")

    external_power = None
    if plugged_raw.lower() in ("true", "false"):
        external_power = plugged_raw.lower() == "true"
    charging = None
    if charging_raw.lower() in ("true", "false"):
        charging = charging_raw.lower() == "true"

    return {
        "available": True,
        "model": model,
        "state": "metered",
        "voltage_supported": voltage is not None,
        "percent_supported": percent is not None,
        "voltage": voltage,
        "percent": percent,
        "external_power": external_power,
        "charging": charging,
        "source": socket_path,
        "summary": "Battery meter read from PiSugar power-manager.",
    }


def power_status() -> Dict[str, Any]:
    """Return the best power status available on this hardware."""
    metered = _query_pisugar_manager()
    if metered:
        return metered

    return {
        "available": True,
        "model": PISUGAR_S_MODEL,
        "state": "ups_installed_no_meter",
        "voltage_supported": False,
        "percent_supported": False,
        "voltage": None,
        "percent": None,
        "external_power": None,
        "charging": None,
        "source": "pisugar_s_hardware_only",
        "summary": "UPS installed; PiSugar S does not expose battery voltage or percent to software.",
    }

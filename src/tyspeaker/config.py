"""Runtime configuration, all overridable via environment variables.

Hardware-specific settings are isolated here per project conventions.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


# --- Storage ---------------------------------------------------------------
# All user data (samples + state) lives under one directory on the SD card.
DATA_DIR: Path = _env_path("TYSPEAKER_DATA", Path.home() / "tyspeaker")
SAMPLES_DIR: Path = _env_path("TYSPEAKER_SAMPLES", DATA_DIR / "samples")
SETTINGS_FILE: Path = _env_path("TYSPEAKER_SETTINGS", DATA_DIR / "settings.json")

# --- Web server ------------------------------------------------------------
HOST: str = os.environ.get("TYSPEAKER_HOST", "0.0.0.0")
PORT: int = int(os.environ.get("TYSPEAKER_PORT", "8000"))

# --- GPS (serial NMEA, e.g. GP-20U7 on the mini-UART) ----------------------
# /dev/ttyS0 = the GPIO14/15 mini-UART (BT keeps the PL011). Empty = disabled.
GPS_PORT: str = os.environ.get("TYSPEAKER_GPS_PORT", "/dev/ttyS0")
GPS_BAUD: int = int(os.environ.get("TYSPEAKER_GPS_BAUD", "9600"))
GPS_LOG_DIR: Path = _env_path("TYSPEAKER_GPS_LOG", DATA_DIR / "gps")
GPS_LOG_INTERVAL: int = int(os.environ.get("TYSPEAKER_GPS_LOG_INTERVAL", "10"))

# --- GPIO ------------------------------------------------------------------
# BCM pin for the push button. Set to a negative value to disable GPIO
# (e.g. when developing off-Pi). Button wires to this pin and GND (internal
# pull-up enabled, so the button pulls the pin LOW when pressed).
BUTTON_PIN: int = int(os.environ.get("TYSPEAKER_BUTTON_PIN", "17"))

# --- Audio -----------------------------------------------------------------
# Upload constraints.
ALLOWED_EXTENSIONS = frozenset(
    {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus"}
)
MAX_UPLOAD_BYTES: int = int(
    os.environ.get("TYSPEAKER_MAX_UPLOAD", str(50 * 1024 * 1024))
)

# Optional explicit player command, space-separated, using {path} as the file
# placeholder, e.g. "mpv --no-video --really-quiet {path}". Empty = auto-detect
# from a list of common players (see audio.py).
PLAYER_CMD: str = os.environ.get("TYSPEAKER_PLAYER", "").strip()

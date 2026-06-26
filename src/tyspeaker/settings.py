"""Persisted, user-editable settings (Config tab) backed by a JSON file.

Distinct from static/env defaults in ``config.py``: these can be changed at
runtime from the web UI and survive restarts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from . import config

DEFAULTS: Dict[str, Any] = {
    # playback
    "playlist": [],              # ordered list of sample rel_paths (files)
    "mode": "sequential",        # "sequential" | "random"
    "volume": 70,                # 0..100
    "player_cmd": config.PLAYER_CMD,   # "" = auto-detect
    "default_sink": "",          # audio sink name/id for output
    "bt_autoconnect_mac": "",    # trusted speaker to reconnect in background
    "theme": "neon",             # UI theme selected from the Settings tab
    # GPIO — rotary encoder push button + piezo (negative = disabled)
    "encoder_clk": -1,           # rotary encoder pin A
    "encoder_dt": -1,            # rotary encoder pin B
    "encoder_sw": -1,            # encoder's push switch (if separate)
    "piezo_pin": -1,             # piezo buzzer for feedback (PWM ch1 / GPIO19)
    "piezo2_pin": 18,            # 2nd piezo for harmony (PWM ch0 / GPIO18); -1 = none
    "piezo_freq": 2000,          # system tone (Hz) for all the cue beeps
    "piezo_volume": 100,         # 0..100 piezo loudness (scales PWM duty); live
    # input behaviour
    "double_click_ms": 350,
    "long_press_ms": 700,
    "button_bounce_ms": 60,
    "encoder_bounce_ms": 3,
    "encoder_role": "select",    # "select" (rotate cycles samples) | "volume"
    "gps_log_min_mph": 3.0,      # (legacy quality gate; logging is now network-driven)
    "gps_log_min_sats": 7,
    "gps_log_max_hdop": 3.0,
    "gps_home_prefix": "192.168.3.",  # home Wi-Fi subnet; off it = "out riding"
    "gps_away_debounce_s": 10,        # auto-start logging after away this long
    "gps_home_debounce_s": 20,        # stop only after home this long (not passing by)
    "gps_log_always": True,           # PROVING MODE: log every fix regardless of gates
}

# Settings that only take effect after a service restart (GPIO devices).
RESTART_REQUIRED = {
    "encoder_clk", "encoder_dt", "encoder_sw", "piezo_pin", "piezo2_pin",
    "double_click_ms", "long_press_ms", "button_bounce_ms",
    "encoder_bounce_ms",
}


class Settings:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else config.SETTINGS_FILE
        self._data: Dict[str, Any] = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._data.update(
                        {k: v for k, v in loaded.items() if k in DEFAULTS}
                    )
            except (ValueError, OSError):
                pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, indent=2), encoding="utf-8"
        )

    # dict-style access
    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def as_dict(self) -> Dict[str, Any]:
        return dict(self._data)

    def update(self, changes: Dict[str, Any]) -> List[str]:
        """Apply known keys, coercing to the default's type. Returns the list of
        keys that changed and require a restart to take effect."""
        restart = []
        for key, value in changes.items():
            if key not in DEFAULTS:
                continue
            value = self._coerce(key, value)
            if self._data.get(key) != value:
                self._data[key] = value
                if key in RESTART_REQUIRED:
                    restart.append(key)
        self.save()
        return restart

    @staticmethod
    def _coerce(key: str, value: Any) -> Any:
        default = DEFAULTS[key]
        if isinstance(default, bool):
            return bool(value)
        if isinstance(default, float):
            try:
                return float(value)
            except (TypeError, ValueError):
                return default
        if isinstance(default, int):
            try:
                return int(value)
            except (TypeError, ValueError):
                return default
        if isinstance(default, list):
            return list(value) if isinstance(value, (list, tuple)) else default
        return value

    # convenience typed accessors for hot paths
    @property
    def playlist(self) -> List[str]:
        return list(self._data.get("playlist", []))

    @playlist.setter
    def playlist(self, value: List[str]) -> None:
        self._data["playlist"] = list(value)
        self.save()

    @property
    def mode(self) -> str:
        return self._data.get("mode", "sequential")

    @property
    def volume(self) -> int:
        return int(self._data.get("volume", 70))

"""GPS (NMEA) reader for a serial GPS such as the GP-20U7.

Reads NMEA sentences from a serial device (default ``/dev/ttyS0`` @ 9600),
parses the latest fix, exposes ``status()`` for the UI, and appends a snapshot
to a daily log on the SD card every N seconds ("log whatever the GPS says").

Reads the device directly via unbuffered readline (no pyserial dependency);
the line is configured once with ``stty``. If the port is absent (e.g. off-Pi)
the reader is a no-op so the rest of the app still runs.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

from .logsetup import get_logger

log = get_logger("gps")


def _nmea_ok(line: str) -> bool:
    """Validate the ``*HH`` XOR checksum."""
    if not line.startswith("$") or "*" not in line:
        return False
    body, _, cks = line[1:].partition("*")
    try:
        want = int(cks[:2], 16)
    except ValueError:
        return False
    got = 0
    for ch in body:
        got ^= ord(ch)
    return got == want


def _dm_to_deg(value: str, hemi: str) -> Optional[float]:
    """NMEA ddmm.mmmm / dddmm.mmmm + hemisphere -> signed decimal degrees."""
    if not value or "." not in value:
        return None
    try:
        dot = value.index(".")
        deg = int(value[: dot - 2] or "0")
        minutes = float(value[dot - 2:])
    except (ValueError, IndexError):
        return None
    dec = deg + minutes / 60.0
    return round(-dec if hemi in ("S", "W") else dec, 6)


def _f(value: str) -> Optional[float]:
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _hhmmss(v: str) -> Optional[str]:
    return f"{v[0:2]}:{v[2:4]}:{v[4:6]}" if v and len(v) >= 6 else None


def _ddmmyy(v: str) -> Optional[str]:
    return f"20{v[4:6]}-{v[2:4]}-{v[0:2]}" if v and len(v) >= 6 else None


class GpsReader:
    def __init__(
        self,
        port: str = "/dev/ttyS0",
        baud: int = 9600,
        log_dir: Optional[Path] = None,
        log_interval: int = 10,
    ) -> None:
        self.port = port
        self.baud = baud
        self.log_dir = Path(log_dir) if log_dir else None
        self.log_interval = max(2, int(log_interval))
        self._state: Dict[str, Any] = self._blank_state()
        self._raw: "deque[str]" = deque(maxlen=24)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sentences = 0
        self._bytes = 0
        self._errors = 0
        self._last_rx = 0.0
        self._started_at = time.time()
        self._log_file: Optional[Path] = None
        self._log_count = 0

    @staticmethod
    def _blank_state() -> Dict[str, Any]:
        return {
            "fix": False, "fix_type": 1, "fix_quality": 0,
            "lat": None, "lon": None, "alt_m": None,
            "speed_kmh": None, "course_deg": None,
            "sats_used": 0, "sats_in_view": 0, "hdop": None,
            "utc": None, "date": None,
        }

    @property
    def available(self) -> bool:
        try:
            return Path(self.port).exists()
        except OSError:
            return False

    def start(self) -> "GpsReader":
        if not self.available:
            log.info("GPS port %s not present — GPS disabled", self.port)
            return self
        threading.Thread(target=self._read_loop, daemon=True).start()
        if self.log_dir is not None:
            threading.Thread(target=self._log_loop, daemon=True).start()
        log.info(
            "GPS reader started on %s @ %s (log every %ss -> %s)",
            self.port, self.baud, self.log_interval, self.log_dir,
        )
        return self

    def stop(self) -> None:
        self._stop.set()

    # -- reading ------------------------------------------------------------
    def _configure_port(self) -> None:
        try:
            subprocess.run(
                ["stty", "-F", self.port, str(self.baud), "raw", "-echo"],
                capture_output=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("stty failed: %s", exc)

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._configure_port()
                with open(self.port, "rb", buffering=0) as f:
                    while not self._stop.is_set():
                        raw = f.readline()
                        if not raw:
                            time.sleep(0.2)
                            continue
                        self._bytes += len(raw)
                        line = raw.decode("ascii", "replace").strip()
                        if line.startswith("$"):
                            self._handle(line)
            except Exception as exc:  # device vanished, perms, etc. — retry
                self._errors += 1
                log.debug("GPS read error: %s", exc)
                self._stop.wait(2.0)

    def _handle(self, line: str) -> None:
        self._sentences += 1
        self._last_rx = time.time()
        with self._lock:
            self._raw.append(line)
        if not _nmea_ok(line):
            return
        fields = line.split("*")[0].split(",")
        typ = fields[0][-3:]
        try:
            handler = {
                "GGA": self._gga, "RMC": self._rmc, "VTG": self._vtg,
                "GSA": self._gsa, "GSV": self._gsv,
            }.get(typ)
            if handler:
                handler(fields)
        except (IndexError, ValueError):
            pass

    def _gga(self, fl: List[str]) -> None:
        with self._lock:
            self._state["utc"] = _hhmmss(fl[1])
            lat, lon = _dm_to_deg(fl[2], fl[3]), _dm_to_deg(fl[4], fl[5])
            q = int(fl[6] or 0)
            self._state["fix_quality"] = q
            if lat is not None and lon is not None:
                self._state["lat"], self._state["lon"] = lat, lon
            self._state["sats_used"] = int(fl[7] or 0)
            self._state["hdop"] = _f(fl[8])
            self._state["alt_m"] = _f(fl[9])
            if q > 0:
                self._state["fix"] = True

    def _rmc(self, fl: List[str]) -> None:
        with self._lock:
            self._state["fix"] = fl[2] == "A"
            self._state["utc"] = _hhmmss(fl[1])
            lat, lon = _dm_to_deg(fl[3], fl[4]), _dm_to_deg(fl[5], fl[6])
            if lat is not None and lon is not None:
                self._state["lat"], self._state["lon"] = lat, lon
            spd = _f(fl[7])
            if spd is not None:
                self._state["speed_kmh"] = round(spd * 1.852, 1)
            crs = _f(fl[8])
            if crs is not None:
                self._state["course_deg"] = crs
            self._state["date"] = _ddmmyy(fl[9])

    def _vtg(self, fl: List[str]) -> None:
        with self._lock:
            crs = _f(fl[1])
            if crs is not None:
                self._state["course_deg"] = crs
            kmh = _f(fl[7])
            if kmh is not None:
                self._state["speed_kmh"] = round(kmh, 1)

    def _gsa(self, fl: List[str]) -> None:
        with self._lock:
            self._state["fix_type"] = int(fl[2] or 1)  # 1=none 2=2D 3=3D
            hdop = _f(fl[16])
            if hdop is not None:
                self._state["hdop"] = hdop

    def _gsv(self, fl: List[str]) -> None:
        with self._lock:
            self._state["sats_in_view"] = int(fl[3] or 0)

    # -- logging ------------------------------------------------------------
    def _log_loop(self) -> None:
        while not self._stop.wait(self.log_interval):
            try:
                self._write_log()
            except Exception as exc:
                log.debug("GPS log error: %s", exc)

    def _write_log(self) -> None:
        if self.log_dir is None or self._sentences == 0:
            return  # nothing received yet -> nothing to log
        now = time.time()
        with self._lock:
            snap = dict(self._state)
            raw = list(self._raw)
        lt = time.localtime(now)
        raw_dir = self.log_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        fname = raw_dir / time.strftime("%Y-%m-%d.jsonl", lt)
        rec = {
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", lt),
            "ts": round(now, 1),
            **snap,
            "receiving": (now - self._last_rx) < 5.0,
            "raw_rmc": next((r for r in reversed(raw) if "RMC" in r), None),
            "raw_gga": next((r for r in reversed(raw) if "GGA" in r), None),
        }
        with open(fname, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        self._log_file = fname
        self._log_count += 1

    # -- status -------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            snap = dict(self._state)
            last_raw = self._raw[-1] if self._raw else None
        receiving = (
            self.available and self._sentences > 0 and (now - self._last_rx) < 5.0
        )
        return {
            "available": self.available,
            "port": self.port,
            "baud": self.baud,
            "receiving": receiving,
            "last_data_age": round(now - self._last_rx, 1) if self._last_rx else None,
            "sentences": self._sentences,
            "bytes": self._bytes,
            "errors": self._errors,
            "uptime": round(now - self._started_at, 1),
            "last_sentence": last_raw,
            "log_file": self._log_file.name if self._log_file else None,
            "log_count": self._log_count,
            "log_interval": self.log_interval,
            **snap,
        }

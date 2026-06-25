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
        min_log_mph: float = 3.0,
        min_log_sats: int = 7,
        max_log_hdop: float = 3.0,
        diag_interval: int = 30,
        diag_keep_days: int = 14,
        get_battery=None,
    ) -> None:
        self.port = port
        self.baud = baud
        self.log_dir = Path(log_dir) if log_dir else None
        self.log_interval = max(2, int(log_interval))
        # Flight recorder: a SEPARATE always-on diagnostic log (GPS state +
        # battery every diag_interval, regardless of the track gate) so we can
        # reconstruct "what happened" on a ride — when it got satellite lock,
        # whether the battery died, etc. Rotated to the last diag_keep_days.
        self.diag_interval = max(5, int(diag_interval))
        self.diag_keep_days = max(1, int(diag_keep_days))
        self.get_battery = get_battery  # callable -> power_status() dict, or None
        self._diag_file: Optional[Path] = None
        self._diag_count = 0
        # Log only a GOOD, MOVING fix. Indoors the fix is weak (few satellites,
        # high HDOP) and throws fake speeds up to ~12 mph, so speed alone leaks.
        # Satellite count is the clean discriminator: indoor jitter uses 3-6
        # sats, a real outdoor ride uses 8-12. All live-tunable.
        self.min_log_mph = float(min_log_mph)
        self.min_log_sats = int(min_log_sats)
        self.max_log_hdop = float(max_log_hdop)
        self._skipped = 0
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
            threading.Thread(target=self._diag_loop, daemon=True).start()
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
        # Quality + movement gate: only log a GOOD, MOVING fix.
        passes, _reason = self._gate(snap)
        if not passes:
            self._skipped += 1
            return
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

    def _gate(self, snap: Dict[str, Any]):
        """(passes, reason) for the track-logging gate: a good, MOVING fix."""
        if not snap.get("fix"):
            return False, "no fix"
        sats = snap.get("sats_used") or 0
        if sats < self.min_log_sats:
            return False, "%d sats (<%d)" % (sats, self.min_log_sats)
        hdop = snap.get("hdop")
        if hdop is None or hdop > self.max_log_hdop:
            return False, "HDOP %s (>%s)" % (hdop, self.max_log_hdop)
        speed_mph = (snap.get("speed_kmh") or 0.0) * 0.621371
        if speed_mph < self.min_log_mph:
            return False, "%.1f mph (<%s)" % (speed_mph, self.min_log_mph)
        return True, "ok"

    # -- flight recorder (always-on diagnostic log) -------------------------
    def _diag_loop(self) -> None:
        self._write_diag(event="boot")  # mark each app start / power-up
        self._prune_diag()
        while not self._stop.wait(self.diag_interval):
            try:
                self._write_diag()
            except Exception as exc:
                log.debug("GPS diag error: %s", exc)

    def _write_diag(self, event: str = "") -> None:
        if self.log_dir is None:
            return
        now = time.time()
        with self._lock:
            snap = dict(self._state)
        passes, reason = self._gate(snap)
        batt = None
        if self.get_battery is not None:
            try:
                b = self.get_battery() or {}
                batt = {
                    "pct": b.get("percent"),
                    "volt": b.get("voltage"),
                    "plugged": b.get("external_power"),
                    "charging": b.get("charging"),
                    "state": b.get("state"),
                }
            except Exception:
                batt = None
        lt = time.localtime(now)
        diag_dir = self.log_dir / "diag"
        diag_dir.mkdir(parents=True, exist_ok=True)
        fname = diag_dir / time.strftime("%Y-%m-%d.jsonl", lt)
        rec = {
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", lt),
            "ts": round(now, 1),
            "event": event or "tick",
            "gps_utc": snap.get("utc"),
            "gps_date": snap.get("date"),
            "receiving": (now - self._last_rx) < 5.0,
            "fix": snap.get("fix"),
            "fix_type": snap.get("fix_type"),
            "sats_used": snap.get("sats_used"),
            "sats_in_view": snap.get("sats_in_view"),
            "hdop": snap.get("hdop"),
            "lat": snap.get("lat"),
            "lon": snap.get("lon"),
            "speed_kmh": snap.get("speed_kmh"),
            "would_log": passes,
            "why": reason,
            "sentences": self._sentences,
            "battery": batt,
        }
        with open(fname, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        self._diag_file = fname
        self._diag_count += 1

    def _prune_diag(self) -> None:
        diag_dir = self.log_dir / "diag" if self.log_dir else None
        if not diag_dir or not diag_dir.exists():
            return
        cutoff = time.time() - self.diag_keep_days * 86400
        for p in diag_dir.glob("*.jsonl"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass

    def recent_diag(self, n: int = 200) -> List[Dict[str, Any]]:
        """The last n flight-recorder entries (newest last), across recent days."""
        diag_dir = self.log_dir / "diag" if self.log_dir else None
        if not diag_dir or not diag_dir.exists():
            return []
        out: List[Dict[str, Any]] = []
        for p in sorted(diag_dir.glob("*.jsonl"))[-3:]:
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except ValueError:
                            pass
            except OSError:
                pass
        return out[-n:]

    # -- status -------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            snap = dict(self._state)
            last_raw = self._raw[-1] if self._raw else None
        receiving = (
            self.available and self._sentences > 0 and (now - self._last_rx) < 5.0
        )
        passes, reason = self._gate(snap)
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
            "min_log_mph": round(self.min_log_mph, 1),
            "min_log_sats": self.min_log_sats,
            "max_log_hdop": round(self.max_log_hdop, 1),
            "skipped_logs": self._skipped,
            "logging_now": passes,        # is it recording the track right now?
            "why_not": None if passes else reason,
            "diag_file": self._diag_file.name if self._diag_file else None,
            "diag_count": self._diag_count,
            **snap,
        }

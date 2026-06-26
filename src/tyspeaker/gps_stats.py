"""Aggregate logged GPS fixes into per-day tracks + ride metrics + an odometer.

Reads the raw 10s snapshots written by ``gps.GpsReader`` (``gps/raw/<date>.jsonl``),
computes a jitter-filtered track and ride stats per day, caches each daily summary
to ``gps/days/<date>.json`` and the running total to ``gps/odometer.json`` — all on
the SD card. Built for a kid's scooter: miles ridden, time riding, avg/top speed,
and a lifetime odometer.

Folder layout under the GPS dir (default ``~/tyspeaker/gps``):
    raw/<YYYY-MM-DD>.jsonl   one 10s snapshot per line (raw NMEA + parsed)
    days/<YYYY-MM-DD>.json   computed {date, stats, track, bounds}
    odometer.json            {total_mi, updated}
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .logsetup import get_logger

log = get_logger("gps_stats")

_MI_PER_KM = 0.621371
_MIN_MOVE_KMH = 1.5       # below this between fixes = GPS drift while stopped
_MAX_PLAUSIBLE_KMH = 25   # above this between fixes = a GPS jump (a kid scooter
                          # tops ~15 mph); ignore the segment as jitter
_MOVING_KMH = 2.0         # "actually riding" threshold for moving time


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


class GpsStats:
    def __init__(self, gps_dir: Path) -> None:
        self.dir = Path(gps_dir)
        self.raw_dir = self.dir / "raw"
        self.days_dir = self.dir / "days"
        self.odo_path = self.dir / "odometer.json"

    # -- raw ----------------------------------------------------------------
    def _read_raw(self, date: str) -> List[Dict[str, Any]]:
        p = self.raw_dir / f"{date}.jsonl"
        if not p.exists():
            return []
        recs: List[Dict[str, Any]] = []
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        recs.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return []
        return recs

    def available_dates(self) -> List[str]:
        if not self.raw_dir.exists():
            return []
        return sorted(p.stem for p in self.raw_dir.glob("*.jsonl"))

    # -- per-day ------------------------------------------------------------
    def compute_day(self, date: str, force: bool = False) -> Dict[str, Any]:
        cache = self.days_dir / f"{date}.json"
        raw = self.raw_dir / f"{date}.jsonl"
        today = time.strftime("%Y-%m-%d", time.localtime())
        # past days are immutable -> use the cache if it's newer than the raw log
        if (
            not force
            and date != today
            and cache.exists()
            and raw.exists()
            and cache.stat().st_mtime >= raw.stat().st_mtime
        ):
            try:
                return json.loads(cache.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
        summary = self._build_day(date)
        try:
            self.days_dir.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(summary), encoding="utf-8")
        except OSError as exc:
            log.debug("day cache write failed: %s", exc)
        return summary

    # A new trip starts when there's a gap this long between fixes (came home /
    # took a break between rides) — so two rides in a day don't get joined.
    _TRIP_GAP_S = 120

    def _segment_stats(self, pts: List[tuple]) -> Dict[str, Any]:
        """pts = sorted [(ts, lat, lon, spd_kmh)] -> {stats, track, bounds}."""
        track: List[list] = []
        dist_km = 0.0
        moving_s = 0.0
        max_kmh = 0.0
        prev: Optional[tuple] = None
        for ts, lat, lon, spd in pts:
            mph = round((spd or 0.0) * _MI_PER_KM, 1) if spd is not None else None
            track.append([round(lat, 6), round(lon, 6), mph])
            if spd is not None and spd > max_kmh:
                max_kmh = spd
            if prev is not None:
                dt = ts - prev[0]
                if 0 < dt <= 60:
                    seg = _haversine_km(prev[1], prev[2], lat, lon)
                    implied = seg / (dt / 3600.0) if dt > 0 else 0.0
                    if _MIN_MOVE_KMH <= implied <= _MAX_PLAUSIBLE_KMH:
                        dist_km += seg
                        if implied >= _MOVING_KMH:
                            moving_s += dt
            prev = (ts, lat, lon, spd)
        dist_mi = dist_km * _MI_PER_KM
        bounds = None
        if track:
            lats = [p[0] for p in track]
            lons = [p[1] for p in track]
            bounds = [[min(lats), min(lons)], [max(lats), max(lons)]]
        return {
            "stats": {
                "distance_mi": round(dist_mi, 2),
                "moving_s": int(moving_s),
                "total_s": int(pts[-1][0] - pts[0][0]) if len(pts) >= 2 else 0,
                "avg_mph": round(dist_mi / (moving_s / 3600.0), 1) if moving_s > 5 else 0.0,
                "max_mph": round(max_kmh * _MI_PER_KM, 1),
                "points": len(track),
            },
            "track": track,
            "bounds": bounds,
        }

    def _build_day(self, date: str) -> Dict[str, Any]:
        # Only "away from home" fixes form a ride (home points are jitter/idle).
        # Old logs have no 'home' key -> kept (treated as away).
        away: List[tuple] = []
        for r in self._read_raw(date):
            lat, lon, ts = r.get("lat"), r.get("lon"), r.get("ts")
            if lat is None or lon is None or ts is None or not r.get("fix"):
                continue
            if r.get("home"):
                continue
            away.append((float(ts), float(lat), float(lon), r.get("speed_kmh")))
        away.sort(key=lambda x: x[0])

        # split into trips wherever there's a long gap (home/break between rides)
        segs: List[List[tuple]] = []
        cur: List[tuple] = []
        for p in away:
            if cur and (p[0] - cur[-1][0]) > self._TRIP_GAP_S:
                segs.append(cur)
                cur = []
            cur.append(p)
        if cur:
            segs.append(cur)

        trips = []
        for seg in segs:
            s = self._segment_stats(seg)
            st = s["stats"]
            st["start"] = time.strftime("%H:%M", time.localtime(seg[0][0]))
            st["end"] = time.strftime("%H:%M", time.localtime(seg[-1][0]))
            trips.append(
                {"index": len(trips) + 1, "stats": st,
                 "track": s["track"], "bounds": s["bounds"]}
            )

        bounds = None
        if trips:
            lats = [p[0] for t in trips for p in t["track"]]
            lons = [p[1] for t in trips for p in t["track"]]
            if lats:
                bounds = [[min(lats), min(lons)], [max(lats), max(lons)]]
        day = {
            "distance_mi": round(sum(t["stats"]["distance_mi"] for t in trips), 2),
            "moving_s": sum(t["stats"]["moving_s"] for t in trips),
            "max_mph": max([t["stats"]["max_mph"] for t in trips], default=0.0),
            "points": sum(t["stats"]["points"] for t in trips),
            "trips": len(trips),
        }
        day["avg_mph"] = (
            round(day["distance_mi"] / (day["moving_s"] / 3600.0), 1)
            if day["moving_s"] > 5 else 0.0
        )
        return {"date": date, "stats": day, "trips": trips, "bounds": bounds}

    # -- aggregates ---------------------------------------------------------
    def list_days(self) -> Dict[str, Any]:
        months: Dict[str, List[Dict[str, Any]]] = {}
        odo = 0.0
        for date in self.available_dates():
            s = self.compute_day(date)["stats"]
            odo += s["distance_mi"]
            months.setdefault(date[:7], []).append(
                {
                    "date": date,
                    "distance_mi": s["distance_mi"],
                    "moving_s": s["moving_s"],
                    "max_mph": s["max_mph"],
                }
            )
        out = []
        for month in sorted(months.keys(), reverse=True):
            days = sorted(months[month], key=lambda d: d["date"], reverse=True)
            out.append(
                {
                    "month": month,
                    "days": days,
                    "total_mi": round(sum(d["distance_mi"] for d in days), 2),
                    "total_moving_s": sum(d["moving_s"] for d in days),
                }
            )
        odo = round(odo, 2)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.odo_path.write_text(
                json.dumps(
                    {"total_mi": odo, "updated": time.strftime("%Y-%m-%dT%H:%M:%S")}
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
        return {"months": out, "odometer_mi": odo}

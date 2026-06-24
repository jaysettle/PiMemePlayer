"""Playback engine: ties the player, ordered playlist, mixer and settings.

Exposes the high-level actions the web UI and the physical inputs (button /
rotary encoder) both call.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Optional

from .audio import Player
from .library import Library
from .logsetup import get_logger
from .mixer import Mixer
from .playlist import Playlist
from .settings import Settings

log = get_logger("engine")


class PlaybackEngine:
    def __init__(
        self,
        library: Library,
        settings: Settings,
        player: Optional[Player] = None,
        mixer: Optional[Mixer] = None,
    ) -> None:
        self.library = library
        self.settings = settings
        self.playlist = Playlist(library, settings)
        self.player = player or Player(
            settings.get("player_cmd", "").split() or None
        )
        self.mixer = mixer or Mixer()
        self._lock = threading.Lock()
        self._playing_index: Optional[int] = None  # cursor for "next"
        self._playing_rel: Optional[str] = None
        self._started_at: Optional[float] = None
        self._selection: int = 0                    # cursor for the encoder
        self.player.set_volume(self.settings.volume)
        # apply persisted volume to the system on startup
        try:
            self.mixer.set_volume(self.settings.volume)
        except Exception:
            pass

    # -- playback -----------------------------------------------------------
    def play_rel(self, rel: str) -> None:
        log.info("play_rel %s", rel)
        # Feed the current speaker MAC to the player so a bluez-alsa player_cmd
        # ("...bluealsa:DEV={btmac}...") targets the connected speaker.
        self.player.bt_mac = str(self.settings.get("bt_autoconnect_mac", "") or "")
        self.player.play(self.library.path_for(rel))
        idx = self.playlist.index_of(rel)
        with self._lock:
            self._playing_index = idx
            self._playing_rel = rel
            self._started_at = time.monotonic()
            if idx is not None:
                self._selection = idx

    def play_index(self, index: int) -> None:
        rel = self.playlist.rel_at(index)
        if rel is None:
            raise IndexError(index)
        self.play_rel(rel)

    def stop(self) -> None:
        self.player.stop()
        with self._lock:
            self._playing_rel = None
            self._started_at = None

    def next(self) -> Optional[str]:
        """Advance per the current mode and play. Returns the rel played."""
        with self._lock:
            current = self._playing_index
        idx = self.playlist.next_index(current, self.settings.mode)
        if idx is None:
            return None
        self.play_index(idx)
        return self.playlist.rel_at(idx)

    def play_selected(self) -> Optional[str]:
        rel = self.playlist.rel_at(self._selection)
        if rel:
            self.play_rel(rel)
        return rel

    def step_and_play(self, step: int) -> Optional[dict]:
        """Move the selection sequentially by `step` and play it. Used by the
        physical 'hold = next sound' gesture and knob auditioning. Returns
        {index, rel_path, wrapped, count} or None if the playlist is empty."""
        order = self.playlist.order()
        if not order:
            return None
        with self._lock:
            prev = self._selection
            new = (prev + step) % len(order)
        wrapped = (step > 0 and new < prev) or (step < 0 and new > prev)
        rel = order[new]
        self.play_rel(rel)  # also syncs the selection cursor to `new`
        return {
            "index": new,
            "rel_path": rel,
            "wrapped": wrapped,
            "count": len(order),
        }

    def play_random(self) -> Optional[dict]:
        """Pick a random sample (different from the current one if possible)
        and play it. Used by the physical 'double-tap = surprise' gesture."""
        order = self.playlist.order()
        if not order:
            return None
        with self._lock:
            cur = self._selection
        if len(order) == 1:
            idx = 0
        else:
            idx = random.choice([i for i in range(len(order)) if i != cur])
        rel = order[idx]
        self.play_rel(rel)
        return {"index": idx, "rel_path": rel, "count": len(order)}

    # -- encoder selection cursor ------------------------------------------
    def cycle_selection(self, step: int) -> Optional[dict]:
        order = self.playlist.order()
        if not order:
            return None
        with self._lock:
            self._selection = (self._selection + step) % len(order)
            idx = self._selection
        rel = order[idx]
        return {"index": idx, "rel_path": rel}

    # -- mode ---------------------------------------------------------------
    def set_mode(self, mode: str) -> str:
        mode = "random" if mode == "random" else "sequential"
        self.settings.update({"mode": mode})
        return mode

    def toggle_mode(self) -> str:
        return self.set_mode(
            "random" if self.settings.mode == "sequential" else "sequential"
        )

    # -- volume -------------------------------------------------------------
    def set_volume(self, level: int) -> int:
        level = self.mixer.set_volume(level)
        self.player.set_volume(level)
        self.settings.update({"volume": level})
        return level

    def volume_step(self, delta: int) -> int:
        return self.set_volume(self.mixer.get_volume() + delta)

    # -- status -------------------------------------------------------------
    def status(self) -> dict:
        now_playing = self.player.now_playing()
        with self._lock:
            selection = self._selection
            playing_rel = self._playing_rel if now_playing else None
            started_at = self._started_at if now_playing else None
            if not now_playing:
                self._playing_rel = None
                self._started_at = None
        duration = None
        elapsed = None
        if playing_rel:
            try:
                duration = self.library.duration_for_path(
                    self.library.path_for(playing_rel)
                )
            except (FileNotFoundError, ValueError):
                duration = None
            if started_at is not None:
                elapsed = max(0.0, time.monotonic() - started_at)
        return {
            "now_playing": now_playing,
            "now_playing_rel": playing_rel,
            "now_playing_duration": duration,
            "now_playing_elapsed": elapsed,
            "mode": self.settings.mode,
            "volume": self.mixer.get_volume(),
            "selection": selection,
            "count": len(self.playlist.order()),
            "player_available": self.player.available,
            "player_name": self.player.player_name,
            "mixer_backend": self.mixer.backend,
        }

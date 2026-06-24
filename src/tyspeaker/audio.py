"""Audio playback.

Shells out to whichever common player is installed. Each candidate plays many
formats and routes to the system default audio sink — which becomes the
Bluetooth A2DP sink once the speaker is paired and set as default.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import List, Optional

from .logsetup import get_logger

log = get_logger("audio")

# Priority order. {path} is replaced with the file path at play time.
_PLAYER_CANDIDATES: List[List[str]] = [
    ["mpv", "--no-video", "--really-quiet", "{path}"],
    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", "{path}"],
    ["cvlc", "--play-and-exit", "--intf", "dummy", "{path}"],
    ["mpg123", "-q", "{path}"],
]


def _detect_player() -> Optional[List[str]]:
    for cmd in _PLAYER_CANDIDATES:
        if shutil.which(cmd[0]):
            return cmd
    return None


class Player:
    """Single-stream player: starting a new sample stops the previous one."""

    def __init__(self, player_cmd: Optional[List[str]] = None) -> None:
        self._template: Optional[List[str]] = player_cmd or _detect_player()
        self._proc: Optional[subprocess.Popen] = None
        self._current: Optional[str] = None
        self._ipc_path: Optional[Path] = None
        self._volume = 70
        # MAC of the target Bluetooth speaker, substituted into a {btmac}
        # placeholder in player_cmd (used by the bluez-alsa player command:
        # "mpg123 -o alsa -a bluealsa:DEV={btmac},PROFILE=a2dp"). The engine
        # refreshes this from settings before each play.
        self.bt_mac: str = ""
        self._lock = threading.Lock()
        if self._template:
            log.info("player selected: %s", " ".join(self._template))
        else:
            log.warning(
                "NO audio player found (need mpv/ffplay/cvlc/mpg123) — "
                "playback will fail"
            )

    @property
    def available(self) -> bool:
        return self._template is not None

    @property
    def player_name(self) -> Optional[str]:
        return self._template[0] if self._template else None

    def set_volume(self, level: int) -> int:
        level = max(0, min(100, int(level)))
        with self._lock:
            self._volume = level
            if (
                self.player_name == "mpv"
                and self._proc is not None
                and self._proc.poll() is None
            ):
                self._set_mpv_live_volume(level)
        return level

    def play(self, path: Path) -> None:
        if self._template is None:
            raise RuntimeError(
                "No audio player found. Install one of: mpv, ffmpeg (ffplay), "
                "vlc (cvlc), mpg123."
            )
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        has_path = any("{path}" in arg for arg in self._template)
        cmd = [arg.format(path=str(path), btmac=self.bt_mac) for arg in self._template]
        if not has_path:
            # player_cmd without a {path} placeholder (e.g. "mpg123 -q -o pulse")
            # -> append the file so the player has something to play.
            cmd.append(str(path))
        with self._lock:
            self._stop_locked()
            if self.player_name == "mpv":
                cmd = self._mpv_command(cmd)
            log.info("play %s -> %s", path.name, " ".join(cmd))
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if self.player_name == "mpv" else None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self._current = path.name
            # watch for non-zero exit and surface the player's stderr (this is
            # what used to fail silently when stderr went to DEVNULL).
            threading.Thread(
                target=self._watch, args=(self._proc, cmd), daemon=True
            ).start()

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def is_playing(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def now_playing(self) -> Optional[str]:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return self._current
            return None

    def _watch(self, proc: subprocess.Popen, cmd: List[str]) -> None:
        """Wait for the player to exit; log stderr on unexpected failure."""
        try:
            _, err = proc.communicate()
        except Exception as exc:  # pragma: no cover
            log.debug("watch error: %s", exc)
            return
        rc = proc.returncode
        # -15 SIGTERM / -9 SIGKILL = we stopped it on purpose (new sample)
        if rc in (0, -15, -9, None):
            log.debug("player exit rc=%s (%s)", rc, cmd[0])
            return
        msg = (err or b"").decode("utf-8", "replace").strip()
        log.warning(
            "player FAILED rc=%s cmd=%s%s",
            rc,
            cmd[0],
            (" stderr=" + msg[-600:]) if msg else " (no stderr)",
        )

    # -- internal (call with _lock held) ------------------------------------
    def _stop_locked(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        self._current = None
        if self._ipc_path is not None:
            try:
                self._ipc_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._ipc_path = None

    def _mpv_command(self, cmd: List[str]) -> List[str]:
        has_volume = any(arg == "--volume" or arg.startswith("--volume=") for arg in cmd)
        has_input = any(
            arg == "--input-terminal" or arg.startswith("--input-terminal=")
            for arg in cmd
        )
        has_ipc = any(
            arg == "--input-ipc-server" or arg.startswith("--input-ipc-server=")
            for arg in cmd
        )
        extra: List[str] = []
        if not has_volume:
            extra.append(f"--volume={self._volume}")
        if not has_input:
            extra.append("--input-terminal=yes")
        if os.name == "posix" and not has_ipc:
            self._ipc_path = Path(tempfile.gettempdir()) / (
                f"tyspeaker-mpv-{os.getpid()}-{id(self)}.sock"
            )
            self._ipc_path.unlink(missing_ok=True)
            extra.append(f"--input-ipc-server={self._ipc_path}")
        return [cmd[0], *extra, *cmd[1:]]

    def _set_mpv_live_volume(self, level: int) -> None:
        if self._ipc_path is not None:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(1)
                    sock.connect(str(self._ipc_path))
                    payload = {"command": ["set_property", "volume", level]}
                    sock.sendall((json.dumps(payload) + "\n").encode())
                    return
            except OSError:
                pass
        if self._proc is not None and self._proc.stdin is not None:
            try:
                self._proc.stdin.write(f"set volume {level}\n".encode())
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

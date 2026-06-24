"""System audio volume + output control.

On the TySpeaker Pi, Bluetooth audio goes through **bluez-alsa** (bluealsa): the
A2DP volume is the speaker's own control, exposed as an ALSA mixer on the
``bluealsa`` device (e.g. ``'AL HydraMini - A2DP'``). Falls back to PipeWire
(``wpctl``) / PulseAudio (``pactl``) / ALSA (``amixer``) on other setups, and to
an in-memory level when nothing is present (e.g. off-Pi, or no speaker yet).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import List, Optional, Tuple


def _run(args: List[str]) -> Tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=8)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def _run_out(args: List[str]) -> str:
    """stdout only — bluealsa's ctl plugin spams a harmless battery-status
    warning to stderr that would otherwise pollute parsing."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=8)
        return p.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


class Mixer:
    def __init__(self) -> None:
        self.backend: Optional[str] = None
        # This Pi routes A2DP through bluez-alsa (PipeWire is masked), so prefer
        # the bluealsa mixer whenever the daemon is installed.
        if shutil.which("amixer") and shutil.which("bluealsa"):
            self.backend = "bluealsa"
        else:
            for tool in ("wpctl", "pactl", "amixer"):
                if shutil.which(tool):
                    self.backend = tool
                    break
        self._fallback = 70  # used when no backend, or no speaker connected

    @property
    def available(self) -> bool:
        return self.backend is not None

    # -- bluealsa -----------------------------------------------------------
    def _bluealsa_control(self) -> Optional[str]:
        """Name of the connected speaker's A2DP mixer control, or None when no
        speaker is connected (the control only exists while connected)."""
        for line in _run_out(["amixer", "-D", "bluealsa", "scontrols"]).splitlines():
            m = re.search(r"Simple mixer control '([^']+)'", line)
            if m:
                return m.group(1)
        return None

    # -- volume -------------------------------------------------------------
    def get_volume(self) -> int:
        if self.backend == "bluealsa":
            ctl = self._bluealsa_control()
            if ctl:
                out = _run_out(["amixer", "-D", "bluealsa", "sget", ctl])
                m = re.search(r"\[(\d+)%\]", out)
                if m:
                    return int(m.group(1))
        elif self.backend == "wpctl":
            rc, out = _run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"])
            m = re.search(r"([0-9]*\.?[0-9]+)", out)
            if rc == 0 and m:
                return max(0, min(100, round(float(m.group(1)) * 100)))
        elif self.backend == "pactl":
            rc, out = _run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
            m = re.search(r"(\d+)%", out)
            if rc == 0 and m:
                return int(m.group(1))
        elif self.backend == "amixer":
            rc, out = _run(["amixer", "get", "Master"])
            m = re.search(r"\[(\d+)%\]", out)
            if rc == 0 and m:
                return int(m.group(1))
        return self._fallback

    def set_volume(self, level: int) -> int:
        level = max(0, min(100, int(level)))
        self._fallback = level
        if self.backend == "bluealsa":
            ctl = self._bluealsa_control()
            if ctl:
                _run(["amixer", "-D", "bluealsa", "sset", ctl, f"{level}%"])
        elif self.backend == "wpctl":
            _run(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{level/100:.2f}"])
        elif self.backend == "pactl":
            _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"])
        elif self.backend == "amixer":
            _run(["amixer", "set", "Master", f"{level}%"])
        return level

    def step(self, delta: int) -> int:
        # Step from our last-set level for bluealsa to avoid a hardware read on
        # every encoder detent (keeps volume turns snappy).
        base = self._fallback if self.backend == "bluealsa" else self.get_volume()
        return self.set_volume(base + delta)

    # -- output sink --------------------------------------------------------
    def list_sinks(self) -> List[dict]:
        sinks: List[dict] = []
        if self.backend == "bluealsa":
            ctl = self._bluealsa_control()
            if ctl:
                sinks.append({"id": ctl, "name": ctl})
        elif self.backend == "pactl":
            rc, out = _run(["pactl", "list", "short", "sinks"])
            if rc == 0:
                for line in out.splitlines():
                    parts = line.split("\t")
                    if len(parts) >= 2:
                        sinks.append({"id": parts[1], "name": parts[1]})
        elif self.backend == "wpctl":
            rc, out = _run(["wpctl", "status"])
            if rc == 0:
                in_sinks = False
                for line in out.splitlines():
                    if "Sinks:" in line and "Sink endpoints:" not in line:
                        in_sinks = True
                        continue
                    if "Sink endpoints:" in line:
                        in_sinks = False
                    if not in_sinks:
                        continue
                    m = re.search(r"\*?\s*(\d+)\.\s+(.*?)\s+\[", line)
                    if m:
                        sinks.append({"id": m.group(1), "name": m.group(2).strip()})
        return sinks

    def set_default_sink(self, sink: str) -> bool:
        if self.backend == "bluealsa":
            return True  # single implicit sink: the connected speaker
        if not sink:
            return False
        if self.backend == "pactl":
            rc, _ = _run(["pactl", "set-default-sink", sink])
            return rc == 0
        if self.backend == "wpctl":
            rc, _ = _run(["wpctl", "set-default", sink])
            return rc == 0
        return False

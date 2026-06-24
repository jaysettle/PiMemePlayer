"""Bluetooth speaker management via ``bluetoothctl`` (BlueZ).

The app is deliberately conservative here: A2DP speakers are classic
Bluetooth devices, and reconnect only works reliably after a real pair/bond
creates a stored link key. Trusting a discovered MAC is not enough.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from typing import Callable, Dict, List, Optional, Set, Tuple

from .logsetup import get_logger

log = get_logger("bluetooth")

_MAC_RE = re.compile(r"((?:[0-9A-F]{2}:){5}[0-9A-F]{2})", re.I)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean(text: str) -> str:
    return _ANSI_RE.sub("", text).replace("\x01", "").replace("\x02", "")


def _last_message(text: str, fallback: str) -> str:
    lines = [
        line.strip()
        for line in _clean(text).splitlines()
        if line.strip() and not line.strip().startswith("[bluetooth]")
    ]
    return lines[-1] if lines else fallback


def _run(
    args: List[str], timeout: int = 12, input_text: str | None = None
) -> Tuple[int, str]:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            input=input_text,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, _clean((proc.stdout or "") + (proc.stderr or ""))
    except subprocess.TimeoutExpired as exc:
        text = (exc.stdout or "") + (exc.stderr or "")
        return 124, _clean(text + "\nCommand timed out")
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def normalize_mac(mac: str) -> str:
    return mac.strip().upper()


class BluetoothManager:
    def __init__(self) -> None:
        self._bin = shutil.which("bluetoothctl")
        self._hcitool = shutil.which("hcitool")
        self._lock = threading.RLock()
        self._connect_lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self._bin is not None

    def _ctl(self, *args: str, timeout: int = 12) -> Tuple[int, str]:
        if not self._bin:
            return 1, ""
        with self._lock:
            return _run([self._bin, *args], timeout=timeout)

    def _ctl_agent(self, *args: str, timeout: int = 12) -> Tuple[int, str]:
        if not self._bin:
            return 1, ""
        with self._lock:
            return _run([self._bin, "--agent=NoInputNoOutput", *args], timeout=timeout)

    def _ctl_script(self, script: str, timeout: int = 45) -> Tuple[int, str]:
        if not self._bin:
            return 1, ""
        with self._lock:
            return _run([self._bin], timeout=timeout, input_text=script)

    # -- adapter ------------------------------------------------------------
    def status(self) -> Dict:
        if not self.available:
            return {"available": False}
        _, out = self._ctl("show")
        powered = "Powered: yes" in out
        name = None
        m = re.search(r"Name:\s*(.+)", out)
        if m:
            name = m.group(1).strip()
        connected = [d for d in self.devices() if d["connected"]]
        return {
            "available": True,
            "powered": powered,
            "adapter": name,
            "audio_ready": self._audio_ready_from_show(out),
            "connected": connected,
        }

    def _audio_ready_from_show(self, out: str) -> bool:
        return "Audio Sink" in out or "0000110b-0000-1000-8000-00805f9b34fb" in out

    def _audio_summary_from_show(self, out: str) -> str:
        uuid_lines = [
            line.strip()
            for line in out.splitlines()
            if "UUID:" in line
            and any(
                word in line.lower()
                for word in ("audio", "a/v", "handsfree", "headset")
            )
        ]
        return "; ".join(uuid_lines) if uuid_lines else "no audio UUIDs registered"

    def audio_ready(self) -> bool:
        if not self.available:
            return False
        rc, out = self._ctl("show", timeout=6)
        return rc == 0 and self._audio_ready_from_show(out)

    def audio_sink_present(self, mac: str, name: str = "") -> bool:
        """Return true when PipeWire exposes this speaker as an output sink."""
        if not shutil.which("wpctl"):
            return True
        rc, out = _run(["wpctl", "status"], timeout=4)
        if rc != 0:
            log.info("audio sink check failed mac=%s rc=%s", mac, rc)
            return False

        sink_lines: List[str] = []
        in_sinks = False
        for line in out.splitlines():
            if "Sinks:" in line and "Sink endpoints:" not in line:
                in_sinks = True
                continue
            if in_sinks and re.search(
                r"\b(Sources|Filters|Streams|Devices|Video):", line
            ):
                in_sinks = False
            if in_sinks:
                sink_lines.append(line)

        sink_text = "\n".join(sink_lines).lower()
        token = normalize_mac(mac).replace(":", "_").lower()
        ready = token in sink_text or bool(name and name.lower() in sink_text)
        if not ready:
            log.info(
                "audio sink missing mac=%s name=%s sinks=%s",
                mac,
                name,
                " | ".join(line.strip() for line in sink_lines) or "none",
            )
        return ready

    def wait_audio_sink(self, mac: str, timeout: float = 8.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        name = ""
        while time.monotonic() <= deadline:
            dev = self.device(mac)
            name = str((dev or {}).get("name", ""))
            if self.audio_sink_present(mac, name):
                log.info("audio sink ready mac=%s name=%s", mac, name or mac)
                return True
            time.sleep(0.5)
        log.warning("audio sink wait timed out mac=%s name=%s", mac, name or mac)
        return False

    def power(self, on: bool) -> bool:
        log.info("power %s requested", "on" if on else "off")
        rc, _ = self._ctl("power", "on" if on else "off")
        log.info("power %s rc=%s", "on" if on else "off", rc)
        return rc == 0

    def scan(self, seconds: int = 8) -> List[Dict]:
        """Run a timed discovery, then return the known device list."""
        seconds = max(2, min(30, int(seconds)))
        log.info("scan start seconds=%s", seconds)
        rc, out = self._ctl("--timeout", str(seconds), "scan", "on", timeout=seconds + 5)
        log.info("scan bluetoothctl rc=%s msg=%s", rc, _last_message(out, "scan done"))
        devices = {d["mac"]: d for d in self.devices()}
        classic = self._classic_scan()
        for mac, name in classic.items():
            devices.setdefault(
                mac,
                {
                    "mac": mac,
                    "name": name,
                    "paired": False,
                    "bonded": False,
                    "connected": False,
                    "trusted": False,
                    "pairable": False,
                    "discovered_via": "classic",
                },
            )
        result = sorted(devices.values(), key=lambda d: d["name"].lower())
        log.info(
            "scan result bluez_or_cached=%s classic=%s total=%s names=%s",
            len(devices) - len(classic),
            len(classic),
            len(result),
            ", ".join(f"{d.get('name')} {d.get('mac')}" for d in result[:10]) or "none",
        )
        return result

    # -- devices ------------------------------------------------------------
    def _parse_devices(self, text: str) -> Dict[str, str]:
        found: Dict[str, str] = {}
        for line in text.splitlines():
            m = _MAC_RE.search(line)
            if m:
                mac = m.group(1).upper()
                name = line.split(mac, 1)[-1].strip() or mac
                found[mac] = name
        return found

    def _classic_scan(self) -> Dict[str, str]:
        if not self._hcitool:
            log.info("classic hcitool scan skipped: hcitool not installed")
            return {}
        with self._lock:
            rc, out = _run([self._hcitool, "scan", "--flush"], timeout=15)
        if rc != 0:
            log.info("classic hcitool scan failed rc=%s msg=%s", rc, _last_message(out, "hcitool scan"))
            return {}
        found = self._parse_devices(out)
        log.info("classic hcitool scan found=%s", found)
        return found

    def _filtered_macs(self, *flt: str) -> Set[str]:
        rc, out = self._ctl("devices", *flt)
        if rc != 0 or not out.strip():
            if flt == ("Paired",):
                _, out = self._ctl("paired-devices")
            else:
                return set()
        return set(self._parse_devices(out).keys())

    def devices(self) -> List[Dict]:
        if not self.available:
            return []
        all_devs = self._parse_devices(self._ctl("devices")[1])
        paired = self._filtered_macs("Paired")
        bonded = self._filtered_macs("Bonded")
        connected = self._filtered_macs("Connected")
        trusted = self._filtered_macs("Trusted")
        result = []
        for mac, name in sorted(all_devs.items(), key=lambda kv: kv[1].lower()):
            result.append(
                {
                    "mac": mac,
                    "name": name,
                    "paired": mac in paired,
                    "bonded": mac in bonded,
                    "connected": mac in connected,
                    "trusted": mac in trusted,
                    "pairable": True,
                    "discovered_via": "bluez",
                }
            )
        return result

    def first_connected(self) -> Optional[Dict]:
        if not self.available:
            return None
        rc, out = self._ctl("devices", "Connected", timeout=6)
        if rc != 0:
            return None
        for mac, name in self._parse_devices(out).items():
            return {"mac": mac, "name": name}
        return None

    def device(self, mac: str) -> Optional[Dict]:
        mac = normalize_mac(mac)
        if not _MAC_RE.fullmatch(mac):
            return None
        rc, out = self._ctl("info", mac)
        if rc != 0:
            return None
        name = mac
        m = re.search(r"^\s*Name:\s*(.+)$", out, re.M)
        if m:
            name = m.group(1).strip()
        paired = "Paired: yes" in out
        bonded_known = "Bonded:" in out
        bonded = "Bonded: yes" in out
        return {
            "mac": mac,
            "name": name,
            "paired": paired,
            "bonded": bonded,
            "bonded_known": bonded_known,
            "connected": "Connected: yes" in out,
            "trusted": "Trusted: yes" in out,
            "pairable": True,
            "discovered_via": "bluez",
        }

    # -- actions (return (ok, message)) ------------------------------------
    def _action(self, verb: str, mac: str, timeout: int = 20) -> Tuple[bool, str]:
        if not _MAC_RE.fullmatch(mac or ""):
            return False, "Invalid MAC address"
        log.info("action %s start mac=%s timeout=%s", verb, mac, timeout)
        rc, out = self._ctl(verb, mac, timeout=timeout)
        msg = _last_message(out, verb)
        log.info("action %s done mac=%s rc=%s msg=%s", verb, mac, rc, msg)
        return rc == 0, msg

    def pair(self, mac: str) -> Tuple[bool, str]:
        if not _MAC_RE.fullmatch(mac or ""):
            return False, "Invalid MAC address"
        log.info("pair start mac=%s", mac)
        self.power(True)
        self._ctl("pairable", "on")
        rc, out = self._ctl_agent("pair", mac, timeout=45)
        ok, msg = rc == 0, _last_message(out, "pair")
        log.info("pair bluetoothctl done mac=%s rc=%s msg=%s", mac, rc, msg)
        lower = msg.lower()
        ok = ok or "pairing successful" in lower or "already paired" in lower
        if not ok:
            if "authenticationfailed" in lower.replace(".", ""):
                log.info("pair authentication failed mac=%s", mac)
                return (
                    False,
                    "Speaker did not accept pairing. Hold its button until it blinks, then tap Find speaker.",
                )
            log.info("pair failed mac=%s msg=%s", mac, msg)
            return False, msg

        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            dev = self.device(mac)
            if dev and (dev.get("paired") or dev.get("bonded")):
                log.info("pair saved mac=%s dev=%s", mac, dev)
                return True, "Pairing saved"
            time.sleep(0.5)
        log.warning("pair beeped but no saved pair/bond mac=%s", mac)
        return (
            False,
            "Pairing beeped but was not saved. Tap Start over, hold the speaker button until it blinks, then Find speaker.",
        )

    # Genuinely transient handshake errors worth a quick in-call retry. NOTE we
    # deliberately do NOT retry on profile-unavailable / "protocol not available":
    # those mean the A2DP media endpoints aren't registered this instant, and
    # hammering connect only feeds the endpoint churn. The autoconnect cadence
    # (~15s) will try again once, calmly, after the endpoints settle.
    _TRANSIENT = (
        "br-connection-busy",
        "br-connection-aborted-by-local",
        "in progress",
        "inprogress",
    )

    def connect(
        self, mac: str, tries: int = 3, delay: float = 2.0
    ) -> Tuple[bool, str]:
        """Plain connect, the way Bluetooth normally works. No audio-profile gate
        and no half-connected disconnect dance: the endpoint flap and the BlueZ
        SEP-cache crash those guarded against are fixed (power-on-once +
        main.conf Cache=no). The only retry is a light transient one that
        absorbs ``br-connection-busy`` while a handshake settles."""
        if not _MAC_RE.fullmatch(mac or ""):
            return False, "Invalid MAC address"
        if not self._connect_lock.acquire(blocking=False):
            log.info("connect rejected mac=%s: connect lock busy", mac)
            return False, "Bluetooth is already trying to connect."
        try:
            log.info("connect start mac=%s tries=%s", mac, tries)
            ok, msg = self._connect_locked(mac, tries=tries, delay=delay)
            log.info("connect complete mac=%s ok=%s msg=%s", mac, ok, msg)
            return ok, msg
        finally:
            self._connect_lock.release()

    def _connect_locked(self, mac: str, tries: int, delay: float) -> Tuple[bool, str]:
        ok, msg = False, "connect"
        for attempt in range(1, tries + 1):
            log.info("connect try %d/%d mac=%s", attempt, tries, mac)
            ok, msg = self._action("connect", mac, timeout=18)
            if ok:
                log.info("connect %s succeeded on try %d", mac, attempt)
                return True, msg
            if not any(t in msg.lower() for t in self._TRANSIENT):
                log.info("connect %s non-transient failure on try %d: %s", mac, attempt, msg)
                break
            log.info(
                "connect %s transient '%s' (try %d/%d), retrying",
                mac,
                msg,
                attempt,
                tries,
            )
            time.sleep(delay)
        return ok, msg

    def disconnect(self, mac: str) -> Tuple[bool, str]:
        return self._action("disconnect", mac)

    def trust(self, mac: str) -> Tuple[bool, str]:
        return self._action("trust", mac)

    def remove(self, mac: str) -> Tuple[bool, str]:
        return self._action("remove", mac)


class BluetoothAutoConnector:
    """Background retry loop for a preferred Bluetooth speaker."""

    def __init__(
        self,
        manager: BluetoothManager,
        get_mac: Callable[[], str],
        interval: int = 10,
        on_connect: Optional[Callable[[], None]] = None,
    ) -> None:
        self.manager = manager
        self.get_mac = get_mac
        self.interval = max(5, interval)
        # Fired once each time the speaker transitions to connected (boot greeting
        # + every reconnect). Used to play a random "personality" clip.
        self.on_connect = on_connect
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._last_skip_log: Dict[str, float] = {}

    def _announce_connected(self) -> None:
        cb = self.on_connect
        if cb is None:
            return
        try:
            cb()
        except Exception as exc:  # never let the greeting break the loop
            log.debug("on_connect callback error: %s", exc)

    def start(self) -> "BluetoothAutoConnector":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _skip(self, mac: str, reason: str, every: float = 60.0) -> None:
        key = f"{mac}:{reason}"
        now = time.monotonic()
        if now - self._last_skip_log.get(key, 0.0) >= every:
            log.info("autoconnect %s skipped: %s", mac, reason)
            self._last_skip_log[key] = now

    def _run(self) -> None:
        # The way Bluetooth normally works: pair once, then connect/reconnect is
        # automatic and bond-based. BlueZ AutoEnable and the speaker's own
        # power-on reconnect do the heavy lifting. This loop only nudges a plain
        # connect when we're disconnected, and otherwise just observes. No
        # audio-profile gate, no backoff state machine, no half-connected dance.
        powered_once = False
        last_attempt: Dict[str, float] = {}
        fail_wait: Dict[str, float] = {}
        was_connected = False  # detect not-connected -> connected transitions
        while not self._stop.is_set():
            mac = normalize_mac(self.get_mac())
            if self.manager.available and _MAC_RE.fullmatch(mac):
                try:
                    if not powered_once:
                        # Once only. Per-cycle `power on` toggled the adapter and
                        # made PipeWire re-register the A2DP endpoints every cycle
                        # (the "endpoint flap"). AutoEnable=true keeps it powered.
                        self.manager.power(True)
                        powered_once = True
                    device = self.manager.device(mac)
                    if not device:
                        self._skip(mac, "not paired yet")
                    elif not (device.get("paired") or device.get("bonded")):
                        self._skip(mac, "known but not paired; pair from the UI")
                    elif device.get("connected"):
                        if not was_connected:
                            was_connected = True
                            self._announce_connected()  # boot greeting / reconnect
                        last_attempt.pop(mac, None)  # connected; let BlueZ keep it
                    else:
                        was_connected = False
                        # Disconnected: one plain connect, with a LIGHT backoff so
                        # we don't hammer (and churn the A2DP endpoints on) an
                        # unreachable / asleep speaker. 15s normally; doubles up to
                        # 60s while it keeps failing; resets to 15s on success. This
                        # is a simple cadence backoff, NOT the old profile state
                        # machine. A host cannot wake an asleep BT speaker — it
                        # reconnects on its own power-up — so trying faster is futile
                        # and only churns endpoints.
                        now = time.monotonic()
                        wait = fail_wait.get(mac, 15.0)
                        if now - last_attempt.get(mac, 0.0) >= wait:
                            last_attempt[mac] = now
                            if not device.get("trusted"):
                                self.manager.trust(mac)
                            ok, msg = self.manager.connect(mac)
                            fail_wait[mac] = 15.0 if ok else min(wait * 2, 60.0)
                            log.info(
                                "autoconnect %s -> ok=%s msg=%s (next in %ss)",
                                mac, ok, msg, int(fail_wait[mac]),
                            )
                except Exception as exc:
                    log.debug("autoconnect error: %s", exc)
            self._stop.wait(self.interval)

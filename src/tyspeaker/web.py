"""Flask application: Ty Player, Bluetooth Manager and Config — fully open on
the LAN (no authentication, by design)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import List, Optional

from flask import Flask, jsonify, render_template, request, send_file

from . import config
from .bluetooth import BluetoothAutoConnector, BluetoothManager, normalize_mac
from .engine import PlaybackEngine
from .gps import GpsReader
from .gps_stats import GpsStats
from .inputs import start_inputs
from .library import Library
from .logsetup import configure_logging, get_logger, recent_logs
from .power import power_status
from .settings import RESTART_REQUIRED, Settings

configure_logging()
log = get_logger("web")

_BT_LOG_KEYWORDS = (
    "bluetooth",
    "bluetoothd",
    "bluez",
    "btmgmt",
    "btuart",
    "bcm",
    "bcm434",
    "brcm",
    "wireplumber",
    "pipewire",
    "a2dp",
    "avrcp",
    "rfcomm",
    "hci",
    "hci0",
    "hciconfig",
    "rfkill",
    "firmware",
    "link key",
    "endpoint",
    "transport",
    "profile",
    "connect",
    "disconnect",
    "sonix",
    "ns-cspbt03",
    "9d:31:da:ad:24:69",
    "08:eb:ed:0d:1d:bf",
    "tyspeaker.bluetooth",
)


def _run_if_present(command: str, args: List[str], timeout: float = 3.0) -> List[str]:
    if not shutil.which(command):
        return [f"{command}: not installed"]
    return _run_text([command, *args], timeout=timeout).splitlines()


def _run_text(args: List[str], timeout: float = 3.0) -> str:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "XDG_RUNTIME_DIR": "/run/user/1000"},
        )
        return (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{args[0]} failed: {exc}"


def _filtered_journal(minutes: int, limit: int) -> List[str]:
    since = f"-{max(1, min(30, minutes))} min"
    commands = [
        ["journalctl", "--no-pager", "-o", "short-iso", "--since", since],
        ["journalctl", "--user", "--no-pager", "-o", "short-iso", "--since", since],
        [
            "journalctl",
            "-k",
            "--no-pager",
            "-o",
            "short-iso",
            "--since",
            since,
        ],
        [
            "journalctl",
            "--no-pager",
            "-o",
            "short-iso",
            "--since",
            since,
            "-u",
            "bluetooth",
            "-u",
            "tyspeaker",
        ],
        [
            "journalctl",
            "--user",
            "--no-pager",
            "-o",
            "short-iso",
            "--since",
            since,
            "-u",
            "pipewire",
            "-u",
            "pipewire-pulse",
            "-u",
            "wireplumber",
            "-u",
            "tyspeaker-keepalive",
        ],
    ]
    lines: List[str] = []
    seen = set()
    for cmd in commands:
        raw = _run_text(cmd, timeout=4.0)
        for line in raw.splitlines():
            text = line.strip()
            if not text:
                continue
            lower = text.lower()
            if "no journal files were opened" in lower or "permission denied" in lower:
                keep = True
            else:
                keep = any(k in lower for k in _BT_LOG_KEYWORDS)
            if not keep or text in seen:
                continue
            seen.add(text)
            lines.append(text)
    return lines[-limit:]


def _snapshot_lines(mac: str) -> List[str]:
    lines = ["=== live snapshot ==="]
    lines.extend(_run_text(["bluetoothctl", "show"], timeout=2.5).splitlines()[:40])
    for group in ("Connected", "Paired", "Bonded", "Trusted"):
        lines.append(f"--- bluetoothctl devices {group} ---")
        lines.extend(_run_text(["bluetoothctl", "devices", group], timeout=2.5).splitlines()[:40])
    if mac:
        lines.append(f"--- bluetoothctl info {mac} ---")
        lines.extend(_run_text(["bluetoothctl", "info", mac], timeout=2.5).splitlines()[:60])
    lines.append("--- bluetooth driver state ---")
    lines.extend(_run_if_present("btmgmt", ["info"], timeout=3.0)[:80])
    lines.extend(_run_if_present("hciconfig", ["-a"], timeout=3.0)[:80])
    lines.extend(_run_if_present("rfkill", ["list", "bluetooth"], timeout=2.0)[:40])
    lines.append("--- kernel bluetooth modules ---")
    for line in _run_text(["lsmod"], timeout=2.0).splitlines():
        lower = line.lower()
        if any(k in lower for k in ("bluetooth", "btbcm", "hci", "rfkill", "brcm")):
            lines.append(line)
    lines.append("--- bluetooth service state ---")
    lines.extend(
        _run_text(
            [
                "systemctl",
                "--no-pager",
                "--plain",
                "status",
                "--lines=12",
                "bluetooth",
            ],
            timeout=3.0,
        ).splitlines()[:70]
    )
    lines.append("--- wpctl status ---")
    wp = _run_text(["wpctl", "status"], timeout=2.5)
    capture = False
    for line in wp.splitlines():
        if line.strip() == "Audio":
            capture = True
        if line.strip() == "Video":
            break
        if capture:
            lines.append(line)
    lines.append("--- user audio service state ---")
    lines.extend(
        _run_text(
            [
                "systemctl",
                "--user",
                "--no-pager",
                "--plain",
                "status",
                "--lines=8",
                "pipewire",
                "pipewire-pulse",
                "wireplumber",
                "tyspeaker-keepalive",
            ],
            timeout=3.0,
        ).splitlines()[:90]
    )
    lines.append("--- audio process list ---")
    lines.extend(
        _run_text(
            ["ps", "-C", "bluetoothd,wireplumber,pipewire", "-o", "pid,etime,cmd"],
            timeout=2.5,
        ).splitlines()[:40]
    )
    if shutil.which("pactl"):
        lines.append("--- pactl cards / sinks ---")
        lines.extend(_run_text(["pactl", "list", "cards", "short"], timeout=2.5).splitlines()[:40])
        lines.extend(_run_text(["pactl", "list", "sinks", "short"], timeout=2.5).splitlines()[:40])
    return [line for line in lines if line.strip()]


def _input_snapshot_lines(inputs) -> List[str]:
    lines = ["=== physical controls ==="]
    if inputs is None or not hasattr(inputs, "diagnostics"):
        return lines + ["inputs object is not available"]
    try:
        diag = inputs.diagnostics()
    except Exception as exc:
        return lines + [f"input diagnostics failed: {exc}"]

    button = diag.get("button", {})
    encoder = diag.get("encoder", {})
    btn_counts = button.get("counts", {})
    enc_counts = encoder.get("counts", {})
    lines.extend(
        [
            f"inputs_active={diag.get('active')} gpio_available={diag.get('gpio_available')} event_seq={diag.get('event_seq')}",
            (
                "button: "
                f"pin=GPIO{button.get('pin')} source={button.get('source')} "
                f"active={button.get('active')} pressed={button.get('pressed')} "
                f"bounce_ms={button.get('bounce_ms')} last={button.get('last_event')}"
            ),
            (
                "button counts: "
                f"pressed={btn_counts.get('pressed', 0)} released={btn_counts.get('released', 0)} "
                f"short={btn_counts.get('short', 0)} double={btn_counts.get('double', 0)} held={btn_counts.get('held', 0)}"
            ),
            (
                "encoder: "
                f"A=GPIO{encoder.get('clk')} B=GPIO{encoder.get('dt')} "
                f"active={encoder.get('active')} steps={encoder.get('steps')} "
                f"bounce_ms={encoder.get('bounce_ms')} last={encoder.get('last_direction')}"
            ),
            (
                "encoder counts: "
                f"clockwise={enc_counts.get('clockwise', 0)} "
                f"counter_clockwise={enc_counts.get('counter_clockwise', 0)}"
            ),
        ]
    )
    events = diag.get("events", [])
    if events:
        lines.append("--- recent physical input events ---")
        for event in events[-20:]:
            stamp = event.get("time")
            when = f"{float(stamp):.3f}" if isinstance(stamp, (int, float)) else "--"
            lines.append(f"#{event.get('seq')} {when} {event.get('message')}")
    errors = diag.get("errors", [])
    if errors:
        lines.append("--- physical input errors ---")
        lines.extend(str(e) for e in errors[-10:])
    return lines


def create_app(
    library: Optional[Library] = None,
    settings: Optional[Settings] = None,
    engine: Optional[PlaybackEngine] = None,
    bluetooth: Optional[BluetoothManager] = None,
    enable_inputs: bool = True,
    enable_bt_autoconnect: bool = True,
) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_BYTES

    cfg = settings or Settings()
    lib = library or Library(config.SAMPLES_DIR, set(config.ALLOWED_EXTENSIONS))
    eng = engine or PlaybackEngine(lib, cfg)
    bt = bluetooth or BluetoothManager()

    def _bt_greeting() -> None:
        """Play a random 'personality' clip shortly after the speaker connects
        (covers boot + every reconnect). Delayed + threaded so the A2DP transport
        has settled and the autoconnect loop is never blocked."""
        import threading
        import time

        def _later() -> None:
            time.sleep(1.5)
            try:
                eng.play_random()
            except Exception:
                pass

        threading.Thread(target=_later, daemon=True).start()

    bt_auto = (
        BluetoothAutoConnector(
            bt,
            lambda: str(cfg.get("bt_autoconnect_mac", "")),
            on_connect=_bt_greeting,
        ).start()
        if enable_bt_autoconnect
        else None
    )
    inputs = start_inputs(cfg, eng) if enable_inputs else None
    gps = (
        GpsReader(
            config.GPS_PORT, config.GPS_BAUD,
            config.GPS_LOG_DIR, config.GPS_LOG_INTERVAL,
            min_log_mph=float(cfg.get("gps_log_min_mph", 3.0)),
        ).start()
        if config.GPS_PORT
        else None
    )
    gps_stats = GpsStats(config.GPS_LOG_DIR)
    app.config["TYSPEAKER_INPUTS"] = inputs  # keep refs alive
    app.config["TYSPEAKER_BT_AUTOCONNECT"] = bt_auto
    app.config["TYSPEAKER_GPS"] = gps

    def _body() -> dict:
        return request.get_json(silent=True) or request.form.to_dict()

    @app.after_request
    def no_cache(response):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    # ===================================================================
    # Pages
    # ===================================================================
    @app.get("/")
    def index():
        return render_template(
            "index.html",
            inputs_active=bool(inputs and inputs.active),
            bt_available=bt.available,
        )

    # ===================================================================
    # Tab 1 — Ty Player
    # ===================================================================
    @app.get("/api/playlist")
    def api_playlist():
        return jsonify(entries=eng.playlist.entries(), power=power_status(), **eng.status())

    @app.post("/api/upload")
    def api_upload():
        files = request.files.getlist("file")
        if not files:
            return jsonify(error="No file provided"), 400
        saved, errors = [], []
        for f in files:
            try:
                entry = lib.save_upload(f, "")
                eng.playlist.add(entry.rel_path)
                saved.append(entry.__dict__)
            except ValueError as exc:
                errors.append({"name": f.filename, "error": str(exc)})
        return jsonify(saved=saved, errors=errors), (201 if saved else 400)

    @app.post("/api/playlist/reorder")
    def api_reorder():
        order = _body().get("order") or []
        return jsonify(order=eng.playlist.reorder(list(order)))

    @app.post("/api/playlist/remove")
    def api_remove():
        data = _body()
        rel = data.get("path", "")
        eng.playlist.remove(rel)
        if data.get("delete"):
            try:
                lib.delete(rel)
            except (FileNotFoundError, ValueError):
                pass
        return jsonify(ok=True)

    @app.post("/api/play")
    def api_play():
        data = _body()
        try:
            if "index" in data and data.get("index") not in (None, ""):
                log.info("API play index=%s", data["index"])
                eng.play_index(int(data["index"]))
            else:
                log.info("API play path=%s", data.get("path", ""))
                eng.play_rel(data.get("path", ""))
        except (FileNotFoundError, ValueError, IndexError) as exc:
            log.warning("play failed: %s", exc)
            return jsonify(error=str(exc)), 400
        except RuntimeError as exc:
            log.error("play unavailable: %s", exc)
            return jsonify(error=str(exc)), 503
        return jsonify(ok=True, **eng.status())

    @app.post("/api/stop")
    def api_stop():
        eng.stop()
        return jsonify(ok=True)

    @app.post("/api/next")
    def api_next():
        try:
            played = eng.next()
        except RuntimeError as exc:
            return jsonify(error=str(exc)), 503
        return jsonify(ok=True, played=played, **eng.status())

    @app.post("/api/mode")
    def api_mode():
        return jsonify(mode=eng.set_mode(_body().get("mode", "sequential")))

    @app.post("/api/volume")
    def api_volume():
        data = _body()
        if "delta" in data:
            level = eng.volume_step(int(data["delta"]))
        else:
            level = eng.set_volume(int(data.get("level", 70)))
        return jsonify(volume=level)

    @app.get("/api/status")
    def api_status():
        return jsonify(power=power_status(), **eng.status())

    @app.get("/api/power")
    def api_power():
        return jsonify(power_status())

    @app.get("/api/gps")
    def api_gps():
        if gps is not None:
            return jsonify(gps.status())
        return jsonify(available=False, receiving=False, port=config.GPS_PORT)

    @app.post("/api/gps/config")
    def api_gps_config():
        try:
            mph = max(0.0, min(50.0, float(_body().get("min_mph"))))
        except (TypeError, ValueError):
            return jsonify(error="min_mph must be a number"), 400
        cfg.update({"gps_log_min_mph": mph})
        if gps is not None:
            gps.min_log_mph = mph
        return jsonify(ok=True, min_log_mph=mph)

    @app.get("/api/gps/days")
    def api_gps_days():
        return jsonify(gps_stats.list_days())

    @app.get("/api/gps/day/<date>")
    def api_gps_day(date):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
            return jsonify(error="bad date"), 400
        return jsonify(gps_stats.compute_day(date))

    @app.get("/api/diagnostics/inputs")
    def api_input_diagnostics():
        if inputs is not None and hasattr(inputs, "diagnostics"):
            return jsonify(inputs.diagnostics())
        return jsonify(
            active=False,
            gpio_available=False,
            event_seq=0,
            encoder={
                "configured": False,
                "active": False,
                "clk": -1,
                "dt": -1,
                "steps": None,
                "bounce_ms": 0,
                "counts": {"clockwise": 0, "counter_clockwise": 0},
                "last_direction": "",
            },
            button={
                "configured": False,
                "active": False,
                "pin": -1,
                "source": "off",
                "pressed": None,
                "bounce_ms": 0,
                "counts": {
                    "pressed": 0,
                    "released": 0,
                    "held": 0,
                    "short": 0,
                    "double": 0,
                },
                "last_event": "",
            },
            events=[],
            errors=[],
        )

    @app.get("/api/logs")
    def api_logs():
        return jsonify(lines=recent_logs(int(request.args.get("limit", 200))))

    @app.post("/api/client-log")
    def api_client_log():
        data = _body()
        message = str(data.get("message", ""))[:240]
        detail = str(data.get("detail", ""))[:500]
        if message:
            log.info("UI %s%s", message, f" | {detail}" if detail else "")
        return jsonify(ok=True)

    @app.post("/api/diagnostics/beep")
    def api_diag_beep():
        piezo = getattr(inputs, "piezo", None) if inputs is not None else None
        if piezo is None:
            return jsonify(ok=False, error="piezo not configured"), 400
        wired = getattr(piezo, "_pwm", None) is not None
        freq = _body().get("freq")
        if freq:
            try:
                piezo.tone(float(freq), 250)  # single beep at the chosen pitch
                return jsonify(ok=True, wired=wired, freq=float(freq))
            except (TypeError, ValueError):
                pass
        # default: two short beeps at the resonant pitch = recognizable test cue
        piezo.play_pattern([(120, 90), (120, 0)])
        return jsonify(ok=True, wired=wired)

    @app.get("/api/download")
    def api_download():
        try:
            return send_file(lib.path_for(request.args.get("path", "")))
        except (FileNotFoundError, ValueError) as exc:
            return jsonify(error=str(exc)), 404

    # ===================================================================
    # Tab 2 — Bluetooth Manager
    # ===================================================================
    @app.get("/api/bt")
    def api_bt():
        if not bt.available:
            return jsonify(available=False, devices=[])
        return jsonify(
            **bt.status(),
            devices=bt.devices(),
            autoconnect_mac=cfg.get("bt_autoconnect_mac", ""),
        )

    @app.get("/api/bt/connected")
    def api_bt_connected():
        """Fast, pollable realtime status for the live UI pill. Checks the
        configured speaker specifically (reliable), else any connected device."""
        if not bt.available:
            return jsonify(available=False, connected=False, name=None, mac=None)
        mac = normalize_mac(str(cfg.get("bt_autoconnect_mac", "")))
        if mac:
            dev = bt.device(mac)
            return jsonify(
                available=True,
                connected=bool(dev and dev["connected"]),
                name=(dev or {}).get("name") or mac,
                mac=mac,
            )
        dev = bt.first_connected()
        return jsonify(
            available=True,
            connected=bool(dev),
            name=(dev or {}).get("name"),
            mac=(dev or {}).get("mac"),
        )

    @app.get("/api/bt/logs")
    def api_bt_logs():
        """Realtime Bluetooth diagnostics for the browser log window."""
        minutes = max(1, min(30, int(request.args.get("minutes", 10))))
        limit = max(80, min(600, int(request.args.get("limit", 450))))
        mac = normalize_mac(str(request.args.get("mac", "")))
        if not mac:
            mac = normalize_mac(str(cfg.get("bt_autoconnect_mac", "")))

        lines = _snapshot_lines(mac)
        lines.append("")
        lines.extend(_input_snapshot_lines(inputs))
        lines.append("")
        lines.append(f"=== filtered bluetooth journal: last {minutes} min ===")
        lines.extend(_filtered_journal(minutes, limit))
        lines.append("")
        lines.append("=== TySpeaker app memory log ===")
        lines.extend(recent_logs(160))
        return jsonify(lines=lines[-(limit + 180) :], minutes=minutes, limit=limit, mac=mac)

    @app.post("/api/bt/power")
    def api_bt_power():
        on = bool(_body().get("on", True))
        log.info("BT API power requested on=%s", on)
        ok = bt.power(on)
        log.info("BT API power result on=%s ok=%s", on, ok)
        return jsonify(ok=ok)

    @app.post("/api/bt/scan")
    def api_bt_scan():
        secs = int(_body().get("seconds", 8))
        log.info("BT API scan start seconds=%s", secs)
        devices = bt.scan(secs)
        log.info(
            "BT API scan done devices=%s names=%s",
            len(devices),
            ", ".join(f"{d.get('name')} {d.get('mac')}" for d in devices[:8]) or "none",
        )
        return jsonify(
            **bt.status(),
            devices=devices,
            autoconnect_mac=cfg.get("bt_autoconnect_mac", ""),
        )

    def _bt_action(fn):
        mac = _body().get("mac", "")
        log.info("BT API %s start mac=%s", fn.__name__, mac)
        ok, msg = fn(mac)
        log.info("BT %s %s -> ok=%s msg=%s", fn.__name__, mac, ok, msg)
        return jsonify(ok=ok, message=msg), (200 if ok else 400)

    @app.post("/api/bt/pair")
    def api_bt_pair():
        mac = _body().get("mac", "")
        log.info("BT API pair start mac=%s", mac)
        dev = bt.device(mac)
        log.info("BT API pair precheck mac=%s dev=%s", mac, dev)
        if dev and dev.get("connected"):
            bt.trust(mac)
            cfg.update({"bt_autoconnect_mac": normalize_mac(mac)})
            log.info("BT API pair skipped because already connected mac=%s", mac)
            return jsonify(ok=True, message="Speaker ready")
        ok, msg = bt.pair(mac)
        if ok:
            bt.trust(mac)
            dev = bt.device(mac)
            log.info("BT API pair postcheck mac=%s dev=%s", mac, dev)
            if dev and dev.get("connected"):
                cfg.update({"bt_autoconnect_mac": normalize_mac(mac)})
        elif "authenticationfailed" in msg.replace(".", "").lower():
            msg = "Speaker did not accept pairing. Hold its button until it blinks, then tap Find speaker."
        log.info("BT pair %s -> ok=%s msg=%s", mac, ok, msg)
        return jsonify(ok=ok, message=msg), (200 if ok else 400)

    @app.post("/api/bt/connect")
    def api_bt_connect():
        return _bt_action(bt.connect)

    @app.post("/api/bt/setup")
    def api_bt_setup():
        data = _body()
        mac = normalize_mac(
            data.get("mac", "") or str(cfg.get("bt_autoconnect_mac", ""))
        )
        log.info("BT API setup start mac=%s body=%s", mac, data)
        if not mac:
            log.info("BT API setup failed: no mac")
            return jsonify(ok=False, message="Find the speaker first"), 400
        dev = bt.device(mac)
        log.info("BT API setup device before pair/connect mac=%s dev=%s", mac, dev)
        if not dev:
            log.info("BT API setup failed: device unavailable mac=%s", mac)
            return (
                jsonify(
                    ok=False,
                    message="Keep the speaker blinking, then tap Find speaker again",
                ),
                400,
            )
        if not dev.get("paired"):
            ok, msg = bt.pair(mac)
            log.info("BT API setup pair result mac=%s ok=%s msg=%s", mac, ok, msg)
            if not ok:
                return jsonify(ok=False, message=msg), 400
        ok, msg = bt.trust(mac)
        log.info("BT API setup trust result mac=%s ok=%s msg=%s", mac, ok, msg)
        if not ok:
            return jsonify(ok=False, message=msg), 400
        ok, msg = bt.connect(mac)
        log.info("BT API setup connect result mac=%s ok=%s msg=%s", mac, ok, msg)
        if not ok:
            return jsonify(ok=False, message=msg), 400
        cfg.update({"bt_autoconnect_mac": mac})
        log.info("BT API setup ready mac=%s", mac)
        return jsonify(ok=True, mac=mac, message="Speaker ready")

    @app.post("/api/bt/disconnect")
    def api_bt_disconnect():
        return _bt_action(bt.disconnect)

    @app.post("/api/bt/trust")
    def api_bt_trust():
        return _bt_action(bt.trust)

    @app.post("/api/bt/remove")
    def api_bt_remove():
        mac = normalize_mac(_body().get("mac", ""))
        log.info("BT API remove start mac=%s", mac)
        ok, msg = bt.remove(mac)
        if ok and normalize_mac(str(cfg.get("bt_autoconnect_mac", ""))) == mac:
            cfg.update({"bt_autoconnect_mac": ""})
        log.info("BT remove %s -> ok=%s msg=%s", mac, ok, msg)
        return jsonify(ok=ok, message=msg), (200 if ok else 400)

    @app.post("/api/bt/autoconnect")
    def api_bt_autoconnect():
        mac = normalize_mac(_body().get("mac", ""))
        log.info("BT API autoconnect start mac=%s", mac)
        if not mac:
            cfg.update({"bt_autoconnect_mac": ""})
            log.info("BT API autoconnect disabled")
            return jsonify(ok=True, mac="")
        dev = bt.device(mac)
        log.info("BT API autoconnect precheck mac=%s dev=%s", mac, dev)
        if not dev:
            return jsonify(ok=False, mac=mac, message="Pair the speaker first"), 400
        if not dev.get("paired"):
            return jsonify(ok=False, mac=mac, message="Pair the speaker first"), 400
        ok, msg = bt.trust(mac)
        if ok:
            cfg.update({"bt_autoconnect_mac": mac})
            if dev.get("connected"):
                log.info(
                    "BT API autoconnect saved without reconnect mac=%s: already connected",
                    mac,
                )
            else:
                ok, msg = bt.connect(mac)
        log.info("BT API autoconnect result mac=%s ok=%s msg=%s", mac, ok, msg)
        return jsonify(ok=ok, mac=mac, message=msg), (200 if ok else 400)

    @app.get("/api/bt/sinks")
    def api_bt_sinks():
        return jsonify(sinks=eng.mixer.list_sinks(), backend=eng.mixer.backend)

    @app.post("/api/bt/sink")
    def api_bt_sink():
        sink = _body().get("sink", "")
        ok = eng.mixer.set_default_sink(sink)
        if ok:
            cfg.update({"default_sink": sink})
        return jsonify(ok=ok)

    # ===================================================================
    # Tab 3 — Config
    # ===================================================================
    @app.get("/api/settings")
    def api_settings_get():
        return jsonify(
            settings=cfg.as_dict(),
            restart_required_keys=sorted(RESTART_REQUIRED),
            mixer_backend=eng.mixer.backend,
            bt_available=bt.available,
            inputs_active=bool(inputs and inputs.active),
        )

    @app.post("/api/settings")
    def api_settings_set():
        changes = _body()
        # apply live settings immediately where it makes sense
        if "volume" in changes:
            eng.set_volume(int(changes["volume"]))
        if "mode" in changes:
            eng.set_mode(str(changes["mode"]))
        restart = cfg.update(changes)
        return jsonify(ok=True, restart_required=restart, settings=cfg.as_dict())

    # ===================================================================
    # System — reboot (for applying GPIO/wire changes from the UI)
    # ===================================================================
    @app.post("/api/reboot")
    def api_reboot():
        import subprocess
        import threading
        import time

        def _do() -> None:
            time.sleep(1)  # let the HTTP 200 flush to the browser first
            # Requires a sudoers drop-in granting NOPASSWD for this exact cmd:
            #   jaysettle ALL=(root) NOPASSWD: /usr/bin/systemctl reboot
            subprocess.run(
                ["sudo", "-n", "/usr/bin/systemctl", "reboot"], check=False
            )

        threading.Thread(target=_do, daemon=True).start()
        return jsonify(ok=True, rebooting=True)

    return app

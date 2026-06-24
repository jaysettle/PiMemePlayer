# PiMemePlayer (TySpeaker)

A push-button **meme / sound-board player** for a **Raspberry Pi Zero 2 W**, with a
**LAN web UI** for uploading and organizing clips and a **rotary encoder + button**
for hands-free play. Audio streams to a **Bluetooth speaker** over A2DP. Built to
ride on a kid's scooter — so it boots and plays **with no network**.

Open on the LAN, no login. Three tabs:

1. **Ty Player** — drag-drop clips (mp3/wav/ogg/flac/m4a/aac/opus), reorder, play /
   stop / next, **🔀 Shuffle** on/off, volume. Stored on the SD card.
2. **Bluetooth** — scan / pair / connect / trust / auto-connect a speaker; live
   connection status and logs.
3. **Settings** — GPIO pins (encoder CLK/DT/SW + piezo), click timings, knob role,
   player command, diagnostics (test beep, logs).

## Physical controls (rotary encoder + piezo)

Knob role **select** (default):
- **tap** → replay the current clip
- **hold** → next clip (follows Shuffle: random when on, in-order when off)
- **double-tap** → surprise (random clip)
- **turn** → browse + audition clips

Knob role **volume**:
- **turn** → volume, 0–10 in 10% steps. The **piezo pitch maps to the level**
  (low pitch = quiet → high pitch = loud); at min/max the pitch holds at the
  boundary tone as an "end of range" cue.

A **random clip plays automatically on boot and on every Bluetooth (re)connect** —
the scooter "greets" you with a meme. The piezo is a *passive* buzzer driven by PWM
at its resonant frequency (see `inputs.py`).

## Audio: bluez-alsa (not PipeWire)

Bluetooth A2DP runs through **bluez-alsa**, not PipeWire/WirePlumber. On Pi OS
Bookworm the PipeWire A2DP path was unstable across multiple speakers (connect →
drop in ~20s, `bluetoothd` crashes — upstream race/crash bluez/bluez #1545/#1951).
bluez-alsa removes PipeWire from the BT path entirely. **Full setup + rationale:
[`deploy/bluez-alsa/README.md`](deploy/bluez-alsa/README.md).**

- Player: `mpg123 -o alsa -a bluealsa:DEV=<mac>,PROFILE=a2dp` (the app injects the
  connected speaker's MAC).
- Daemon: `bluealsa --keep-alive=600 -p a2dp-source` (keep-alive stops the speaker
  idle-sleeping between clips).
- Volume: the speaker's own AVRCP control via `amixer -D bluealsa`.

## Project layout

```
src/tyspeaker/      config, audio, mixer, library, playlist, engine,
                    bluetooth, inputs, settings, power, web
  templates/        single-page 3-tab UI
  static/           logo, pinout (add your own gallery images)
deploy/             systemd units, bluez-alsa setup, wireplumber (legacy)
```

## Develop (any machine)

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m tyspeaker             # serves http://localhost:8000
```

Off-Pi, GPIO is a no-op and audio uses any installed player (`mpv`, `ffmpeg`,
`vlc`, or `mpg123`).

## Deploy to the Pi

```bash
sudo apt update && sudo apt install -y python3-venv mpg123 bluez-alsa-utils
mkdir -p ~/tyspeaker-app && cd ~/tyspeaker-app   # copy this repo here (scp/git)
python3 -m venv --system-site-packages .venv
. .venv/bin/activate
pip install -e ".[pi]"          # flask, waitress, gpiozero, lgpio

# Bluetooth audio (bluez-alsa) — see deploy/bluez-alsa/README.md:
sudo install -D -m644 deploy/systemd/system/bluealsa.service.d/override.conf \
    /etc/systemd/system/bluealsa.service.d/override.conf
systemctl --user mask pipewire pipewire-pulse wireplumber
sudo systemctl daemon-reload && sudo systemctl restart bluetooth bluealsa

# Service + offline boot (no stall when away from home WiFi):
sudo cp deploy/tyspeaker.service /etc/systemd/system/
sudo install -D -m644 deploy/systemd/system/tyspeaker.service.d/offline.conf \
    /etc/systemd/system/tyspeaker.service.d/offline.conf
sudo systemctl disable NetworkManager-wait-online.service
sudo systemctl enable --now tyspeaker
```

Open `http://<pi-ip>:8000`, then in **Settings** set `player_cmd` to
`mpg123 -q -o alsa -a bluealsa:DEV={btmac},PROFILE=a2dp` and pair a speaker.

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `TYSPEAKER_DATA` | `~/tyspeaker` | base dir for samples + state |
| `TYSPEAKER_PORT` | `8000` | web server port |
| `TYSPEAKER_HOST` | `0.0.0.0` | bind address |
| `TYSPEAKER_PLAYER` | auto | explicit player cmd (`{path}`, `{btmac}` placeholders) |
| `TYSPEAKER_MAX_UPLOAD` | `52428800` | max upload size (bytes) |

GPIO pins, click timings, volume and the player command are also editable live in
the **Settings** tab and persisted to `~/tyspeaker/settings.json`.

# PiMemePlayer (TySpeaker)

A push-button **meme / sound-board player** for a **Raspberry Pi Zero 2 W**, built to
ride on a kid's scooter. A **rotary encoder + button** plays clips hands-free to a
**Bluetooth speaker** (A2DP), a **LAN web UI** manages everything, and it boots and
plays **with no network**. Beyond the soundboard it also does **GPS ride tracking**, a
**two-piezo harmony engine / beep playground**, a fully **reassignable encoder**, and
an on-demand **Wi-Fi hotspot** so a phone can connect mid-ride.

Open on the LAN, no login. Four tabs:

1. **Ty Player** — drag-drop clips (mp3/wav/ogg/flac/m4a/aac/opus), reorder, play /
   stop / next, **🔀 Shuffle**, volume. Stored on the SD card.
2. **Bluetooth** — scan / pair / connect / trust / auto-connect a speaker; live status.
3. **GPS** — per-ride tracks on a map (rides auto-split per day), miles / time / top
   speed / odometer, a day & month browser, and a logging override.
4. **Settings** — GPIO pins, click timings, **button actions**, the **🎵 beep
   playground** (two-piezo harmonies + volume), diagnostics, GPS controls, and hotspot.

## Physical controls (rotary encoder + 2 piezos)

The encoder button has five gestures, each with a **reassignable action** *and* an
assignable **piezo sound** (both set in Settings). Defaults:

| Gesture | Default action |
|---|---|
| **tap** | play current clip |
| **double-tap** | random clip |
| **hold** | next clip (random if Shuffle) |
| **triple-tap** | start GPS logging |
| **quad-tap** | toggle the Wi-Fi hotspot |
| **turn** | browse clips, or volume (knob role) — piezo pitch maps to the level |

**17 actions** are assignable per gesture (play / random / next / prev, speaker &
piezo volume ±, shuffle, GPS start/stop/auto, hotspot, droid, reboot, none). A
**random clip greets you on boot and every BT reconnect**. The piezos are *passive*
buzzers on hardware PWM — **two of them**, enabling two-voice harmony.

## GPS ride tracking

A serial GPS (u-blox-7 @9600 on `/dev/ttyS0`) logs rides to the SD card. Logging is
**network-driven**: it records whenever the Pi has been off the home Wi-Fi subnet for
>10s (out riding, incl. on the hotspot) and stops after >20s back home; a **Force ON /
OFF / Auto** override + a triple-tap force it on. A day **auto-splits into separate
rides** (gap-based), each its own track with per-ride miles / time / top speed + a
lifetime odometer. An always-on **flight recorder** logs fix quality every 30s, and the
clock is set from GPS when offline (no NTP).

## Beep playground + two-piezo harmony

**Settings → 🎵 Beep playground**: ~250 tunes/effects — R2-D2 chirps, **Game / Arcade /
Sci-Fi SFX**, retro-game & movie/TV **RTTTL** melodies, cartoons, 80s. With a second
piezo wired (GPIO18), any tune plays as a **two-voice duet**: melodies harmonize in
**diatonic thirds** (key auto-detected via Krumhansl-Schmuckler), effects in parallel
fifths, with a **10-interval picker** (3rd/6th/10th, 4th/5th/octave, above or below).
Piezo loudness is a duty-cycle **volume slider**, and any sound can be **bound to a
button gesture**.

## Wi-Fi hotspot (on-ride access)

**Quad-tap** (or the GPS tab) toggles the Pi into an AP (`tyspeaker`) so a phone can
join and reach the UI at `http://10.42.0.1:8000` mid-ride. It's a NetworkManager
profile with `autoconnect=no`, so a reboot always reverts to home Wi-Fi.

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

## Hardware (pins, piezos, battery)

- **2 passive piezos** on hardware PWM (`dtoverlay=pwm-2chan`): #1 = **GPIO19** (pin 35),
  #2 = **GPIO18** (pin 12), each with a GND. Just signal + GND (passive, 2-wire).
- **Encoder**: A=GPIO5, B=GPIO6, push=GPIO13. **GPS**: VCC pin 1, GND pin 6, TX→pin 10
  (GPIO15). **I²C** enabled on GPIO2/3 for a future ADS1115 (`dtparam=i2c_arm=on`).
- **PiSugar S 1200 mAh UPS**: ~**2h47m** runtime, but **no software battery readout or
  low-battery warning** — it's *not* a PiSugar 2 inside (no SDA pogo; "does not support
  I2C/power inquiry"); it regulates 5 V then dies instantly, so even `vcgencmd`
  under-voltage never trips. An external **ADS1115** on I²C is the only path to a %.
  ⚠️ The S uses GPIO3 (SCL) for auto-boot — disable that switch before using I²C.
- ⚠️ The Zero 2 W shares **one 2.4 GHz antenna** for Wi-Fi + BT; heavy BT or a failing
  speaker autoconnect can cause occasional Wi-Fi timeouts.

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

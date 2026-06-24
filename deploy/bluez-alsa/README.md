# Bluetooth audio: bluez-alsa (NOT PipeWire)

TySpeaker streams A2DP to the speaker via **bluez-alsa (bluealsa)**, not PipeWire.

## Why

The PipeWire/WirePlumber A2DP path on this Pi (Raspberry Pi OS Bookworm, BlueZ
5.66, WirePlumber 0.4.13) was fundamentally unstable across **three different
speakers** (Insignia, Sonix, Altec HydraMini) with the identical signature:
connect succeeds, then drops within ~20s, then `br-connection-profile-unavailable`.
Root cause is a documented upstream PipeWire↔BlueZ race/crash, present on old
*and* current versions:

- **bluez/bluez #1545** — A2DP disconnect-after-connect race (WirePlumber
  MediaTransport `Release` / endpoint registration timing).
- **bluez/bluez #1951** — `bluetoothd` SIGSEGV in `btd_adv_monitor_power_down()`
  when WirePlumber registers its ~19 A2DP media endpoints within ~10s of a
  connect, or when audio plays before connect.
- **bluez/bluez #871** — stale A2DP `LastUsed` SEP cache (`Unable to load
  LastUsed` → `double free`).

bluez-alsa removes PipeWire from the BT path entirely (BlueZ → ALSA PCM
directly), so none of those failure modes can occur. Validated: 4-minute idle
hold with zero drops, no `bluetoothd` crash, audio + volume working, survives a
reboot and auto-reconnects.

## Reproduce on a fresh flash

```bash
sudo apt install -y bluez-alsa-utils

# Pi is the A2DP source; keep the transport alive so the speaker doesn't sleep:
sudo install -D -m644 deploy/systemd/system/bluealsa.service.d/override.conf \
    /etc/systemd/system/bluealsa.service.d/override.conf
sudo systemctl daemon-reload
sudo systemctl disable --now bluealsa-aplay.service   # a2dp-sink autoplay, unused

# Take PipeWire out of the BT path (it fights bluealsa for the A2DP endpoint):
systemctl --user stop  pipewire pipewire-pulse wireplumber
systemctl --user mask  pipewire pipewire-pulse wireplumber
systemctl --user disable tyspeaker-keepalive   # PipeWire-era silence keepalive, obsolete

sudo systemctl restart bluetooth bluealsa
```

## App configuration

- `settings.json` → `player_cmd`:
  `mpg123 -q -o alsa -a bluealsa:DEV={btmac},PROFILE=a2dp`
  (`{btmac}` is substituted with `bt_autoconnect_mac` by `audio.py` / `engine.py`).
- Volume: `mixer.py` prefers the `bluealsa` backend → `amixer -D bluealsa`
  (the speaker's own AVRCP control, e.g. `'AL HydraMini - A2DP'`).
- Pairing / bond / connect: unchanged — `bluetooth.py` still uses
  `bluetoothctl connect` and BlueZ `AutoEnable` for bond-based reconnect.

## Verify

```bash
systemctl is-active bluealsa                 # active
systemctl --user is-active pipewire wireplumber  # inactive (masked)
bluealsa-aplay -L | grep <speaker-mac>       # speaker PCM present when connected
amixer -D bluealsa scontrols                 # '<name> - A2DP' control present
```

## Obsolete (PipeWire-era) assets — kept for reference, not used

- `deploy/wireplumber/80-a2dp-only.lua`
- the `tyspeaker-keepalive` user service (silence keepalive)
- the `tyspeaker.service` PipeWire/Pulse env drop-in (`audio.conf`)

`/etc/bluetooth/main.conf [General] Cache = no` and the immutable SEP cache
(`chattr +i /var/lib/bluetooth/*/cache`) remain as harmless belt-and-suspenders.

# TySpeaker / PiMemePlayer — Feature Backlog

Planned features and their design notes. (No secrets here — Wi-Fi/hotspot
passwords live in settings/NetworkManager on the Pi, never in the repo.)

---

## 1. Battery voltage monitoring (PiSugar S → external ADC)

**Why:** The PiSugar **S exposes no battery data to software** (per PiSugar docs —
no I²C fuel gauge; pisugar-server does nothing on the S). We want to benchmark
runtime, watch the battery drain in the flight recorder, and find resources to cut.

**Hardware (ordered — Comidox ADS1115, Amazon B07KW2QZS2):**
- ADS1115 (16-bit I²C ADC) powered at **3.3 V**.
- 2:1 voltage divider off the PiSugar **`BAT` pad** (3.0–4.2 V) into **A0**:
  `BAT → 100kΩ → A0 → 100kΩ → GND`. `V_batt = A0 × 2`.
- Wiring: `V→3V3 (pin1)`, `G→GND (pin6, common w/ PiSugar)`, `SCL→GPIO3 (pin5)`,
  `SDA→GPIO2 (pin3)`, `ADDR→open (=0x48)`. (3.3 V keeps the board's onboard I²C
  pull-ups off the 3.3 V-only GPIO — don't run it at 5 V.)

**Software (to build when the chip is wired):**
- Enable I²C; confirm `0x48` via `i2cdetect`.
- Add an ADS1115 reader to `power.py` (best-effort, no-op if the chip is absent):
  read A0, ×2 → volts, map to a rough Li-ion %.
- Feed into the **flight recorder** `battery` field + the GPS/UI so we can graph
  drain and catch the Pi dying.
- Then: a **battery-saving pass** — benchmark, then cut resource hogs (GPS/diag
  cadence, Wi-Fi when idle, etc.).

**Status:** spec'd; waiting on hardware.

---

## 2. Network-state GPS logging (log when AWAY from home Wi-Fi)

**Why:** Cleaner than the speed/quality gate for "is he out riding?" — at home the
GPS jitters worst (indoors); away, he's outside with a good fix.

**Model:**
- "Home" = the Pi's `wlan0` has an IP on the **home subnet (192.168.3.0/24)**
  (configurable prefix). Otherwise → "away".
- **Away → GPS logging armed; Home → off.** A good-fix quality filter (sats/HDOP)
  still applies so we never log no-fix garbage. (Open question: keep the speed gate?)
- Works with the hotspot: in AP mode `wlan0` ≠ 192.168.3.x → correctly "away".

**Status:** designing (see open questions).

---

## 3. Auto-hotspot (Wi-Fi AP fallback for on-the-ride access)

**Why:** Connect a phone to the Pi during the ride to see live GPS / control it.

**Model:**
- Prefer the home Wi-Fi (client) when in range. When it's not, **fall back to AP
  mode** broadcasting a hotspot (SSID/password configured on the Pi, NOT in repo).
- Revert to client automatically when the home network returns.
- Note: in AP mode there's no internet, so map *tiles* won't load on the phone
  mid-ride (the track + metrics still work; review the map at home).

**Risk:** ⚠️ Misconfiguring Wi-Fi/AP can **lock the Pi off the network** (headless).
Mitigation: NetworkManager-based switching, keep the **USB-serial rescue console**
working, test with a revert plan.

**Status:** needs go-ahead + credentials (see open questions).

---

## 4. GPS-tab logging override

**Why:** Manually force logging on/off regardless of network state.

**Model:** GPS tab buttons — **Force ON / Force OFF / Auto** (network-driven).
The override **auto-resets to Auto when the home network reconnects** (so it can't
be left stuck on after a ride).

**Status:** designing (see open questions).

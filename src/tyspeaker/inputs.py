"""Physical controls: rotary encoder + push button + piezo feedback.

Wires gpiozero devices to engine actions. Everything is optional and guarded:
on a PC (no gpiozero / no pins configured) this is a no-op so the app still runs.

Default mapping:
  - rotate          -> cycle the selected sample (or volume if encoder_role=volume)
  - short press     -> play the selected sample
  - double click    -> next (respects sequential/random mode)
  - long press      -> toggle sequential/random
The piezo chirps for selection ticks and confirmations.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any, Dict, Optional

from .engine import PlaybackEngine
from .logsetup import get_logger
from .settings import Settings

log = get_logger("inputs")


class _Piezo:
    """Non-blocking beeper for a PASSIVE piezo, driven with a PWM square wave
    near its resonant frequency (a passive element only clicks faintly on plain
    on/off; an oscillating signal at resonance makes it ring loudly). Feedback
    cues are distinguished by beep COUNT and timing. No-op if not wired."""

    FREQ_HZ = 2000  # default resonant peak of this piezo (loudest)

    # GPIO -> hardware-PWM channel (needs dtoverlay=pwm-2chan: 18/12 = channel 0,
    # 19/13 = channel 1). True hardware PWM is glitch-free under CPU load;
    # gpiozero's *software* PWM is not, which is what causes the occasional
    # distortion. We prefer hardware PWM and fall back to gpiozero if it's not set up.
    _HW_CHANNEL = {12: 0, 13: 1, 18: 0, 19: 1}

    def __init__(self, pin: int, sys_freq: int = FREQ_HZ) -> None:
        self._pwm = None
        self._hw = False
        self._lock = threading.Lock()
        self._cur_stop: Optional[threading.Event] = None
        self.sys_freq = int(sys_freq) if sys_freq else self.FREQ_HZ
        self.volume = 1.0   # master gain 0..1; scales the duty cycle (piezo loudness)
        if pin is None or pin < 0:
            return
        ch = self._HW_CHANNEL.get(pin)
        if ch is not None:
            try:
                from rpi_hardware_pwm import HardwarePWM

                self._pwm = HardwarePWM(pwm_channel=ch, hz=self.sys_freq, chip=0)
                self._pwm.start(0)  # 0% duty = silent
                self._hw = True
                log.info("piezo: hardware PWM on GPIO%s (channel %s)", pin, ch)
                return
            except Exception as exc:
                log.info("piezo: hardware PWM unavailable (%s); software PWM", exc)
                self._pwm = None
        try:
            from gpiozero import PWMOutputDevice

            self._pwm = PWMOutputDevice(pin, frequency=self.sys_freq, initial_value=0)
        except Exception:
            self._pwm = None

    @property
    def hardware(self) -> bool:
        return self._hw

    def set_sys_freq(self, freq: float) -> int:
        """Set the system tone (used by all the cue beeps)."""
        self.sys_freq = max(50, min(8000, int(freq)))
        return self.sys_freq

    def set_volume(self, pct) -> int:
        """0..100 master volume; scales every beep's duty cycle (piezo loudness)."""
        v = max(0, min(100, int(pct)))
        self.volume = v / 100.0
        return v

    def _set(self, freq, duty) -> None:
        """Drive the pin: freq Hz at duty 0..1 (scaled by volume); freq<=0 or duty
        0 = silent. Works on both the hardware-PWM and gpiozero backends."""
        if self._pwm is None:
            return
        duty = duty * self.volume
        on = bool(freq and freq > 0 and duty and duty > 0)
        try:
            if self._hw:
                if on:
                    self._pwm.change_frequency(max(1, int(freq)))
                    self._pwm.change_duty_cycle(duty * 100.0)
                else:
                    self._pwm.change_duty_cycle(0)
            else:
                if on:
                    self._pwm.frequency = max(1, int(freq))
                    self._pwm.value = duty
                else:
                    self._pwm.value = 0
        except Exception:
            pass

    def play_tones(self, tones) -> None:
        """Play a non-blocking sequence of (freq_hz, ms) steps; freq 0/None = a
        silent gap. All piezo output funnels through here, and starting a new
        sequence cancels any one still playing (so rapid presses don't garble)."""
        if self._pwm is None:
            return
        with self._lock:
            if self._cur_stop is not None:
                self._cur_stop.set()
            stop = threading.Event()
            self._cur_stop = stop

        def _run() -> None:
            try:
                for freq, ms in tones:
                    if stop.is_set():
                        return
                    self._set(freq, 0.5)
                    time.sleep(max(0, ms) / 1000.0)
            finally:
                if not stop.is_set():  # don't stomp a sequence that replaced us
                    self._set(0, 0)

        threading.Thread(target=_run, daemon=True).start()

    def beep(self, ms: int = 60) -> None:
        self.play_pattern([(ms, 0)])

    def play_pattern(self, pattern) -> None:
        """[(on_ms, gap_ms), ...] beeps at the system tone (loudest at resonance)."""
        tones = []
        for on_ms, gap_ms in pattern:
            tones.append((self.sys_freq, on_ms))
            if gap_ms:
                tones.append((0, gap_ms))
        self.play_tones(tones)

    def tone(self, freq_hz: float, ms: int = 70) -> None:
        """A single tone at an arbitrary frequency (e.g. volume-pitch feedback)."""
        self.play_tones([(freq_hz, ms)])

    def droid(self) -> None:
        """A short C-3PO / R2-D2-style chatter: quick boops + warbling whistle
        sweeps, lightly randomized each time so it sounds 'alive'. Non-blocking."""
        if self._pwm is None:
            return

        def sweep(f1, f2, ms, steps=12):
            return [(f1 + (f2 - f1) * i / (steps - 1), ms / steps) for i in range(steps)]

        def boop():
            return [(random.randint(1200, 2600), random.randint(40, 80))]

        seq = boop() + boop()
        seq += sweep(random.randint(1100, 1400), random.randint(2300, 2700), random.randint(90, 150))
        seq += [(0, 25)] + boop()
        seq += sweep(random.randint(2300, 2700), random.randint(1100, 1400), random.randint(80, 130))
        seq += [(0, 20)] + boop() + boop()
        if random.random() < 0.5:
            seq += [(0, 20)] + sweep(1500, 2500, 80) + [(random.randint(1300, 1700), 50)]
        self.play_tones(seq)


def play_duet(p1, p2, pairs, duty1: float = 0.5, duty2: float = 0.28) -> None:
    """Play [(melody_hz, harmony_hz, ms), ...] on two piezos at once, in lockstep
    (single thread). The harmony (p2) plays at a LOWER duty cycle so it sits under
    the melody instead of competing — keeps the duet clear, not droning. Cancels any
    sequence already running on either piezo; melody-only if p2 isn't wired."""
    if p1 is None or getattr(p1, "_pwm", None) is None:
        return
    has2 = p2 is not None and getattr(p2, "_pwm", None) is not None
    stop = threading.Event()
    for p in (p1, p2 if has2 else None):
        if p is not None:
            with p._lock:
                if p._cur_stop is not None:
                    p._cur_stop.set()
                p._cur_stop = stop

    def _run() -> None:
        try:
            for f1, f2, ms in pairs:
                if stop.is_set():
                    return
                p1._set(f1, duty1)
                if has2:
                    p2._set(f2, duty2)
                time.sleep(max(0, ms) / 1000.0)
        finally:
            if not stop.is_set():
                p1._set(0, 0)
                if has2:
                    p2._set(0, 0)

    threading.Thread(target=_run, daemon=True).start()


# Volume feedback: map the volume level (0..100) to a piezo pitch so the kid
# *hears* where the volume is — low pitch = quiet, high pitch = loud. Log-spaced
# so the steps sound evenly spread; at the ends the pitch simply repeats the
# boundary tone, which doubles as the "you're at min/max" cue.
VOL_FREQ_LOW = 500.0    # Hz at volume 0
VOL_FREQ_HIGH = 2500.0  # Hz at volume 100


def _volume_freq(volume: int) -> float:
    v = max(0, min(100, int(volume))) / 100.0
    return VOL_FREQ_LOW * (VOL_FREQ_HIGH / VOL_FREQ_LOW) ** v


# Piezo feedback cues for the active buzzer — (on_ms, gap_ms) beeps, recognized
# by COUNT/rhythm: 1 = tap, 2 = next, 3 = random, 1 long = looped to start.
CUE_TICK = [(70, 0)]                          # tap / browse:  1 short beep
CUE_NEXT = [(70, 90), (70, 0)]                # hold -> next:  2 beeps
CUE_RANDOM = [(70, 90), (70, 90), (70, 0)]    # double -> random: 3 beeps
CUE_WRAP = [(320, 0)]                         # looped back to #1: 1 long beep
CUE_VOL_UP = [(45, 0)]                         # turn: 1 quick tick
CUE_VOL_DOWN = [(45, 0)]                       # turn: 1 quick tick
# Each gesture gets its own distinct R2-D2 voice (freq_hz, ms); hold = full droid().
R2_TAP = [(2600, 70), (3200, 150)]                                  # tap/replay: quick "affirmative"
R2_RANDOM = [(1800, 80), (2400, 80), (3000, 150)]                   # double/random: curious rising
R2_LOG = [(2600, 50), (3200, 50), (2600, 50), (3400, 70), (3000, 160)]  # triple/start log: excited
R2_HOTSPOT_ON = [(1500, 35), (1900, 35), (2300, 35), (2700, 35), (3100, 35), (3500, 120)]   # quad on: whistle up
R2_HOTSPOT_OFF = [(3500, 35), (3100, 35), (2700, 35), (2300, 35), (1900, 35), (1500, 120)]  # quad off: whistle down


class _ClickHandler:
    """Detect short / double / long press on a gpiozero Button."""

    def __init__(
        self,
        settings,
        on_short,
        on_double,
        on_long,
        diagnostics: "Inputs",
        on_triple=None,
        on_quad=None,
    ) -> None:
        self._double_s = settings.get("double_click_ms", 350) / 1000
        self._on_short = on_short
        self._on_double = on_double
        self._on_long = on_long
        self._on_triple = on_triple
        self._on_quad = on_quad
        self._diagnostics = diagnostics
        self._held = False
        self._count = 0
        self._timer: Optional[threading.Timer] = None

    def attach(self, button) -> None:
        button.when_pressed = self._pressed_cb
        button.when_held = self._held_cb
        button.when_released = self._released_cb

    def _pressed_cb(self) -> None:
        self._diagnostics.note_button("pressed")

    def _held_cb(self) -> None:
        self._held = True
        self._count = 0
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._diagnostics.note_button("held")
        self._safe(self._on_long)

    def _released_cb(self) -> None:
        self._diagnostics.note_button("released")
        if self._held:
            self._held = False
            return
        # Count clicks within the window, then dispatch single/double/triple
        # (triple = hotspot toggle, so double now waits for a possible 3rd click).
        self._count += 1
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self._double_s, self._dispatch)
        self._timer.daemon = True
        self._timer.start()

    def _dispatch(self) -> None:
        n = self._count
        self._count = 0
        self._timer = None
        if self._on_quad is not None and n >= 4:
            self._diagnostics.note_button("quad")
            self._safe(self._on_quad)
        elif self._on_triple is not None and n == 3:
            self._diagnostics.note_button("triple")
            self._safe(self._on_triple)
        elif n >= 2:
            self._diagnostics.note_button("double")
            self._safe(self._on_double)
        else:
            self._diagnostics.note_button("short")
            self._safe(self._on_short)

    @staticmethod
    def _safe(fn) -> None:
        try:
            fn()
        except Exception:
            pass


class Inputs:
    """Holds references to gpiozero devices so they aren't garbage collected."""

    def __init__(self) -> None:
        self.encoder = None
        self.button = None
        self.piezo: Optional[_Piezo] = None
        self.piezo2: Optional[_Piezo] = None
        self.active = False
        self.gpio_available = False
        self.encoder_clk = -1
        self.encoder_dt = -1
        self.button_pin = -1
        self.button_source = "off"
        self.encoder_bounce_ms = 0
        self.button_bounce_ms = 0
        self._lock = threading.Lock()
        self._events = []
        self._event_seq = 0
        self._encoder_counts = {"clockwise": 0, "counter_clockwise": 0}
        self._button_counts = {
            "pressed": 0,
            "released": 0,
            "held": 0,
            "short": 0,
            "double": 0,
            "triple": 0,
            "quad": 0,
        }
        self._last_encoder_direction = ""
        self._last_button_event = ""
        self._errors = []

    def note_encoder(self, step: int) -> None:
        direction = "clockwise" if step > 0 else "counter_clockwise"
        with self._lock:
            self._encoder_counts[direction] += 1
            self._last_encoder_direction = direction
            self._append_event(f"encoder {direction}")
            count = self._encoder_counts[direction]
        log.info("physical encoder %s count=%s", direction, count)

    def note_button(self, event: str) -> None:
        with self._lock:
            if event in self._button_counts:
                self._button_counts[event] += 1
            self._last_button_event = event
            self._append_event(f"button {event}")
            count = self._button_counts.get(event, 0)
        log.info("physical button %s count=%s", event, count)

    def note_error(self, area: str, exc: Exception) -> None:
        with self._lock:
            self._errors.append(f"{area}: {exc}")
            self._errors = self._errors[-6:]
            self._append_event(f"{area} error")
        log.warning("physical input %s error: %s", area, exc)

    def diagnostics(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            events = list(self._events)
            encoder_counts = dict(self._encoder_counts)
            button_counts = dict(self._button_counts)
            last_encoder_direction = self._last_encoder_direction
            last_button_event = self._last_button_event
            errors = list(self._errors)
            event_seq = self._event_seq
        encoder_steps = None
        encoder_active = self.encoder is not None
        button_pressed = None
        button_active = self.button is not None
        try:
            if self.encoder is not None:
                encoder_steps = getattr(self.encoder, "steps", None)
        except Exception:
            encoder_steps = None
        try:
            if self.button is not None:
                button_pressed = bool(self.button.is_pressed)
        except Exception:
            button_pressed = None
        return {
            "active": self.active,
            "gpio_available": self.gpio_available,
            "event_seq": event_seq,
            "now": now,
            "encoder": {
                "configured": self.encoder_clk >= 0 and self.encoder_dt >= 0,
                "active": encoder_active,
                "clk": self.encoder_clk,
                "dt": self.encoder_dt,
                "steps": encoder_steps,
                "bounce_ms": self.encoder_bounce_ms,
                "counts": encoder_counts,
                "last_direction": last_encoder_direction,
            },
            "button": {
                "configured": self.button_pin >= 0,
                "active": button_active,
                "pin": self.button_pin,
                "source": self.button_source,
                "pressed": button_pressed,
                "bounce_ms": self.button_bounce_ms,
                "counts": button_counts,
                "last_event": last_button_event,
            },
            "events": events,
            "errors": errors,
        }

    def _append_event(self, message: str) -> None:
        self._event_seq += 1
        self._events.append(
            {
                "seq": self._event_seq,
                "time": time.time(),
                "message": message,
            }
        )
        self._events = self._events[-12:]


def _milliseconds(value: Any, default: int) -> int:
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, ms)


def _bounce_seconds(value: Any) -> Optional[float]:
    ms = _milliseconds(value, 0)
    if ms <= 0:
        return None
    return ms / 1000


def start_inputs(settings: Settings, engine: PlaybackEngine, gps=None) -> Inputs:
    handles = Inputs()
    try:
        from gpiozero import Button, RotaryEncoder
    except Exception:
        handles.note_error("gpiozero", Exception("gpiozero import failed"))
        return handles  # not on a Pi -> no-op
    handles.gpio_available = True

    piezo = _Piezo(settings.get("piezo_pin", -1), settings.get("piezo_freq", 2000))
    handles.piezo = piezo
    # Second piezo (GPIO18 = PWM channel 0) for two-voice harmony, if wired.
    piezo2 = _Piezo(settings.get("piezo2_pin", -1), settings.get("piezo_freq", 2000))
    handles.piezo2 = piezo2
    _pvol = settings.get("piezo_volume", 100)
    piezo.set_volume(_pvol)
    piezo2.set_volume(_pvol)

    # -- rotary encoder -----------------------------------------------------
    clk, dt = settings.get("encoder_clk", -1), settings.get("encoder_dt", -1)
    handles.encoder_clk = clk
    handles.encoder_dt = dt
    handles.encoder_bounce_ms = _milliseconds(
        settings.get("encoder_bounce_ms", 3), 3
    )
    if clk >= 0 and dt >= 0:
        try:
            enc = RotaryEncoder(
                clk,
                dt,
                max_steps=0,
                bounce_time=_bounce_seconds(handles.encoder_bounce_ms),
            )

            def rotate(step: int) -> None:
                handles.note_encoder(step)
                if settings.get("encoder_role", "select") == "volume":
                    # 10% per detent -> 11 levels (0..10). Beep a pitch that maps
                    # to the new level; at min/max the volume clamps so the pitch
                    # stays at the boundary tone (the out-of-bounds cue).
                    new_vol = engine.volume_step(step * 10)
                    piezo.tone(_volume_freq(new_vol), 70)
                else:
                    # "select" role = browse + audition: each detent plays the
                    # next/prev sound so he can hear what he's landing on.
                    res = engine.step_and_play(step)
                    piezo.play_pattern(
                        CUE_WRAP if (res and res.get("wrapped")) else CUE_TICK
                    )

            enc.when_rotated_clockwise = lambda: rotate(+1)
            enc.when_rotated_counter_clockwise = lambda: rotate(-1)
            handles.encoder = enc
        except Exception as exc:
            handles.note_error("encoder", exc)
            handles.encoder = None

    # -- push button (the rotary encoder's push switch) ---------------------
    pin = settings.get("encoder_sw", -1)
    handles.button_pin = pin
    handles.button_source = "encoder_sw" if pin >= 0 else "off"
    handles.button_bounce_ms = _milliseconds(
        settings.get("button_bounce_ms", 60), 60
    )
    if pin >= 0:
        try:
            btn = Button(
                pin,
                pull_up=True,
                bounce_time=_bounce_seconds(handles.button_bounce_ms),
                hold_time=settings.get("long_press_ms", 700) / 1000,
            )

            def _gesture_cue(gesture, default_fn) -> None:
                # Play the beep-playground sound assigned to this gesture (a duet if
                # configured); else the built-in default R2 cue. Read live from
                # settings so assignments take effect without a restart.
                asn = (settings.get("gesture_sounds") or {}).get(gesture)
                name = asn.get("name") if isinstance(asn, dict) else None
                if name:
                    from . import beeps
                    if asn.get("duet"):
                        pairs = beeps.harmonize_named(name, asn.get("harmony") or "3rd above")
                        if pairs:
                            play_duet(piezo, piezo2, pairs)
                            return
                    tune = beeps.find(name)
                    if tune:
                        piezo.play_tones(tune)
                        return
                default_fn()

            def short() -> None:
                # Tap = replay the current sound.
                engine.play_selected()
                _gesture_cue("tap", lambda: piezo.play_tones(R2_TAP))

            def double() -> None:
                # Double-tap = surprise me (random sound, plays it).
                engine.play_random()
                _gesture_cue("double", lambda: piezo.play_tones(R2_RANDOM))

            def long() -> None:
                # Hold = the droid "talks" (default) AND advances + plays the meme on
                # the BT speaker (piezo and speaker are separate). Respects shuffle.
                _gesture_cue("hold", lambda: piezo.droid())
                if settings.get("mode", "sequential") == "random":
                    engine.play_random()
                else:
                    engine.step_and_play(+1)

            def triple() -> None:
                # Triple-tap = START GPS logging (force it on); cue defaults to an
                # excited R2 chirp. Auto-stops after ~20s back on home Wi-Fi.
                if gps is not None:
                    gps.set_override("on")
                _gesture_cue("triple", lambda: piezo.play_tones(R2_LOG))

            def quad() -> None:
                # Quad-tap = toggle the Wi-Fi hotspot; cue defaults to a whistle
                # up (on) / down (off).
                from . import hotspot
                going_on = hotspot.status() != "on"
                hotspot.toggle()
                _gesture_cue("quad", lambda: piezo.play_tones(
                    R2_HOTSPOT_ON if going_on else R2_HOTSPOT_OFF))

            _ClickHandler(
                settings, short, double, long, handles,
                on_triple=triple, on_quad=quad,
            ).attach(btn)
            handles.button = btn
        except Exception as exc:
            handles.note_error("button", exc)
            handles.button = None

    handles.active = handles.encoder is not None or handles.button is not None
    return handles

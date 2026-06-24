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

    FREQ_HZ = 2000  # measured resonant peak of this piezo (loudest)

    def __init__(self, pin: int) -> None:
        self._pwm = None
        if pin is None or pin < 0:
            return
        try:
            from gpiozero import PWMOutputDevice

            self._pwm = PWMOutputDevice(
                pin, frequency=self.FREQ_HZ, initial_value=0
            )
        except Exception:
            self._pwm = None

    def beep(self, ms: int = 60) -> None:
        self.play_pattern([(ms, 0)])

    def play_pattern(self, pattern) -> None:
        """Play a non-blocking sequence of (on_ms, gap_ms) beeps, each a burst
        of the resonant square wave at 50% duty (loudest)."""
        if self._pwm is None:
            return

        def _run() -> None:
            try:
                for on_ms, gap_ms in pattern:
                    self._pwm.frequency = self.FREQ_HZ
                    self._pwm.value = 0.5
                    time.sleep(on_ms / 1000)
                    self._pwm.value = 0
                    if gap_ms:
                        time.sleep(gap_ms / 1000)
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()

    def tone(self, freq_hz: float, ms: int = 70) -> None:
        """Beep a single tone at an arbitrary frequency (non-blocking). Used for
        volume feedback where pitch maps to the level."""
        if self._pwm is None:
            return

        def _run() -> None:
            try:
                self._pwm.frequency = max(50, int(freq_hz))
                self._pwm.value = 0.5
                time.sleep(ms / 1000)
                self._pwm.value = 0
            except Exception:
                pass

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


class _ClickHandler:
    """Detect short / double / long press on a gpiozero Button."""

    def __init__(
        self,
        settings,
        on_short,
        on_double,
        on_long,
        diagnostics: "Inputs",
    ) -> None:
        self._double_s = settings.get("double_click_ms", 350) / 1000
        self._on_short = on_short
        self._on_double = on_double
        self._on_long = on_long
        self._diagnostics = diagnostics
        self._held = False
        self._pending: Optional[threading.Timer] = None

    def attach(self, button) -> None:
        button.when_pressed = self._pressed_cb
        button.when_held = self._held_cb
        button.when_released = self._released_cb

    def _pressed_cb(self) -> None:
        self._diagnostics.note_button("pressed")

    def _held_cb(self) -> None:
        self._held = True
        self._diagnostics.note_button("held")
        self._safe(self._on_long)

    def _released_cb(self) -> None:
        self._diagnostics.note_button("released")
        if self._held:
            self._held = False
            return
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None
            self._diagnostics.note_button("double")
            self._safe(self._on_double)
        else:
            self._pending = threading.Timer(self._double_s, self._fire_single)
            self._pending.daemon = True
            self._pending.start()

    def _fire_single(self) -> None:
        self._pending = None
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


def start_inputs(settings: Settings, engine: PlaybackEngine) -> Inputs:
    handles = Inputs()
    try:
        from gpiozero import Button, RotaryEncoder
    except Exception:
        handles.note_error("gpiozero", Exception("gpiozero import failed"))
        return handles  # not on a Pi -> no-op
    handles.gpio_available = True

    piezo = _Piezo(settings.get("piezo_pin", -1))
    handles.piezo = piezo

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

            def short() -> None:
                # Tap = replay the current sound.
                engine.play_selected()
                piezo.play_pattern(CUE_TICK)

            def double() -> None:
                # Double-tap = surprise me (random sound, plays it).
                engine.play_random()
                piezo.play_pattern(CUE_RANDOM)

            def long() -> None:
                # Hold = advance and play. Respects shuffle ("random") mode so
                # turning Random on actually changes the kid's main "next" gesture:
                # random -> a surprise clip; sequential -> the next in order.
                if settings.get("mode", "sequential") == "random":
                    engine.play_random()
                    piezo.play_pattern(CUE_RANDOM)
                else:
                    res = engine.step_and_play(+1)
                    piezo.play_pattern(
                        CUE_WRAP if (res and res.get("wrapped")) else CUE_NEXT
                    )

            _ClickHandler(settings, short, double, long, handles).attach(btn)
            handles.button = btn
        except Exception as exc:
            handles.note_error("button", exc)
            handles.button = None

    handles.active = handles.encoder is not None or handles.button is not None
    return handles

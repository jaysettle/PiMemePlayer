"""A catalog of fun piezo tunes for the Settings 'Beep playground'.

Melodies use **RTTTL** (the Nokia ringtone format) — the de-facto standard for
piezo/buzzer tunes, so the famous ones are accurate, not hand-approximated. The
accurate game/movie strings come from the MicroPython upy-rtttl library
(github.com/dhylands/upy-rtttl) plus a few canonical strings; R2-D2 chirps and
alert cues stay as raw (freq_hz, ms) effects since they're sound effects, not songs.

The piezo is loudest near its ~2 kHz resonance, so volume varies a little across a
melody's range — they stay recognizable by rhythm + relative pitch on one buzzer.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple, Union

Tune = List[Tuple[int, int]]            # [(freq_hz, ms), ...]; freq 0 = rest
Entry = Union[str, Tune]                # RTTTL string OR raw tones

# -- RTTTL parser -----------------------------------------------------------
_SEMI = {"c": -9, "c#": -8, "d": -7, "d#": -6, "e": -5, "f": -4,
         "f#": -3, "g": -2, "g#": -1, "a": 0, "a#": 1, "b": 2}


def _hz(note: str, octave: int) -> int:
    return int(round(440.0 * (2.0 ** ((_SEMI[note] + (octave - 4) * 12) / 12.0))))


def parse_rtttl(s: str) -> Tune:
    """RTTTL 'name:d=..,o=..,b=..:notes' -> [(freq_hz, ms), ...]."""
    try:
        _name, defaults, melody = s.split(":")
    except ValueError:
        return []
    d, o, b = 4, 6, 63
    for part in defaults.split(","):
        part = part.strip()
        if part[:2] == "d=":
            d = int(part[2:])
        elif part[:2] == "o=":
            o = int(part[2:])
        elif part[:2] == "b=":
            b = int(part[2:])
    whole = 4 * 60000.0 / b   # ms for a whole note
    tones: Tune = []
    for tok in melody.split(","):
        tok = tok.strip().lower()
        if not tok:
            continue
        m = re.match(r"(\d*)([a-gp]#?)(\.?)(\d*)(\.?)", tok)
        if not m:
            continue
        dur = int(m.group(1)) if m.group(1) else d
        note = m.group(2)
        octv = int(m.group(4)) if m.group(4) else o
        dotted = bool(m.group(3) or m.group(5))
        ms = whole / max(dur, 1)
        if dotted:
            ms *= 1.5
        if note == "p":
            tones.append((0, int(ms)))
        else:
            on = int(ms * 0.92)
            tones.append((_hz(note, octv), on))
            gap = int(ms - on)
            if gap > 0:
                tones.append((0, gap))
    return tones


# -- sound-effect helpers ---------------------------------------------------
def _sweep(f1: int, f2: int, total_ms: int, steps: int = 14) -> Tune:
    """A pitch glide from f1->f2 as stepped tones (laser/zap/warp effects)."""
    step = max(8, total_ms // steps)
    return [(int(f1 + (f2 - f1) * i / (steps - 1)), step) for i in range(steps)]


# -- R2-D2 chirps & alert effects (raw tones) -------------------------------
_R2D2 = {
    "Happy chirp":  [(2200, 60), (2800, 60), (3300, 110)],
    "Excited":      [(2600, 45), (3200, 45), (2600, 45), (3400, 90)],
    "Curious?":     [(1800, 80), (2400, 80), (3000, 140)],
    "Affirmative":  [(2600, 70), (3200, 150)],
    "Negative":     [(1500, 100), (1000, 180)],
    "Worried":      [(2800, 60), (2400, 60), (2800, 60), (1900, 180)],
    "Sad":          [(2400, 130), (1800, 130), (1200, 240)],
    "Alarm!":       [(3000, 70), (2000, 70), (3000, 70), (2000, 70), (3200, 120)],
    "Whistle up":   [(1500, 30), (1900, 30), (2300, 30), (2700, 30), (3100, 30), (3500, 90)],
    "Whistle down": [(3500, 30), (3100, 30), (2700, 30), (2300, 30), (1900, 30), (1500, 90)],
    "Chatter":      [(2200, 45), (2900, 45), (2500, 45), (3100, 45), (2400, 45), (2800, 100)],
    "Boop boop":    [(2400, 90), (0, 60), (2400, 90), (0, 60), (3000, 120)],
}

_ALERTS = {
    "Level up":   [(523, 80), (659, 80), (784, 80), (1047, 220)],
    "Power down": [(1047, 110), (784, 110), (523, 110), (392, 320)],
    "Fanfare":    [(523, 120), (523, 120), (523, 120), (659, 420)],
    "Ta-da!":     [(784, 120), (0, 40), (1047, 120), (1319, 360)],
    "Red alert":  [(1200, 160), (0, 80), (1200, 160), (0, 80), (1200, 220)],
    "Airhorn":    [(440, 90), (0, 50), (440, 90), (0, 50), (440, 260)],
    "Sad trombone": [(440, 220), (415, 220), (392, 220), (370, 200), (349, 560)],
    "SOS":        [(2000, 80), (0, 90), (2000, 80), (0, 90), (2000, 80), (0, 220),
                   (2000, 240), (0, 90), (2000, 240), (0, 90), (2000, 240), (0, 220),
                   (2000, 80), (0, 90), (2000, 80), (0, 90), (2000, 80)],
}

# -- retro / arcade game sound effects (raw tones) --------------------------
_GAME_FX = {
    "Coin":        [(988, 80), (1319, 360)],
    "Coin shower": [(1568, 40), (0, 18), (2093, 40), (0, 18), (2637, 40), (0, 18), (3136, 40), (0, 18), (3520, 100)],
    "Pickup":      [(1319, 50), (1760, 50), (2637, 130)],
    "Bonus":       [(2093, 40), (2349, 40), (2637, 40), (2794, 40), (3136, 40), (3520, 150)],
    "1-UP":        [(1319, 120), (1568, 120), (2637, 120), (2093, 120), (2349, 120), (3136, 200)],
    "Extra life":  [(1047, 80), (1319, 80), (1568, 80), (2093, 200)],
    "Power-up":    [(523, 50), (659, 50), (784, 50), (1047, 50), (1319, 50), (1568, 180)],
    "Jump":        _sweep(520, 1200, 130, 9),
    "Super jump":  _sweep(400, 1700, 200, 14),
    "Dash":        _sweep(1200, 3200, 80, 8),
    "Whoosh":      _sweep(600, 2600, 110, 10),
    "Boing":       [(2600, 40), (1800, 50), (2400, 40), (1700, 60), (2200, 40), (1600, 95)],
    "Bounce":      [(2400, 55), (0, 80), (2200, 50), (0, 120), (2000, 45), (0, 160), (1800, 40), (0, 200), (1600, 35)],
    "Laser":       _sweep(4200, 350, 160, 16),
    "Pew pew":     _sweep(4200, 500, 70, 7) + [(0, 40)] + _sweep(4200, 500, 70, 7),
    "Ray gun":     _sweep(3600, 500, 130, 12),
    "Fireball":    _sweep(2400, 700, 140, 11),
    "Hit":         [(220, 90), (170, 130)],
    "Hurt":        _sweep(820, 200, 180, 10),
    "Bump":        [(150, 120)],
    "Explosion":   _sweep(900, 70, 340, 20),
    "Death":       _sweep(1200, 180, 360, 16),
    "Star power":  [(1568, 55), (2093, 55), (1568, 55), (2093, 55), (1568, 55), (2093, 110)],
    "Charge":      _sweep(400, 2400, 280, 16),
    "Shield":      [(2000, 45), (2400, 45), (2000, 45), (2400, 45), (2000, 90)],
    "Warp":        _sweep(700, 3600, 150, 11) + _sweep(3600, 700, 150, 11),
    "Teleport":    [(3000, 30), (1500, 30), (3200, 30), (1700, 30), (3400, 30), (1900, 30), (3600, 30), (2100, 30), (3800, 70)],
    "Blip":        [(2600, 35)],
    "Menu select": [(1568, 50), (2093, 130)],
    "Sparkle":     [(3200, 30), (3800, 30), (2800, 30), (3600, 30), (3000, 30), (3900, 80)],
    "Score tally": [(2600, 25)] * 10 + [(0, 30), (3200, 150)],
    "Boss alert":  [(1400, 160), (900, 160), (1400, 160), (900, 160), (1400, 230)],
    "Continue?":   [(880, 120), (1175, 120), (880, 120), (1175, 280)],
    "Level up":    [(523, 80), (659, 80), (784, 80), (1047, 220)],
    "Game over":   [(523, 160), (494, 160), (440, 160), (392, 420)],
}

# -- the catalog (RTTTL strings + raw effects) ------------------------------
SECTIONS: "Dict[str, Dict[str, Entry]]" = {
    "R2-D2": dict(_R2D2),
    "Game SFX": dict(_GAME_FX),
    "Video games": {
        "Super Mario": "smb:d=4,o=5,b=125:a,8f.,16c,16d,16f,16p,f,16d,16c,16p,16f,16p,16f,16p,8c6,8a.,g,16c,a,8f.,16c,16d,16f,16p,f,16d,16c,16p,16f,16p,16a#,16a,16g,2f",
        "Mario underworld": "SMBunderground:d=16,o=6,b=100:c,c5,a5,a,a#5,a#,2p,8p,c,c5,a5,a,a#5,a#,2p,8p,f5,f,d5,d,d#5,d#,2p,8p,f5,f,d5,d,d#5,d#,2p,32d#,d,32c#,c,p,d#,p,d,p,g#5,p,g5,p,c#,p,32c,f#,32f,32e,a#,32a,g#,32p,d#,b5,32p,a#5,32p,a5,g#5",
        "Mario water": "SMBwater:d=8,o=6,b=225:4d5,4e5,4f#5,4g5,4a5,4a#5,b5,b5,b5,p,b5,p,2b5,p,g5,2e.,2d#.,2e.,p,g5,a5,b5,c,d,2e.,2d#,4f,2e.",
        "Tetris": "tetris:d=4,o=5,b=160:e6,8b,8c6,8d6,16e6,16d6,8c6,8b,a,8a,8c6,e6,8d6,8c6,b,8b,8c6,d6,e6,c6,a,2a",
        "Coin": [(988, 90), (1319, 380)],
        "1-UP": [(1319, 120), (1568, 120), (2637, 120), (2093, 120), (2349, 120), (3136, 240)],
        "Zelda secret": [(784, 130), (740, 130), (622, 130), (440, 130), (415, 130), (659, 130), (831, 130), (1047, 320)],
        "Sonic ring": [(2637, 60), (3520, 220)],
        "Game over": [(523, 160), (494, 160), (440, 160), (392, 420)],
    },
    "Movies & TV": {
        "Star Wars": "StarWars:d=4,o=5,b=45:32p,32f#,32f#,32f#,8b.,8f#.6,32e6,32d#6,32c#6,8b.6,16f#.6,32e6,32d#6,32c#6,8b.6,16f#.6,32e6,32d#6,32e6,8c#.6",
        "Imperial March": "imperial:d=4,o=5,b=120:8g,8g,8g,8d#,16p,16a#,8g,8d#,16p,16a#,8g,2p,8d6,8d6,8d6,8d#6,16p,16a#,8f#,8d#,16p,16a#,8g,2p",
        "Indiana Jones": "Indiana:d=4,o=5,b=250:e,8p,8f,8g,8p,1c6,8p.,d,8p,8e,1f,p.,g,8p,8a,8b,8p,1f6,p,a,8p,8b,2c6,2d6,2e6",
        "James Bond": "Bond:d=4,o=5,b=80:32p,16c#6,32d#6,32d#6,16d#6,8d#6,16c#6,16c#6,16c#6,16c#6,32e6,32e6,16e6,8e6,16d#6,16d#6,16d#6,16c#6,32d#6,32d#6,16d#6,8d#6,16c#6,16c#6,16c#6,16c#6,32e6,32e6,16e6,8e6,16d#6,16d6,16c#6,16c#7,c.7,16g#6,16f#6,g#.6",
        "Mission: Impossible": "MI:d=16,o=6,b=95:32d,32d#,32d,32d#,32d,32d#,32d,32d#,32d,32d,32d#,32e,32f,32f#,32g,g,8p,g,8p,a#,p,c7,p,g,8p,g,8p,f,p,f#,p,g,8p,g,8p,a#,p,c7,p,g,8p,g,8p,f,p,f#,p",
        "The Simpsons": "Simpsons:d=4,o=5,b=160:c.6,e6,f#6,8a6,g.6,e6,c6,8a,8f#,8f#,8f#,2g",
        "A-Team": "ATeam:d=8,o=5,b=125:4d#6,a#,2d#6,16p,g#,4a#,4d#.,p,16g,16a#,d#6,a#,f6,2d#6,16p,c#.6,16c6,16a#,g#.,2a#",
        "Jeopardy": "Jeopardy:d=4,o=6,b=125:c,f,c,f5,c,f,2c,c,f,c,f,a.,8g,8f,8e,8d,8c#,c,f,c,f5,c,f,2c,f.,8d,c,a#5,a5,g5,f5",
        "X-Files": "Xfiles:d=4,o=5,b=125:e,b,a,b,d6,2b.,1p,e,b,a,b,e6,2b.,1p,g6,f#6,e6,d6,e6,2b.,1p,g6,f#6,e6,d6,f#6,2b.",
        "Top Gun": "TopGun:d=4,o=4,b=31:32p,16c#,16g#,16g#,32f#,32f,32f#,32f,16d#,16d#,32c#,32d#,16f,32d#,32f,16f#,32f,32c#,16f,d#,16c#,16g#,16g#,32f#,32f,32f#,32f,16d#,16d#,32c#,32d#,16f,32d#,32f,16f#,32f,32c#,g#",
        "Good, Bad & Ugly": "GoodBad:d=4,o=5,b=56:32p,32a#,32d#6,32a#,32d#6,8a#.,16f#.,16g#.,d#,32a#,32d#6,32a#,32d#6,8a#.,16f#.,16g#.,c#6,32a#,32d#6,32a#,32d#6,8a#.,16f#.,32f.,32d#.,c#,32a#,32d#6,32a#,32d#6,8a#.,16g#.,d#",
        "M*A*S*H": "MASH:d=8,o=5,b=140:4a,4g,f#,g,p,f#,p,g,p,f#,p,2e.,p,f#,e,4f#,e,f#,p,e,p,4d.,p,f#,4e,d,e,p,d,p,e,p,d,p,2c#.,p,d,c#,4d,c#,d,p,e,p,4f#",
    },
    "Cartoons": {
        "Flintstones": "Flinstones:d=4,o=5,b=40:32p,16f6,16a#,16a#6,32g6,16f6,16a#.,16f6,32d#6,32d6,32d6,32d#6,32f6,16a#,16c6,d6,16f6,16a#.,16a#6,32g6,16f6,16a#.,32f6,32f6,32d#6,32d6,32d6,32d#6,32f6,16a#,16c6,a#",
        "Inspector Gadget": "Gadget:d=16,o=5,b=50:32d#,32f,32f#,32g#,a#,f#,a,f,g#,f#,32d#,32f,32f#,32g#,a#,d#6,4d6,32d#,32f,32f#,32g#,a#,f#,a,f,g#,f#,8d#",
        "The Muppets": "Muppets:d=4,o=5,b=250:c6,c6,a,b,8a,b,g,p,c6,c6,a,8b,8a,8p,g.,p,e,e,g,f,8e,f,8c6,8c,8d,e,8e,8e,8p,8e,g,2p,c6,c6,a,b,8a,b,g,p,c6,c6,a,8b,a,g.",
        "Mahna Mahna": "MahnaMahna:d=16,o=6,b=125:c#,c.,b5,8a#.5,8f.,4g#,a#,g.,4d#,8p,c#,c.,b5,8a#.5,8f.,g#.,8a#.,4g,8p,c#,c.,b5,8a#.5,8f.,4g#,f,g.,8d#.,f,g.,8d#.,f,8g,8d#.,f,8g,d#,8c,a#5,8d#.,8d#.,4d#,8d#.",
        "Looney Tunes": "Looney:d=4,o=5,b=140:32p,c6,8f6,8e6,8d6,8c6,a.,8c6,8f6,8e6,8d6,8d#6,e.6,8e6,8e6,8c6,8d6,8c6,8e6,8c6,8d6,8a,8c6,8g,8a#,8a,8f",
        "Smurfs": "Smurfs:d=32,o=5,b=200:4c#6,16p,4f#6,p,16c#6,p,8d#6,p,8b,p,4g#,16p,4c#6,p,16a#,p,8f#,p,8a#,p,4g#,4p,g#,p,a#,p,b,p,c6,p,4c#6,16p,4f#6,p,16c#6,p,8d#6,p,8b,p,4g#,16p,4c#6,p,16a#,p,8b,p,8f,p,4f#",
    },
    "Memes & 80s": {
        "Nokia": "Nokia:d=4,o=5,b=125:8e6,8d6,4f#,4g#,8c#6,8b,4d,4e,8b,8a,4c#,4e,2a",
        "Entertainer": "Ent:d=4,o=5,b=140:8d,8d#,8e,c6,8e,c6,8e,2c.6,8c6,8d6,8d#6,8e6,8c6,8d6,e6,8b,d6,2c6",
        "Megalovania": [(587, 110), (587, 110), (1175, 240), (880, 360), (0, 60), (831, 240), (784, 240),
                        (698, 240), (587, 120), (698, 120), (784, 120)],
        "Rickroll": [(523, 150), (587, 150), (698, 150), (587, 150), (880, 300), (880, 300), (784, 500)],
        "Axel F": [(587, 150), (698, 110), (587, 150), (587, 80), (784, 110), (587, 110), (523, 110),
                   (587, 300), (880, 150), (587, 150), (587, 80), (932, 110), (880, 110), (698, 110),
                   (587, 110), (880, 150), (1175, 150), (587, 150), (587, 80), (622, 110), (880, 110),
                   (587, 110), (523, 320)],
        "Take On Me": "TakeOnMe:d=4,o=4,b=160:8f#5,8f#5,8f#5,8d5,8p,8b,8p,8e5,8p,8e5,8p,8e5,8g#5,8g#5,8a5,8b5,8a5,8a5,8a5,8e5,8p,8d5,8p,8f#5,8p,8f#5,8p,8f#5,8e5,8e5,8f#5,8e5,8f#5",
        "Final Countdown": [(932, 150), (831, 150), (932, 320), (622, 320), (0, 100), (1047, 150),
                            (932, 150), (1047, 110), (932, 110), (831, 360)],
    },
    "Alerts & fun": dict(_ALERTS),
}


def catalog() -> "Dict[str, List[str]]":
    """Sections -> tune names (for building the UI; no tone data)."""
    return {section: list(tunes.keys()) for section, tunes in SECTIONS.items()}


def find(name: str) -> "Optional[Tune]":
    for tunes in SECTIONS.values():
        if name in tunes:
            v = tunes[name]
            return parse_rtttl(v) if isinstance(v, str) else list(v)
    return None

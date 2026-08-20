"""
Procedurally generated sound effects.

NO pygame. NO files on disk. This module turns numbers into raw PCM bytes and
stops there -- ui.py is still the only thing that talks to the audio hardware,
exactly as it is still the only thing that talks to the screen.

Everything is synthesised at startup rather than loaded from WAVs, for the same
reason the card suits are drawn from polygons instead of a font: there is
nothing to ship, nothing to find at runtime, and nothing that can be missing on
the cabinet. It also happens to be the right sound -- an arcade machine from
the era this thing is pretending to be from made square waves, not samples.

Being pure makes it testable without a sound card, which matters because the Pi
in question may not have working audio at all.

Output format throughout: signed 16-bit little-endian mono at SAMPLE_RATE.
"""

from __future__ import annotations

import array
import math

#: 22.05kHz is plenty for square waves through a television speaker, and it is
#: half the samples of 44.1k for a Pi to push around.
SAMPLE_RATE = 22050

_PEAK = 32767

# A handful of note frequencies (Hz), so the tunes below read as music rather
# than as magic numbers.
NOTES = {
    "E3": 164.81, "A3": 220.00, "B3": 246.94,
    "C4": 261.63, "E4": 329.63, "G4": 392.00, "A4": 440.00, "B4": 493.88,
    "C5": 523.25, "D5": 587.33, "E5": 659.25, "G5": 783.99, "B5": 987.77,
    "C6": 1046.50, "E6": 1318.51, "G6": 1567.98,
}


# ---------------------------------------------------------------------------
# Waveforms
# ---------------------------------------------------------------------------


def _wave_sample(shape: str, phase: float, duty: float) -> float:
    """One sample of a unit-amplitude waveform at `phase` (0..1)."""
    if shape == "square":
        return 1.0 if phase < duty else -1.0
    if shape == "saw":
        return 2.0 * phase - 1.0
    if shape == "triangle":
        return 4.0 * abs(phase - 0.5) - 1.0
    return math.sin(2.0 * math.pi * phase)


def _envelope(i: int, total: int, attack: int, release: int) -> float:
    """Attack-then-decay gain. Silence at both ends, so notes never click.

    A hard-edged square wave that starts or stops mid-cycle pops audibly, and
    on a small television speaker the pop is louder than the note.
    """
    if i < attack:
        return i / max(1, attack)
    remaining = total - i
    if remaining < release:
        return remaining / max(1, release)
    return 1.0


def tone(
    freq: float,
    ms: float,
    shape: str = "square",
    volume: float = 0.6,
    attack_ms: float = 2.0,
    release_ms: float | None = None,
    duty: float = 0.5,
    bend: float = 1.0,
) -> list[float]:
    """One note. `bend` multiplies the frequency by the end (1.5 = up a fifth).

    Phase is accumulated rather than computed from `freq * t` so that a bend
    stays continuous -- recomputing from absolute time makes a sliding pitch
    jump backwards every sample and buzz.
    """
    total = max(1, int(SAMPLE_RATE * ms / 1000.0))
    release = int(SAMPLE_RATE * (ms * 0.4 if release_ms is None else release_ms) / 1000.0)
    attack = int(SAMPLE_RATE * attack_ms / 1000.0)

    out: list[float] = []
    phase = 0.0
    for i in range(total):
        progress = i / total
        current = freq * (1.0 + (bend - 1.0) * progress)
        phase = (phase + current / SAMPLE_RATE) % 1.0
        gain = volume * _envelope(i, total, attack, release)
        out.append(_wave_sample(shape, phase, duty) * gain)
    return out


def noise(
    ms: float, volume: float = 0.5, release_ms: float | None = None, colour: int = 1
) -> list[float]:
    """Filtered white noise -- the flick of a card, the rattle of a coin.

    Deliberately its own little generator rather than `random`: identical every
    boot, so a sound that comes out wrong can be reproduced. `colour` averages
    N successive samples, which dulls the hiss towards something more like a
    paper or plastic sound and less like a hi-hat.
    """
    total = max(1, int(SAMPLE_RATE * ms / 1000.0))
    release = int(SAMPLE_RATE * (ms * 0.6 if release_ms is None else release_ms) / 1000.0)

    out: list[float] = []
    state = 0x2545F491  # any non-zero seed
    history: list[float] = []
    for i in range(total):
        # xorshift32
        state ^= (state << 13) & 0xFFFFFFFF
        state ^= state >> 17
        state ^= (state << 5) & 0xFFFFFFFF
        sample = (state / 0xFFFFFFFF) * 2.0 - 1.0

        history.append(sample)
        if len(history) > max(1, colour):
            history.pop(0)
        smoothed = sum(history) / len(history)

        out.append(smoothed * volume * _envelope(i, total, 1, release))
    return out


def silence(ms: float) -> list[float]:
    return [0.0] * max(0, int(SAMPLE_RATE * ms / 1000.0))


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def sequence(*parts: list[float]) -> list[float]:
    """Play one after another."""
    out: list[float] = []
    for part in parts:
        out.extend(part)
    return out


def layer(*parts: list[float]) -> list[float]:
    """Play at the same time. Sums, then relies on to_pcm() to clip."""
    if not parts:
        return []
    out = [0.0] * max(len(p) for p in parts)
    for part in parts:
        for i, value in enumerate(part):
            out[i] += value
    return out


def to_pcm(samples: list[float], volume: float = 1.0) -> bytes:
    """Float -1..1 -> signed 16-bit little-endian bytes, hard-clipped.

    Clipping rather than normalising on purpose: a fixed relationship between
    the numbers above and the output level means adjusting one sound cannot
    quietly change the loudness of every other one.
    """
    buffer = array.array("h")
    for value in samples:
        scaled = int(max(-1.0, min(1.0, value * volume)) * _PEAK)
        buffer.append(scaled)
    if array.array("h").itemsize != 2:  # pragma: no cover - not a real platform
        raise RuntimeError("expected 16-bit shorts")
    import sys

    if sys.byteorder != "little":  # pragma: no cover - Pi and PC are both LE
        buffer.byteswap()
    return buffer.tobytes()


# ---------------------------------------------------------------------------
# The sounds themselves
# ---------------------------------------------------------------------------


def _coin() -> list[float]:
    """Two rising square blips: the sound every arcade cabinet makes."""
    return sequence(
        tone(NOTES["B5"], 55, volume=0.55, release_ms=10),
        tone(NOTES["E6"], 110, volume=0.55, release_ms=70),
    )


def _card() -> list[float]:
    """One card landing on felt. Short, dull, and quiet -- this plays four
    times in the first second of a hand and must never become annoying."""
    return layer(
        noise(45, volume=0.30, release_ms=40, colour=4),
        tone(196.0, 40, shape="triangle", volume=0.16, release_ms=34),
    )


def _deal() -> list[float]:
    """The riffle as a hand starts: a few flicks in quick succession."""
    flick = noise(28, volume=0.22, release_ms=24, colour=3)
    gap = silence(26)
    return sequence(flick, gap, flick, gap, flick)


def _bet() -> list[float]:
    return tone(NOTES["A4"], 45, volume=0.35, release_ms=25, duty=0.25)


def _win() -> list[float]:
    """Rising major arpeggio."""
    return sequence(
        tone(NOTES["C5"], 80, volume=0.45, release_ms=20),
        tone(NOTES["E5"], 80, volume=0.45, release_ms=20),
        tone(NOTES["G5"], 80, volume=0.45, release_ms=20),
        tone(NOTES["C6"], 200, volume=0.45, release_ms=140),
    )


def _blackjack() -> list[float]:
    """Longer and higher than a normal win -- a natural should feel special."""
    quick = 65
    return sequence(
        tone(NOTES["C5"], quick, volume=0.45, release_ms=15),
        tone(NOTES["E5"], quick, volume=0.45, release_ms=15),
        tone(NOTES["G5"], quick, volume=0.45, release_ms=15),
        tone(NOTES["C6"], quick, volume=0.45, release_ms=15),
        tone(NOTES["E6"], quick, volume=0.45, release_ms=15),
        tone(NOTES["G6"], 320, volume=0.50, release_ms=240),
    )


def _lose() -> list[float]:
    """Descending, with a fat duty cycle so it sounds like a buzz not a tune."""
    return sequence(
        tone(NOTES["B3"], 110, volume=0.40, release_ms=20, duty=0.18),
        tone(NOTES["A3"], 110, volume=0.40, release_ms=20, duty=0.18),
        tone(NOTES["E3"], 260, volume=0.42, release_ms=180, duty=0.18),
    )


def _push() -> list[float]:
    """Neither good nor bad: the same note twice."""
    return sequence(
        tone(NOTES["C5"], 70, volume=0.32, release_ms=20),
        silence(30),
        tone(NOTES["C5"], 130, volume=0.32, release_ms=90),
    )


def _cashout() -> list[float]:
    """A run up, for the moment the machine starts giving money back."""
    return sequence(
        tone(NOTES["C5"], 55, volume=0.40, release_ms=12),
        tone(NOTES["E5"], 55, volume=0.40, release_ms=12),
        tone(NOTES["G5"], 55, volume=0.40, release_ms=12),
        tone(NOTES["C6"], 55, volume=0.40, release_ms=12),
        tone(NOTES["E6"], 150, volume=0.42, release_ms=110),
    )


def _dispense() -> list[float]:
    """One quarter hitting the tray. Plays once per coin, so keep it short."""
    return layer(
        noise(60, volume=0.34, release_ms=52, colour=2),
        tone(NOTES["E6"], 55, shape="triangle", volume=0.22, release_ms=45, bend=0.75),
    )


def _jam() -> list[float]:
    """Two-tone alarm. Deliberately unpleasant: it means the machine owes
    somebody money and cannot pay it."""
    return sequence(
        tone(440.0, 160, volume=0.45, release_ms=15, duty=0.5),
        tone(330.0, 160, volume=0.45, release_ms=15, duty=0.5),
        tone(440.0, 160, volume=0.45, release_ms=15, duty=0.5),
        tone(330.0, 260, volume=0.45, release_ms=160, duty=0.5),
    )


def _shuffle() -> list[float]:
    """A long riffle for the cut card coming up."""
    out: list[float] = []
    for _ in range(7):
        out.extend(noise(22, volume=0.20, release_ms=18, colour=3))
        out.extend(silence(18))
    return out


#: name -> builder. main.py and ui.py only ever refer to these strings.
BUILDERS = {
    "coin": _coin,
    "card": _card,
    "deal": _deal,
    "bet": _bet,
    "win": _win,
    "blackjack": _blackjack,
    "lose": _lose,
    "push": _push,
    "cashout": _cashout,
    "dispense": _dispense,
    "jam": _jam,
    "shuffle": _shuffle,
}


def build_library(volume: float = 1.0) -> dict[str, bytes]:
    """Render every sound to PCM. Called once, at startup.

    A few hundred milliseconds of arithmetic on a Pi, done while the display is
    still being set up, and after that playing a sound is a buffer copy.
    """
    return {name: to_pcm(builder(), volume) for name, builder in BUILDERS.items()}

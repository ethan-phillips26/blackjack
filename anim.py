"""
Easing curves and timing helpers -- the shapes the animations move along.

NO pygame. NO gpiozero. NO clock of its own: every helper is handed `now_ms`
by the caller, the same monotonic milliseconds hardware.now_ms() produces. So
animation state is a pure function of time and can be unit-tested frame by
frame exactly like the payout state machine, and a dropped frame can never
leave a tween stuck half-way.

Nothing in here knows what a card is. ui.py owns the geometry and the pixels;
this file owns the motion.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable


def clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


# ---------------------------------------------------------------------------
# Easing curves. All map 0..1 -> 0..1 (ease_out_back overshoots past 1 and
# comes back, which is exactly what makes it feel like an arcade).
# ---------------------------------------------------------------------------


def linear(t: float) -> float:
    return t


def ease_in_quad(t: float) -> float:
    return t * t


def ease_out_quad(t: float) -> float:
    return 1.0 - (1.0 - t) ** 2


def ease_in_cubic(t: float) -> float:
    return t * t * t


def ease_out_cubic(t: float) -> float:
    return 1.0 - (1.0 - t) ** 3


def ease_in_out_cubic(t: float) -> float:
    if t < 0.5:
        return 4.0 * t * t * t
    return 1.0 - (-2.0 * t + 2.0) ** 3 / 2.0


def ease_out_back(t: float, overshoot: float = 1.9) -> float:
    """Overshoots the target and settles back -- a card landing with weight."""
    c3 = overshoot + 1.0
    u = t - 1.0
    return 1.0 + c3 * u * u * u + overshoot * u * u


def ease_out_bounce(t: float) -> float:
    n, d = 7.5625, 2.75
    if t < 1 / d:
        return n * t * t
    if t < 2 / d:
        t -= 1.5 / d
        return n * t * t + 0.75
    if t < 2.5 / d:
        t -= 2.25 / d
        return n * t * t + 0.9375
    t -= 2.625 / d
    return n * t * t + 0.984375


# ---------------------------------------------------------------------------
# Tween
# ---------------------------------------------------------------------------


@dataclass
class Tween:
    """One timed 0->1 ramp. `start_ms` in the future IS the delay -- that is
    how the deal stagger works, without a queue or a callback anywhere."""

    start_ms: int
    duration_ms: int
    ease: Callable[[float], float] = ease_out_cubic

    def progress(self, now_ms: int) -> float:
        """Un-eased 0..1. A zero-length tween is finished the moment it starts,
        which is what makes ANIMATIONS_ENABLED = False collapse to a snap."""
        if self.duration_ms <= 0:
            return 1.0 if now_ms >= self.start_ms else 0.0
        return clamp01((now_ms - self.start_ms) / self.duration_ms)

    def value(self, now_ms: int) -> float:
        return self.ease(self.progress(now_ms))

    def at(self, now_ms: int, a: float, b: float) -> float:
        return lerp(a, b, self.value(now_ms))

    def started(self, now_ms: int) -> bool:
        return now_ms >= self.start_ms

    def done(self, now_ms: int) -> bool:
        return now_ms >= self.start_ms + max(0, self.duration_ms)

    @property
    def end_ms(self) -> int:
        return self.start_ms + max(0, self.duration_ms)


# ---------------------------------------------------------------------------
# Cyclic helpers -- these never end, so nothing owns them
# ---------------------------------------------------------------------------


def wave(now_ms: int, period_ms: int, phase: float = 0.0) -> float:
    """Smooth -1..1 sine. `phase` is in turns, so 0.25 == a quarter cycle."""
    if period_ms <= 0:
        return 0.0
    return math.sin(2.0 * math.pi * ((now_ms / period_ms) + phase))


def pulse(now_ms: int, period_ms: int, phase: float = 0.0) -> float:
    """Same thing mapped to 0..1, for brightness and scale."""
    return (wave(now_ms, period_ms, phase) + 1.0) * 0.5


def blink(now_ms: int, period_ms: int, duty: float = 0.5) -> bool:
    """Hard on/off. Cheaper to read at a distance than a fade, and it is what
    every real cabinet does with INSERT COIN."""
    if period_ms <= 0:
        return True
    return (now_ms % period_ms) < period_ms * duty


def decaying_shake(now_ms: int, start_ms: int, duration_ms: int, amplitude: float,
                   cycles: float = 3.5) -> float:
    """Offset for a knock that rattles and dies out. 0.0 once it's over."""
    if duration_ms <= 0:
        return 0.0
    t = clamp01((now_ms - start_ms) / duration_ms)
    if t <= 0.0 or t >= 1.0:
        return 0.0
    return amplitude * (1.0 - t) * math.sin(2.0 * math.pi * cycles * t)


def mix_color(a: tuple[int, int, int], b: tuple[int, int, int], t: float
              ) -> tuple[int, int, int]:
    t = clamp01(t)
    return (
        int(lerp(a[0], b[0], t)),
        int(lerp(a[1], b[1], t)),
        int(lerp(a[2], b[2], t)),
    )


# ---------------------------------------------------------------------------
# Rolling counter
# ---------------------------------------------------------------------------


@dataclass
class RollingCounter:
    """A number that runs up to its new value instead of jumping.

    The DISPLAYED value only -- bank.py's integer quarters are the truth, and
    this must never be read back into the accounting path.
    """

    value: int = 0
    duration_ms: int = 260
    _from: float = field(default=0.0, init=False)
    _tween: Tween | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._from = float(self.value)

    def set(self, target: int, now_ms: int) -> None:
        if target == self.value:
            return
        self._from = self.current(now_ms)
        self.value = target
        self._tween = Tween(now_ms, self.duration_ms, ease_out_cubic)

    def snap(self, target: int) -> None:
        self.value = target
        self._from = float(target)
        self._tween = None

    def current(self, now_ms: int) -> float:
        if self._tween is None:
            return float(self.value)
        if self._tween.done(now_ms):
            self._tween = None
            return float(self.value)
        return self._tween.at(now_ms, self._from, float(self.value))

    def display(self, now_ms: int) -> int:
        """Rounded for drawing. Always lands exactly on the true value."""
        return int(round(self.current(now_ms)))

    def rolling(self, now_ms: int) -> bool:
        return self._tween is not None and not self._tween.done(now_ms)

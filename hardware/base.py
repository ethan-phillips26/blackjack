"""
The abstract hardware boundary.

Everything above this line (game.py, bank.py, ui.py, main.py) is written once
and does not know whether it is talking to a Raspberry Pi or a keyboard.
Everything below it (real.py, mock.py) is the only code allowed to touch GPIO.

Shape of the contract
---------------------
INPUTS are pull-based: the backend collects edges whenever they happen (on a
gpiozero callback thread for real hardware, inside the pygame event loop for
mock) and parks them in a thread-safe queue. The main loop drains that queue
once per frame with poll_events(). No callbacks fire into game code, so game
code never needs a lock.

OUTPUT is a single dumb primitive: set_solenoid(True/False). The backend does
NOT know about payout pacing, jam retries, or how many coins are owed -- that
state machine is pure logic and lives in bank.py, driven by a monotonic clock
so it can be unit-tested without any hardware or sleeps. This is deliberate:
the money-critical timing logic is the part most worth testing, so it must not
be trapped inside a GPIO backend.
"""

from __future__ import annotations

import queue
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto

import config


def now_ms() -> int:
    """Monotonic milliseconds. The single clock for the whole program.

    Monotonic, not wall clock: an NTP step must never make a coin look like it
    fell before it was dispensed.
    """
    return time.monotonic_ns() // 1_000_000


class EventType(Enum):
    """Everything the outside world can tell the game."""

    COIN_INSERTED = auto()  # a whole quarter was accepted (pulses regrouped)
    BUTTON_PRESSED = auto()  # a debounced press of a logical button
    COIN_DROP_DETECTED = auto()  # IR beam broken: one coin left the chute
    QUIT_REQUESTED = auto()  # mock only: user closed the window / pressed ESC


@dataclass(frozen=True)
class HardwareEvent:
    type: EventType
    # Set for BUTTON_PRESSED: one of config.ALL_BUTTONS.
    button: str | None = None
    # Monotonic milliseconds when the backend observed it.
    timestamp_ms: int = 0


class Hardware(ABC):
    """Abstract base for both backends. Concrete queue plumbing is shared."""

    #: Human-readable name for the splash screen / logs, e.g. "mock", "pi".
    name: str = "abstract"

    #: True when an IR break-beam is fitted AND enabled, i.e. dispensing runs
    #: closed-loop and each coin must be confirmed. False = open loop.
    has_drop_sensor: bool = False

    #: True when the backend accepts simulated input from the keyboard, so ui.py
    #: knows whether to show the test-key legend and forward keys to it.
    accepts_simulated_input: bool = False

    def __init__(self) -> None:
        self._events: queue.Queue[HardwareEvent] = queue.Queue()
        self._solenoid_on = False
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    @abstractmethod
    def start(self) -> None:
        """Claim pins / initialise. Called once before the main loop."""

    @abstractmethod
    def close(self) -> None:
        """Release pins. MUST de-energize the solenoid. Safe to call twice."""

    def __enter__(self) -> "Hardware":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- inputs ------------------------------------------------------------

    def poll_events(self) -> list[HardwareEvent]:
        """Drain and return everything observed since the last call.

        Never blocks. Safe to call every frame.
        """
        drained: list[HardwareEvent] = []
        while True:
            try:
                drained.append(self._events.get_nowait())
            except queue.Empty:
                return drained

    def _emit(self, event: HardwareEvent) -> None:
        """Backends call this, possibly from a non-main thread."""
        self._events.put(event)

    def update(self, now: int) -> None:
        """Per-frame hook. Default no-op; backends use it for timed housekeeping
        (flushing a stale pulse group, firing simulated coin drops)."""

    # -- simulated input ---------------------------------------------------
    #
    # main.py forwards keyboard activity here for BOTH backends, so it never
    # has to branch on which one is live.

    def inject_button(self, button: str) -> None:
        """Keyboard mirror of an arcade button.

        Allowed on real hardware too -- a USB keyboard is genuinely useful for
        servicing a machine, and pressing HIT costs nothing.
        """
        if button not in config.ALL_BUTTONS:
            return
        self._emit(
            HardwareEvent(EventType.BUTTON_PRESSED, button=button, timestamp_ms=now_ms())
        )

    def simulate_coin_insert(self, count: int = 1) -> None:
        """Test key: pretend the acceptor took a quarter.

        No-op unless the backend opts in. This MUST stay inert on real hardware
        -- a keystroke that mints credits is a free-money bug, not a debug aid.
        """

    def simulate_coin_drop(self) -> None:
        """Test key: pretend the IR beam saw a coin fall. Inert on real hardware."""

    def toggle_simulated_jam(self) -> bool:
        """Test key: stop auto-confirming drops so the jam path can be exercised.

        Returns the new "coins will drop" state. Inert on real hardware.
        """
        return True

    # -- output ------------------------------------------------------------

    @abstractmethod
    def set_solenoid(self, energized: bool) -> None:
        """Energize / de-energize the dispenser coil.

        Implementations must honour config.SOLENOID_ACTIVE_HIGH and must fail
        SAFE: any error, shutdown, or unhandled exception leaves the coil OFF.
        """

    @property
    def solenoid_energized(self) -> bool:
        return self._solenoid_on

    # -- optional -----------------------------------------------------------

    def set_coin_acceptor_enabled(self, enabled: bool) -> None:
        """Inhibit the acceptor (CH-926 has an inhibit line).

        Default is a no-op. Useful later to refuse coins during a jam.
        EXTENSION POINT -- not wired to a pin yet.
        """

    def describe(self) -> str:
        """One-line summary for the boot log."""
        loop = "closed-loop" if self.has_drop_sensor else "open-loop"
        return f"{self.name} hardware ({loop} dispenser)"


# ---------------------------------------------------------------------------
# Shared, testable input conditioning
# ---------------------------------------------------------------------------
#
# Both backends need identical debounce and pulse-grouping behaviour, and both
# are easier to trust if that behaviour is pure functions of (edge, timestamp)
# rather than something you can only verify with a real coin acceptor. These
# helpers take an explicit `now_ms` so tests can drive them frame by frame.


class Debouncer:
    """Swallows edges that arrive too soon after the previous accepted one."""

    def __init__(self, interval_ms: int) -> None:
        self.interval_ms = interval_ms
        self._last_ms: int | None = None

    def accept(self, now_ms: int) -> bool:
        if self._last_ms is not None and now_ms - self._last_ms < self.interval_ms:
            return False
        self._last_ms = now_ms
        return True


class PulseAccumulator:
    """Turns a stream of acceptor pulses into whole-coin counts.

    With pulses_per_coin == 1 every debounced pulse is a coin. With > 1, pulses
    landing within `window_ms` of each other are grouped, and the coin is only
    reported once the full count arrives; a partial group that goes quiet is
    dropped by flush() rather than being counted as a coin. Erring toward
    dropping a partial group is intentional -- inventing a coin from electrical
    noise is worse than making someone re-insert a quarter.
    """

    def __init__(
        self,
        pulses_per_coin: int = config.COIN_PULSES_PER_COIN,
        window_ms: int = config.COIN_PULSE_GROUP_WINDOW_MS,
        debounce_ms: int = config.COIN_PULSE_DEBOUNCE_MS,
    ) -> None:
        if pulses_per_coin < 1:
            raise ValueError("pulses_per_coin must be >= 1")
        self.pulses_per_coin = pulses_per_coin
        self.window_ms = window_ms
        self._debouncer = Debouncer(debounce_ms)
        self._pending = 0
        self._last_pulse_ms: int | None = None

    def pulse(self, now_ms: int) -> int:
        """Feed one raw edge. Returns how many whole coins that completed."""
        if not self._debouncer.accept(now_ms):
            return 0
        if (
            self._last_pulse_ms is not None
            and now_ms - self._last_pulse_ms > self.window_ms
        ):
            self._pending = 0  # previous group timed out -- abandon it
        self._last_pulse_ms = now_ms
        self._pending += 1
        coins, self._pending = divmod(self._pending, self.pulses_per_coin)
        return coins

    def flush(self, now_ms: int) -> None:
        """Call periodically: discards a stale, incomplete pulse group."""
        if (
            self._last_pulse_ms is not None
            and now_ms - self._last_pulse_ms > self.window_ms
        ):
            self._pending = 0
            self._last_pulse_ms = None

"""
Mock backend: a whole arcade cabinet made of keyboard keys.

No GPIO, no gpiozero, nothing to install. Every physical event the machine can
see has a key that fakes it, so the full coin-in -> play -> cash-out loop --
including dispenser jams -- can be exercised on a laptop.

Dispenser actuations are printed to the console rather than pushing a slide.
"""

from __future__ import annotations

import config
from hardware.base import EventType, Hardware, HardwareEvent, PulseAccumulator, now_ms


class MockHardware(Hardware):
    name = "mock"
    accepts_simulated_input = True

    def __init__(self, has_drop_sensor: bool = config.USE_DROP_SENSOR) -> None:
        super().__init__()
        self.has_drop_sensor = has_drop_sensor
        # Real pulse-grouping logic runs here too, so the acceptor path the PC
        # exercises is the same code the Pi runs -- only the edge source differs.
        self._pulses = PulseAccumulator()
        #: Flipped by the jam test key. False => actuations produce no coin.
        self.coins_will_drop = True
        #: now_ms at which a simulated coin should break the beam, or None.
        self._pending_drop_ms: int | None = None
        self._coins_dispensed = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        print(f"[mock] {self.describe()}")
        print(
            f"[mock] test keys: '{config.KEY_TEST_INSERT_COIN}' insert quarter, "
            f"'{config.KEY_TEST_COIN_DROP}' coin drop, "
            f"'{config.KEY_TEST_JAM_TOGGLE}' toggle jam"
        )

    def close(self) -> None:
        if self._solenoid_on:
            self.set_solenoid(False)
        print(f"[mock] shutting down; {self._coins_dispensed} coins dispensed this run")

    # -- output ------------------------------------------------------------

    def set_solenoid(self, energized: bool) -> None:
        if energized == self._solenoid_on:
            return  # only log transitions, not 30 frames of "still on"
        self._solenoid_on = energized
        if energized:
            self._coins_dispensed += 1
            print(f"[mock] SOLENOID ON   -- actuation #{self._coins_dispensed}")
            # Arm the fake coin now: on real hardware the coin can break the
            # beam while the coil is still energized, and the payout FSM has to
            # cope with that ordering.
            if config.MOCK_AUTO_CONFIRM_DROPS and self.coins_will_drop:
                self._pending_drop_ms = now_ms() + config.MOCK_AUTO_CONFIRM_DELAY_MS
        else:
            print("[mock] SOLENOID OFF  -- slide returning")

    # -- per-frame ---------------------------------------------------------

    def update(self, now: int) -> None:
        self._pulses.flush(now)
        if self._pending_drop_ms is not None and now >= self._pending_drop_ms:
            self._pending_drop_ms = None
            self._emit(HardwareEvent(EventType.COIN_DROP_DETECTED, timestamp_ms=now))
            print("[mock] coin drop detected (auto)")

    # -- simulated input ---------------------------------------------------

    def simulate_coin_insert(self, count: int = 1) -> None:
        """Feed real pulses through the real accumulator, not a shortcut."""
        # One running stamp across the whole burst: restarting it per coin puts
        # every pulse in the same millisecond, where the debouncer correctly
        # discards all but the first and count > 1 quietly loses coins.
        stamp = now_ms()
        for _ in range(count):
            for _pulse in range(config.COIN_PULSES_PER_COIN):
                coins = self._pulses.pulse(stamp)
                for _coin in range(coins):
                    self._emit(
                        HardwareEvent(EventType.COIN_INSERTED, timestamp_ms=stamp)
                    )
                    print("[mock] quarter accepted")
                stamp += config.COIN_PULSE_DEBOUNCE_MS + 5  # clear the debouncer

    def simulate_coin_drop(self) -> None:
        self._pending_drop_ms = None  # a manual drop supersedes the armed one
        self._emit(HardwareEvent(EventType.COIN_DROP_DETECTED, timestamp_ms=now_ms()))
        print("[mock] coin drop detected (manual)")

    def toggle_simulated_jam(self) -> bool:
        self.coins_will_drop = not self.coins_will_drop
        if not self.coins_will_drop:
            self._pending_drop_ms = None
        state = "will drop" if self.coins_will_drop else "WILL NOT DROP (jam sim)"
        print(f"[mock] coins {state}")
        return self.coins_will_drop

    def request_quit(self) -> None:
        self._emit(HardwareEvent(EventType.QUIT_REQUESTED, timestamp_ms=now_ms()))

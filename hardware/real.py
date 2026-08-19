"""
Real backend: Raspberry Pi GPIO via gpiozero.

gpiozero is imported INSIDE the class, never at module scope, so that importing
this file on a PC (as hardware/__init__.py may do while probing) cannot fail.

SAFETY -- read hardware/../README.md before wiring anything:
  * The solenoid pin drives a MOSFET gate or a relay input. It NEVER carries
    coil current. The coil needs its own supply and a flyback diode.
  * The CH-926 is a 12V part; its pulse output must be level-shifted or
    opto-isolated down to 3.3V before it reaches a Pi pin.
"""

from __future__ import annotations

import config
from hardware.base import EventType, Hardware, HardwareEvent, PulseAccumulator, now_ms


class RealHardware(Hardware):
    name = "pi"
    accepts_simulated_input = False  # keyboard must not be able to mint coins

    def __init__(self, has_drop_sensor: bool = config.USE_DROP_SENSOR) -> None:
        super().__init__()
        self.has_drop_sensor = has_drop_sensor
        self._pulses = PulseAccumulator()
        self._buttons: dict[str, object] = {}
        self._coin_input = None
        self._ir_sensor = None
        self._solenoid = None
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        # Imported here, not at module top level: a PC build has no gpiozero.
        from gpiozero import Button, DigitalInputDevice, OutputDevice

        # Solenoid driver. initial_value=False => coil de-energized at boot,
        # which matters because a crash-and-restart must not leave it latched.
        self._solenoid = OutputDevice(
            config.PIN_SOLENOID,
            active_high=config.SOLENOID_ACTIVE_HIGH,
            initial_value=False,
        )
        self._solenoid_on = False

        # Coin acceptor pulse line. Open-collector outputs idle high and pull
        # LOW on a pulse, so pull_up mirrors COIN_PULSE_ACTIVE_LOW.
        self._coin_input = DigitalInputDevice(
            config.PIN_COIN_ACCEPTOR,
            pull_up=config.COIN_PULSE_ACTIVE_LOW,
            bounce_time=config.COIN_PULSE_DEBOUNCE_MS / 1000.0,
        )
        self._coin_input.when_activated = self._on_coin_pulse

        if self.has_drop_sensor:
            self._ir_sensor = DigitalInputDevice(
                config.PIN_IR_SENSOR,
                pull_up=config.IR_SENSOR_ACTIVE_LOW,
                bounce_time=config.IR_SENSOR_DEBOUNCE_MS / 1000.0,
            )
            self._ir_sensor.when_activated = self._on_beam_break

        for name, pin in config.BUTTON_PINS.items():
            button = Button(
                pin,
                pull_up=config.BUTTON_PULL_UP,
                bounce_time=config.BUTTON_DEBOUNCE_MS / 1000.0,
            )
            # Bind `name` per-iteration; gpiozero fires this on its own thread.
            button.when_pressed = lambda _dev=None, _name=name: self._on_button(_name)
            self._buttons[name] = button

        print(f"[pi] {self.describe()}")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # De-energize FIRST and unconditionally: whatever else fails during
        # shutdown, the coil must not be left powered.
        try:
            if self._solenoid is not None:
                self._solenoid.off()
                self._solenoid_on = False
        finally:
            for device in (self._coin_input, self._ir_sensor, self._solenoid):
                if device is not None:
                    try:
                        device.close()
                    except Exception:  # noqa: BLE001 - shutdown must not raise
                        pass
            for button in self._buttons.values():
                try:
                    button.close()  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
            self._buttons.clear()

    # -- GPIO callbacks (these run on gpiozero threads) --------------------
    #
    # They only touch the thread-safe event queue and the accumulator, and the
    # accumulator is only ever touched from the coin callback, so no locking.

    def _on_coin_pulse(self, _device: object = None) -> None:
        stamp = now_ms()
        for _coin in range(self._pulses.pulse(stamp)):
            self._emit(HardwareEvent(EventType.COIN_INSERTED, timestamp_ms=stamp))

    def _on_beam_break(self, _device: object = None) -> None:
        self._emit(HardwareEvent(EventType.COIN_DROP_DETECTED, timestamp_ms=now_ms()))

    def _on_button(self, name: str) -> None:
        self._emit(
            HardwareEvent(EventType.BUTTON_PRESSED, button=name, timestamp_ms=now_ms())
        )

    # -- per-frame ---------------------------------------------------------

    def update(self, now: int) -> None:
        # Discard a pulse group that never completed (noise, or a coin the
        # acceptor rejected part-way).
        self._pulses.flush(now)

    # -- output ------------------------------------------------------------

    def set_solenoid(self, energized: bool) -> None:
        if self._solenoid is None or energized == self._solenoid_on:
            return
        if energized:
            self._solenoid.on()
        else:
            self._solenoid.off()
        self._solenoid_on = energized

    def set_coin_acceptor_enabled(self, enabled: bool) -> None:
        # EXTENSION POINT: the CH-926 has an inhibit input. Drive it from a
        # spare pin here to refuse coins during a jam or a payout.
        pass

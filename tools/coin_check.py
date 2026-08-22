#!/usr/bin/env python3
"""
Find out where a quarter gets lost between the acceptor and the credit meter.

The acceptor flashing its LED only proves it RECOGNISED the coin. Between that
and the game there are four more links, any one of which drops it silently:

    acceptor COIN wire -> optocoupler -> GPIO 17 edge -> gpiozero -> credit

This watches the pin directly, with no pygame and no game running, and prints
what it sees at every link. Stop the game first (`sudo systemctl stop
blackjack`) -- two processes cannot both own the pin.

    python3 tools/coin_check.py              # watch GPIO 17 as configured
    python3 tools/coin_check.py --invert     # try the opposite polarity
    python3 tools/coin_check.py --raw        # no debounce: measure true width
    python3 tools/coin_check.py --scan       # which pin IS the pulse on?

What the output tells you
------------------------
  RESTING: high      the opto is idle and the pull-up is working. Correct.
  RESTING: low       the opto is conducting all the time, or the pull-up is
                     absent and the line is floating down. Nothing can pulse.
  no edges at all    the pulse is not reaching this pin. Wiring, not software:
                     check the COIN wire, the opto, and the shared ground.
                     Try --scan to see if it landed on a different GPIO.
  edges, no coin     the pulse arrives but gpiozero's debounce eats it. The
                     width printed here is shorter than COIN_PULSE_DEBOUNCE_MS.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from hardware.base import PulseAccumulator, now_ms  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--pin",
        type=int,
        default=config.PIN_COIN_ACCEPTOR,
        help=f"BCM pin to watch (default {config.PIN_COIN_ACCEPTOR}, from config)",
    )
    parser.add_argument(
        "--invert",
        action="store_true",
        help="flip the active level: test COIN_PULSE_ACTIVE_LOW the other way "
        "without editing config.py",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="disable gpiozero's debounce, so a pulse SHORTER than "
        "COIN_PULSE_DEBOUNCE_MS still shows up and can be measured",
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="watch every free input pin at once and report any that move -- "
        "finds a pulse wire landed on the wrong GPIO",
    )
    parser.add_argument(
        "--seconds", type=float, default=60.0, help="how long to watch (default 60)"
    )
    return parser.parse_args()


def load_gpiozero():
    """Import gpiozero and report which pin factory backend it picked.

    Worth printing: on Bookworm and on the Pi 5 the old RPi.GPIO factory does
    not work, and a wrong factory fails in ways that look like dead wiring.
    """
    try:
        import gpiozero
        from gpiozero import DigitalInputDevice
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"gpiozero unavailable: {exc}\nThis tool only runs on the Pi.")

    probe = None
    try:
        probe = gpiozero.Device._default_pin_factory()  # noqa: SLF001
        factory = type(probe).__name__
    except Exception:  # noqa: BLE001
        factory = "unknown"
    print(f"gpiozero {gpiozero.__version__}, pin factory: {factory}")
    return DigitalInputDevice


def watch_one(DigitalInputDevice, args: argparse.Namespace) -> None:
    active_low = config.COIN_PULSE_ACTIVE_LOW
    if args.invert:
        active_low = not active_low

    # pull_up mirrors the active level, exactly as hardware/real.py does it.
    # An optocoupler pulls the line DOWN when it conducts, so active-low with
    # the pull-up on is the normal arrangement.
    bounce = None if args.raw else config.COIN_PULSE_DEBOUNCE_MS / 1000.0

    device = DigitalInputDevice(args.pin, pull_up=active_low, bounce_time=bounce)

    print(
        f"\nwatching BCM {args.pin}  "
        f"pull_up={active_low}  "
        f"active={'LOW' if active_low else 'HIGH'}  "
        f"debounce={'OFF (raw)' if args.raw else f'{config.COIN_PULSE_DEBOUNCE_MS}ms'}"
    )

    # Settle, then report where the line rests. This single number catches most
    # miswiring before a coin is ever dropped.
    time.sleep(0.2)
    raw_high = bool(device.pin.state)
    print(f"RESTING: {'high' if raw_high else 'low'} (device.value={device.value})")
    if device.is_active:
        print(
            "  !! the line is ALREADY in its active state at rest. It cannot\n"
            "     pulse from here. Either the polarity is backwards (try\n"
            "     --invert) or the opto is conducting continuously."
        )

    # The same accumulator the game uses, fed the same way, so a pulse that
    # reaches here and still does not become a coin is a config problem
    # (COIN_PULSES_PER_COIN) rather than a wiring one.
    pulses = PulseAccumulator()
    counts = {"edges": 0, "coins": 0}
    rising_ms: list[int] = []

    def on_active(_dev=None) -> None:
        stamp = now_ms()
        rising_ms.append(stamp)
        counts["edges"] += 1
        coins = pulses.pulse(stamp)
        counts["coins"] += coins
        note = f"  -> COIN #{counts['coins']} credited" if coins else "  (partial)"
        print(f"[{counts['edges']:3d}] pulse ACTIVE  {note}")

    def on_inactive(_dev=None) -> None:
        if rising_ms:
            width = now_ms() - rising_ms[-1]
            print(f"      pulse released after {width}ms", end="")
            if not args.raw:
                print()
            elif width < config.COIN_PULSE_DEBOUNCE_MS:
                print(
                    f"   !! SHORTER than COIN_PULSE_DEBOUNCE_MS "
                    f"({config.COIN_PULSE_DEBOUNCE_MS}ms) -- the game's debounce "
                    f"is throwing this pulse away. Lower that setting, or widen "
                    f"the acceptor's pulse."
                )
            else:
                print()

    device.when_activated = on_active
    device.when_deactivated = on_inactive

    print("\nDrop quarters in now. Ctrl-C to stop.\n")
    deadline = time.monotonic() + args.seconds
    try:
        while time.monotonic() < deadline:
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        device.close()

    print(f"\n{counts['edges']} edge(s), {counts['coins']} whole coin(s).")
    if counts["edges"] == 0:
        print(
            "No edges at all. The pulse is not arriving on this pin:\n"
            "  * is the acceptor's COIN wire connected, and is the acceptor set\n"
            "    to pulse output rather than a level?\n"
            "  * does the opto's output share GROUND with the Pi?\n"
            "  * run with --invert, then with --scan."
        )
    elif counts["coins"] == 0:
        print(
            f"Edges arrived but no coin completed. COIN_PULSES_PER_COIN is "
            f"{config.COIN_PULSES_PER_COIN} -- set it to match what the acceptor "
            f"actually sends."
        )


def scan(DigitalInputDevice, args: argparse.Namespace) -> None:
    """Watch every safe input pin and report the ones that move."""
    # PIN_SOLENOID is excluded DELIBERATELY: claiming an output pin as an input
    # leaves the MOSFET gate floating, and a floating gate can switch the coil
    # on and hold it there. Never scan the pin that drives the coil.
    skip = {config.PIN_SOLENOID}
    candidates = [p for p in range(2, 28) if p not in skip]

    devices = {}
    for pin in candidates:
        try:
            devices[pin] = DigitalInputDevice(pin, pull_up=True)
        except Exception:  # noqa: BLE001 - pin busy or reserved; not interesting
            continue

    time.sleep(0.2)
    resting = {pin: bool(dev.pin.state) for pin, dev in devices.items()}
    moved: dict[int, int] = {}

    print(f"\nscanning {len(devices)} pins (solenoid pin {config.PIN_SOLENOID} "
          f"excluded). Drop a quarter. Ctrl-C to stop.\n")
    deadline = time.monotonic() + args.seconds
    try:
        while time.monotonic() < deadline:
            for pin, dev in devices.items():
                if bool(dev.pin.state) != resting[pin]:
                    moved[pin] = moved.get(pin, 0) + 1
                    resting[pin] = bool(dev.pin.state)
                    print(f"  BCM {pin:2d} changed ({moved[pin]} so far)")
            time.sleep(0.001)
    except KeyboardInterrupt:
        pass
    finally:
        for dev in devices.values():
            dev.close()

    if moved:
        print("\nactivity on: " + ", ".join(f"BCM {p} ({n})" for p, n in moved.items()))
        print(f"config.PIN_COIN_ACCEPTOR is currently {config.PIN_COIN_ACCEPTOR}.")
    else:
        print("\nNothing moved on any pin. The pulse is not reaching the Pi at all.")


def main() -> int:
    args = parse_args()
    DigitalInputDevice = load_gpiozero()
    if args.scan:
        scan(DigitalInputDevice, args)
    else:
        watch_one(DigitalInputDevice, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

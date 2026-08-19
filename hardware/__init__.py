"""
Backend auto-detection and factory.

The rest of the program calls create_hardware() and then only ever talks to the
abstract Hardware interface -- it never learns which backend it got.
"""

from __future__ import annotations

import os

from hardware.base import (
    Debouncer,
    EventType,
    Hardware,
    HardwareEvent,
    PulseAccumulator,
    now_ms,
)

__all__ = [
    "Debouncer",
    "EventType",
    "Hardware",
    "HardwareEvent",
    "PulseAccumulator",
    "create_hardware",
    "is_raspberry_pi",
    "now_ms",
]

DEVICE_TREE_MODEL = "/proc/device-tree/model"


def is_raspberry_pi() -> bool:
    """Two independent checks, cheapest first.

    The device-tree model string is the reliable one. Falling back to "can I
    import gpiozero" is deliberately secondary: the package installs fine on a
    PC, so on its own it would produce false positives.
    """
    try:
        with open(DEVICE_TREE_MODEL, "rb") as handle:
            # NUL-terminated in device tree, hence the strip.
            model = handle.read().decode("utf-8", "ignore").strip("\x00").strip()
        if "raspberry pi" in model.lower():
            return True
    except OSError:
        pass

    # Some distros/containers hide the device tree. Require BOTH gpiozero and
    # an ARM machine before believing it.
    if os.uname().machine.startswith(("arm", "aarch64")):
        try:
            import gpiozero  # noqa: F401
        except Exception:  # noqa: BLE001 - a broken install is a "no"
            return False
        return True
    return False


def detect_backend_name() -> str:
    return "real" if is_raspberry_pi() else "mock"


def create_hardware(force: str | None = None) -> Hardware:
    """Build the backend.

    force: "real" / "mock" from the --real / --mock flags, or None to detect.
    Falls back to mock if the real backend cannot be constructed, so a bad Pi
    install leaves you with a playable machine instead of a traceback.
    """
    choice = force or detect_backend_name()

    if choice == "real":
        try:
            # Probe here rather than letting start() fail later: by then the
            # display is already up, and a half-initialised money machine is a
            # much worse thing to debug than a message at launch.
            import gpiozero  # noqa: F401

            from hardware.real import RealHardware

            return RealHardware()
        except Exception as exc:  # noqa: BLE001
            if force == "real":
                raise  # explicitly asked for it: don't paper over the failure
            print(f"[hardware] real backend unavailable ({exc}); using mock")

    from hardware.mock import MockHardware

    return MockHardware()

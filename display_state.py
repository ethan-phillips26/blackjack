"""
Remembers the per-television picture geometry between runs.

The position and size adjustments (arrow keys, `-` and `=`) are the software
half of a television service menu: you tune them once against the tube you
actually own. Until now they lived only in memory, and the only way to keep
them was to paste the printed numbers into config.py by hand -- fine for a
cabinet tuned once and bolted shut, tedious for one still on the bench.

So this file remembers them. The rules it follows are deliberately different
from bank.py's, because this is not money:

  * config.py stays the DEFAULT. Delete state/display.json and the machine
    behaves exactly as config.py says it should, which is what keeps the file
    the documented source of truth and makes "reset it" a deletion rather than
    an incantation.
  * A missing, corrupt, or hand-mangled file is not an error. It falls back to
    the config defaults silently-ish; a cabinet must boot.
  * No fsync, and nothing is written while the game is running. Losing a
    display tweak to a power cut costs one keypress. Blocking the shutdown of a
    money machine on a failing SD card costs considerably more, and bank.py is
    already the thing that has earned the right to do that.

Pure stdlib + config, like game.py and bank.py -- no pygame here, so it is
testable on a machine with no graphics at all.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass

import config


@dataclass(frozen=True)
class Geometry:
    """Where the picture sits and how big it is, in 640x480 pixels."""

    offset_x: int
    offset_y: int
    scale: float

    @classmethod
    def defaults(cls) -> "Geometry":
        return cls(
            offset_x=config.IMAGE_OFFSET_X,
            offset_y=config.IMAGE_OFFSET_Y,
            scale=config.IMAGE_SCALE,
        )

    @classmethod
    def coerced(cls, offset_x: object, offset_y: object, scale: object) -> "Geometry":
        """Build one from untrusted values, clamped into range.

        The file is plain JSON in a directory a person is invited to poke at,
        so every field is treated as hostile. A scale of 0, or an offset of
        99999, would move the picture off the tube entirely -- which on a
        cabinet is indistinguishable from a machine that no longer boots.
        """
        max_x = config.SCREEN_WIDTH // 2
        max_y = config.SCREEN_HEIGHT // 2
        return cls(
            offset_x=_clamp_int(offset_x, -max_x, max_x, config.IMAGE_OFFSET_X),
            offset_y=_clamp_int(offset_y, -max_y, max_y, config.IMAGE_OFFSET_Y),
            scale=_clamp_float(
                scale, config.IMAGE_SCALE_MIN, config.IMAGE_SCALE_MAX, config.IMAGE_SCALE
            ),
        )


def _clamp_int(value: object, low: int, high: int, fallback: int) -> int:
    try:
        # bool is an int subclass and `True` is not a coordinate.
        if isinstance(value, bool):
            raise TypeError
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def _clamp_float(value: object, low: float, high: float, fallback: float) -> float:
    try:
        if isinstance(value, bool):
            raise TypeError
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if number != number:  # NaN survives every comparison; catch it explicitly
        return fallback
    return round(max(low, min(high, number)), 3)


def load(path: str | None = None) -> Geometry:
    """The saved geometry, or config.py's defaults if there isn't one."""
    path = path or config.DISPLAY_STATE_PATH
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return Geometry.defaults()
    except (OSError, json.JSONDecodeError) as exc:
        # Worth one line in the log: a cabinet that quietly forgets its
        # geometry every boot is a puzzle, and this is the only clue.
        print(f"[display] ignoring unreadable {path}: {exc}")
        return Geometry.defaults()

    if not isinstance(data, dict):
        print(f"[display] ignoring {path}: expected an object")
        return Geometry.defaults()

    defaults = Geometry.defaults()
    return Geometry.coerced(
        data.get("offset_x", defaults.offset_x),
        data.get("offset_y", defaults.offset_y),
        data.get("scale", defaults.scale),
    )


def save(geometry: Geometry, path: str | None = None) -> bool:
    """Write the geometry. Returns whether it landed.

    Atomic (temp file + os.replace) so an interrupted write cannot leave a torn
    file that the next boot then refuses -- but deliberately WITHOUT fsync, and
    it never raises. This is called from the shutdown path of a machine whose
    storage may be failing; a display setting is not worth blocking on, and is
    certainly not worth turning a clean exit into a traceback.
    """
    path = path or config.DISPLAY_STATE_PATH
    tmp_path = f"{path}.tmp.{os.getpid()}"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(asdict(geometry), handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_path, path)
        return True
    except OSError as exc:
        print(f"[display] could not save {path}: {exc}")
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return False

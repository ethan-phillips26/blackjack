#!/usr/bin/env python3
"""
Time the storage the money lives on.

The game blocks on every balance change: bank.py writes the state atomically
(temp file, fsync, rename, fsync the directory), because a coin that is only in
RAM is a coin a power cut erases. On a healthy card that costs a few
milliseconds. On a tired one it can cost SECONDS, and the machine appears to
freeze after a hand.

This isolates that cost from the game entirely -- no pygame, no GPIO, no Pi
required -- so you can tell a slow card apart from slow graphics:

    python3 tools/disk_check.py
    python3 tools/disk_check.py --count 50 --dir /mnt/usb-ssd

It writes to a scratch file alongside your real state and deletes it after.
bank.json is never touched.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from bank import BankState, _atomic_write_json


def _plain_write(path: str, payload: dict) -> None:
    """The same bytes with no durability at all -- the control group."""
    import json

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)


def time_writes(path: str, payload: dict, count: int, writer) -> list[float]:
    samples = []
    for _ in range(count):
        started = time.perf_counter()
        writer(path, payload)
        samples.append((time.perf_counter() - started) * 1000.0)
    return samples


def report(label: str, samples: list[float]) -> tuple[float, float]:
    ordered = sorted(samples)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    median = statistics.median(ordered)
    print(
        f"  {label:<28} median {median:8.2f}ms   "
        f"p95 {p95:8.2f}ms   worst {max(ordered):9.2f}ms"
    )
    return median, max(ordered)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--count", type=int, default=25,
        help="writes per test; raise it to catch intermittent stalls",
    )
    parser.add_argument(
        "--dir",
        default=config.STATE_DIR,
        help="directory to test (default: the machine's state dir)",
    )
    args = parser.parse_args(argv)

    os.makedirs(args.dir, exist_ok=True)
    scratch = os.path.join(args.dir, "disk_check.scratch.json")
    payload = BankState(balance_quarters=7, lifetime_coins_in=1234).to_dict()

    print(f"testing {args.dir}  ({args.count} writes each)\n")
    try:
        plain, _plain_worst = report(
            "plain write (no fsync)",
            time_writes(scratch, payload, args.count, _plain_write),
        )
        atomic, atomic_worst = report(
            "atomic write (as the game)",
            time_writes(scratch, payload, args.count, _atomic_write_json),
        )
    finally:
        for leftover in (scratch, f"{scratch}.tmp.{os.getpid()}"):
            try:
                os.remove(leftover)
            except OSError:
                pass

    print()
    # Judge on the WORST case as well as the typical one. A card that is fast
    # almost always and stalls for seconds occasionally is the classic failing
    # -- and it is exactly what "the game freezes now and then" feels like.
    # Reporting only the median would call that card healthy.
    if atomic_worst >= 1000:
        print(f"VERDICT: typical saves are fine ({atomic:.0f}ms) but the worst "
              f"was {atomic_worst:.0f}ms.")
        print("         INTERMITTENT MULTI-SECOND STALLS ARE THE PROBLEM.")
        print("         A card that is usually quick and occasionally freezes")
        print("         for seconds is a card that is wearing out: the")
        print("         controller is stalling on erase/remap. It will get")
        print("         worse, and the file it stalls on is the one holding")
        print("         the balance. Replace it, or move state/ to an SSD:")
        print("           BLACKJACK_STATE_DIR=/mnt/ssd/state python3 main.py --real")
        print()
        print("         Run this again with --count 200 to see how often it")
        print("         happens; a healthy card never does it at all.")
    elif atomic < 20:
        print(f"VERDICT: storage is fine ({atomic:.0f}ms typical, "
              f"{atomic_worst:.0f}ms worst).")
        print("         If the game still stutters it is not the disk. Look at")
        print("         --profile: 'draw' is graphics, 'gap' is the OS not")
        print("         scheduling us, and DEALER_STEP_MS /")
        print("         RESULT_DISPLAY_SECONDS set the pace of a round.")
    elif atomic < config.SLOW_WRITE_WARN_MS:
        print(f"VERDICT: {atomic:.0f}ms per save. Noticeable but survivable --")
        print("         expect a short hitch on DEAL and on a winning hand.")
    else:
        print(f"VERDICT: {atomic:.0f}ms per save. THIS is your freeze.")
        print("         Every coin, every bet and every win pays this cost, and")
        print("         the loop is stopped for all of it.")
        print()
        print("         The fsync is not optional -- it is what stops a power")
        print("         cut eating a player's balance -- so the fix is faster")
        print("         storage, not different code:")
        print("           * a better SD card (the cheap ones are terrible at")
        print("             the small synchronous writes this does), or")
        print("           * boot from a USB SSD, or")
        print("           * keep state/ on a USB SSD via BLACKJACK_STATE_DIR.")
        print()
        print("         Do NOT put state/ on a tmpfs. It would be instant and")
        print("         it would lose somebody's money the first time the plug")
        print("         is pulled, which is the whole thing this code exists")
        print("         to prevent.")
    if atomic > 10 * max(plain, 0.01):
        print()
        print(f"         (fsync accounts for ~{atomic - plain:.0f}ms of it; the")
        print("          raw write is fast, so the card is stalling on sync.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

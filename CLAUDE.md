# Claude Code prompt — CRT blackjack arcade machine

Copy everything below the line into Claude Code.

---

Build a coin-operated blackjack arcade game in Python. It runs on a **Raspberry Pi 4** driving a **composite CRT TV**, takes **US quarters** via a coin acceptor, and pays winnings out as physical quarters through a DIY coin-slide dispenser. It must **also run on a normal Linux PC** in a "mock hardware" mode so I can develop and play-test the whole thing before any electronics are wired up.

## Core architecture requirement (most important)

Separate **pure game logic** from **all I/O**. The blackjack rules engine, the deck/shoe, and the money accounting must have **zero dependencies on pygame or GPIO** so they can be unit-tested and reused. All hardware access goes through a single abstract interface with two interchangeable backends:

- `RealHardware` — Raspberry Pi, using `gpiozero`. Talks to the real coin acceptor, arcade buttons, dispenser solenoid, and optional IR sensor.
- `MockHardware` — Linux PC, no GPIO. Coin inserts, buttons, and coin-drop confirmations are simulated from the keyboard; dispenser actuations just log to the console.

Select the backend by **auto-detecting** whether we're on a Pi (e.g. check `/proc/device-tree/model` or try importing `gpiozero`), with a manual override via `--mock` / `--real` CLI flags. The rest of the program must not know or care which backend is active.

## Tech stack

- Python 3, **pygame** for graphics and keyboard input.
- `gpiozero` for GPIO on the Pi (imported only inside `RealHardware`, never at module top level, so the PC build doesn't need it installed).
- No external services, no network, no databases. Local files only.

## Display / CRT constraints

- Fullscreen **640×480**. On the PC, default to a 640×480 window with a key to toggle fullscreen.
- Design for composite video: **large, bold, high-contrast** fonts and shapes; avoid thin 1px lines (they shimmer on composite). Keep all important content inside an **overscan-safe area** — inset every edge by ~5% (about 32px) so a CRT's overscan doesn't clip it. Put the safe-area inset in config so I can tune it per TV.

## Hardware behavior

### Coin acceptor (CH-926, or similar pulse-output acceptor)
- Treat it as a digital input that emits **pulses**. Configurable **pulses-per-coin** (default **1 pulse = 1 quarter**).
- Debounce and count edges; if pulses-per-coin > 1, group pulses within a configurable time window into one coin.
- Each accepted quarter adds one quarter to the on-screen balance.

### Coin-slide dispenser (DIY, solenoid-driven)
- To pay out N quarters, actuate the solenoid **once per quarter**: energize the GPIO pin for `SOLENOID_ON_MS`, de-energize, wait `SOLENOID_RESET_MS` for the slide to return, repeat. All timings in config.
- **Optional IR break-beam sensor** at the exit chute confirms each coin actually dropped:
  - If present: after each actuation, wait up to `COIN_DROP_TIMEOUT_MS` for a beam-break. No drop → treat as a jam: retry up to `MAX_JAM_RETRIES`, then stop paying, show an on-screen "dispenser jam — coins owed: X" message, and hold the owed count.
  - If absent (open-loop mode, config flag): assume each actuation dispensed exactly one coin.

### Money integrity (this is a machine that holds people's money — treat it seriously)
- Persist the **quarter balance** to disk with **atomic writes** (write to a temp file, then `os.replace`) so a power cut can't corrupt or zero it.
- Persist any **in-progress payout**: how many quarters are still owed. On startup, if a payout was interrupted (power loss mid-dispense), **reconcile** — resume paying the remainder or surface the owed amount. Never double-pay and never silently lose coins.

## Game rules (blackjack)

- Single player vs. dealer.
- **Variable bet, 1 to 4 quarters** (max in config). Player adjusts the bet before dealing.
- **6-deck shoe**, reshuffled when it runs low (cut-card behavior; threshold in config).
- Actions: **hit** and **stand** only. No double, split, insurance, or surrender — but structure the code so these are easy to add later (leave clear extension points / TODOs).
- Dealer **stands on all 17s** (make soft-17 behavior a config flag).
- Payouts: normal win pays **1:1**, push returns the bet, **natural blackjack pays 3:2**.
- **Quarter-rounding rule (call this out clearly in code):** since payouts are physical quarters, a 3:2 blackjack on an odd bet (1 or 3 quarters) yields a half-quarter. Default: **round the blackjack bonus down to a whole quarter** (house-favorable, standard for coin machines). Implement this as one small, well-commented function/constant so I can change the rounding policy in one place.
- Keep the entire money calculation in **integer quarters** end-to-end. No floats in the accounting path.

## Controls

Physical **arcade buttons** on the Pi, mirrored by **keyboard keys** for PC testing. Define every GPIO pin and every key binding as **named constants at the top of config**. Debounce all buttons.

Button set (adjust if you have a cleaner scheme):
- **BET** — cycles the bet 1→2→3→4→1
- **DEAL** — starts a hand at the current bet
- **HIT**
- **STAND**
- **CASH OUT** — dispenses the entire balance as quarters

Keyboard mirror (suggested): `B` bet, `Enter` deal, `H` hit, `S` stand, `C` cash out. Plus **PC-only test keys**: a key to simulate inserting a quarter, and a key to simulate an IR coin-drop, so I can exercise the acceptor and dispenser logic with no hardware.

## Suggested file layout

```
blackjack/
  main.py              # entry point: arg parsing, backend selection, main loop
  config.py            # ALL constants: GPIO pins, key bindings, timings, rules, bet max, safe-area inset
  game.py              # pure blackjack rules engine (no pygame, no GPIO)
  cards.py             # card + shoe/deck model
  bank.py              # quarter balance, atomic persistence, payout accounting/reconciliation
  ui.py                # pygame rendering + input handling
  hardware/
    __init__.py        # auto-detect + backend factory
    base.py            # abstract Hardware interface
    real.py            # gpiozero backend (Pi)
    mock.py            # keyboard/no-GPIO backend (PC)
  tests/
    test_game.py       # rules + payout math
  README.md            # PC run instructions, Pi wiring + setup, systemd
  blackjack.service    # systemd unit to launch fullscreen on boot
```

## Deliverables

1. All source files above, runnable.
2. **Unit tests** for the rules engine and money math: blackjack detection, dealer draw logic, win/push/bust outcomes, and the 3:2 payout **including the odd-bet rounding**.
3. A **README** covering:
   - Running on the PC (mock mode) and the keyboard/test-key map.
   - Running on the Pi, including the note that composite output on the Pi 4 must be enabled with `enable_tvout=1` in `config.txt` and that this is a 4-pole 3.5mm AV jack (camcorder-style cable), and that not all such cables share the same pinout.
   - **Wiring notes / cautions** for each device: CH-926 is a 12V device whose pulse output must be brought safely to the Pi's 3.3V logic; the solenoid needs its own supply with a driver (logic-level MOSFET or relay) and a flyback diode, sharing ground with the Pi — the Pi GPIO only switches the driver, never the solenoid directly; the IR sensor is a simple pulled-up digital input.
   - The `systemd` install steps.

## Acceptance criteria

- On a Linux PC with only pygame installed, `python main.py` launches, auto-selects mock hardware, and I can insert quarters, adjust the bet, play full hands, win/lose/push, and cash out — all from the keyboard, with dispenser actuations printed to the console.
- The rules engine and `bank.py` import and run with **no pygame and no gpiozero present**.
- All tests pass.
- Killing the process mid-payout and restarting reconciles the owed quarters rather than losing or duplicating them.
- Nothing in the accounting path uses floating-point money.

## Out of scope (don't build these now)
- Networking, remote logging, or any cloud features.
- Double/split/insurance/surrender (leave extension points only).
- Sound (leave a clearly marked hook; the machine can be silent for now).

Start by proposing the `config.py` constants and the abstract `Hardware` interface in `base.py`, then confirm those with me before implementing the rest.

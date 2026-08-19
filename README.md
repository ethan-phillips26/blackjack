# Coin-op Blackjack

A quarter-eating blackjack machine for a Raspberry Pi 4 and a composite CRT.
Takes US quarters through a pulse-output coin acceptor, pays winnings back out
as physical quarters through a solenoid-driven coin slide.

Runs identically on a normal Linux PC in mock-hardware mode, so the whole
machine — coins in, hands played, coins out, dispenser jams — can be developed
and play-tested before a single wire is crimped.

---

## Layout

```
main.py              entry point: args, backend selection, main loop
config.py            EVERY constant: pins, keys, timings, rules, safe area
game.py              blackjack rules engine        (pure)
cards.py             card + shoe model             (pure)
bank.py              quarter balance, atomic persistence, payout FSM (pure)
ui.py                pygame rendering + keyboard   (the only pygame import)
hardware/
  __init__.py        auto-detect + backend factory
  base.py            abstract Hardware interface, debounce, pulse grouping
  real.py            gpiozero backend (Pi)
  mock.py            keyboard backend (PC)
tests/               unittest suite, no third-party deps
blackjack.service    systemd unit
```

`game.py`, `cards.py` and `bank.py` import **nothing but the standard library
and `config.py`**. They run — and are tested — on a machine with neither pygame
nor gpiozero installed. `ui.py` is the only file that imports pygame; `real.py`
is the only file that imports gpiozero, and it does so *inside* a method so
that merely importing it on a PC cannot fail.

---

## Running on a PC (mock mode)

```bash
cd blackjack
python3 -m venv .venv
.venv/bin/pip install pygame-ce      # or plain `pygame`, either works
.venv/bin/python main.py
```

It auto-detects that this is not a Pi and selects the mock backend. Force it
either way with `--mock` / `--real`. Other flags: `--fullscreen`, `--windowed`,
`--seed N` (deterministic shoe).

### Keys

| Key | Does |
| --- | --- |
| `B` | BET — cycles 1 → 2 → 3 → 4 → 1 |
| `Enter` | DEAL |
| `H` | HIT |
| `S` | STAND |
| `C` | CASH OUT — dispenses the whole balance (and doubles as jam retry) |

### Test keys (mock backend only)

| Key | Simulates |
| --- | --- |
| `Q` | inserting a quarter — fed through the real pulse-grouping code |
| `D` | the IR beam seeing a coin drop |
| `J` | toggle "coins will drop" — turn it off to force a dispenser jam |
| `F11` | fullscreen |
| `G` | draw the overscan-safe-area box |
| `Esc` | quit |

These are inert on real hardware. `Hardware.simulate_coin_insert()` is a no-op
in `RealHardware` on purpose: a keystroke that mints credits is a free-money
bug, not a debug aid.

Dispenser actuations print to the console:

```
[mock] SOLENOID ON   -- actuation #3
[mock] SOLENOID OFF  -- slide returning
[mock] coin drop detected (auto)
```

By default the mock backend auto-confirms each drop after 80 ms so cash-out
just works. Press `J` to stop confirming and watch the retry-then-jam path.

---

## Tests

```bash
cd blackjack
python3 -m unittest discover -s tests -t .
```

Deliberately `unittest`, not pytest, and deliberately runnable with the *system*
Python rather than the venv — that is the proof that the rules engine and the
money code have no GUI or GPIO dependencies.

`tests/test_game.py` covers hand values and ace demotion, natural detection
(including 3-card 21 *not* being a natural), dealer draw logic on hard and soft
17, every win/lose/push/bust outcome, and the payout table — with the 3:2
odd-bet rounding checked under all three policies.

`tests/test_bank.py` covers atomic persistence, cash-out transfer, open- and
closed-loop dispensing, jams, and crash reconciliation. The headline test is
`test_killed_at_every_instant_the_coin_count_still_adds_up`: it kills the
machine at every single moment of a 5-quarter payout, restarts, reconciles,
finishes, and asserts the coins that physically fell are never more than 5 and
never fewer than 4.

---

## How the money works

Everything is **integer quarters**, end to end. There is no float anywhere in
the accounting path, and the tests assert that.

**Payouts.** Win pays 1:1, push returns the bet, a natural pays 3:2.

**The odd-bet rounding rule.** 3:2 on an odd bet lands on half a quarter, and
there is no such coin:

| Bet | 3:2 bonus | `down` (default) | `up` | `nearest` |
| --- | --- | --- | --- | --- |
| 1 | 1.5 | **1** | 2 | 2 |
| 2 | 3 | 3 | 3 | 3 |
| 3 | 4.5 | **4** | 5 | 4 |
| 4 | 6 | 6 | 6 | 6 |

Default is `down` — the house keeps the half, which is the coin-op convention.
Change `config.BLACKJACK_ROUNDING`; the arithmetic lives in exactly one place,
`game.blackjack_bonus_quarters()`.

**Winnings go to the credit meter, not the hopper.** Coins only move when the
player presses CASH OUT.

### Crash safety

The balance is persisted with an atomic write — temp file, `fsync`, `os.replace`,
then `fsync` on the directory — so a power cut leaves either the old file or the
new one, never a half-written one.

Cash-out moves the whole balance into `owed_quarters` in a *single* write, so
the quarters are either entirely credit or entirely owed, never both and never
neither. Each coin is then bracketed by two more writes: `coil_actuating = True`
**before** the coil energizes, and `owed -= 1` **after** the drop is confirmed.

Lose power between those two and it is genuinely unknowable whether that one
quarter fell. The policy is explicit and documented in `bank.py`:

> **A coin in flight at the moment of a crash is treated as PAID.**

That makes double-paying impossible — otherwise someone could drain the hopper
by repeatedly yanking the plug. The cost is that in the worst case the player
is short exactly one quarter, and that is not silent: it is written to
`state/ledger.log` as `RECONCILE_ASSUMED_PAID` and shown on screen at startup.

`state/bank.json` is authoritative; `state/ledger.log` is an append-only audit
trail of every coin in and out. Back up the directory, don't hand-edit it while
the machine is running.

---

## Running on the Raspberry Pi

### Composite video

The Pi 4 does **not** output composite by default. In `/boot/firmware/config.txt`
(older images: `/boot/config.txt`):

```ini
enable_tvout=1
sdtv_mode=0        # 0 = NTSC (US). 2 = PAL.
sdtv_aspect=1      # 1 = 4:3
```

Reboot. Note that on the Pi 4 enabling composite disables 4K/dual-HDMI modes —
that's fine here, but it means HDMI and composite don't coexist happily, so do
your setup over SSH or with a keyboard you can drive blind.

The composite output is on the **4-pole 3.5 mm AV jack**, not a dedicated RCA
socket. You need a camcorder-style 3.5 mm → 3×RCA cable.

> ⚠️ **Not all 4-pole AV cables share the same pinout.** The Pi uses
> tip = left audio, ring 1 = right audio, ring 2 = ground, sleeve = video.
> A lot of cables (notably many made for Zune/iPod/older camcorders) put video
> and ground the other way round and will give you a black screen or a rolling
> picture. If you get nothing, try a different cable before you debug anything
> else — the Pi-specific ones are usually sold as "Raspberry Pi AV cable".

### Safe area

CRTs hide the outer edge of the picture. Everything important is kept inside an
inset rectangle (5% per edge = 32 px horizontally, 24 px vertically at 640×480).
Press `G` to draw the boundary in magenta and tune
`SAFE_AREA_INSET_X_FRAC` / `SAFE_AREA_INSET_Y_FRAC` in `config.py` until it sits
just inside your tube's visible area. Every set is different; this is a per-TV
adjustment, not a set-and-forget one.

The palette and fonts are chosen for composite too: no pure white or black
(they bloom and smear), desaturated red instead of saturated red (chroma crawl),
big bold type, and a 3 px minimum stroke — 1 px lines shimmer on an interlaced
display.

### Install

```bash
sudo apt install python3-pygame python3-gpiozero
# or, for a venv:
python3 -m venv .venv --system-site-packages
.venv/bin/pip install pygame-ce gpiozero lgpio

python3 main.py --real
```

Backend detection reads `/proc/device-tree/model` and looks for "raspberry pi".
Failing that it requires *both* an ARM machine and an importable `gpiozero`,
because gpiozero installs fine on a PC and would otherwise false-positive.
`--mock` / `--real` override it. If `--real` is auto-selected but fails to
initialise, it falls back to mock with a warning so you get a playable machine
rather than a traceback; an explicit `--real` raises instead.

---

## Wiring

All pins are BCM numbering and all of them live at the top of `config.py`.

| Device | Pin | Direction |
| --- | --- | --- |
| Coin acceptor pulse | GPIO 17 | in, pull-up |
| IR break-beam | GPIO 27 | in, pull-up |
| Solenoid **driver** | GPIO 18 | out |
| BET / DEAL / HIT / STAND / CASH OUT | GPIO 5 / 6 / 13 / 19 / 26 | in, pull-up |

### ⚠️ Coin acceptor (CH-926) — 12 V part on a 3.3 V pin

The CH-926 runs on **12 V**, and its COIN pulse output is referenced to that
supply. **Do not connect it straight to a GPIO pin.** Pi GPIO is 3.3 V and is
*not* 5 V tolerant, let alone 12 V — you will destroy the pin, and quite
possibly the SoC.

The output is open-collector (it pulls the line down and floats otherwise), so
the safe options are:

- **Best: an optocoupler** (PC817 + ~1 kΩ on the LED side). Full galvanic
  isolation, which also keeps the solenoid's electrical noise out of the Pi.
- **Acceptable: pull up to 3.3 V.** Because the output only ever *sinks*
  current, wire COIN to GPIO 17 with a 10 kΩ pull-up to the Pi's **3.3 V** rail
  and nothing to 12 V. Verify with a meter that the line idles at 3.3 V and not
  12 V before connecting the Pi. A resistor divider is not a substitute if the
  output ever drives high.

Ground the acceptor's 12 V supply to the Pi ground. Set the acceptor to
**1 pulse per coin** (`COIN_PULSES_PER_COIN = 1`); if you program it for more,
raise that constant and check `COIN_PULSE_GROUP_WINDOW_MS` sits comfortably
above the acceptor's inter-pulse gap.

### ⚠️ Solenoid — never driven directly

GPIO 18 switches a **driver**, never the coil. A Pi pin can source about 16 mA;
a coin-slide solenoid wants amps.

```
        +12V (own supply, NOT the Pi's 5V)
          │
          ├──────────┐
          │          │
       [coil]     [flyback diode 1N4007, cathode to +12V]
          │          │
          ├──────────┘
          │
        drain
   MOSFET (logic-level, e.g. IRLZ44N / IRL540N)
        gate ──[220Ω]── GPIO 18
          │
          └──[10kΩ]──┐
        source       │
          │          │
         GND ────────┴──── Pi GND   (grounds MUST be common)
```

- **Flyback diode across the coil is mandatory.** Collapsing coil current will
  otherwise put hundreds of volts across your MOSFET and back into the Pi.
- Use a **logic-level** MOSFET — a standard IRF540 will not fully turn on at
  3.3 V, and a partly-on MOSFET gets hot.
- 220 Ω in series with the gate, 10 kΩ gate-to-source pull-down so the coil
  stays off while the Pi boots and the pin floats.
- The solenoid supply must **share ground with the Pi**, but should not be the
  Pi's 5 V rail — the inrush will brown out the Pi and corrupt the SD card.
- A relay module works too, and is easier; it's just slower and wears out.
  Check whether yours is active-low and set `SOLENOID_ACTIVE_HIGH` to match.
- `SOLENOID_MAX_ON_MS` hard-caps how long the coil can be energized. Coin-slide
  solenoids are not continuous-duty; don't raise it far.

### IR break-beam sensor

A plain digital input. Emitter and detector face each other across the exit
chute; the detector output goes to GPIO 27, pulled up, and is driven **low**
when a coin interrupts the beam (`IR_SENSOR_ACTIVE_LOW = True` — invert if
yours is the other way). Power it from 3.3 V if it will run there; if it needs
5 V, level-shift the output.

No sensor? Set `USE_DROP_SENSOR = False` for open-loop mode: each actuation is
assumed to be exactly one coin. Simpler, but a jam then silently short-changes
the player, which is why closed loop is the default.

### Buttons

Wire each arcade button between its pin and **ground**; the internal pull-ups
are enabled in software. Debounce is handled in `config.BUTTON_DEBOUNCE_MS`.

---

## systemd

```bash
sudo cp blackjack.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now blackjack
journalctl -u blackjack -f
```

The unit assumes the code is at `/home/pi/blackjack` with its venv at
`.venv` — edit `WorkingDirectory`, `ExecStart` and `User` if not.

It sets `Restart=always` on purpose: a crash mid-payout must come straight back
up so `Bank.reconcile()` can resume paying what is owed. `KillSignal=SIGINT`
gives the main loop's `finally` block a chance to de-energize the solenoid and
release the pins.

---

## Not built (extension points left in place)

- **Double / split / insurance / surrender.** `game.py` ends with a commented
  block describing exactly where each hooks in. The money functions already
  work for any bet size, so double needs no accounting changes; split is the
  one that needs `player_cards` to become a list of hands. Insurance re-opens
  the odd-quarter rounding question — route it through the same integer path.
- **Sound.** `ui.play_sound()` is a no-op stub with call sites already in place
  (coin, deal, card, win, lose, cash-out). Init `pygame.mixer`, load WAVs, flip
  `config.SOUND_ENABLED`.
- **Coin acceptor inhibit.** `Hardware.set_coin_acceptor_enabled()` exists and
  does nothing; the CH-926 has an inhibit line if you want to refuse coins
  during a jam.
- **Refund-to-credits on jam.** `Bank.abandon_payout_to_balance()` is written
  and tested but not reachable from the UI — currently a jam *holds* the owed
  count, as specified. Wire it to a maintenance key if you'd rather.
- Networking, remote logging, cloud anything. Out of scope by design; the
  machine only ever touches `state/` on the local disk.

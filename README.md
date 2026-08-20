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
ui.py                pygame rendering + animation + keyboard (only pygame import)
anim.py              easing curves, tweens, timing helpers   (pure)
hardware/
  __init__.py        auto-detect + backend factory
  base.py            abstract Hardware interface, debounce, pulse grouping
  real.py            gpiozero backend (Pi)
  mock.py            keyboard backend (PC)
tests/               unittest suite, no third-party deps
blackjack.service    systemd unit
```

`game.py`, `cards.py`, `bank.py` and `anim.py` import **nothing but the standard library
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
| `F11` | fullscreen |
| `G` | draw the overscan-safe-area box |
| `Esc` | quit |

**These all work on the Pi too**, alongside the arcade buttons — see
[Keyboard on the Pi](#keyboard-on-the-pi).

### Test keys (mock backend only)

| Key | Simulates |
| --- | --- |
| `Q` | inserting a quarter — fed through the real pulse-grouping code |
| `D` | the IR beam seeing a coin drop |
| `J` | toggle "coins will drop" — turn it off to force a dispenser jam |

These three are inert on real hardware. `Hardware.simulate_coin_insert()` is a
no-op in `RealHardware` on purpose: a keystroke that mints credits is a
free-money bug, not a debug aid.

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

`tests/test_anim.py` covers the animation timing primitives — easing curves
that start at 0 and land on 1, delayed and zero-length tweens, shake decay, and
a credit meter that always settles on exactly the number the bank holds. No
pygame involved, so it runs with the same bare Python as the rest.

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

## Animation

All motion lives in `ui.py` and is **purely cosmetic**. The rules engine still
deals a hand in one call and the bank still credits winnings the instant a hand
settles; the renderer diffs what it sees each frame against what it drew last
frame and starts whatever motion that implies. Nothing an animation does can
change a card, a bet, or a quarter.

Set `ANIMATIONS_ENABLED = False` in `config.py` and every duration collapses to
zero, every blink stays lit, and you get exactly the machine you had before —
same screens, same pacing, no second code path. Useful on a very slow Pi, or if
a cabinet is going somewhere the motion would be a distraction.

**On the table**

- **The deal.** Cards fly out of the shoe drawn top-right and land in the hand,
  face down, then turn over. They come out in real table order — player, dealer
  up-card, player, dealer hole card — 125 ms apart, so a full deal takes about
  three quarters of a second. `game.deal()` still hands the UI all four at once;
  `ui.py` walks them out in order on its own.
- **Hand totals count only what is face up.** "YOU 18" over two face-down cards
  would give the deal away, so the totals climb card by card as each one turns.
  The dealer's shows `15+` while anything of theirs is still hidden.
- **The hole card** turns over when the player stands, and the dealer waits for
  it to finish before drawing (`DEALER_BEAT_MS`).
- **A growing hand re-centres**, so the cards already down slide over to make
  room instead of jumping.
- **End of hand**, the cards sweep off to the discard tray.
- **The result banner** springs in on a win and shakes on a loss — and waits for
  the cards to land first, so a natural doesn't shout BLACKJACK! over cards that
  are still in the air.
- **CREDITS rolls** to its new value and flashes on a coin; **BET** pops when
  you change it; **SHUFFLING THE SHOE** slides in when the cut card comes up.
- **Paying out**, a quarter tumbles down the screen for each coin — driven by
  `bank.owed_quarters` going down, so what you see is what the dispenser has
  actually confirmed, never an optimistic guess.

**The title screen**

`IDLE_ATTRACT_SECONDS` after the last button press — and at boot — the cabinet
shows its attract screen: the title dropping in a letter at a time and then
rippling, an ace and a king sliding in from the wings, a blinking INSERT
QUARTER, drifting suit pips, and a marquee of the house rules along the bottom.
Any button or any coin wakes it; the press that wakes it is swallowed, so
nobody's first touch costs them a bet.

It will **never** appear while there are credits on the meter, a hand in
progress, coins owed, or a jam on screen. Money on the meter always beats a
pretty animation — that rule is in `App.update_attract()` and is worth keeping
one function long.

**Timings** are all in the `# Animation` block of `config.py`, in milliseconds.
`tests/test_anim.py` covers the tween and easing behaviour that everything else
is built on, including that a zero-length tween snaps and that the credit meter
always comes to rest on exactly the bank's number.

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

### Keyboard on the Pi

The keyboard mirror is **always live** — it is `Hardware.inject_button()` on the
base class, not something the mock backend adds, so `B` / `Enter` / `H` / `S` /
`C` work on the cabinet exactly as they do on a PC, in parallel with the wired
arcade buttons. Nothing to enable and no flag to pass:

```bash
python3 main.py --real
```

A USB keyboard is genuinely useful for servicing a machine, which is why
pressing HIT from one is allowed. What is *not* allowed is `Q` / `D` / `J`: the
three test keys stay no-ops on real hardware, because a keystroke that mints
credits or fakes a coin drop is a free-money bug rather than a debug aid. If
you want those on the Pi — bench-testing the cabinet before any electronics are
wired — run the mock backend on it instead:

```bash
python3 main.py --mock --fullscreen     # composite output, no GPIO, all test keys
```

Two things actually get in the way, and neither is this program:

- **The keyboard has to be plugged into the Pi.** With no X or Wayland session,
  SDL reads `/dev/input/event*` directly, so keys typed into an *SSH* session
  never reach the game. Log in over SSH to start it if you like, but press the
  keys on a keyboard attached to the Pi.
- **The process needs permission to read those devices**, i.e. membership of
  the `input` group. `blackjack.service` already grants it
  (`SupplementaryGroups=gpio video render input`); for a manual run:

  ```bash
  sudo usermod -aG input,video,render,gpio $USER   # log out and back in
  ```

  Symptom of getting this wrong: the game draws on the CRT perfectly and
  ignores every key.

If you are running under the desktop rather than straight to the framebuffer,
none of that applies — it is an ordinary window and just needs focus.

### Performance: stalls and missing animations

Symptoms on a Pi: a pause after pressing DEAL, a pause before the result
appears, and the deal animation not showing at all even though hitting and the
dealer's draw animate fine.

That last detail is the diagnostic one. DEAL and settling are the only two
moments in a hand that **write to the bank**, and `bank.py` writes atomically —
`fsync` the temp file, rename, then `fsync` the directory. On an SD card those
two syncs routinely cost 100–500 ms, and the loop is blocked for all of it.
Hitting and standing touch no money, which is exactly why they never stutter.

The blocked loop used to poison the animation as well: the frame's clock was
read at the top of `step()`, so by the time the renderer was handed it, it was
stale by the length of the write. The deal was scheduled as though it had begun
before the stall, and at ~700 ms of fsync the whole 725 ms sequence was over
before a single frame of it was drawn. `main.py` now re-reads the clock after
the event and payout phases, so the deal always plays from where it really is.
The stall itself is still there — that is the cost of not losing your money in a
power cut — but you get the pause and *then* the full animation, rather than a
pause and a hand that has already appeared.

A press that arrives during the settle write no longer wipes the result screen
either: `RESULT_SKIP_GRACE_MS` ignores BET/DEAL for the first moments after the
banner goes up, so a keypress queued during a stall is not delivered as "skip".
Deliberately skipping still works a beat later. Set it to 0 for the old
always-skippable behaviour.

**The tail of a round.** "It freezes after a hand" is usually not a freeze at
all — it is the fixed sequence between standing and being able to deal again.
Measured end to end, with a dealer who draws twice:

| | |
| --- | --- |
| STAND pressed | 0.00s |
| dealer finishes drawing, result decided | 1.36s |
| BET/DEAL accepted again | 1.95s |
| back to the betting screen | 4.40s |

The two long stretches are both config, not bugs: `DEALER_STEP_MS` (650 ms per
dealer card) and `RESULT_DISPLAY_SECONDS` (3.0 s). Halving them roughly halves
the tail. What was a genuine fault is the middle row — the machine used to
ignore BET and DEAL until every card had stopped moving, which on a Pi is
seconds of pressing a button and getting nothing, and reads as a crash rather
than a pause. Two changes:

- Input timing is now measured from when the hand was **decided**, not from
  when the banner finished animating in. What the renderer is still finishing
  no longer decides whether a button works.
- A press that is still too early is **held, not dropped**, and acted on the
  moment it becomes legal. Press DEAL whenever you like; the machine always
  responds, at worst `RESULT_SKIP_GRACE_MS` later.

**Measure yours before changing anything:**

```bash
python3 main.py --real --profile
```

Every frame over twice the budget prints a breakdown, and a summary lands on
exit. The phase names answer the only question that matters here:

```
[profile] SLOW FRAME 1: 352.4ms (mostly events)  input 0.0  events 350.4 \
          payout 0.0  animate 0.0  logic 0.0  draw 1.9
[profile] frame: median 0.8ms  p95 1.6ms  worst 352.4ms
```

- Big **`events`** or **`logic`** worst case → the SD card, in a bank write.
  Confirm it in ten seconds, without the game in the way:

  ```bash
  python3 tools/disk_check.py
  ```

  That times the real atomic write against a plain one, on the real state
  directory, and tells you whether the fsync is what is stalling you. A healthy
  card is single-digit milliseconds; an SSD is ~15 ms; a tired card can be
  **seconds**, and every coin, bet and win pays that cost with the loop stopped.

  The machine now also reports this on its own: any save slower than
  `SLOW_WRITE_WARN_MS` prints `[bank] SLOW WRITE: …ms` to the journal and
  leaves a `SLOW_WRITE` line in the ledger. Seconds-long saves mean the card is
  failing, and the file at risk is the one holding the balance — replace it.

  **Or stop paying for durability you don't need.** `config.PERSIST_MODE`
  decides how hard the machine works not to forget the balance, and the
  expensive part is fsync, not writing:

  | mode | cost per save | survives |
  | --- | --- | --- |
  | `durable` | 12.8 ms (and seconds on a failing card) | the plug being pulled mid-write |
  | `fast` *(default)* | 0.047 ms | quit, crash, reboot — not a power cut mid-write |
  | `memory` | 0 ms, one write at exit | nothing; a kill loses your credits |

  `fast` is 272× cheaper and **cannot stall**, because nothing waits on the
  card to commit. It still writes the file atomically, so a restart finds your
  balance exactly where you left it. That is the right setting for a cabinet in
  your own home.

  Run `durable` if the machine is ever somewhere the public can feed it real
  money — it is the only mode in which the interrupted-payout reconciliation
  means anything. Under the other two, a power cut during a cash-out can lose
  the owed count.

  If you keep `durable`, the fix is faster storage, not different code, and
  `state/` can live anywhere:

  ```bash
  BLACKJACK_STATE_DIR=/mnt/ssd/blackjack-state python3 main.py --real
  ```

  Do **not** point that at a tmpfs. It would be instant, and it would lose
  somebody's money the first time the plug was pulled.
- Big **`draw`** → graphics. `draw` excludes the frame limiter's sleep, so a
  healthy machine shows a small number here and spends the rest idle. If it is
  large, try `--windowed`, drop `FPS`, or set `ANIMATIONS_ENABLED = False`.

Under X with no desktop, also check you are not landing on a software renderer:
`pygame.SCALED` asks SDL to scale a 640×480 buffer to the display, and with no
GPU-accelerated renderer available that is a full-screen software blit every
frame. Running the X server at 640×480 so no scaling is needed avoids it
entirely, and is what a CRT wants anyway.

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
  `config.SOUND_ENABLED`. The animations give you the timings to hang it on: a
  card lands `CARD_FLY_MS` after it leaves the shoe, and turns over over the
  following `CARD_FLIP_MS`.
- **Coin acceptor inhibit.** `Hardware.set_coin_acceptor_enabled()` exists and
  does nothing; the CH-926 has an inhibit line if you want to refuse coins
  during a jam.
- **Refund-to-credits on jam.** `Bank.abandon_payout_to_balance()` is written
  and tested but not reachable from the UI — currently a jam *holds* the owed
  count, as specified. Wire it to a maintenance key if you'd rather.
- Networking, remote logging, cloud anything. Out of scope by design; the
  machine only ever touches `state/` on the local disk.

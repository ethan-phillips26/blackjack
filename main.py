"""
Entry point: argument parsing, backend selection, and the main loop.

This file is the only place the four layers meet. It owns no rules and no
money math -- it moves events from the hardware into game.py / bank.py and
hands the resulting state to ui.py to draw.

It also owns the attract/title screen: which screen is up is a property of the
machine, not of the renderer. The rule is one line long and worth keeping that
way -- NEVER show the title screen while the player has credits or is owed
coins. Money on the meter always beats a pretty animation.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time

import config
from bank import Bank, PayoutController
from cards import Shoe
from game import BlackjackGame, Phase
from hardware import EventType, create_hardware, now_ms


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Coin-op blackjack machine")
    backend = parser.add_mutually_exclusive_group()
    backend.add_argument(
        "--mock",
        dest="backend",
        action="store_const",
        const="mock",
        help="force the keyboard/no-GPIO backend (PC development)",
    )
    backend.add_argument(
        "--real",
        dest="backend",
        action="store_const",
        const="real",
        help="force the gpiozero backend (Raspberry Pi); errors if unavailable",
    )
    screen = parser.add_mutually_exclusive_group()
    screen.add_argument("--fullscreen", action="store_true")
    screen.add_argument("--windowed", action="store_true")
    parser.add_argument(
        "--seed", type=int, default=None, help="deterministic shoe, for testing"
    )
    parser.add_argument(
        "--profile",
        action="store_true",
        help="report where each frame's time goes; names slow frames as they happen",
    )
    parser.set_defaults(backend=None)
    return parser.parse_args(argv)


class NullProfiler:
    """The profiler when --profile is off: every call is a no-op.

    A null object rather than `if self.profiling:` guards, so the measured and
    unmeasured loops are the same code and the instrumentation cannot change
    the thing it is measuring.
    """

    enabled = False

    def begin(self) -> None:
        pass

    def mark(self, phase: str) -> None:
        pass

    def event_start(self) -> float:
        return 0.0

    def event_done(self, event, started: float) -> None:
        pass

    def end(self, draw_ms: float) -> None:
        pass

    def summary(self) -> None:
        pass


class FrameProfiler(NullProfiler):
    """Where the frame actually went. Enabled with --profile.

    Written for one specific question, because it is the question a cabinet
    always raises: is this slow because of the SD card or because of the
    graphics? Those land in different phases --

        events  a blocking bank write (placing a bet, crediting a win)
        logic   the settle write, likewise
        draw    pygame actually rendering

    -- so one run tells you which, instead of guessing. `draw` excludes the
    frame limiter's sleep, so a healthy machine shows a small draw number and
    a large idle remainder, not a flat 33ms.
    """

    enabled = True
    # "gap" is the time we were NOT running: the frame limiter's sleep plus
    # anything the OS did instead of us. A large gap with small phases means
    # the process is being starved from outside -- swap, thermal throttling, a
    # busy X server -- not that this program is slow.
    PHASES = ("gap", "input", "hw", "events", "payout", "animate", "logic", "draw")

    def __init__(self, slow_frame_ms: float) -> None:
        self.slow_frame_ms = slow_frame_ms
        self.frames = 0
        self.slow_frames = 0
        self.started = time.perf_counter()
        self._phase_start = self.started
        self._last_end = self.started
        self._current: dict[str, float] = {}
        self._events: list[tuple[str, float]] = []
        self._samples: dict[str, list[float]] = {p: [] for p in self.PHASES}
        self._totals: list[float] = []

    def begin(self) -> None:
        now = time.perf_counter()
        self._current = {"gap": (now - self._last_end) * 1000.0}
        self._events = []
        self._phase_start = now

    def event_start(self) -> float:
        return time.perf_counter()

    def event_done(self, event, started: float) -> None:
        name = event.type.name
        if event.button:
            name = f"{name}({event.button})"
        self._events.append((name, (time.perf_counter() - started) * 1000.0))

    def mark(self, phase: str) -> None:
        now = time.perf_counter()
        self._current[phase] = (now - self._phase_start) * 1000.0
        self._phase_start = now

    def end(self, draw_ms: float) -> None:
        self._current["draw"] = draw_ms
        self._last_end = time.perf_counter()
        self.frames += 1
        total = sum(self._current.values())
        self._totals.append(total)
        for phase, value in self._current.items():
            if phase in self._samples:
                self._samples[phase].append(value)

        if total >= self.slow_frame_ms:
            self.slow_frames += 1
            worst = max(self._current.items(), key=lambda kv: kv[1])
            detail = "  ".join(
                f"{p} {self._current.get(p, 0.0):.1f}" for p in self.PHASES
            )
            print(
                f"[profile] SLOW FRAME {self.frames}: {total:.1f}ms "
                f"(mostly {worst[0]})  {detail}"
            )
            if self._events:
                # Which event, not just which phase. An event storm and one
                # slow handler both land in "events" and are fixed differently.
                busiest = sorted(self._events, key=lambda kv: -kv[1])[:4]
                listed = "  ".join(f"{n} {ms:.1f}" for n, ms in busiest)
                print(
                    f"[profile]   {len(self._events)} event(s) this frame: {listed}"
                )

    @staticmethod
    def _p95(values: list[float]) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]

    def summary(self) -> None:
        if not self.frames:
            return
        elapsed = time.perf_counter() - self.started
        print(f"\n[profile] {self.frames} frames in {elapsed:.1f}s "
              f"= {self.frames / max(elapsed, 1e-9):.1f} fps average")
        print(f"[profile] frame: median {statistics.median(self._totals):.1f}ms  "
              f"p95 {self._p95(self._totals):.1f}ms  "
              f"worst {max(self._totals):.1f}ms")
        for phase in self.PHASES:
            values = self._samples[phase]
            if not values or max(values) < 0.05:
                continue
            print(f"[profile]   {phase:<8} median {statistics.median(values):6.2f}  "
                  f"p95 {self._p95(values):6.2f}  worst {max(values):8.2f}")
        share = 100.0 * self.slow_frames / self.frames
        print(f"[profile] {self.slow_frames} frames over "
              f"{self.slow_frame_ms:.0f}ms ({share:.1f}%)")
        print("[profile] a big 'events'/'logic' worst case is a bank write "
              "(check tools/disk_check.py); 'draw' is the graphics;")
        print("[profile] 'gap' is the OS not scheduling us -- swap, thermal "
              "throttling, or something else on the machine.")


class App:
    def __init__(self, args: argparse.Namespace) -> None:
        self.hardware = create_hardware(args.backend)
        self.bank = Bank()
        self.game = BlackjackGame(shoe=Shoe(seed=args.seed))
        self.payout = PayoutController(
            self.bank, has_drop_sensor=self.hardware.has_drop_sensor
        )

        # Resolve any payout that a power cut interrupted, BEFORE play starts.
        self.notice = self.bank.reconcile()
        if self.notice:
            print(f"[bank] {self.notice}")
        self.notice_until_ms = now_ms() + 8000 if self.notice else 0

        # Twice the frame budget: a frame that misses two refreshes is one a
        # player can see stutter.
        self.profiler = (
            FrameProfiler(slow_frame_ms=2000.0 / config.FPS)
            if getattr(args, "profile", False)
            else NullProfiler()
        )

        self.running = True
        self.dealer_next_ms = 0
        #: When the finished hand's banner went up. 0 = waiting for the cards
        #: to stop moving first, so the result never outruns the deal.
        self.settled_at_ms = 0
        #: When the hand was DECIDED, which is earlier and is what input timing
        #: keys off. Deliberately not the same clock as the banner: what the
        #: renderer is still finishing must never decide whether a button works.
        self.settled_decided_ms = 0
        #: A BET/DEAL press that arrived before the result could be skipped.
        #: Held, not dropped -- see handle_button.
        self.pending_button: str | None = None
        self.result_credited = False
        self.last_input_ms = now_ms()
        self.attract = False

        # ui.py is imported late so that --help and a broken pygame install
        # don't stop main.py from being importable.
        from ui import UI, play_sound

        self.play_sound = play_sound
        fullscreen = self._want_fullscreen(args)
        self.ui = UI(fullscreen=fullscreen)

        # A cabinet where a keystroke mints credits must SAY so, on the screen
        # in front of whoever is playing it -- not only in a log nobody reads.
        self.ui.test_coins_armed = (
            self.hardware.accepts_simulated_input and self.hardware.name != "mock"
        )

        # Boot into the title screen, but only from a genuinely idle machine:
        # an empty meter, nothing owed, and no reconcile message to read.
        self.attract = (
            config.IDLE_ATTRACT_SECONDS > 0
            and self.bank.balance_quarters == 0
            and self.bank.owed_quarters == 0
            and not self.notice
        )

    def _want_fullscreen(self, args: argparse.Namespace) -> bool:
        if args.fullscreen:
            return True
        if args.windowed:
            return False
        if self.hardware.name == "pi":
            return config.FULLSCREEN_DEFAULT_ON_PI
        return config.FULLSCREEN_DEFAULT_ON_PC

    # ------------------------------------------------------------------

    @property
    def payout_busy(self) -> bool:
        """True whenever coins are owed -- no betting or dealing until they're out."""
        return self.payout.is_active or self.bank.owed_quarters > 0

    def run(self) -> int:
        self.hardware.start()
        print(f"[main] {self.hardware.describe()}")
        try:
            while self.running:
                self.step()
        except KeyboardInterrupt:
            print("\n[main] interrupted")
        finally:
            # Ordering matters: kill the coil before anything else can fail.
            self.hardware.set_solenoid(False)
            self.hardware.close()
            # Under PERSIST_MODE = "memory" this is the only write there is.
            self.bank.flush()
            self.ui.quit()
            self.profiler.summary()
            if self.bank.slow_writes:
                print(
                    f"\n[bank] {self.bank.slow_writes} slow write(s) this run, "
                    f"worst {self.bank.worst_write_ms:.0f}ms. The storage, not "
                    f"the game, is what stopped the machine.\n"
                    f"[bank] Check it with: python3 tools/disk_check.py --count 200"
                )
        return 0

    def step(self) -> None:
        now = now_ms()
        self.profiler.begin()

        if not self.ui.process_input(self.hardware):
            self.running = False
            return
        self.profiler.mark("input")

        self.hardware.update(now)
        self.profiler.mark("hw")

        for event in self.hardware.poll_events():
            started = self.profiler.event_start()
            self.handle_hardware_event(event, now)
            self.profiler.event_done(event, started)
        self.profiler.mark("events")

        # RE-READ THE CLOCK. Everything from here measures elapsed real time,
        # and the event handling above can BLOCK for a long time: bank.py's
        # atomic writes fsync the file and then its directory, which on a Pi's
        # SD card is routinely 100-500ms, and placing a bet does exactly one.
        # `now` from the top of the frame is stale by that much, and an
        # animation scheduled from a stale clock is born part-finished -- on a
        # slow card the whole deal can be over before its first frame is drawn.
        # This is also why HIT and STAND always looked fine: they touch no money
        # and so never stall.
        now = now_ms()

        # Drive the dispenser. tick() has already performed any bank writes it
        # needed, so it is safe to energize on its say-so.
        self.hardware.set_solenoid(self.payout.tick(now))
        self.profiler.mark("payout")

        # ...and again: tick() writes to the bank too, and a coil that is held
        # on by a stale clock is held on for longer than SOLENOID_ON_MS in real
        # wall-clock time, which is the one thing that overheats it.
        now = now_ms()

        # Animation state is refreshed BEFORE the hand advances, so that
        # advance_hand() sees this frame's cards when it asks is_dealing().
        self.ui.update(self.game, self.bank, self.payout, now)
        self.update_attract(now)
        self.profiler.mark("animate")

        self.advance_hand(now)
        self.profiler.mark("logic")

        notice = self.notice if now < self.notice_until_ms else None
        self.ui.render(self.game, self.bank, self.payout, notice=notice)
        # The frame limiter sleeps inside render(); count only the drawing.
        self.profiler.end(self.ui.last_draw_ms)

    # ------------------------------------------------------------------

    def handle_hardware_event(self, event, now: int) -> None:
        if event.type is EventType.COIN_INSERTED:
            self.last_input_ms = now
            self.attract = False  # a coin always wakes the machine
            # event.simulated distinguishes a test-key credit from a real
            # quarter. main.py doesn't care which it is -- the bank does.
            self.bank.insert_quarters(1, simulated=event.simulated)
            self.game.clamp_bet(self.bank.balance_quarters)
            self.play_sound("coin")
        elif event.type is EventType.COIN_DROP_DETECTED:
            self.payout.on_drop_detected(now)
        elif event.type is EventType.BUTTON_PRESSED:
            self.last_input_ms = now
            if self.attract:
                # Arcade convention: the press that wakes the cabinet only
                # wakes it. Nobody's first touch should cost them a bet.
                self.attract = False
                return
            self.handle_button(event.button, now)
        elif event.type is EventType.QUIT_REQUESTED:
            self.running = False

    def result_is_skippable(self, now: int) -> bool:
        """May a BET/DEAL press clear the result screen yet?

        Measured from when the hand was DECIDED, not from when the banner
        finished sliding in. Tying this to the animation meant the machine
        ignored the player for as long as a card was still moving -- on a Pi,
        two whole seconds of pressing DEAL and nothing happening, which reads
        as a crash rather than as a pause.

        The remaining grace exists for one specific case: a press queued while
        the loop was blocked in a bank write gets delivered the instant the
        machine catches up, and would wipe a result nobody had seen yet.
        """
        if self.settled_decided_ms == 0:
            return False
        return now - self.settled_decided_ms >= config.RESULT_SKIP_GRACE_MS

    def handle_button(self, button: str, now: int) -> None:
        game = self.game

        if button == config.BTN_CASHOUT:
            self.on_cash_out(now)
            return

        # Everything else is blocked while quarters are physically owed.
        if self.payout_busy:
            return

        if button == config.BTN_BET:
            if game.phase is Phase.SETTLED:
                if not self.result_is_skippable(now):
                    self.pending_button = button
                    return
                self.clear_hand()
            if game.phase is Phase.BETTING:
                game.cycle_bet()

        elif button == config.BTN_DEAL:
            if game.phase is Phase.SETTLED:
                # Let an impatient player skip the result -- but only once it
                # has actually been on screen (see result_is_skippable). A
                # press that is too early is HELD, never dropped: a button that
                # does nothing at all is what makes a cabinet feel broken.
                if not self.result_is_skippable(now):
                    self.pending_button = button
                    return
                self.clear_hand()
            if game.can_deal(self.bank.balance_quarters):
                if self.bank.place_bet(game.bet_quarters):
                    game.deal()
                    self.result_credited = False
                    self.dealer_next_ms = now + config.DEALER_STEP_MS
                    self.play_sound("deal")

        elif button == config.BTN_HIT:
            if game.phase is Phase.PLAYER_TURN:
                game.hit()
                self.dealer_next_ms = now + config.DEALER_STEP_MS
                self.play_sound("card")

        elif button == config.BTN_STAND:
            if game.phase is Phase.PLAYER_TURN:
                game.stand()
                self.dealer_next_ms = now + config.DEALER_STEP_MS

    def on_cash_out(self, now: int) -> None:
        if self.payout.status.jammed:
            self.payout.retry_after_jam(now)  # have another go at the stuck coin
            return
        if self.payout_busy:
            return
        if self.game.phase is Phase.SETTLED:
            self.clear_hand()
        if self.game.phase is not Phase.BETTING:
            return  # never cash out mid-hand: the wager is already committed
        if self.bank.balance_quarters > 0:
            self.bank.begin_cashout()
            self.play_sound("cashout")

    # ------------------------------------------------------------------

    def update_attract(self, now: int) -> None:
        """Decide whether the title screen is up.

        Two hard rules, in this order: the machine must be idle AND the player
        must have nothing riding on it. Credits on the meter, a hand in
        progress, coins owed, or a jam all keep the game screen up no matter
        how long nobody has touched a button.
        """
        if config.IDLE_ATTRACT_SECONDS <= 0:
            self.attract = False
            self.ui.set_attract(False)
            return

        idle_ok = (
            self.game.phase is Phase.BETTING
            and self.bank.balance_quarters == 0
            and not self.payout_busy
            and not self.payout.status.jammed
            and not self.ui.is_dealing()
        )
        if not idle_ok:
            self.attract = False
        elif now - self.last_input_ms >= config.IDLE_ATTRACT_SECONDS * 1000:
            self.attract = True

        self.ui.set_attract(self.attract)

    def advance_hand(self, now: int) -> None:
        game = self.game

        if game.phase is Phase.DEALER_TURN:
            if self.ui.is_dealing():
                # Wait for the hole card to finish turning over (or the last
                # card to land) and then take a beat, so the dealer looks like
                # it decided rather than glitched.
                self.dealer_next_ms = max(
                    self.dealer_next_ms, now + config.DEALER_BEAT_MS
                )
            elif now >= self.dealer_next_ms:
                game.dealer_step()
                self.dealer_next_ms = now + config.DEALER_STEP_MS

        if (
            self.pending_button is not None
            and game.phase is Phase.SETTLED
            and self.result_is_skippable(now)
        ):
            held, self.pending_button = self.pending_button, None
            self.handle_button(held, now)
            return  # the replayed press has already moved the hand on

        if game.phase is Phase.SETTLED and game.result is not None:
            if not self.result_credited:
                # Winnings go to the CREDIT METER, not to the hopper. Coins only
                # move when the player asks for them with CASH OUT.
                # Credited IMMEDIATELY -- the money is never made to wait on an
                # animation. Only the on-screen countdown does.
                self.bank.credit(game.result.returned_quarters, "SETTLE")
                self.result_credited = True
                self.settled_at_ms = 0
                self.settled_decided_ms = now
                self.play_sound("win" if game.result.outcome.player_won else "lose")
            elif self.settled_at_ms == 0:
                if not self.ui.is_dealing():
                    self.settled_at_ms = now  # cards have landed: start the clock
            elif now - self.settled_at_ms >= config.RESULT_DISPLAY_SECONDS * 1000:
                self.clear_hand()

    def clear_hand(self) -> None:
        self.game.clear()
        self.game.clamp_bet(self.bank.balance_quarters)
        self.result_credited = False
        self.settled_at_ms = 0
        self.settled_decided_ms = 0
        self.pending_button = None


def main(argv: list[str] | None = None) -> int:
    return App(parse_args(argv)).run()


if __name__ == "__main__":
    sys.exit(main())

"""
Entry point: argument parsing, backend selection, and the main loop.

This file is the only place the four layers meet. It owns no rules and no
money math -- it moves events from the hardware into game.py / bank.py and
hands the resulting state to ui.py to draw.
"""

from __future__ import annotations

import argparse
import sys

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
    parser.set_defaults(backend=None)
    return parser.parse_args(argv)


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

        self.running = True
        self.dealer_next_ms = 0
        self.settled_at_ms = 0
        self.result_credited = False

        # ui.py is imported late so that --help and a broken pygame install
        # don't stop main.py from being importable.
        from ui import UI, play_sound

        self.play_sound = play_sound
        fullscreen = self._want_fullscreen(args)
        self.ui = UI(fullscreen=fullscreen)

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
            self.ui.quit()
        return 0

    def step(self) -> None:
        now = now_ms()

        if not self.ui.process_input(self.hardware):
            self.running = False
            return

        self.hardware.update(now)

        for event in self.hardware.poll_events():
            self.handle_hardware_event(event, now)

        # Drive the dispenser. tick() has already performed any bank writes it
        # needed, so it is safe to energize on its say-so.
        self.hardware.set_solenoid(self.payout.tick(now))

        self.advance_hand(now)

        notice = self.notice if now < self.notice_until_ms else None
        self.ui.render(self.game, self.bank, self.payout, notice=notice)

    # ------------------------------------------------------------------

    def handle_hardware_event(self, event, now: int) -> None:
        if event.type is EventType.COIN_INSERTED:
            self.bank.insert_quarters(1)
            self.game.clamp_bet(self.bank.balance_quarters)
            self.play_sound("coin")
        elif event.type is EventType.COIN_DROP_DETECTED:
            self.payout.on_drop_detected(now)
        elif event.type is EventType.BUTTON_PRESSED:
            self.handle_button(event.button, now)
        elif event.type is EventType.QUIT_REQUESTED:
            self.running = False

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
                self.clear_hand()
            if game.phase is Phase.BETTING:
                game.cycle_bet()

        elif button == config.BTN_DEAL:
            if game.phase is Phase.SETTLED:
                self.clear_hand()  # let an impatient player skip the result
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

    def advance_hand(self, now: int) -> None:
        game = self.game

        if game.phase is Phase.DEALER_TURN and now >= self.dealer_next_ms:
            game.dealer_step()
            self.dealer_next_ms = now + config.DEALER_STEP_MS

        if game.phase is Phase.SETTLED and game.result is not None:
            if not self.result_credited:
                # Winnings go to the CREDIT METER, not to the hopper. Coins only
                # move when the player asks for them with CASH OUT.
                self.bank.credit(game.result.returned_quarters, "SETTLE")
                self.result_credited = True
                self.settled_at_ms = now
                self.play_sound("win" if game.result.outcome.player_won else "lose")
            elif now - self.settled_at_ms >= config.RESULT_DISPLAY_SECONDS * 1000:
                self.clear_hand()

    def clear_hand(self) -> None:
        self.game.clear()
        self.game.clamp_bet(self.bank.balance_quarters)
        self.result_credited = False


def main(argv: list[str] | None = None) -> int:
    return App(parse_args(argv)).run()


if __name__ == "__main__":
    sys.exit(main())

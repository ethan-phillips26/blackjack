"""
Pure blackjack rules engine.

NO pygame. NO gpiozero. NO file I/O. Everything here is a function of its
arguments and the shoe, so the whole thing is unit-testable at speed.

All money is INTEGER QUARTERS, end to end. There is not a single float in the
accounting path -- see blackjack_bonus_quarters() for the one place a fraction
could have crept in and how it is resolved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto

import config
from cards import Card, Shoe

BLACKJACK = 21


# ---------------------------------------------------------------------------
# Hand evaluation
# ---------------------------------------------------------------------------


def hand_value(cards: list[Card]) -> tuple[int, bool]:
    """Return (best total, is_soft).

    Aces count 11 while that keeps the total <= 21, otherwise 1. "Soft" means
    at least one ace is still being counted as 11, so the hand cannot bust on
    the next card.
    """
    total = sum(card.base_value for card in cards)
    aces = sum(1 for card in cards if card.is_ace)
    while total > BLACKJACK and aces:
        total -= 10  # demote an ace from 11 to 1
        aces -= 1
    return total, aces > 0


def hand_total(cards: list[Card]) -> int:
    return hand_value(cards)[0]


def is_bust(cards: list[Card]) -> bool:
    return hand_total(cards) > BLACKJACK


def is_blackjack(cards: list[Card]) -> bool:
    """A NATURAL: 21 on the first two cards only.

    21 built from three or more cards is just 21 -- it pays 1:1, not 3:2.
    """
    return len(cards) == 2 and hand_total(cards) == BLACKJACK


def dealer_should_hit(cards: list[Card]) -> bool:
    """House rule. Stands on all 17s unless config.DEALER_HITS_SOFT_17."""
    total, soft = hand_value(cards)
    if total < 17:
        return True
    if total == 17 and soft and config.DEALER_HITS_SOFT_17:
        return True
    return False


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------


class Outcome(Enum):
    PLAYER_BLACKJACK = auto()  # natural, pays 3:2
    PLAYER_WIN = auto()  # higher total, pays 1:1
    DEALER_BUST = auto()  # dealer over 21, pays 1:1
    PUSH = auto()  # tie, bet returned
    PLAYER_BUST = auto()  # player over 21, bet lost
    DEALER_WIN = auto()  # dealer higher, bet lost

    @property
    def player_won(self) -> bool:
        return self in (Outcome.PLAYER_BLACKJACK, Outcome.PLAYER_WIN, Outcome.DEALER_BUST)

    @property
    def is_push(self) -> bool:
        return self is Outcome.PUSH


def blackjack_bonus_quarters(bet_quarters: int) -> int:
    """The 3:2 BONUS on a natural, in whole quarters (excludes the bet itself).

    ---------------------------------------------------------------------
    THE QUARTER-ROUNDING RULE -- the one place money can't divide evenly.
    ---------------------------------------------------------------------
    3:2 on an odd bet lands on a half quarter, and there is no such coin:

        bet 1 -> 3/2 = 1.5  ->  1 (down) / 2 (up) / 2 (nearest, ties-to-even)
        bet 2 -> 3          ->  3   exact
        bet 3 -> 9/2 = 4.5  ->  4 (down) / 5 (up) / 4 (nearest, ties-to-even)
        bet 4 -> 6          ->  6   exact

    config.BLACKJACK_ROUNDING picks the policy; "down" (house keeps the half)
    is the default and is what coin-op machines conventionally do. Change that
    constant -- do not re-derive this arithmetic anywhere else in the codebase.

    Integer-only: // and divmod, never float division.
    """
    numerator = bet_quarters * config.BLACKJACK_PAYOUT_NUMERATOR
    denominator = config.BLACKJACK_PAYOUT_DENOMINATOR
    whole, remainder = divmod(numerator, denominator)

    policy = config.BLACKJACK_ROUNDING
    if remainder == 0:
        return whole
    if policy == "down":
        return whole
    if policy == "up":
        return whole + 1
    if policy == "nearest":
        # Ties-to-even. With a denominator of 2 every fraction IS a tie, so
        # this rounds to whichever of whole / whole+1 is even.
        if remainder * 2 > denominator:
            return whole + 1
        if remainder * 2 < denominator:
            return whole
        return whole if whole % 2 == 0 else whole + 1
    raise ValueError(f"unknown BLACKJACK_ROUNDING policy: {policy!r}")


def payout_quarters(outcome: Outcome, bet_quarters: int) -> int:
    """Quarters RETURNED to the player's balance for a settled hand.

    This is the gross return, not the profit: the bet was already deducted
    from the balance when the hand was dealt.

        loss   -> 0                       (bet already gone)
        push   -> bet                      (net 0)
        win    -> bet * 2                  (net +bet, i.e. 1:1)
        natural-> bet + bonus              (net +bonus, i.e. 3:2 rounded)
    """
    if outcome.is_push:
        return bet_quarters
    if outcome is Outcome.PLAYER_BLACKJACK:
        return bet_quarters + blackjack_bonus_quarters(bet_quarters)
    if outcome.player_won:
        return bet_quarters * 2
    return 0


def determine_outcome(player: list[Card], dealer: list[Card]) -> Outcome:
    """Compare two finished hands. Player bust is checked first: a player who
    busts loses even if the dealer would also have busted."""
    if is_bust(player):
        return Outcome.PLAYER_BUST

    player_natural = is_blackjack(player)
    dealer_natural = is_blackjack(dealer)
    if player_natural and dealer_natural:
        return Outcome.PUSH
    if player_natural:
        return Outcome.PLAYER_BLACKJACK
    if dealer_natural:
        return Outcome.DEALER_WIN

    if is_bust(dealer):
        return Outcome.DEALER_BUST

    player_total = hand_total(player)
    dealer_total = hand_total(dealer)
    if player_total > dealer_total:
        return Outcome.PLAYER_WIN
    if player_total < dealer_total:
        return Outcome.DEALER_WIN
    return Outcome.PUSH


# ---------------------------------------------------------------------------
# Round state machine
# ---------------------------------------------------------------------------


class Phase(Enum):
    BETTING = auto()  # idle: taking coins, adjusting the bet, cash-out allowed
    PLAYER_TURN = auto()  # hit / stand
    DEALER_TURN = auto()  # dealer drawing, paced one card per dealer_step()
    SETTLED = auto()  # result on screen, waiting to clear


@dataclass
class RoundResult:
    outcome: Outcome
    bet_quarters: int
    returned_quarters: int  # gross back to balance
    player_cards: list[Card]
    dealer_cards: list[Card]

    @property
    def net_quarters(self) -> int:
        """Signed change to the player's balance across the whole hand."""
        return self.returned_quarters - self.bet_quarters

    @property
    def message(self) -> str:
        return {
            Outcome.PLAYER_BLACKJACK: "BLACKJACK!",
            Outcome.PLAYER_WIN: "YOU WIN",
            Outcome.DEALER_BUST: "DEALER BUSTS",
            Outcome.PUSH: "PUSH",
            Outcome.PLAYER_BUST: "BUST",
            Outcome.DEALER_WIN: "DEALER WINS",
        }[self.outcome]


@dataclass
class BlackjackGame:
    """One seat at one table. Owns the shoe and the current hand.

    Knows nothing about coins, GPIO, or pixels: the caller deducts the bet from
    the bank before deal() and credits result.returned_quarters after settle.
    """

    shoe: Shoe = field(default_factory=Shoe)
    phase: Phase = Phase.BETTING
    bet_quarters: int = config.MIN_BET_QUARTERS
    player_cards: list[Card] = field(default_factory=list)
    dealer_cards: list[Card] = field(default_factory=list)
    result: RoundResult | None = None
    #: While True the dealer's second card is face down.
    dealer_hole_hidden: bool = True

    # -- betting -----------------------------------------------------------

    def cycle_bet(self) -> int:
        """BET button: 1 -> 2 -> 3 -> 4 -> 1 (bounds from config)."""
        if self.phase is not Phase.BETTING:
            return self.bet_quarters
        nxt = self.bet_quarters + 1
        if nxt > config.MAX_BET_QUARTERS:
            nxt = config.MIN_BET_QUARTERS
        self.bet_quarters = nxt
        return self.bet_quarters

    def clamp_bet(self, balance_quarters: int) -> int:
        """Trim the bet down to what the player can actually afford."""
        affordable = min(config.MAX_BET_QUARTERS, balance_quarters)
        if affordable < config.MIN_BET_QUARTERS:
            self.bet_quarters = config.MIN_BET_QUARTERS
        elif self.bet_quarters > affordable:
            self.bet_quarters = affordable
        return self.bet_quarters

    def can_deal(self, balance_quarters: int) -> bool:
        return self.phase is Phase.BETTING and balance_quarters >= self.bet_quarters

    # -- the hand ----------------------------------------------------------

    def deal(self) -> None:
        """Start a hand at the current bet. Caller must have debited the bank."""
        if self.phase is not Phase.BETTING:
            raise RuntimeError(f"cannot deal from phase {self.phase.name}")

        self.shoe.reshuffle_if_needed()  # cut card is honoured between hands only
        self.result = None
        self.dealer_hole_hidden = True
        # Dealt alternating, player first, exactly as at a table: player,
        # dealer up-card, player, dealer hole card.
        self.player_cards = []
        self.dealer_cards = []
        for _ in range(2):
            self.player_cards.append(self.shoe.draw())
            self.dealer_cards.append(self.shoe.draw())
        self.phase = Phase.PLAYER_TURN

        # A natural on either side ends the hand immediately -- no player turn.
        if is_blackjack(self.player_cards) or is_blackjack(self.dealer_cards):
            self._settle()

    def hit(self) -> None:
        if self.phase is not Phase.PLAYER_TURN:
            return
        self.player_cards.append(self.shoe.draw())
        if is_bust(self.player_cards):
            self._settle()  # dealer never draws against a busted player
        elif hand_total(self.player_cards) == BLACKJACK:
            self.stand()  # 21: nothing left to decide, don't make them press it

    def stand(self) -> None:
        if self.phase is not Phase.PLAYER_TURN:
            return
        self.phase = Phase.DEALER_TURN
        self.dealer_hole_hidden = False

    def dealer_step(self) -> bool:
        """Draw at most ONE dealer card. Returns True if the dealer drew.

        Called on a timer by the UI so the dealer's hand plays out at a
        watchable pace instead of appearing all at once. Settles when done.
        """
        if self.phase is not Phase.DEALER_TURN:
            return False
        if dealer_should_hit(self.dealer_cards):
            self.dealer_cards.append(self.shoe.draw())
            return True
        self._settle()
        return False

    def _settle(self) -> None:
        outcome = determine_outcome(self.player_cards, self.dealer_cards)
        self.result = RoundResult(
            outcome=outcome,
            bet_quarters=self.bet_quarters,
            returned_quarters=payout_quarters(outcome, self.bet_quarters),
            player_cards=list(self.player_cards),
            dealer_cards=list(self.dealer_cards),
        )
        self.dealer_hole_hidden = False
        self.phase = Phase.SETTLED

    def clear(self) -> None:
        """Return to BETTING, leaving the bet where the player set it."""
        self.phase = Phase.BETTING
        self.player_cards = []
        self.dealer_cards = []
        self.result = None
        self.dealer_hole_hidden = True

    # -- introspection for the UI -----------------------------------------

    @property
    def player_total(self) -> int:
        return hand_total(self.player_cards)

    @property
    def player_is_soft(self) -> bool:
        return hand_value(self.player_cards)[1]

    @property
    def dealer_visible_cards(self) -> list[Card]:
        if self.dealer_hole_hidden:
            return self.dealer_cards[:1]
        return self.dealer_cards

    @property
    def dealer_visible_total(self) -> int:
        return hand_total(self.dealer_visible_cards)


# ---------------------------------------------------------------------------
# EXTENSION POINTS -- deliberately not built (out of scope), sketched so the
# shape of the change is obvious later.
# ---------------------------------------------------------------------------
#
# DOUBLE DOWN
#   BlackjackGame.double(): assert PLAYER_TURN and len(player_cards) == 2 and
#   the bank can cover a second bet; debit it, set self.bet_quarters *= 2, draw
#   exactly one card, then stand(). payout_quarters() already handles any bet
#   size, so no money changes are needed. Note the doubled bet may exceed
#   MAX_BET_QUARTERS -- decide whether that ceiling is per-wager or per-hand.
#
# SPLIT
#   The big one: player_cards must become a list[Hand] with an active index,
#   and settle() must produce one RoundResult per hand. Everything money-side
#   (payout_quarters, blackjack_bonus_quarters) already works per-hand, so the
#   change is confined to this file and to ui.py's layout.
#
# INSURANCE / SURRENDER
#   Both need a decision point between deal() and PLAYER_TURN, i.e. a new
#   Phase.OFFER_INSURANCE that deal() enters when the up-card is an ace.
#   Insurance pays 2:1 on a half bet -- which re-opens exactly the same
#   odd-quarter rounding question, so route it through
#   blackjack_bonus_quarters()-style integer math, never a float.

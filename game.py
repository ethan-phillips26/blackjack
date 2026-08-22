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


def determine_outcome(
    player: list[Card], dealer: list[Card], player_natural: bool | None = None
) -> Outcome:
    """Compare two finished hands. Player bust is checked first: a player who
    busts loses even if the dealer would also have busted.

    `player_natural` overrides the two-card check, and exists for exactly one
    case: a hand made by SPLITTING that reaches 21 on its two cards is
    twenty-one, not a natural, and pays 1:1 rather than 3:2. Leave it None and
    the hand speaks for itself.
    """
    if is_bust(player):
        return Outcome.PLAYER_BUST

    if player_natural is None:
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
class Hand:
    """One wager and the cards riding on it.

    A round is a LIST of these. Without splitting the list is always one long,
    which is why every money property here is per-hand: doubling changes this
    hand's bet, splitting makes another Hand with its own bet, and settlement
    compares each Hand against the one dealer hand independently.
    """

    cards: list[Card] = field(default_factory=list)
    #: Quarters riding on THIS hand. Doubling doubles it. The player's credit
    #: meter was debited for every quarter counted here, at the moment it was
    #: committed -- never retroactively.
    bet_quarters: int = config.MIN_BET_QUARTERS
    doubled: bool = False
    #: True when this hand came out of a split, which is what stops a two-card
    #: 21 here being paid as a natural.
    from_split: bool = False
    #: The player has no more decisions on this hand: stood, doubled, busted,
    #: hit to 21, or it is a split ace that gets exactly one card.
    finished: bool = False

    @property
    def total(self) -> int:
        return hand_total(self.cards)

    @property
    def is_soft(self) -> bool:
        return hand_value(self.cards)[1]

    @property
    def is_bust(self) -> bool:
        return is_bust(self.cards)

    @property
    def is_natural(self) -> bool:
        """A natural pays 3:2 -- and a split hand can never have one.

        Two cards totalling 21 after a split is TWENTY-ONE and pays 1:1. Every
        house plays it this way, which is why it is enforced here rather than
        left to a config flag.
        """
        return not self.from_split and is_blackjack(self.cards)

    @property
    def is_pair(self) -> bool:
        """Splittable-looking: two cards of the same rank, or -- unless
        config.SPLIT_REQUIRES_SAME_RANK -- of the same VALUE, so K-10 pairs."""
        if len(self.cards) != 2:
            return False
        first, second = self.cards
        if config.SPLIT_REQUIRES_SAME_RANK:
            return first.rank == second.rank
        return first.base_value == second.base_value


@dataclass
class HandResult:
    """What one hand did, and what it gets back."""

    outcome: Outcome
    bet_quarters: int  # total wagered on this hand, doubling included
    returned_quarters: int  # gross back to the balance
    cards: list[Card]
    doubled: bool = False
    from_split: bool = False

    @property
    def net_quarters(self) -> int:
        return self.returned_quarters - self.bet_quarters

    @property
    def message(self) -> str:
        return OUTCOME_MESSAGES[self.outcome]


OUTCOME_MESSAGES = {
    Outcome.PLAYER_BLACKJACK: "BLACKJACK!",
    Outcome.PLAYER_WIN: "YOU WIN",
    Outcome.DEALER_BUST: "DEALER BUSTS",
    Outcome.PUSH: "PUSH",
    Outcome.PLAYER_BUST: "BUST",
    Outcome.DEALER_WIN: "DEALER WINS",
}


@dataclass
class RoundResult:
    """Everything the round settled to: one HandResult per hand played.

    The aggregate properties (bet_quarters, returned_quarters, net_quarters)
    are what main.py credits and what the screen shows, and they are plain
    integer sums -- there is nothing to round at this level, because each
    hand's 3:2 was already resolved to whole quarters in payout_quarters().
    """

    hands: list[HandResult]
    dealer_cards: list[Card] = field(default_factory=list)

    @property
    def bet_quarters(self) -> int:
        return sum(h.bet_quarters for h in self.hands)

    @property
    def returned_quarters(self) -> int:
        return sum(h.returned_quarters for h in self.hands)

    @property
    def net_quarters(self) -> int:
        """Signed change to the player's balance across the whole round."""
        return self.returned_quarters - self.bet_quarters

    @property
    def player_cards(self) -> list[Card]:
        """The first (usually only) hand's cards."""
        return self.hands[0].cards if self.hands else []

    @property
    def outcome(self) -> Outcome:
        """One hand: its own outcome. Split: the round's NET reduced to one.

        A split round can genuinely be a win and a loss at once, so there is no
        honest single Outcome for it -- the money is what matters, and that is
        what this reports. The per-hand truth is still in self.hands, which is
        what the screen draws next to each hand.
        """
        if len(self.hands) == 1:
            return self.hands[0].outcome
        net = self.net_quarters
        if net > 0:
            return Outcome.PLAYER_WIN
        if net < 0:
            return Outcome.DEALER_WIN
        return Outcome.PUSH

    @property
    def message(self) -> str:
        if len(self.hands) == 1:
            return OUTCOME_MESSAGES[self.outcome]
        # A split round is summarised by the MONEY. "DEALER WINS" over a hand
        # the player can see they won would read as a bug, so the banner talks
        # about the round's net and the per-hand truth is shown next to each
        # hand instead.
        net = self.net_quarters
        if net > 0:
            return "YOU WIN"
        if net < 0:
            return "YOU LOSE"
        return "EVEN"


@dataclass
class BlackjackGame:
    """One seat at one table. Owns the shoe and the current round.

    Knows nothing about coins, GPIO, or pixels: the caller deducts every wager
    from the bank BEFORE the corresponding call here (deal, double, split) and
    credits result.returned_quarters after settle.
    """

    shoe: Shoe = field(default_factory=Shoe)
    phase: Phase = Phase.BETTING
    #: The bet the BET button sets, i.e. what the NEXT hand starts at. Once a
    #: hand is dealt the money lives on the Hand objects, because doubling and
    #: splitting make the hands disagree with each other.
    bet_quarters: int = config.MIN_BET_QUARTERS
    hands: list[Hand] = field(default_factory=list)
    #: Which hand the player is acting on. Only meaningful in PLAYER_TURN.
    active_index: int = 0
    dealer_cards: list[Card] = field(default_factory=list)
    result: RoundResult | None = None
    #: While True the dealer's second card is face down.
    dealer_hole_hidden: bool = True
    #: Bumped on every split, ever -- never reset by clear(). The renderer
    #: watches it to know that one hand became two, which it cannot infer from
    #: the cards alone (a row getting SHORTER otherwise means a new deal).
    split_serial: int = 0

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

    @property
    def wagered_quarters(self) -> int:
        """Everything riding on the table right now, across every hand.

        This is what the BET readout shows mid-hand: after a double or a split
        the player has more than the opening bet at stake, and a display still
        insisting on the opening number would be lying about their money.
        """
        if not self.hands:
            return self.bet_quarters
        return sum(hand.bet_quarters for hand in self.hands)

    # -- the hand ----------------------------------------------------------

    def deal(self) -> None:
        """Start a hand at the current bet. Caller must have debited the bank."""
        if self.phase is not Phase.BETTING:
            raise RuntimeError(f"cannot deal from phase {self.phase.name}")

        self.shoe.reshuffle_if_needed()  # cut card is honoured between hands only
        self.result = None
        self.dealer_hole_hidden = True
        self.active_index = 0
        # Dealt alternating, player first, exactly as at a table: player,
        # dealer up-card, player, dealer hole card.
        hand = Hand(bet_quarters=self.bet_quarters)
        self.hands = [hand]
        self.dealer_cards = []
        for _ in range(2):
            hand.cards.append(self.shoe.draw())
            self.dealer_cards.append(self.shoe.draw())
        self.phase = Phase.PLAYER_TURN

        # A natural on either side ends the hand immediately -- no player turn,
        # and so no chance to double or split into a hand that was already over.
        if hand.is_natural or is_blackjack(self.dealer_cards):
            hand.finished = True
            self._settle()

    @property
    def active_hand(self) -> Hand | None:
        if self.phase is not Phase.PLAYER_TURN:
            return None
        if 0 <= self.active_index < len(self.hands):
            return self.hands[self.active_index]
        return None

    def hit(self) -> None:
        hand = self.active_hand
        if hand is None or hand.finished:
            return
        self._draw_to(hand)
        if hand.finished:
            self._advance()

    def stand(self) -> None:
        hand = self.active_hand
        if hand is None:
            return
        hand.finished = True
        self._advance()

    # -- double ------------------------------------------------------------

    def double_cost(self) -> int:
        """Quarters the player must have on the meter to double right now.

        The second wager always equals the first, so this is simply the active
        hand's current bet -- and the caller must debit exactly this before
        calling double().
        """
        hand = self.active_hand
        return hand.bet_quarters if hand is not None else 0

    def can_double(self, balance_quarters: int) -> bool:
        """Legal AND affordable. main.py asks this before touching the bank."""
        if not config.ALLOW_DOUBLE:
            return False
        hand = self.active_hand
        if hand is None or hand.finished or len(hand.cards) != 2:
            return False
        if hand.from_split and not config.DOUBLE_AFTER_SPLIT:
            return False
        if not config.DOUBLE_ANY_TWO_CARDS:
            if hand.total not in config.DOUBLE_ALLOWED_TOTALS:
                return False
        return balance_quarters >= hand.bet_quarters

    def double(self) -> bool:
        """Second wager, exactly one more card, hand over.

        Returns False and changes nothing if the move is not legal, so a caller
        that has already debited the bank can put the quarters back. main.py
        checks can_double() first and never sees this.

        No money math is needed beyond doubling bet_quarters: payout_quarters()
        works on whatever a hand's bet ended up being, and a doubled hand can
        never be a natural (it has three cards by the time it settles).
        """
        hand = self.active_hand
        if hand is None or hand.finished or len(hand.cards) != 2:
            return False
        hand.bet_quarters *= 2
        hand.doubled = True
        self._draw_to(hand)
        hand.finished = True  # one card only, whatever it was
        self._advance()
        return True

    # -- split -------------------------------------------------------------

    def split_cost(self) -> int:
        """Quarters needed to split: a matching wager on the new hand."""
        hand = self.active_hand
        return hand.bet_quarters if hand is not None else 0

    def can_split(self, balance_quarters: int) -> bool:
        if not config.ALLOW_SPLIT:
            return False
        hand = self.active_hand
        if hand is None or hand.finished or not hand.is_pair:
            return False
        if len(self.hands) >= config.MAX_SPLIT_HANDS:
            return False
        if hand.cards[0].is_ace and hand.from_split and not config.RESPLIT_ACES:
            return False
        return balance_quarters >= hand.bet_quarters

    def split(self) -> bool:
        """Turn the active hand's pair into two hands, one card each.

        The new hand is inserted immediately AFTER the active one, so the
        player finishes the left hand before the right one ever gets a second
        card -- the same order as a live table, and the reason the renderer can
        follow along with a single "a hand became two" signal.

        Returns False and changes nothing if the move is not legal.
        """
        hand = self.active_hand
        if hand is None or hand.finished or not hand.is_pair:
            return False
        if len(self.hands) >= config.MAX_SPLIT_HANDS:
            return False

        moved = hand.cards.pop()
        hand.from_split = True
        sibling = Hand(
            cards=[moved], bet_quarters=hand.bet_quarters, from_split=True
        )
        self.hands.insert(self.active_index + 1, sibling)
        self.split_serial += 1

        # The hand being played gets its replacement card straight away; the
        # sibling waits until the player reaches it (see _advance).
        self._draw_to(hand)
        if hand.finished:
            self._advance()
        return True

    # -- turn plumbing -----------------------------------------------------

    def _draw_to(self, hand: Hand) -> None:
        """One card onto `hand`, then apply every rule that closes it out."""
        hand.cards.append(self.shoe.draw())

        if hand.is_bust:
            hand.finished = True
        elif hand.from_split and hand.cards[0].is_ace and not config.HIT_SPLIT_ACES:
            # Split aces get one card each and are done. Without this rule two
            # aces would be a licence to draw to two soft hands.
            hand.finished = True
        elif hand.total == BLACKJACK:
            hand.finished = True  # nothing left to decide; don't make them press it

    def _advance(self) -> None:
        """Move to the next hand the player can act on, or end the player turn."""
        while self.active_index < len(self.hands):
            hand = self.hands[self.active_index]
            if len(hand.cards) == 1:
                # A hand that has been waiting since the split: deal its second
                # card now, at the moment the player arrives at it.
                self._draw_to(hand)
            if not hand.finished:
                return  # the player is up on this one
            self.active_index += 1

        # Every hand is closed out.
        self.dealer_hole_hidden = False
        if all(hand.is_bust for hand in self.hands):
            # Nothing left to beat -- the dealer never draws against a table
            # that has already paid the house.
            self._settle()
        else:
            self.phase = Phase.DEALER_TURN

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
        results = []
        for hand in self.hands:
            outcome = determine_outcome(
                hand.cards, self.dealer_cards, player_natural=hand.is_natural
            )
            results.append(
                HandResult(
                    outcome=outcome,
                    bet_quarters=hand.bet_quarters,
                    returned_quarters=payout_quarters(outcome, hand.bet_quarters),
                    cards=list(hand.cards),
                    doubled=hand.doubled,
                    from_split=hand.from_split,
                )
            )
        self.result = RoundResult(hands=results, dealer_cards=list(self.dealer_cards))
        self.dealer_hole_hidden = False
        self.phase = Phase.SETTLED

    def clear(self) -> None:
        """Return to BETTING, leaving the bet where the player set it."""
        self.phase = Phase.BETTING
        self.hands = []
        self.active_index = 0
        self.dealer_cards = []
        self.result = None
        self.dealer_hole_hidden = True

    # -- introspection for the UI -----------------------------------------

    @property
    def player_cards(self) -> list[Card]:
        """The active hand's cards -- or the first hand's, once the round is
        over and nobody is "active" any more."""
        if not self.hands:
            return []
        index = min(self.active_index, len(self.hands) - 1)
        return self.hands[index].cards

    @property
    def player_total(self) -> int:
        return hand_total(self.player_cards)

    @property
    def player_is_soft(self) -> bool:
        return hand_value(self.player_cards)[1]

    @property
    def is_split(self) -> bool:
        return len(self.hands) > 1

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
# INSURANCE / SURRENDER
#   Both need a decision point between deal() and PLAYER_TURN, i.e. a new
#   Phase.OFFER_INSURANCE that deal() enters when the up-card is an ace.
#   Insurance pays 2:1 on a half bet -- which re-opens exactly the same
#   odd-quarter rounding question, so route it through
#   blackjack_bonus_quarters()-style integer math, never a float. Note that a
#   half bet of one quarter is not payable at all in coins: the cleanest answer
#   for this machine is to offer insurance only on even bets.
#
# SIX-CARD CHARLIE / other automatic winners
#   One more clause in _draw_to(), next to the bust and 21 checks.

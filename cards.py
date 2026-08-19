"""
Card and shoe model. Pure data -- no pygame, no GPIO, no I/O.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import config

RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")
SUITS = ("S", "H", "D", "C")

SUIT_SYMBOLS = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}
RED_SUITS = frozenset({"H", "D"})


@dataclass(frozen=True, slots=True)
class Card:
    rank: str
    suit: str

    def __post_init__(self) -> None:
        if self.rank not in RANKS:
            raise ValueError(f"bad rank: {self.rank!r}")
        if self.suit not in SUITS:
            raise ValueError(f"bad suit: {self.suit!r}")

    @property
    def is_ace(self) -> bool:
        return self.rank == "A"

    @property
    def base_value(self) -> int:
        """Ace counts 11 here; game.hand_value() demotes it to 1 as needed."""
        if self.rank == "A":
            return 11
        if self.rank in ("10", "J", "Q", "K"):
            return 10
        return int(self.rank)

    @property
    def is_red(self) -> bool:
        return self.suit in RED_SUITS

    @property
    def symbol(self) -> str:
        return SUIT_SYMBOLS[self.suit]

    def __str__(self) -> str:
        return f"{self.rank}{self.symbol}"


def build_deck() -> list[Card]:
    return [Card(rank, suit) for suit in SUITS for rank in RANKS]


class Shoe:
    """A multi-deck shoe with cut-card reshuffling.

    Pass a seed (or a random.Random) to make dealing deterministic in tests.
    """

    def __init__(
        self,
        num_decks: int = config.NUM_DECKS,
        reshuffle_threshold: float = config.SHOE_RESHUFFLE_THRESHOLD,
        rng: random.Random | None = None,
        seed: int | None = None,
    ) -> None:
        if num_decks < 1:
            raise ValueError("num_decks must be >= 1")
        self.num_decks = num_decks
        self.reshuffle_threshold = reshuffle_threshold
        self.rng = rng if rng is not None else random.Random(seed)
        self.total_cards = num_decks * 52
        self._cards: list[Card] = []
        #: Bumped every reshuffle -- the UI flashes a "SHUFFLING" message on change.
        self.shuffle_count = 0
        self.shuffle()

    def shuffle(self) -> None:
        self._cards = []
        for _ in range(self.num_decks):
            self._cards.extend(build_deck())
        self.rng.shuffle(self._cards)
        self.shuffle_count += 1

    @property
    def cards_remaining(self) -> int:
        return len(self._cards)

    @property
    def needs_reshuffle(self) -> bool:
        """True once we've dealt past the cut card.

        Only ever acted on BETWEEN hands -- never reshuffle mid-hand.
        """
        return self.cards_remaining < self.total_cards * self.reshuffle_threshold

    def reshuffle_if_needed(self) -> bool:
        if self.needs_reshuffle:
            self.shuffle()
            return True
        return False

    def draw(self) -> Card:
        if not self._cards:
            # Belt and braces: a hand should never outrun the cut card, but
            # running dry mid-hand must not crash a machine holding money.
            self.shuffle()
        return self._cards.pop()

    def stack(self, cards: list[Card]) -> None:
        """Force the next cards to be dealt, for tests. cards[0] comes first."""
        self._cards.extend(reversed(cards))

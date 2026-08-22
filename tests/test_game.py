"""
Rules engine and money math.

Runs on a bare Python install -- importing this must not pull in pygame or
gpiozero, and test_no_gui_dependencies() asserts exactly that.
"""

import sys
import unittest
from unittest import mock

import config
from cards import Card, Shoe
from game import (
    BlackjackGame,
    Hand,
    Outcome,
    Phase,
    blackjack_bonus_quarters,
    dealer_should_hit,
    determine_outcome,
    hand_total,
    hand_value,
    is_blackjack,
    is_bust,
    payout_quarters,
)


def hand(*specs: str) -> list[Card]:
    """hand("AS", "10H") -> two Cards. Rank first, suit last."""
    return [Card(spec[:-1], spec[-1]) for spec in specs]


class TestHandValue(unittest.TestCase):
    def test_simple_totals(self):
        self.assertEqual(hand_value(hand("5H", "9S")), (14, False))
        self.assertEqual(hand_value(hand("KD", "QC")), (20, False))

    def test_ace_counts_eleven_when_it_fits(self):
        self.assertEqual(hand_value(hand("AS", "6H")), (17, True))

    def test_ace_demotes_to_one_to_avoid_bust(self):
        # A + 6 + 10 would be 27 as a soft hand, so the ace drops to 1.
        self.assertEqual(hand_value(hand("AS", "6H", "10D")), (17, False))

    def test_multiple_aces_demote_one_at_a_time(self):
        self.assertEqual(hand_value(hand("AS", "AH")), (12, True))
        self.assertEqual(hand_value(hand("AS", "AH", "9D")), (21, True))
        self.assertEqual(hand_value(hand("AS", "AH", "AD", "AC")), (14, True))
        self.assertEqual(hand_value(hand("AS", "AH", "9D", "5C")), (16, False))

    def test_bust_detection(self):
        self.assertTrue(is_bust(hand("KS", "QH", "5D")))
        self.assertFalse(is_bust(hand("KS", "QH")))


class TestBlackjackDetection(unittest.TestCase):
    def test_natural_is_exactly_two_cards(self):
        self.assertTrue(is_blackjack(hand("AS", "KH")))
        self.assertTrue(is_blackjack(hand("10D", "AC")))

    def test_twentyone_on_three_cards_is_not_a_natural(self):
        cards = hand("7S", "4H", "10D")
        self.assertEqual(hand_total(cards), 21)
        self.assertFalse(is_blackjack(cards))

    def test_two_cards_under_21_is_not_a_natural(self):
        self.assertFalse(is_blackjack(hand("AS", "9H")))


class TestDealerDrawLogic(unittest.TestCase):
    def test_hits_below_seventeen(self):
        self.assertTrue(dealer_should_hit(hand("10S", "6H")))
        self.assertTrue(dealer_should_hit(hand("2S", "3H")))

    def test_stands_on_hard_seventeen_and_above(self):
        self.assertFalse(dealer_should_hit(hand("10S", "7H")))
        self.assertFalse(dealer_should_hit(hand("10S", "9H")))

    def test_stands_on_soft_seventeen_by_default(self):
        self.assertFalse(config.DEALER_HITS_SOFT_17)
        self.assertFalse(dealer_should_hit(hand("AS", "6H")))

    def test_hits_soft_seventeen_when_configured(self):
        with mock.patch.object(config, "DEALER_HITS_SOFT_17", True):
            self.assertTrue(dealer_should_hit(hand("AS", "6H")))
            # Still stands on HARD 17 -- the flag is soft-17 only.
            self.assertFalse(dealer_should_hit(hand("10S", "7H")))

    def test_soft_eighteen_always_stands(self):
        with mock.patch.object(config, "DEALER_HITS_SOFT_17", True):
            self.assertFalse(dealer_should_hit(hand("AS", "7H")))


class TestOutcomes(unittest.TestCase):
    def test_player_bust_loses_even_if_dealer_would_bust(self):
        outcome = determine_outcome(hand("KS", "QH", "5D"), hand("KC", "QD", "3H"))
        self.assertIs(outcome, Outcome.PLAYER_BUST)

    def test_dealer_bust(self):
        self.assertIs(
            determine_outcome(hand("10S", "8H"), hand("KC", "QD", "3H")),
            Outcome.DEALER_BUST,
        )

    def test_higher_total_wins(self):
        self.assertIs(
            determine_outcome(hand("10S", "9H"), hand("10C", "8D")), Outcome.PLAYER_WIN
        )
        self.assertIs(
            determine_outcome(hand("10S", "7H"), hand("10C", "8D")), Outcome.DEALER_WIN
        )

    def test_equal_totals_push(self):
        self.assertIs(
            determine_outcome(hand("10S", "8H"), hand("9C", "9D")), Outcome.PUSH
        )

    def test_natural_beats_a_three_card_twentyone(self):
        outcome = determine_outcome(hand("7S", "4H", "10D"), hand("AC", "KD"))
        self.assertIs(outcome, Outcome.DEALER_WIN)

    def test_player_natural(self):
        self.assertIs(
            determine_outcome(hand("AS", "KH"), hand("10C", "9D")),
            Outcome.PLAYER_BLACKJACK,
        )

    def test_two_naturals_push(self):
        self.assertIs(
            determine_outcome(hand("AS", "KH"), hand("AC", "QD")), Outcome.PUSH
        )


class TestPayoutMath(unittest.TestCase):
    """Every assertion here also asserts the value is an int, because a float
    creeping into the accounting path is the bug this whole design prevents."""

    def assertIntEqual(self, actual, expected):
        self.assertIsInstance(actual, int)
        self.assertNotIsInstance(actual, bool)
        self.assertEqual(actual, expected)

    def test_loss_returns_nothing(self):
        for outcome in (Outcome.PLAYER_BUST, Outcome.DEALER_WIN):
            for bet in range(1, config.MAX_BET_QUARTERS + 1):
                self.assertIntEqual(payout_quarters(outcome, bet), 0)

    def test_push_returns_the_bet(self):
        for bet in range(1, config.MAX_BET_QUARTERS + 1):
            self.assertIntEqual(payout_quarters(Outcome.PUSH, bet), bet)

    def test_ordinary_win_pays_one_to_one(self):
        for outcome in (Outcome.PLAYER_WIN, Outcome.DEALER_BUST):
            for bet in range(1, config.MAX_BET_QUARTERS + 1):
                self.assertIntEqual(payout_quarters(outcome, bet), bet * 2)

    # -- the odd-bet rounding rule ------------------------------------

    def test_blackjack_bonus_rounds_down_by_default(self):
        self.assertEqual(config.BLACKJACK_ROUNDING, "down")
        # bet 1 -> 1.5 -> 1 ;  bet 3 -> 4.5 -> 4 ;  evens are exact.
        expected = {1: 1, 2: 3, 3: 4, 4: 6}
        for bet, bonus in expected.items():
            self.assertIntEqual(blackjack_bonus_quarters(bet), bonus)

    def test_blackjack_bonus_rounding_up(self):
        with mock.patch.object(config, "BLACKJACK_ROUNDING", "up"):
            expected = {1: 2, 2: 3, 3: 5, 4: 6}
            for bet, bonus in expected.items():
                self.assertIntEqual(blackjack_bonus_quarters(bet), bonus)

    def test_blackjack_bonus_rounding_nearest_ties_to_even(self):
        with mock.patch.object(config, "BLACKJACK_ROUNDING", "nearest"):
            # 1.5 -> 2 (2 is even); 4.5 -> 4 (4 is even).
            expected = {1: 2, 2: 3, 3: 4, 4: 6}
            for bet, bonus in expected.items():
                self.assertIntEqual(blackjack_bonus_quarters(bet), bonus)

    def test_even_bets_are_exact_under_every_policy(self):
        for policy in ("down", "up", "nearest"):
            with mock.patch.object(config, "BLACKJACK_ROUNDING", policy):
                self.assertIntEqual(blackjack_bonus_quarters(2), 3)
                self.assertIntEqual(blackjack_bonus_quarters(4), 6)

    def test_unknown_rounding_policy_is_rejected_loudly(self):
        with mock.patch.object(config, "BLACKJACK_ROUNDING", "sideways"):
            with self.assertRaises(ValueError):
                blackjack_bonus_quarters(1)

    def test_natural_returns_bet_plus_rounded_bonus(self):
        # Gross return, i.e. stake back plus the 3:2 bonus.
        expected = {1: 2, 2: 5, 3: 7, 4: 10}
        for bet, total in expected.items():
            self.assertIntEqual(payout_quarters(Outcome.PLAYER_BLACKJACK, bet), total)

    def test_house_keeps_the_half_quarter_on_odd_bets(self):
        # The whole point of the rounding rule: a 1-quarter natural nets +1,
        # not +1.5, and a 3-quarter natural nets +4, not +4.5.
        self.assertIntEqual(payout_quarters(Outcome.PLAYER_BLACKJACK, 1) - 1, 1)
        self.assertIntEqual(payout_quarters(Outcome.PLAYER_BLACKJACK, 3) - 3, 4)


class TestShoe(unittest.TestCase):
    def test_deck_composition(self):
        shoe = Shoe(num_decks=6, seed=1)
        self.assertEqual(shoe.cards_remaining, 312)

    def test_reshuffles_at_the_cut_card(self):
        shoe = Shoe(num_decks=1, reshuffle_threshold=0.5, seed=1)
        for _ in range(30):  # past 50% of 52
            shoe.draw()
        self.assertTrue(shoe.needs_reshuffle)
        self.assertTrue(shoe.reshuffle_if_needed())
        self.assertEqual(shoe.cards_remaining, 52)

    def test_never_runs_dry(self):
        shoe = Shoe(num_decks=1, seed=1)
        for _ in range(60):  # more draws than a single deck holds
            self.assertIsNotNone(shoe.draw())

    def test_stack_deals_in_order(self):
        shoe = Shoe(num_decks=1, seed=1)
        shoe.stack(hand("AS", "KH"))
        self.assertEqual(str(shoe.draw()), "A♠")
        self.assertEqual(str(shoe.draw()), "K♥")


class TestRoundFlow(unittest.TestCase):
    def make_game(self, *cards: str) -> BlackjackGame:
        """Stack the shoe in deal order: player, dealer, player, dealer, ..."""
        game = BlackjackGame(shoe=Shoe(num_decks=1, seed=7))
        game.shoe.stack(hand(*cards))
        return game

    def test_player_natural_settles_immediately(self):
        game = self.make_game("AS", "9C", "KH", "7D")
        game.bet_quarters = 2
        game.deal()
        self.assertIs(game.phase, Phase.SETTLED)
        self.assertIs(game.result.outcome, Outcome.PLAYER_BLACKJACK)
        self.assertEqual(game.result.returned_quarters, 5)  # 2 back + 3 bonus
        self.assertEqual(game.result.net_quarters, 3)
        self.assertFalse(game.dealer_hole_hidden)  # hole card is revealed

    def test_dealer_natural_settles_immediately(self):
        game = self.make_game("10S", "AC", "9H", "KD")
        game.deal()
        self.assertIs(game.phase, Phase.SETTLED)
        self.assertIs(game.result.outcome, Outcome.DEALER_WIN)
        self.assertEqual(game.result.returned_quarters, 0)

    def test_hit_to_bust_ends_the_hand_without_the_dealer_drawing(self):
        game = self.make_game("10S", "9C", "8H", "7D", "9S")
        game.deal()
        self.assertIs(game.phase, Phase.PLAYER_TURN)
        game.hit()  # 10 + 8 + 9 = 27
        self.assertIs(game.phase, Phase.SETTLED)
        self.assertIs(game.result.outcome, Outcome.PLAYER_BUST)
        self.assertEqual(len(game.dealer_cards), 2)  # dealer never drew

    def test_hitting_to_21_stands_automatically(self):
        game = self.make_game("7S", "9C", "4H", "6D", "10S")
        game.deal()
        game.hit()  # 7 + 4 + 10 = 21
        self.assertIn(game.phase, (Phase.DEALER_TURN, Phase.SETTLED))
        self.assertFalse(game.dealer_hole_hidden)

    def test_dealer_draws_one_card_per_step_until_seventeen(self):
        game = self.make_game("10S", "5C", "9H", "6D", "4S", "3H")
        game.deal()
        game.stand()
        self.assertIs(game.phase, Phase.DEALER_TURN)

        steps = 0
        while game.phase is Phase.DEALER_TURN and steps < 10:
            game.dealer_step()
            steps += 1
        self.assertIs(game.phase, Phase.SETTLED)
        self.assertGreaterEqual(hand_total(game.dealer_cards), 17)

    def test_hole_card_is_hidden_during_the_player_turn(self):
        game = self.make_game("10S", "5C", "9H", "6D")
        game.deal()
        self.assertTrue(game.dealer_hole_hidden)
        self.assertEqual(len(game.dealer_visible_cards), 1)
        game.stand()
        self.assertFalse(game.dealer_hole_hidden)
        self.assertEqual(len(game.dealer_visible_cards), 2)

    def test_push_returns_the_stake(self):
        game = self.make_game("10S", "10C", "8H", "8D")
        game.bet_quarters = 3
        game.deal()
        game.stand()
        while game.phase is Phase.DEALER_TURN:
            game.dealer_step()
        self.assertIs(game.result.outcome, Outcome.PUSH)
        self.assertEqual(game.result.returned_quarters, 3)
        self.assertEqual(game.result.net_quarters, 0)

    def test_cannot_deal_twice(self):
        game = self.make_game("10S", "5C", "9H", "6D")
        game.deal()
        with self.assertRaises(RuntimeError):
            game.deal()

    def test_hit_and_stand_are_ignored_outside_the_player_turn(self):
        game = self.make_game("10S", "5C", "9H", "6D")
        game.hit()  # still BETTING
        game.stand()
        self.assertIs(game.phase, Phase.BETTING)
        self.assertEqual(game.player_cards, [])

    def test_clear_returns_to_betting_and_keeps_the_bet(self):
        game = self.make_game("AS", "9C", "KH", "7D")
        game.bet_quarters = 4
        game.deal()
        game.clear()
        self.assertIs(game.phase, Phase.BETTING)
        self.assertEqual(game.bet_quarters, 4)
        self.assertIsNone(game.result)


class TestBetting(unittest.TestCase):
    def test_bet_cycles_and_wraps(self):
        game = BlackjackGame(shoe=Shoe(seed=1))
        self.assertEqual(game.bet_quarters, 1)
        self.assertEqual([game.cycle_bet() for _ in range(4)], [2, 3, 4, 1])

    def test_bet_is_frozen_mid_hand(self):
        game = BlackjackGame(shoe=Shoe(seed=1))
        game.deal()
        if game.phase is Phase.PLAYER_TURN:
            self.assertEqual(game.cycle_bet(), 1)

    def test_clamp_bet_to_affordable(self):
        game = BlackjackGame(shoe=Shoe(seed=1))
        game.bet_quarters = 4
        self.assertEqual(game.clamp_bet(2), 2)
        self.assertEqual(game.clamp_bet(0), 1)  # floor at the minimum

    def test_can_deal_requires_the_balance_to_cover_the_bet(self):
        game = BlackjackGame(shoe=Shoe(seed=1))
        game.bet_quarters = 3
        self.assertFalse(game.can_deal(2))
        self.assertTrue(game.can_deal(3))



class TestDouble(unittest.TestCase):
    """Double down: a second wager, exactly one card, hand over."""

    def make_game(self, *cards: str) -> BlackjackGame:
        game = BlackjackGame(shoe=Shoe(num_decks=1, seed=7))
        game.shoe.stack(hand(*cards))
        return game

    def play_out_dealer(self, game: BlackjackGame) -> None:
        while game.phase is Phase.DEALER_TURN:
            game.dealer_step()

    def test_double_takes_one_card_and_ends_the_hand(self):
        game = self.make_game("6S", "9C", "5H", "7D", "10S")
        game.bet_quarters = 2
        game.deal()  # player 6+5 = 11, dealer shows 9
        self.assertTrue(game.can_double(balance_quarters=2))
        self.assertEqual(game.double_cost(), 2)

        self.assertTrue(game.double())
        self.assertEqual(len(game.player_cards), 3)  # exactly one more card
        self.assertIs(game.phase, Phase.DEALER_TURN)
        self.assertFalse(game.dealer_hole_hidden)

    def test_doubling_doubles_the_wager_and_the_win(self):
        game = self.make_game("6S", "9C", "5H", "8D", "10S")
        game.bet_quarters = 2
        game.deal()
        game.double()  # 6 + 5 + 10 = 21 against the dealer's 17
        self.play_out_dealer(game)

        result = game.result
        self.assertIs(result.outcome, Outcome.PLAYER_WIN)
        self.assertEqual(result.bet_quarters, 4)  # 2 + the second wager
        self.assertEqual(result.returned_quarters, 8)  # 1:1 on 4 quarters
        self.assertEqual(result.net_quarters, 4)
        self.assertTrue(result.hands[0].doubled)

    def test_doubling_into_a_loss_costs_both_wagers(self):
        game = self.make_game("6S", "10C", "5H", "9D", "2S")
        game.bet_quarters = 3
        game.deal()
        game.double()  # 6+5+2 = 13 against the dealer's 19
        self.play_out_dealer(game)
        self.assertEqual(game.result.bet_quarters, 6)
        self.assertEqual(game.result.returned_quarters, 0)
        self.assertEqual(game.result.net_quarters, -6)

    def test_doubling_into_a_bust_settles_without_the_dealer_drawing(self):
        game = self.make_game("10S", "6C", "6H", "5D", "9S")
        game.deal()
        game.double()  # 10+6+9 = 25
        self.assertIs(game.phase, Phase.SETTLED)
        self.assertIs(game.result.outcome, Outcome.PLAYER_BUST)
        self.assertEqual(len(game.dealer_cards), 2)  # dealer never drew

    def test_cannot_double_after_hitting(self):
        game = self.make_game("5S", "9C", "4H", "7D", "3S")
        game.deal()
        game.hit()
        self.assertFalse(game.can_double(balance_quarters=99))
        self.assertFalse(game.double())
        self.assertEqual(len(game.player_cards), 3)  # nothing was drawn

    def test_cannot_double_without_the_credits(self):
        game = self.make_game("6S", "9C", "5H", "7D", "10S")
        game.bet_quarters = 3
        game.deal()
        self.assertFalse(game.can_double(balance_quarters=2))
        self.assertTrue(game.can_double(balance_quarters=3))

    def test_double_can_be_switched_off(self):
        game = self.make_game("6S", "9C", "5H", "7D", "10S")
        game.deal()
        with mock.patch.object(config, "ALLOW_DOUBLE", False):
            self.assertFalse(game.can_double(balance_quarters=99))

    def test_restricted_doubling_only_on_the_allowed_totals(self):
        with mock.patch.object(config, "DOUBLE_ANY_TWO_CARDS", False), \
                mock.patch.object(config, "DOUBLE_ALLOWED_TOTALS", (9, 10, 11)):
            game = self.make_game("6S", "9C", "5H", "7D", "10S")
            game.deal()  # 11
            self.assertTrue(game.can_double(balance_quarters=99))

            game = self.make_game("6S", "9C", "2H", "7D", "10S")
            game.deal()  # 8
            self.assertFalse(game.can_double(balance_quarters=99))

    def test_double_is_ignored_outside_the_player_turn(self):
        game = self.make_game("6S", "9C", "5H", "7D", "10S")
        self.assertFalse(game.can_double(balance_quarters=99))
        self.assertFalse(game.double())  # still BETTING
        game.deal()
        game.stand()
        self.assertFalse(game.double())  # dealer's turn


class TestSplit(unittest.TestCase):
    """Split: two hands, two wagers, settled independently."""

    def make_game(self, *cards: str) -> BlackjackGame:
        game = BlackjackGame(shoe=Shoe(num_decks=1, seed=7))
        game.shoe.stack(hand(*cards))
        return game

    def play_out_dealer(self, game: BlackjackGame) -> None:
        while game.phase is Phase.DEALER_TURN:
            game.dealer_step()

    def test_split_makes_two_hands_with_matching_wagers(self):
        game = self.make_game("8S", "5C", "8H", "6D", "3D", "2C")
        game.bet_quarters = 2
        game.deal()
        self.assertTrue(game.can_split(balance_quarters=2))
        self.assertEqual(game.split_cost(), 2)

        self.assertTrue(game.split())
        self.assertEqual(len(game.hands), 2)
        self.assertEqual([h.bet_quarters for h in game.hands], [2, 2])
        self.assertEqual(game.wagered_quarters, 4)
        # The first hand is dealt its second card at once; the second waits.
        self.assertEqual(len(game.hands[0].cards), 2)
        self.assertEqual(len(game.hands[1].cards), 1)
        self.assertEqual(game.active_index, 0)
        self.assertEqual(game.split_serial, 1)

    def test_the_second_hand_is_dealt_when_the_player_reaches_it(self):
        game = self.make_game("8S", "5C", "8H", "6D", "3D", "2C")
        game.deal()
        game.split()
        game.stand()  # done with hand one
        self.assertIs(game.phase, Phase.PLAYER_TURN)
        self.assertEqual(game.active_index, 1)
        self.assertEqual(len(game.hands[1].cards), 2)
        self.assertEqual(game.hands[1].total, 10)  # 8 + 2

    def test_hands_are_settled_independently(self):
        # Hand one draws to 20 and wins; hand two busts.
        game = self.make_game("8S", "9C", "8H", "8D", "10D", "9C", "5S")
        game.bet_quarters = 1
        game.deal()  # player 8/8, dealer 9 + 8 = 17 -> stands
        game.split()
        game.stand()  # hand one: 8 + 10 = 18
        game.hit()  # hand two: 8 + 9 = 17, + 5 = 22 -> bust, hand over
        self.play_out_dealer(game)

        result = game.result
        self.assertEqual(len(result.hands), 2)
        self.assertIs(result.hands[0].outcome, Outcome.PLAYER_WIN)
        self.assertIs(result.hands[1].outcome, Outcome.PLAYER_BUST)
        self.assertEqual(result.hands[0].returned_quarters, 2)
        self.assertEqual(result.hands[1].returned_quarters, 0)
        # Two wagers out, one paid at 1:1: the round nets exactly zero.
        self.assertEqual(result.bet_quarters, 2)
        self.assertEqual(result.returned_quarters, 2)
        self.assertEqual(result.net_quarters, 0)
        self.assertIs(result.outcome, Outcome.PUSH)  # net-zero round

    def test_twentyone_after_a_split_is_not_a_natural(self):
        # A ten on a split ace is 21, and pays 1:1 -- not 3:2.
        game = self.make_game("AS", "9C", "AH", "8D", "KD", "QC")
        game.bet_quarters = 2
        game.deal()
        game.split()
        self.play_out_dealer(game)

        result = game.result
        for hand_result in result.hands:
            self.assertEqual(hand_total(hand_result.cards), 21)
            self.assertIs(hand_result.outcome, Outcome.PLAYER_WIN)
            self.assertNotIn(hand_result.outcome, (Outcome.PLAYER_BLACKJACK,))
            self.assertEqual(hand_result.returned_quarters, 4)  # 1:1, not 3:2
        self.assertEqual(result.net_quarters, 4)

    def test_split_aces_get_exactly_one_card_each(self):
        game = self.make_game("AS", "9C", "AH", "8D", "3D", "4C", "5S")
        game.deal()
        game.split()
        # Both hands are closed out by the one-card rule, so the split alone
        # hands the round straight to the dealer.
        self.assertIs(game.phase, Phase.DEALER_TURN)
        self.assertEqual([len(h.cards) for h in game.hands], [2, 2])
        self.assertTrue(all(h.finished for h in game.hands))

    def test_split_aces_may_be_played_out_when_configured(self):
        with mock.patch.object(config, "HIT_SPLIT_ACES", True):
            game = self.make_game("AS", "9C", "AH", "8D", "3D", "4C", "5S")
            game.deal()
            game.split()
            self.assertIs(game.phase, Phase.PLAYER_TURN)
            self.assertFalse(game.hands[0].finished)  # A + 3 = soft 14

    def test_pairs_are_by_value_unless_rank_is_required(self):
        game = self.make_game("KS", "9C", "10H", "8D", "3D", "4C")
        game.deal()
        self.assertTrue(game.can_split(balance_quarters=99))
        with mock.patch.object(config, "SPLIT_REQUIRES_SAME_RANK", True):
            self.assertFalse(game.can_split(balance_quarters=99))

    def test_cannot_split_a_non_pair(self):
        game = self.make_game("9S", "9C", "8H", "8D")
        game.deal()
        self.assertFalse(game.can_split(balance_quarters=99))
        self.assertFalse(game.split())
        self.assertEqual(len(game.hands), 1)

    def test_cannot_split_without_the_credits(self):
        game = self.make_game("8S", "5C", "8H", "6D", "3D", "2C")
        game.bet_quarters = 4
        game.deal()
        self.assertFalse(game.can_split(balance_quarters=3))
        self.assertTrue(game.can_split(balance_quarters=4))

    def test_split_can_be_switched_off(self):
        game = self.make_game("8S", "5C", "8H", "6D")
        game.deal()
        with mock.patch.object(config, "ALLOW_SPLIT", False):
            self.assertFalse(game.can_split(balance_quarters=99))

    def test_resplitting_stops_at_the_configured_hand_limit(self):
        # Hand one gets another 8, which would be a re-split.
        game = self.make_game("8S", "9C", "8H", "8D", "8D", "2C")
        game.deal()
        game.split()
        self.assertEqual(len(game.hands), 2)
        self.assertTrue(game.hands[0].is_pair)
        self.assertEqual(config.MAX_SPLIT_HANDS, 2)
        self.assertFalse(game.can_split(balance_quarters=99))

        with mock.patch.object(config, "MAX_SPLIT_HANDS", 4):
            self.assertTrue(game.can_split(balance_quarters=99))
            self.assertTrue(game.split())
            self.assertEqual(len(game.hands), 3)
            # The new hand is inserted immediately after the one it came from.
            self.assertEqual(game.active_index, 0)
            self.assertEqual(game.split_serial, 2)

    def test_doubling_after_a_split(self):
        game = self.make_game("8S", "9C", "8H", "8D", "3D", "2C", "10S")
        game.bet_quarters = 1
        game.deal()
        game.split()  # hand one: 8 + 3 = 11
        self.assertTrue(game.can_double(balance_quarters=1))
        game.double()
        self.assertEqual(game.hands[0].bet_quarters, 2)
        self.assertEqual(game.wagered_quarters, 3)  # 2 doubled + 1 on hand two

        with mock.patch.object(config, "DOUBLE_AFTER_SPLIT", False):
            self.assertFalse(game.can_double(balance_quarters=99))

    def test_every_hand_busting_skips_the_dealer(self):
        game = self.make_game("8S", "9C", "8H", "8D", "10D", "9S", "10C", "8C")
        game.deal()
        game.split()
        game.hit()  # hand one: 8 + 10 + 9 = 27 -> bust
        game.hit()  # hand two: 8 + 10 = 18, + 8 = 26 -> bust
        self.assertIs(game.phase, Phase.SETTLED)
        self.assertEqual(len(game.dealer_cards), 2)  # dealer never drew
        self.assertFalse(game.dealer_hole_hidden)
        self.assertEqual(game.result.returned_quarters, 0)

    def test_a_split_round_is_summarised_by_its_net(self):
        # One hand wins, one busts: the round is a wash, and the banner has to
        # say so rather than pick one hand's outcome and call it the round's.
        game = self.make_game("8S", "9C", "8H", "8D", "10D", "9C", "5S")
        game.deal()
        game.split()
        game.stand()
        game.hit()  # busts
        self.play_out_dealer(game)
        self.assertEqual(game.result.net_quarters, 0)
        self.assertEqual(game.result.message, "EVEN")

        # ...and a single hand still gets its own words.
        game = self.make_game("10S", "9C", "9H", "8D", "5S")
        game.deal()
        game.hit()
        self.assertEqual(game.result.message, "BUST")

    def test_clear_forgets_the_split_but_not_the_serial(self):
        game = self.make_game("8S", "5C", "8H", "6D", "3D", "2C")
        game.deal()
        game.split()
        serial = game.split_serial
        game.clear()
        self.assertEqual(game.hands, [])
        self.assertEqual(game.active_index, 0)
        self.assertEqual(game.player_cards, [])
        # The renderer keys off this counter to tell a split from a new deal,
        # so it must never go backwards.
        self.assertEqual(game.split_serial, serial)


class TestHandHelpers(unittest.TestCase):
    def test_a_split_hand_can_never_hold_a_natural(self):
        dealt = Hand(cards=hand("AS", "KH"), bet_quarters=1)
        self.assertTrue(dealt.is_natural)
        after_split = Hand(cards=hand("AS", "KH"), bet_quarters=1, from_split=True)
        self.assertFalse(after_split.is_natural)

    def test_pair_detection(self):
        self.assertTrue(Hand(cards=hand("8S", "8H")).is_pair)
        self.assertTrue(Hand(cards=hand("KS", "10H")).is_pair)  # same value
        self.assertFalse(Hand(cards=hand("9S", "8H")).is_pair)
        self.assertFalse(Hand(cards=hand("8S", "8H", "2D")).is_pair)


class TestNoGuiDependencies(unittest.TestCase):
    def test_no_gui_dependencies(self):
        """The rules engine must import on a machine with neither library."""
        for forbidden in ("pygame", "gpiozero"):
            self.assertNotIn(
                forbidden,
                sys.modules,
                f"{forbidden} was imported by the pure-logic modules",
            )


if __name__ == "__main__":
    unittest.main()

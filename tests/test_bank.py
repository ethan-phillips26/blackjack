"""
Money integrity: persistence, the payout state machine, jams, and crash
reconciliation.

The interesting tests here are the crash ones. Because PayoutController takes
time as an argument instead of sleeping, a whole cash-out can be replayed in
microseconds -- which makes it cheap to kill the machine at EVERY instant of a
payout and assert the coin count still adds up.
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import config
from bank import Bank, BankState, PayoutController, PayoutPhase

DROP_DELAY_MS = 50  # how long after the coil fires the coin breaks the beam


class BankTestCase(unittest.TestCase):
    def setUp(self):
        # These tests are about the STRONGEST guarantee the machine offers, so
        # they pin the durable mode rather than inheriting whatever config.py
        # happens to ship as the default. config.PERSIST_MODE is a deployment
        # choice; the crash-safety contract below is not.
        patcher = mock.patch.object(config, "PERSIST_MODE", "durable")
        patcher.start()
        self.addCleanup(patcher.stop)

        self.tmpdir = tempfile.mkdtemp(prefix="bjbank")
        self.state_path = os.path.join(self.tmpdir, "bank.json")
        self.ledger_path = os.path.join(self.tmpdir, "ledger.log")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def make_bank(self) -> Bank:
        return Bank(self.state_path, self.ledger_path)


class TestPersistence(BankTestCase):
    def test_balance_survives_a_reload(self):
        bank = self.make_bank()
        bank.insert_quarters(7)
        self.assertEqual(self.make_bank().balance_quarters, 7)

    def test_state_file_is_valid_json_after_every_write(self):
        bank = self.make_bank()
        for _ in range(20):
            bank.insert_quarters(1)
            with open(self.state_path, encoding="utf-8") as handle:
                json.load(handle)  # never a half-written file

    def test_no_temp_files_left_behind(self):
        bank = self.make_bank()
        bank.insert_quarters(3)
        leftovers = [n for n in os.listdir(self.tmpdir) if ".tmp." in n]
        self.assertEqual(leftovers, [])

    def test_missing_state_file_starts_at_zero(self):
        self.assertEqual(self.make_bank().balance_quarters, 0)

    def test_corrupt_state_is_quarantined_not_overwritten(self):
        with open(self.state_path, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        bank = self.make_bank()
        self.assertEqual(bank.balance_quarters, 0)
        quarantined = [n for n in os.listdir(self.tmpdir) if ".corrupt." in n]
        self.assertEqual(len(quarantined), 1)

    def test_every_persisted_field_is_an_integer_or_bool(self):
        bank = self.make_bank()
        bank.insert_quarters(5)
        bank.place_bet(2)
        bank.begin_cashout()
        with open(self.state_path, encoding="utf-8") as handle:
            data = json.load(handle)
        for key, value in data.items():
            self.assertNotIsInstance(value, float, f"{key} is a float: {value!r}")

    def test_ledger_records_coins_in_and_out(self):
        bank = self.make_bank()
        bank.insert_quarters(2)
        bank.begin_cashout()
        bank.mark_coil_actuating()
        bank.confirm_coin_paid()
        with open(self.ledger_path, encoding="utf-8") as handle:
            log = handle.read()
        self.assertIn("COIN_IN", log)
        self.assertIn("CASHOUT_START", log)
        self.assertIn("COIN_OUT", log)


class TestWagering(BankTestCase):
    def test_bet_debits_the_balance(self):
        bank = self.make_bank()
        bank.insert_quarters(4)
        self.assertTrue(bank.place_bet(3))
        self.assertEqual(bank.balance_quarters, 1)

    def test_bet_larger_than_balance_is_refused_and_changes_nothing(self):
        bank = self.make_bank()
        bank.insert_quarters(2)
        self.assertFalse(bank.place_bet(3))
        self.assertEqual(bank.balance_quarters, 2)

    def test_zero_and_negative_bets_are_refused(self):
        bank = self.make_bank()
        bank.insert_quarters(2)
        self.assertFalse(bank.place_bet(0))
        self.assertFalse(bank.place_bet(-5))
        self.assertEqual(bank.balance_quarters, 2)

    def test_credit_adds_winnings(self):
        bank = self.make_bank()
        bank.insert_quarters(1)
        bank.place_bet(1)
        bank.credit(2)
        self.assertEqual(bank.balance_quarters, 2)


class TestCashoutTransfer(BankTestCase):
    def test_balance_moves_to_owed_in_one_transition(self):
        bank = self.make_bank()
        bank.insert_quarters(9)
        self.assertEqual(bank.begin_cashout(), 9)
        self.assertEqual(bank.balance_quarters, 0)
        self.assertEqual(bank.owed_quarters, 9)
        # And it is on disk, not just in RAM.
        reloaded = self.make_bank()
        self.assertEqual(reloaded.owed_quarters, 9)
        self.assertEqual(reloaded.balance_quarters, 0)

    def test_cashout_with_empty_balance_is_a_no_op(self):
        bank = self.make_bank()
        self.assertEqual(bank.begin_cashout(), 0)
        self.assertEqual(bank.owed_quarters, 0)

    def test_second_cashout_during_a_payout_is_ignored(self):
        bank = self.make_bank()
        bank.insert_quarters(5)
        bank.begin_cashout()
        bank.insert_quarters(2)  # player feeds more coins mid-payout
        self.assertEqual(bank.begin_cashout(), 5)  # owed unchanged
        self.assertEqual(bank.balance_quarters, 2)  # new coins stay as credit


# ---------------------------------------------------------------------------
# Dispenser simulation
# ---------------------------------------------------------------------------


class DispenserRig:
    """Drives a PayoutController against a fake coin slide.

    Models the one thing that matters physically: a coin falls through the beam
    DROP_DELAY_MS after the coil energizes. `physical_coins` is therefore the
    real-world truth that every invariant is checked against -- it is counted
    when the coin drops, not when the software says it did.
    """

    def __init__(self, test: BankTestCase, coins_will_drop: bool = True):
        self.test = test
        self.coins_will_drop = coins_will_drop
        self.physical_coins = 0
        self.actuations = 0
        self.max_coil_on_ms = 0

    def session(self, max_steps: int | None = None, step_ms: int = 5, **kwargs):
        """Run one 'power on' of the machine. Returns (bank, controller).

        max_steps=None runs to completion; a number simulates yanking the plug
        after that many frames.
        """
        bank = Bank(self.test.state_path, self.test.ledger_path)
        bank.reconcile()
        controller = PayoutController(bank, **kwargs)

        now = 0
        steps = 0
        prev_on = False
        coil_on_since = 0
        pending_drop_ms: int | None = None

        while max_steps is None or steps < max_steps:
            if pending_drop_ms is not None and now >= pending_drop_ms:
                pending_drop_ms = None
                self.physical_coins += 1
                controller.on_drop_detected(now)

            energized = controller.tick(now)

            if energized and not prev_on:
                self.actuations += 1
                coil_on_since = now
                if self.coins_will_drop:
                    pending_drop_ms = now + DROP_DELAY_MS
            elif prev_on and not energized:
                self.max_coil_on_ms = max(self.max_coil_on_ms, now - coil_on_since)
            prev_on = energized

            if controller.phase is PayoutPhase.JAMMED:
                break
            if bank.owed_quarters == 0 and not energized:
                break

            now += step_ms
            steps += 1

        return bank, controller


class TestDispensing(BankTestCase):
    def test_closed_loop_pays_the_exact_number_of_coins(self):
        bank = self.make_bank()
        bank.insert_quarters(6)
        bank.begin_cashout()

        rig = DispenserRig(self)
        bank, controller = rig.session(has_drop_sensor=True)

        self.assertEqual(rig.physical_coins, 6)
        self.assertEqual(rig.actuations, 6)  # no wasted actuations
        self.assertEqual(bank.owed_quarters, 0)
        self.assertEqual(bank.balance_quarters, 0)
        self.assertFalse(bank.state.coil_actuating)
        self.assertIs(controller.phase, PayoutPhase.IDLE)

    def test_open_loop_assumes_one_actuation_is_one_coin(self):
        bank = self.make_bank()
        bank.insert_quarters(4)
        bank.begin_cashout()

        # No sensor fitted, and the rig drops nothing: open loop must still
        # complete, because it is defined to trust the actuation.
        rig = DispenserRig(self, coins_will_drop=False)
        bank, _ = rig.session(has_drop_sensor=False)

        self.assertEqual(rig.actuations, 4)
        self.assertEqual(bank.owed_quarters, 0)

    def test_coil_is_never_energized_longer_than_the_safety_limit(self):
        bank = self.make_bank()
        bank.insert_quarters(3)
        bank.begin_cashout()
        rig = DispenserRig(self)
        rig.session(has_drop_sensor=True)
        self.assertLessEqual(rig.max_coil_on_ms, config.SOLENOID_MAX_ON_MS)

    def test_absurd_on_time_config_is_clamped(self):
        bank = self.make_bank()
        controller = PayoutController(bank, on_ms=60_000)
        self.assertEqual(controller.on_ms, config.SOLENOID_MAX_ON_MS)

    def test_a_large_cashout_completes(self):
        bank = self.make_bank()
        bank.insert_quarters(40)
        bank.begin_cashout()
        rig = DispenserRig(self)
        bank, _ = rig.session(has_drop_sensor=True)
        self.assertEqual(rig.physical_coins, 40)
        self.assertEqual(bank.owed_quarters, 0)


class TestJams(BankTestCase):
    def test_no_drop_retries_then_jams_holding_the_owed_count(self):
        bank = self.make_bank()
        bank.insert_quarters(5)
        bank.begin_cashout()

        rig = DispenserRig(self, coins_will_drop=False)
        bank, controller = rig.session(has_drop_sensor=True, max_retries=3)

        self.assertIs(controller.phase, PayoutPhase.JAMMED)
        self.assertTrue(bank.jammed)
        # One initial go plus MAX_JAM_RETRIES further attempts.
        self.assertEqual(rig.actuations, 4)
        self.assertEqual(rig.physical_coins, 0)
        # Nothing fell, so nothing was deducted: all 5 are still owed.
        self.assertEqual(bank.owed_quarters, 5)
        self.assertFalse(bank.state.coil_actuating)

    def test_jam_survives_a_restart(self):
        bank = self.make_bank()
        bank.insert_quarters(2)
        bank.begin_cashout()
        DispenserRig(self, coins_will_drop=False).session(
            has_drop_sensor=True, max_retries=1
        )

        reloaded = Bank(self.state_path, self.ledger_path)
        reloaded.reconcile()
        controller = PayoutController(reloaded, has_drop_sensor=True)
        self.assertIs(controller.phase, PayoutPhase.JAMMED)
        self.assertEqual(reloaded.owed_quarters, 2)

    def test_retry_after_jam_pays_out_when_the_slide_frees_up(self):
        bank = self.make_bank()
        bank.insert_quarters(3)
        bank.begin_cashout()

        jam_rig = DispenserRig(self, coins_will_drop=False)
        _, controller = jam_rig.session(has_drop_sensor=True, max_retries=1)
        self.assertIs(controller.phase, PayoutPhase.JAMMED)

        # Operator clears the jam and the machine tries again.
        good_rig = DispenserRig(self)
        bank = Bank(self.state_path, self.ledger_path)
        bank.reconcile()
        controller = PayoutController(bank, has_drop_sensor=True)
        controller.retry_after_jam(0)
        self.assertFalse(bank.jammed)

        bank, controller = good_rig.session(has_drop_sensor=True)
        self.assertEqual(good_rig.physical_coins, 3)
        self.assertEqual(bank.owed_quarters, 0)

    def test_retry_is_ignored_when_not_jammed(self):
        bank = self.make_bank()
        controller = PayoutController(bank)
        controller.retry_after_jam(0)
        self.assertIs(controller.phase, PayoutPhase.IDLE)

    def test_a_partial_payout_keeps_the_coins_already_paid(self):
        """Jam on coin 3 of 5: two are out, three are still owed."""
        bank = self.make_bank()
        bank.insert_quarters(5)
        bank.begin_cashout()

        rig = DispenserRig(self)
        # Let two coins through, then wedge the slide.
        bank_obj = Bank(self.state_path, self.ledger_path)
        controller = PayoutController(bank_obj, has_drop_sensor=True, max_retries=1)
        now = 0
        prev_on = False
        pending: int | None = None
        while now < 60_000:
            if pending is not None and now >= pending:
                pending = None
                rig.physical_coins += 1
                controller.on_drop_detected(now)
            on = controller.tick(now)
            if on and not prev_on and rig.physical_coins < 2:
                pending = now + DROP_DELAY_MS  # only the first two coins fall
            prev_on = on
            if controller.phase is PayoutPhase.JAMMED or bank_obj.owed_quarters == 0:
                break
            now += 5

        self.assertIs(controller.phase, PayoutPhase.JAMMED)
        self.assertEqual(rig.physical_coins, 2)
        self.assertEqual(bank_obj.owed_quarters, 3)


class TestCrashReconciliation(BankTestCase):
    def test_clean_state_needs_no_reconciliation(self):
        bank = self.make_bank()
        bank.insert_quarters(3)
        self.assertIsNone(self.make_bank().reconcile())

    def test_owed_quarters_survive_and_resume(self):
        bank = self.make_bank()
        bank.insert_quarters(4)
        bank.begin_cashout()
        # Power cut between coins: nothing in flight, 4 still owed.
        reloaded = self.make_bank()
        message = reloaded.reconcile()
        self.assertIn("resuming", message)
        self.assertEqual(reloaded.owed_quarters, 4)

        rig = DispenserRig(self)
        bank, _ = rig.session(has_drop_sensor=True)
        self.assertEqual(rig.physical_coins, 4)
        self.assertEqual(bank.owed_quarters, 0)

    def test_coin_in_flight_at_the_crash_counts_as_paid(self):
        """The documented ambiguity: never double-pay, and never silently."""
        bank = self.make_bank()
        bank.insert_quarters(3)
        bank.begin_cashout()
        bank.mark_coil_actuating()  # crash right here

        reloaded = self.make_bank()
        message = reloaded.reconcile()
        self.assertEqual(reloaded.owed_quarters, 2)  # not 3 -- no double-pay
        self.assertFalse(reloaded.state.coil_actuating)
        self.assertIn("mid-dispense", message)  # and it is surfaced, not silent
        with open(self.ledger_path, encoding="utf-8") as handle:
            self.assertIn("RECONCILE_ASSUMED_PAID", handle.read())

    def test_stale_actuating_flag_with_nothing_owed_is_harmless(self):
        bank = self.make_bank()
        bank.insert_quarters(2)
        bank.state.coil_actuating = True
        bank.save()
        reloaded = self.make_bank()
        reloaded.reconcile()
        self.assertEqual(reloaded.owed_quarters, 0)
        self.assertEqual(reloaded.balance_quarters, 2)  # credits untouched

    def test_reconcile_is_idempotent(self):
        bank = self.make_bank()
        bank.insert_quarters(3)
        bank.begin_cashout()
        bank.mark_coil_actuating()
        for _ in range(3):
            reloaded = self.make_bank()
            reloaded.reconcile()
        self.assertEqual(reloaded.owed_quarters, 2)  # deducted once, not thrice

    def test_killed_at_every_instant_the_coin_count_still_adds_up(self):
        """The acceptance criterion, brute-forced.

        For every possible moment to yank the power during a 5-quarter payout:
        restart, reconcile, finish. Then check the coins that PHYSICALLY fell.

          * never more than 5  -- double-paying would let someone drain the
            hopper by repeatedly cutting power, so this bound is absolute;
          * never fewer than 4 -- at most the single in-flight coin can be lost,
            which is the documented, logged trade-off.
        """
        coins = 5
        for crash_after_steps in range(1, 140):
            with self.subTest(steps=crash_after_steps):
                for name in os.listdir(self.tmpdir):
                    os.remove(os.path.join(self.tmpdir, name))

                bank = self.make_bank()
                bank.insert_quarters(coins)
                bank.begin_cashout()

                rig = DispenserRig(self)
                rig.session(max_steps=crash_after_steps, has_drop_sensor=True)
                # ... plug pulled. Now boot back up and finish the job.
                bank, _ = rig.session(has_drop_sensor=True)

                self.assertEqual(bank.owed_quarters, 0)
                self.assertLessEqual(
                    rig.physical_coins, coins, "double-paid after a crash"
                )
                self.assertGreaterEqual(
                    rig.physical_coins,
                    coins - 1,
                    "lost more than the one documented in-flight coin",
                )

    def test_repeated_crashes_never_multiply_coins(self):
        """Yank the power over and over. The hopper must not haemorrhage."""
        coins = 8
        bank = self.make_bank()
        bank.insert_quarters(coins)
        bank.begin_cashout()

        rig = DispenserRig(self)
        for _ in range(12):
            _, _ = rig.session(max_steps=17, has_drop_sensor=True)
        bank, _ = rig.session(has_drop_sensor=True)

        self.assertEqual(bank.owed_quarters, 0)
        self.assertLessEqual(rig.physical_coins, coins)


class TestPayoutControllerBehaviour(BankTestCase):
    def test_idle_controller_keeps_the_coil_off(self):
        bank = self.make_bank()
        controller = PayoutController(bank)
        for now in range(0, 5000, 50):
            self.assertFalse(controller.tick(now))

    def test_unexpected_drop_outside_a_payout_is_logged_not_counted(self):
        bank = self.make_bank()
        bank.insert_quarters(2)
        controller = PayoutController(bank)
        controller.on_drop_detected(0)
        self.assertEqual(bank.balance_quarters, 2)
        with open(self.ledger_path, encoding="utf-8") as handle:
            self.assertIn("DROP_UNEXPECTED", handle.read())

    def test_status_reports_owed_and_jam_state(self):
        bank = self.make_bank()
        bank.insert_quarters(3)
        bank.begin_cashout()
        controller = PayoutController(bank)
        self.assertEqual(controller.status.owed, 3)
        self.assertFalse(controller.status.jammed)

    def test_abandon_payout_returns_owed_coins_to_the_credit_meter(self):
        """The escape hatch for an unfixable jam: refund to credits, not thin air."""
        bank = self.make_bank()
        bank.insert_quarters(6)
        bank.begin_cashout()
        bank.state.jammed = True

        self.assertEqual(bank.abandon_payout_to_balance(), 6)
        self.assertEqual(bank.owed_quarters, 0)
        self.assertEqual(bank.balance_quarters, 6)  # not one quarter lost
        self.assertFalse(bank.jammed)
        self.assertEqual(self.make_bank().balance_quarters, 6)  # and persisted

    def test_abandon_payout_with_nothing_owed_does_nothing(self):
        bank = self.make_bank()
        bank.insert_quarters(2)
        self.assertEqual(bank.abandon_payout_to_balance(), 0)
        self.assertEqual(bank.balance_quarters, 2)

    def test_bank_state_defaults_are_all_zero(self):
        state = BankState()
        self.assertEqual(state.balance_quarters, 0)
        self.assertEqual(state.owed_quarters, 0)
        self.assertFalse(state.coil_actuating)
        self.assertFalse(state.jammed)


if __name__ == "__main__":
    unittest.main()

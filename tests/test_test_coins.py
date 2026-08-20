"""
The test-coin key on real hardware.

config.ALLOW_TEST_COINS_ON_REAL_HARDWARE lets a keystroke mint a credit on a
built cabinet. That is a loaded gun, so what is asserted here is not just "it
works" but that it stays bounded: only that one key, only when the flag is set,
and never mixed into the cash-box totals.

RealHardware is constructible without gpiozero -- the import lives inside
start() -- so all of this runs on a PC with no GPIO and no Pi.
"""

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

import config
from bank import Bank
from hardware.base import EventType, PulseAccumulator
from hardware.real import RealHardware


def drain(hardware) -> list:
    return hardware.poll_events()


class RealHardwareTestCase(unittest.TestCase):
    def make_hardware(self, allowed: bool) -> RealHardware:
        """A RealHardware with the flag forced, and no pins claimed."""
        with mock.patch.object(config, "ALLOW_TEST_COINS_ON_REAL_HARDWARE", allowed):
            hw = RealHardware()
            # The class attribute is read at import time; override the instance
            # so the test controls it rather than whatever config.py shipped as.
            hw.accepts_simulated_input = allowed
            self._allowed = allowed
            return hw

    def simulate(self, hw, count: int = 1) -> list:
        """Press the test key. Stdout is captured, not silenced -- one of the
        tests below asserts the machine actually announces what it just did."""
        self.output = io.StringIO()
        with mock.patch.object(
            config, "ALLOW_TEST_COINS_ON_REAL_HARDWARE", self._allowed
        ), contextlib.redirect_stdout(self.output):
            hw.simulate_coin_insert(count)
        return drain(hw)


class TestTestCoinKey(RealHardwareTestCase):
    def test_armed_flag_credits_a_coin(self):
        hw = self.make_hardware(allowed=True)
        events = self.simulate(hw)
        self.assertEqual(len(events), 1)
        self.assertIs(events[0].type, EventType.COIN_INSERTED)

    def test_the_credit_is_marked_simulated(self):
        """The whole audit trail hangs off this one field."""
        hw = self.make_hardware(allowed=True)
        self.assertTrue(self.simulate(hw)[0].simulated)

    def test_disarmed_flag_mints_nothing(self):
        hw = self.make_hardware(allowed=False)
        self.assertEqual(self.simulate(hw, count=5), [])

    def test_a_real_acceptor_pulse_is_not_marked_simulated(self):
        hw = self.make_hardware(allowed=True)
        hw._on_coin_pulse()
        events = drain(hw)
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0].simulated)

    def test_it_goes_through_the_real_pulse_grouping(self):
        """Not a shortcut to a credit: on an acceptor configured for 2 pulses
        per coin, one keypress must still yield exactly one quarter -- the same
        answer the real acceptor's own output would get."""
        hw = self.make_hardware(allowed=True)
        hw._pulses = PulseAccumulator(pulses_per_coin=2)
        with mock.patch.object(config, "COIN_PULSES_PER_COIN", 2):
            events = self.simulate(hw)
        self.assertEqual(len(events), 1)

    def test_a_burst_credits_every_coin(self):
        """Regression: stamping each coin from a fresh now_ms() put the whole
        burst in one millisecond, where the debouncer -- doing its job --
        dropped all but the first."""
        hw = self.make_hardware(allowed=True)
        self.assertEqual(len(self.simulate(hw, count=4)), 4)

    def test_every_test_coin_is_announced(self):
        """An operator topping up their own credits leaves a trail."""
        hw = self.make_hardware(allowed=True)
        self.simulate(hw, count=2)
        self.assertEqual(self.output.getvalue().count("TEST COIN"), 2)

    def test_the_other_test_keys_stay_inert_even_when_armed(self):
        """'Q' is re-armable. 'D' and 'J' are not, at any setting -- faking a
        coin DROP would make the machine believe it paid a player it didn't."""
        hw = self.make_hardware(allowed=True)
        hw.simulate_coin_drop()
        self.assertEqual(drain(hw), [])
        self.assertTrue(hw.toggle_simulated_jam())
        self.assertEqual(drain(hw), [])

    def test_keyboard_buttons_work_regardless(self):
        hw = self.make_hardware(allowed=False)
        hw.inject_button(config.BTN_HIT)
        events = drain(hw)
        self.assertEqual(len(events), 1)
        self.assertIs(events[0].type, EventType.BUTTON_PRESSED)
        self.assertEqual(events[0].button, config.BTN_HIT)

    def test_boot_log_announces_an_armed_machine(self):
        hw = self.make_hardware(allowed=True)
        self.assertIn("TEST COIN", hw.describe())
        self.assertNotIn("TEST COIN", self.make_hardware(allowed=False).describe())


class TestMockBurst(unittest.TestCase):
    def test_the_mock_backend_credits_every_coin_of_a_burst_too(self):
        """Same debounce trap, same fix, other backend."""
        from hardware.mock import MockHardware

        hw = MockHardware()
        with contextlib.redirect_stdout(io.StringIO()):
            hw.simulate_coin_insert(4)
        events = hw.poll_events()
        self.assertEqual(len(events), 4)
        self.assertTrue(all(e.type is EventType.COIN_INSERTED for e in events))


class TestTestCoinAccounting(unittest.TestCase):
    """A test coin spends like a real one but must never be counted like one."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="bjtest")
        self.state_path = os.path.join(self.tmpdir, "bank.json")
        self.ledger_path = os.path.join(self.tmpdir, "ledger.log")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def make_bank(self) -> Bank:
        return Bank(self.state_path, self.ledger_path)

    def test_it_credits_the_balance_like_any_other_quarter(self):
        bank = self.make_bank()
        bank.insert_quarters(2, simulated=True)
        self.assertEqual(bank.balance_quarters, 2)

    def test_it_stays_out_of_the_cash_box_total(self):
        bank = self.make_bank()
        bank.insert_quarters(3)                    # real coins, into the box
        bank.insert_quarters(2, simulated=True)    # keystrokes, into nothing
        self.assertEqual(bank.balance_quarters, 5)
        self.assertEqual(bank.state.lifetime_coins_in, 3)
        self.assertEqual(bank.state.lifetime_test_coins_in, 2)

    def test_the_ledger_tells_them_apart(self):
        bank = self.make_bank()
        bank.insert_quarters(1)
        bank.insert_quarters(1, simulated=True)
        with open(self.ledger_path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertEqual(sum("\tCOIN_IN\t" in ln for ln in lines), 1)
        self.assertEqual(sum("\tTEST_COIN_IN\t" in ln for ln in lines), 1)

    def test_the_separate_total_survives_a_reload(self):
        bank = self.make_bank()
        bank.insert_quarters(4, simulated=True)
        self.assertEqual(self.make_bank().state.lifetime_test_coins_in, 4)

    def test_an_older_state_file_without_the_field_still_loads(self):
        """bank.json written before this feature existed must not break a
        machine that is holding somebody's money."""
        with open(self.state_path, "w", encoding="utf-8") as handle:
            handle.write('{"version": 1, "balance_quarters": 6}')
        bank = self.make_bank()
        self.assertEqual(bank.balance_quarters, 6)
        self.assertEqual(bank.state.lifetime_test_coins_in, 0)


if __name__ == "__main__":
    unittest.main()

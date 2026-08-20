"""
config.PERSIST_MODE -- how hard the machine works to not forget the balance.

The point of the setting is that the expensive part of saving is fsync, not
writing. "fast" keeps the atomic rename (so the file is never torn and a
restart still finds the balance) and drops only the durability against a power
cut mid-write. "memory" writes nothing until a clean exit.

What is asserted here is that each mode keeps the promises made for it in
config.py -- especially that "fast" still survives a restart, since that is the
whole reason it is the default rather than "memory".
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

import config
from bank import Bank


class PersistModeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="bjmode")
        self.state_path = os.path.join(self.tmpdir, "bank.json")
        self.ledger_path = os.path.join(self.tmpdir, "ledger.log")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)

    def bank(self, mode: str) -> Bank:
        patcher = mock.patch.object(config, "PERSIST_MODE", mode)
        patcher.start()
        self.addCleanup(patcher.stop)
        return Bank(self.state_path, self.ledger_path)


class TestFastMode(PersistModeTestCase):
    """The default. Cheap, and still remembers your credits."""

    def test_the_balance_survives_a_restart(self):
        bank = self.bank("fast")
        bank.insert_quarters(6)
        self.assertEqual(self.bank("fast").balance_quarters, 6)

    def test_it_writes_as_it_goes_not_only_at_exit(self):
        bank = self.bank("fast")
        bank.insert_quarters(2)
        self.assertTrue(os.path.exists(self.state_path))

    def test_it_never_calls_fsync(self):
        """The entire point: fsync is what a failing card stalls on."""
        with mock.patch("bank.os.fsync") as fsync:
            self.bank("fast").insert_quarters(1)
        fsync.assert_not_called()

    def test_durable_mode_still_does(self):
        with mock.patch("bank.os.fsync") as fsync:
            self.bank("durable").insert_quarters(1)
        # One for the file, one for the directory holding the rename.
        self.assertEqual(fsync.call_count, 2)


class TestMemoryMode(PersistModeTestCase):
    def test_nothing_is_written_while_playing(self):
        bank = self.bank("memory")
        bank.insert_quarters(4)
        bank.place_bet(2)
        self.assertFalse(os.path.exists(self.state_path))

    def test_flush_persists_on_a_clean_exit(self):
        bank = self.bank("memory")
        bank.insert_quarters(5)
        bank.flush()
        self.assertEqual(self.bank("memory").balance_quarters, 5)

    def test_an_unflushed_crash_loses_the_balance(self):
        """Stated plainly because it is the documented trade, not a bug: this
        mode is for a machine in your own home."""
        bank = self.bank("memory")
        bank.insert_quarters(9)
        self.assertEqual(self.bank("memory").balance_quarters, 0)


class TestAllModesAgreeOnTheMoney(PersistModeTestCase):
    def test_the_arithmetic_is_identical(self):
        """PERSIST_MODE changes durability, never a single quarter."""
        for mode in ("durable", "fast", "memory"):
            with self.subTest(mode=mode):
                bank = self.bank(mode)
                bank.state.balance_quarters = 0
                bank.insert_quarters(4)
                self.assertTrue(bank.place_bet(3))
                bank.credit(6, "SETTLE")
                self.assertEqual(bank.balance_quarters, 7)


if __name__ == "__main__":
    unittest.main()

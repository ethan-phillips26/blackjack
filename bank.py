"""
Money. Quarter balance, crash-safe persistence, and the payout state machine.

NO pygame. NO gpiozero. The only I/O is writing two files, and both paths come
from config so tests can point them at a tmpdir.

Everything is INTEGER QUARTERS. No floats anywhere in this module -- not for
balances, not for payouts, not for timings.

The single most important property of this file: a power cut at ANY instant
must not invent or destroy a quarter. See "Crash semantics" on Bank.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from enum import Enum, auto

import config

STATE_VERSION = 1


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _atomic_write_json(path: str, payload: dict) -> None:
    """Write JSON so that `path` is always either the old or the new content.

    Never a half-written file: write a sibling temp file, flush it all the way
    down to the platter, then os.replace() (atomic rename on POSIX), then fsync
    the directory so the rename itself survives a power cut.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp.{os.getpid()}"

    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())

    os.replace(tmp_path, path)

    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


@dataclass
class BankState:
    """Exactly what is persisted. Every field is an int or a bool."""

    balance_quarters: int = 0
    #: Quarters promised to the player but not yet in their hand.
    owed_quarters: int = 0
    #: True between "about to energize the coil" and "that coin was confirmed".
    coil_actuating: bool = False
    #: Latched when the dispenser gave up. Cleared by an explicit retry.
    jammed: bool = False
    #: Quarters that physically entered the acceptor. This is the number the
    #: cash box is reconciled against, so nothing but a real coin may touch it.
    lifetime_coins_in: int = 0
    lifetime_coins_out: int = 0
    #: Credits conjured by the test key (see ALLOW_TEST_COINS_ON_REAL_HARDWARE).
    #: Counted separately precisely BECAUSE they can be cashed out as real
    #: quarters: if the box comes up short, this is the number that explains it.
    lifetime_test_coins_in: int = 0

    def to_dict(self) -> dict:
        return {"version": STATE_VERSION, **self.__dict__}

    @classmethod
    def from_dict(cls, data: dict) -> "BankState":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


class Bank:
    """The quarter balance and the payout ledger.

    Crash semantics
    ---------------
    Coins are dispensed one at a time, and each one is bracketed by two atomic
    writes:

        1. coil_actuating = True      <- persisted BEFORE the coil energizes
        2. ...actuate, watch the IR beam...
        3. owed -= 1, coil_actuating = False   <- persisted AFTER confirmation

    Lose power between 1 and 3 and we genuinely cannot know whether that one
    quarter fell -- the coin is physically in the slide and no sensor reading
    survived. Something has to give, so the policy is explicit:

        A coin that was in flight at the moment of a crash is treated as PAID.

    That direction is chosen deliberately. It makes double-paying impossible,
    which is the failure mode that lets someone drain the hopper by yanking the
    plug. The cost is that in the worst case the player is short exactly one
    quarter -- and that is NOT silent: it is written to the ledger as
    RECONCILE_ASSUMED_PAID and surfaced on screen at startup.

    Everything else is unambiguous. Owed quarters survive a crash and are
    re-paid on the next boot; the balance is never held only in RAM.
    """

    def __init__(
        self,
        state_path: str = config.BANK_STATE_PATH,
        ledger_path: str = config.LEDGER_PATH,
    ) -> None:
        self.state_path = state_path
        self.ledger_path = ledger_path
        self.state = BankState()
        #: Set by reconcile() so main.py can show the player what happened.
        self.reconcile_message: str | None = None
        #: Storage health, for the shutdown summary. Not persisted -- these
        #: describe the card, not the money.
        self.slow_writes = 0
        self.worst_write_ms = 0.0
        self.load()

    # -- storage -----------------------------------------------------------

    def load(self) -> None:
        try:
            with open(self.state_path, encoding="utf-8") as handle:
                self.state = BankState.from_dict(json.load(handle))
        except FileNotFoundError:
            self.state = BankState()
            self.log("INIT", "new bank state file")
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            # os.replace() makes this all but impossible, so if it happens the
            # filesystem itself is damaged. Quarantine rather than overwrite:
            # the file plus the ledger are the only record of what was owed.
            quarantine = f"{self.state_path}.corrupt.{int(time.time())}"
            try:
                os.replace(self.state_path, quarantine)
            except OSError:
                quarantine = "<could not quarantine>"
            self.state = BankState()
            self.log("CORRUPT", f"unreadable state ({exc}); saved to {quarantine}")

    def save(self) -> None:
        """Persist the balance. BLOCKING, and deliberately so.

        This is the one place the machine trades responsiveness for not losing
        somebody's money, so it is also the place worth measuring: a card that
        has started taking seconds per write is a card that is failing, and the
        first symptom an operator sees is the game freezing after a hand.
        """
        started = time.perf_counter()
        _atomic_write_json(self.state_path, self.state.to_dict())
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        if elapsed_ms >= config.SLOW_WRITE_WARN_MS:
            self.slow_writes += 1
            self.worst_write_ms = max(self.worst_write_ms, elapsed_ms)
            print(
                f"[bank] SLOW WRITE: {elapsed_ms:.0f}ms to persist the balance "
                f"-- the storage is stalling the machine",
                flush=True,
            )
            # Into the ledger too: if this cabinet ever eats a quarter, the
            # audit trail should show the disk was already misbehaving.
            self.log("SLOW_WRITE", f"{elapsed_ms:.0f}ms")

    def log(self, event: str, detail: str = "") -> None:
        """Append-only audit trail. Advisory: bank.json is authoritative.

        Best-effort by design -- a full disk must never stop us paying out, so
        logging failures are swallowed.
        """
        line = (
            f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\t{event}\t"
            f"bal={self.state.balance_quarters}\towed={self.state.owed_quarters}\t"
            f"{detail}\n"
        )
        try:
            os.makedirs(os.path.dirname(self.ledger_path) or ".", exist_ok=True)
            with open(self.ledger_path, "a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass

    # -- accessors ---------------------------------------------------------

    @property
    def balance_quarters(self) -> int:
        return self.state.balance_quarters

    @property
    def owed_quarters(self) -> int:
        return self.state.owed_quarters

    @property
    def jammed(self) -> bool:
        return self.state.jammed

    # -- startup -----------------------------------------------------------

    def reconcile(self) -> str | None:
        """Resolve an interrupted payout. Call once at startup, before play.

        Returns a message to show the operator, or None if all was clean.
        """
        messages: list[str] = []

        if self.state.coil_actuating:
            # See "Crash semantics": in-flight coin counts as paid.
            self.state.coil_actuating = False
            if self.state.owed_quarters > 0:
                self.state.owed_quarters -= 1
                self.state.lifetime_coins_out += 1
                self.log("RECONCILE_ASSUMED_PAID", "coin in flight at crash")
                messages.append("1 quarter was mid-dispense at shutdown; counted as paid")
            else:
                # Flag set but nothing owed: the confirming write landed and the
                # crash beat only the flag clear. Nothing to settle.
                self.log("RECONCILE_STALE_FLAG", "actuating flag with owed=0")
            self.save()

        if self.state.owed_quarters > 0:
            self.log("RECONCILE_RESUME", f"{self.state.owed_quarters} quarters still owed")
            messages.append(f"resuming interrupted payout: {self.state.owed_quarters} owed")

        self.reconcile_message = " / ".join(messages) if messages else None
        return self.reconcile_message

    # -- taking money in ---------------------------------------------------

    def insert_quarters(self, count: int = 1, simulated: bool = False) -> int:
        """Credit accepted quarters.

        `simulated` marks a credit that came from the test key rather than the
        acceptor. It buys the player exactly the same thing -- a real credit,
        cashable as a real quarter -- so the balance arithmetic is identical.
        What differs is the bookkeeping: it is logged as TEST_COIN_IN and kept
        out of lifetime_coins_in, because that total is what the cash box is
        counted against. A machine whose ledger claims coins went into a box
        that is empty is worse than useless in a dispute.
        """
        if count <= 0:
            return self.balance_quarters
        self.state.balance_quarters += count
        if simulated:
            self.state.lifetime_test_coins_in += count
        else:
            self.state.lifetime_coins_in += count
        self.save()
        self.log("TEST_COIN_IN" if simulated else "COIN_IN", f"+{count}")
        return self.balance_quarters

    # -- wagering ----------------------------------------------------------

    def place_bet(self, quarters: int) -> bool:
        """Debit the wager. False (and no change) if the balance won't cover it."""
        if quarters <= 0 or quarters > self.state.balance_quarters:
            return False
        self.state.balance_quarters -= quarters
        self.save()
        self.log("BET", f"-{quarters}")
        return True

    def credit(self, quarters: int, reason: str = "PAYOUT") -> int:
        """Return winnings/push to the balance. Nothing physical moves here."""
        if quarters > 0:
            self.state.balance_quarters += quarters
            self.save()
            self.log(reason, f"+{quarters}")
        return self.balance_quarters

    # -- paying money out --------------------------------------------------

    def begin_cashout(self) -> int:
        """Move the whole balance into `owed` in ONE atomic write.

        Doing it as a single transition is what makes a crash here harmless:
        the quarters are either entirely in the balance or entirely owed, never
        counted twice and never dropped between the two fields.
        """
        if self.state.owed_quarters > 0 or self.state.balance_quarters <= 0:
            return self.state.owed_quarters
        amount = self.state.balance_quarters
        self.state.owed_quarters = amount
        self.state.balance_quarters = 0
        self.state.jammed = False
        self.save()
        self.log("CASHOUT_START", f"{amount} quarters owed")
        return amount

    def mark_coil_actuating(self) -> None:
        """Persist intent BEFORE energizing. Half of the crash bracket."""
        self.state.coil_actuating = True
        self.save()

    def cancel_coil_actuating(self) -> None:
        """The sensor proved no coin fell, so the actuation didn't count."""
        self.state.coil_actuating = False
        self.save()
        self.log("NO_DROP", "actuation produced no coin; retrying")

    def confirm_coin_paid(self) -> int:
        """One quarter is physically out. Other half of the crash bracket."""
        if self.state.owed_quarters > 0:
            self.state.owed_quarters -= 1
            self.state.lifetime_coins_out += 1
        self.state.coil_actuating = False
        self.save()
        self.log("COIN_OUT", "-1")
        return self.state.owed_quarters

    def record_jam(self) -> None:
        self.state.jammed = True
        self.state.coil_actuating = False
        self.save()
        self.log("JAM", f"{self.state.owed_quarters} quarters owed")

    def clear_jam(self) -> None:
        self.state.jammed = False
        self.save()
        self.log("JAM_CLEARED", "operator retry")

    def abandon_payout_to_balance(self) -> int:
        """Give up on dispensing and put the owed quarters back on the credit
        meter, so the player can at least keep playing with them.

        EXTENSION POINT: not currently reachable from the UI -- a jam holds the
        owed count instead, which is the behaviour the spec asks for. Wire this
        to a maintenance key combo if you'd rather refund to credits.
        """
        amount = self.state.owed_quarters
        if amount > 0:
            self.state.owed_quarters = 0
            self.state.balance_quarters += amount
            self.state.jammed = False
            self.save()
            self.log("PAYOUT_ABANDONED", f"{amount} returned to balance")
        return amount


# ---------------------------------------------------------------------------
# Payout state machine
# ---------------------------------------------------------------------------


class PayoutPhase(Enum):
    IDLE = auto()  # nothing owed, or waiting for a jam retry
    ENERGIZED = auto()  # coil on, slide pushing
    RECOVERING = auto()  # coil off: slide returning and/or awaiting the beam
    JAMMED = auto()  # gave up; owed quarters held


@dataclass
class PayoutStatus:
    phase: PayoutPhase
    owed: int
    attempts: int
    jammed: bool


class PayoutController:
    """Paces the dispenser. Pure logic: no sleeps, no GPIO, no clock of its own.

    Time comes in through tick(now_ms), so a full 40-quarter cash-out -- or a
    jam on coin 12 -- can be tested in microseconds.

    Wiring in main.py is two lines:

        energize = payout.tick(now_ms)
        hardware.set_solenoid(energize)

    and COIN_DROP_DETECTED events are forwarded to on_drop_detected(). tick()
    performs the bank writes itself so that "persist intent" always happens
    before the caller can possibly energize the coil.
    """

    def __init__(
        self,
        bank: Bank,
        has_drop_sensor: bool = config.USE_DROP_SENSOR,
        on_ms: int = config.SOLENOID_ON_MS,
        reset_ms: int = config.SOLENOID_RESET_MS,
        drop_timeout_ms: int = config.COIN_DROP_TIMEOUT_MS,
        max_retries: int = config.MAX_JAM_RETRIES,
    ) -> None:
        self.bank = bank
        self.has_drop_sensor = has_drop_sensor
        # Hard safety clamp: a coin-slide solenoid is not rated for continuous
        # duty, so no configuration mistake may hold the coil on indefinitely.
        self.on_ms = min(on_ms, config.SOLENOID_MAX_ON_MS)
        self.reset_ms = reset_ms
        self.drop_timeout_ms = drop_timeout_ms
        self.max_retries = max_retries

        self.phase = PayoutPhase.JAMMED if bank.jammed else PayoutPhase.IDLE
        self._phase_started_ms = 0
        self._attempts = 0  # actuations spent on the CURRENT quarter
        self._drop_seen = False
        #: Set for one tick when the last owed quarter is paid.
        self.just_finished = False

    # -- inputs ------------------------------------------------------------

    def on_drop_detected(self, now_ms: int) -> None:
        """An IR beam break. May arrive while the coil is still energized."""
        if self.phase in (PayoutPhase.ENERGIZED, PayoutPhase.RECOVERING):
            self._drop_seen = True
        else:
            self.bank.log("DROP_UNEXPECTED", "beam broken outside a payout")

    def retry_after_jam(self, now_ms: int) -> None:
        """Operator/player asks the machine to have another go."""
        if self.phase is not PayoutPhase.JAMMED:
            return
        self._attempts = 0
        self._drop_seen = False
        self.bank.clear_jam()
        self.phase = PayoutPhase.IDLE
        self._phase_started_ms = now_ms

    # -- state -------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        return self.phase in (PayoutPhase.ENERGIZED, PayoutPhase.RECOVERING)

    @property
    def status(self) -> PayoutStatus:
        return PayoutStatus(
            phase=self.phase,
            owed=self.bank.owed_quarters,
            attempts=self._attempts,
            jammed=self.phase is PayoutPhase.JAMMED,
        )

    def _enter(self, phase: PayoutPhase, now_ms: int) -> None:
        self.phase = phase
        self._phase_started_ms = now_ms

    def _elapsed(self, now_ms: int) -> int:
        return now_ms - self._phase_started_ms

    # -- the machine -------------------------------------------------------

    def tick(self, now_ms: int) -> bool:
        """Advance one step. Returns the desired solenoid state for right now.

        Idempotent per instant and safe to call at any frame rate; every
        transition is driven by elapsed time, not by call count.
        """
        self.just_finished = False

        if self.phase is PayoutPhase.JAMMED:
            return False

        if self.phase is PayoutPhase.IDLE:
            if self.bank.owed_quarters > 0:
                self._start_actuation(now_ms)
                return True
            return False

        if self.phase is PayoutPhase.ENERGIZED:
            if self._elapsed(now_ms) < self.on_ms:
                return True  # keep pushing
            self._enter(PayoutPhase.RECOVERING, now_ms)
            return False  # de-energize; slide springs back

        # RECOVERING: coil off. Wait out the mechanical reset, and -- in closed
        # loop -- wait for proof the coin actually left the chute.
        elapsed = self._elapsed(now_ms)

        if not self.has_drop_sensor:
            # Open loop: one actuation is defined to be one coin.
            if elapsed >= self.reset_ms:
                self._complete_coin(now_ms)
            return False

        if self._drop_seen:
            if elapsed >= self.reset_ms:  # confirmed, but let the slide return
                self._complete_coin(now_ms)
            return False

        if elapsed >= self.drop_timeout_ms:
            return self._handle_no_drop(now_ms)
        return False

    def _start_actuation(self, now_ms: int) -> None:
        self._drop_seen = False
        self._attempts += 1
        # ORDER MATTERS: the intent must be on disk before the coil can move,
        # or a crash mid-push would leave no evidence a coin might have fallen.
        self.bank.mark_coil_actuating()
        self._enter(PayoutPhase.ENERGIZED, now_ms)

    def _complete_coin(self, now_ms: int) -> None:
        remaining = self.bank.confirm_coin_paid()
        self._attempts = 0
        self._drop_seen = False
        if remaining <= 0:
            self.just_finished = True
            self.bank.log("CASHOUT_DONE", "payout complete")
        self._enter(PayoutPhase.IDLE, now_ms)

    def _handle_no_drop(self, now_ms: int) -> bool:
        """Timed out waiting for the beam. Retry the same quarter, then give up.

        Returns the solenoid state for this instant, so a retry starts pushing
        immediately rather than a frame late.
        """
        # The sensor is telling us nothing fell, so this actuation did not pay
        # a coin: clear the in-flight flag so a later crash can't count it.
        self.bank.cancel_coil_actuating()

        # _attempts counts actuations spent on this quarter, so the first
        # timeout is attempt 1 and max_retries FURTHER goes are allowed.
        if self._attempts > self.max_retries:
            self.bank.record_jam()
            self._attempts = 0
            self._drop_seen = False
            self._enter(PayoutPhase.JAMMED, now_ms)
            return False

        self._start_actuation(now_ms)  # same quarter, another go
        return True

"""Tests for the IB Gateway connect-retry and error-stringification fix
(backend/market_data.py, #785).

Tonight's (2026-08-24 18:45) run traded nothing: a single 10s connect
attempt overlapped IB Gateway's own login/config window and lost the race,
and the resulting audit row/urgent alert read 'Could not open IB Gateway
session: ' with no cause — asyncio.TimeoutError's str() is empty. Both bugs
are pinned here at the shared connect-retry helper (also exercised through
BrokerSession.open() in test_broker.py); _run_ib itself needs real ib_async
network I/O to fully invoke, out of scope for a no-network unit test.
"""

import pytest

from backend.market_data import (
    CONNECT_RETRY_ATTEMPTS,
    CONNECT_RETRY_WORST_CASE_SECONDS,
    _connect_with_retry,
    describe_exc,
)


class FakeConnectIB:
    """A minimal connectAsync stub: fails *fail_times* times, then succeeds
    (or fails forever if fail_times >= CONNECT_RETRY_ATTEMPTS)."""

    def __init__(self, fail_times: int, exc_factory=lambda: TimeoutError()):
        self.fail_times = fail_times
        self.exc_factory = exc_factory
        self.attempts = 0

    async def connectAsync(self, host, port, clientId):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise self.exc_factory()


class TestDescribeExc:
    def test_timeout_error_str_is_empty_but_describe_exc_is_not(self):
        exc = TimeoutError()
        assert str(exc) == ""  # the bug this exists to work around
        assert describe_exc(exc) != ""
        assert "TimeoutError" in describe_exc(exc)

    def test_describe_exc_names_the_type_for_any_exception(self):
        assert "ConnectionRefusedError" in describe_exc(ConnectionRefusedError("refused"))


class TestConnectWithRetry:
    @pytest.mark.asyncio
    async def test_succeeds_on_a_later_attempt_without_raising(self, monkeypatch):
        from backend import market_data

        monkeypatch.setattr(market_data, "CONNECT_RETRY_DELAY_SECONDS", 0.0)
        ib = FakeConnectIB(fail_times=1)
        await _connect_with_retry(ib, "127.0.0.1", 4002, 17)
        assert ib.attempts == 2  # one failure, then success — no more retries than needed

    @pytest.mark.asyncio
    async def test_exhausts_all_attempts_then_raises_the_last_exception(self, monkeypatch):
        from backend import market_data

        monkeypatch.setattr(market_data, "CONNECT_RETRY_DELAY_SECONDS", 0.0)
        ib = FakeConnectIB(fail_times=CONNECT_RETRY_ATTEMPTS)
        with pytest.raises(TimeoutError):
            await _connect_with_retry(ib, "127.0.0.1", 4002, 17)
        assert ib.attempts == CONNECT_RETRY_ATTEMPTS  # never more than the budget

    @pytest.mark.asyncio
    async def test_retries_uniformly_for_a_non_timeout_exception_too(self, monkeypatch):
        # #785: no exception at this layer reliably distinguishes "never
        # going to succeed" from "not ready yet" — every exception type
        # retries the same way rather than failing fast on a guess.
        from backend import market_data

        monkeypatch.setattr(market_data, "CONNECT_RETRY_DELAY_SECONDS", 0.0)
        ib = FakeConnectIB(fail_times=1, exc_factory=lambda: ConnectionRefusedError("refused"))
        await _connect_with_retry(ib, "127.0.0.1", 4002, 17)
        assert ib.attempts == 2

    @pytest.mark.asyncio
    async def test_sleeps_between_attempts_not_after_the_last_one(self, monkeypatch):
        from backend import market_data

        sleep_calls: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        monkeypatch.setattr(market_data.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(market_data, "CONNECT_RETRY_DELAY_SECONDS", 15.0)
        ib = FakeConnectIB(fail_times=CONNECT_RETRY_ATTEMPTS)
        with pytest.raises(TimeoutError):
            await _connect_with_retry(ib, "127.0.0.1", 4002, 17)
        # CONNECT_RETRY_ATTEMPTS attempts -> CONNECT_RETRY_ATTEMPTS - 1 gaps
        assert sleep_calls == [15.0] * (CONNECT_RETRY_ATTEMPTS - 1)


class TestWorstCaseBudget:
    def test_worst_case_covers_every_attempt_plus_every_gap(self):
        from backend.market_data import CONNECT_RETRY_DELAY_SECONDS, CONNECT_TIMEOUT

        expected = CONNECT_RETRY_ATTEMPTS * CONNECT_TIMEOUT + (CONNECT_RETRY_ATTEMPTS - 1) * CONNECT_RETRY_DELAY_SECONDS
        assert CONNECT_RETRY_WORST_CASE_SECONDS == expected

"""Tests for the IB Gateway connect-retry and error-stringification fix
(backend/market_data.py, #785).

Tonight's (2026-08-24 18:45) run traded nothing: a single 10s connect
attempt overlapped IB Gateway's own login/config window and lost the race,
and the resulting audit row/urgent alert read 'Could not open IB Gateway
session: ' with no cause — asyncio.TimeoutError's str() is empty. Both bugs
are pinned here at the shared connect-retry helper (also exercised through
BrokerSession.open() in test_broker.py).

_run_ib's `retry` parameter defaults to False on purpose: an earlier version
of this fix retried EVERY market-data fetch unconditionally, which broke the
e2e suite — Gateway-unreachable in CI turned each SPY/VIX/index-history call
from a ~10s fail-fast into a ~60s retry loop, multiplied across a dozen
symbols and every synchronous HTTP handler that touches market data. Only
fill_check's one-per-morning-run connect opts in (retry=True); every routine
fetch keeps the module's documented fast-degrade contract.
"""

import sys
import types
from typing import Any, ClassVar

import pytest

from backend.market_data import (
    CONNECT_RETRY_ATTEMPTS,
    CONNECT_RETRY_WORST_CASE_SECONDS,
    _connect_with_retry,
    _run_ib,
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


class _FakeIBModuleIB:
    """Stands in for ib_async.IB inside _run_ib's `from ib_async import IB`
    — connectAsync fails *fail_times* times before succeeding."""

    fail_times = 0
    exc_factory = staticmethod(lambda: TimeoutError())
    attempts: ClassVar[list[int]] = []  # class-level: _run_ib constructs a fresh instance per call

    def __init__(self) -> None:
        self.instance_attempts = 0

    async def connectAsync(self, host, port, clientId):
        self.instance_attempts += 1
        type(self).attempts.append(self.instance_attempts)
        if self.instance_attempts <= type(self).fail_times:
            raise type(self).exc_factory()

    def reqMarketDataType(self, mdt):
        pass

    def disconnect(self):
        pass


@pytest.fixture
def fake_ib_async_module(monkeypatch):
    """Installs a fake `ib_async` module so _run_ib's deferred `from ib_async
    import IB` resolves to a controllable stub with no real network I/O."""
    _FakeIBModuleIB.attempts = []
    fake_module = types.ModuleType("ib_async")
    fake_module.IB = _FakeIBModuleIB  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ib_async", fake_module)
    return _FakeIBModuleIB


class TestRunIbRetryIsOptIn:
    async def _op(self, ib: Any) -> str:
        return "ok"

    def test_default_is_a_single_attempt_no_retry(self, monkeypatch, fake_ib_async_module):
        from backend import market_data

        monkeypatch.setattr(market_data, "CONNECT_RETRY_DELAY_SECONDS", 0.0)
        fake_ib_async_module.fail_times = 1  # would need a 2nd attempt to succeed
        with pytest.raises(TimeoutError):
            _run_ib(self._op)  # retry=False (default) — never gets a 2nd attempt
        assert fake_ib_async_module.attempts == [1]

    def test_retry_true_retries_and_succeeds(self, monkeypatch, fake_ib_async_module):
        from backend import market_data

        monkeypatch.setattr(market_data, "CONNECT_RETRY_DELAY_SECONDS", 0.0)
        fake_ib_async_module.fail_times = 1
        result = _run_ib(self._op, retry=True)
        assert result == "ok"
        assert fake_ib_async_module.attempts == [1, 2]

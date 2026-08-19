"""The option-quote fetch polls until every ticker has data (#230): early
return when ticks are prompt, partial results at the ceiling when one never
arrives — a fixed window clipped slow batches on the first armed run."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import patch

import backend.market_data as md
from backend.market_data import fetch_options_latest_quotes

SYM = "XSP260925C00771000"


class _FakeIB:
    """Just enough IB surface for fetch_options_latest_quotes's operation."""

    def __init__(self, ticker_fields):
        self._fields = ticker_fields
        self.cancelled = 0

    async def qualifyContractsAsync(self, *contracts):
        for i, c in enumerate(contracts):
            c.conId = 1000 + i
        return list(contracts)

    def reqMktData(self, contract, *_args):
        return SimpleNamespace(contract=contract, **self._fields)

    def cancelMktData(self, _contract):
        self.cancelled += 1


def _drive(ib):
    """Run the module's operation against a fake ib instead of a Gateway."""

    def fake_run_ib(operation):
        return asyncio.run(operation(ib))

    with patch.object(md, "_run_ib", fake_run_ib):
        return fetch_options_latest_quotes([SYM])


def test_returns_as_soon_as_all_tickers_have_data():
    ib = _FakeIB({"bid": 1.20, "ask": 1.30, "last": 0.0, "close": 0.0})
    start = time.monotonic()
    quotes = _drive(ib)
    elapsed = time.monotonic() - start
    assert quotes == {SYM: 1.25}
    assert elapsed < md.OPTION_QUOTE_MAX_WAIT_SECONDS / 2  # early exit, not the ceiling
    assert ib.cancelled == 1  # subscription always torn down


def test_waits_out_the_ceiling_when_data_never_arrives(monkeypatch):
    monkeypatch.setattr(md, "OPTION_QUOTE_MAX_WAIT_SECONDS", 1.0)
    ib = _FakeIB({"bid": float("nan"), "ask": -1.0, "last": -1.0, "close": 0.0})
    start = time.monotonic()
    quotes = _drive(ib)
    elapsed = time.monotonic() - start
    assert quotes == {}  # unpriceable symbol omitted, not fabricated
    assert elapsed >= 1.0  # gave the feed the whole (shrunk) ceiling
    assert ib.cancelled == 1

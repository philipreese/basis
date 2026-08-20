"""
market_data.py — Layer B Market Data Fetching (Interactive Brokers)

Fetches SPY historical bars (for SMA20 / daily return / closing price), the
VIX closing value, and option quotes from IB Gateway over the TWS API
(ib_async), using IBKR's free 15-minute-delayed data — no subscriptions.

All broker I/O is isolated here so the rest of the codebase can be tested
without network access (mock external services). The IB event loop runs in a
dedicated thread per call, so these functions stay synchronous and safe to
invoke from async handlers, exactly like the HTTP client they replaced.

If IB Gateway is unreachable (not running, not logged in) or any request
fails, every public function returns None/{} so callers fall back to the
saved database state without crashing. Gateway must be running at call time;
process lifecycle management arrives with the Executor build (#32).
"""

import asyncio
import concurrent.futures
import logging
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

# How many trading days of closes we need for SMA20 (+1 for daily return)
SMA_LOOKBACK = 22
CONNECT_TIMEOUT = 10  # seconds to reach the Gateway
CALL_TIMEOUT = 60  # seconds for a whole fetch operation

_DELAYED = 3  # IBKR market data type: free delayed data, no subscriptions

# Ceiling on how long a streaming option-quote batch waits for delayed ticks
# (#201, #230). Ticks arrive in ~1-3s solo but a fixed 5s window clipped slow
# batches under the 27-book pricing load on the first armed run — the fetch
# now polls every OPTION_QUOTE_POLL_SECONDS and returns as soon as every
# contract has a usable field, so the ceiling only binds when the feed lags.
OPTION_QUOTE_MAX_WAIT_SECONDS = 15
OPTION_QUOTE_POLL_SECONDS = 0.5

_OCC_RE = re.compile(r"^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


def _gateway_config() -> tuple[str, int, int]:
    """Endpoint + the LONG-LIVED session's client id (the executor's broker)."""
    host = os.getenv("IBKR_GATEWAY_HOST", "127.0.0.1")
    port = int(os.getenv("IBKR_GATEWAY_PORT", "4002"))
    client_id = int(os.getenv("IBKR_CLIENT_ID", "17"))
    return host, port, client_id


def _data_client_id() -> int:
    """Client id for this module's TRANSIENT fetch connections. Must differ
    from the broker session's id: IBKR allows one connection per client id,
    and the executor holds IBKR_CLIENT_ID open for its whole run — reusing
    it here is Error 326 and a telemetry blackout (#198). If configuration
    makes them equal anyway, step past the session id rather than collide."""
    data_id = int(os.getenv("IBKR_DATA_CLIENT_ID", "18"))
    session_id = int(os.getenv("IBKR_CLIENT_ID", "17"))
    return data_id if data_id != session_id else session_id + 1


def _run_ib(operation: Callable[[Any], Awaitable[Any]]) -> Any:
    """Connect to IB Gateway and run *operation(ib)* on a dedicated thread.

    ib_async is asyncio-native; running it in its own thread + event loop keeps
    this module's public functions synchronous regardless of the caller's loop.
    Raises on any failure — public wrappers translate that into None/{}.
    """

    async def _session() -> Any:
        from ib_async import IB

        ib = IB()
        host, port, _session_id = _gateway_config()
        await asyncio.wait_for(ib.connectAsync(host, port, clientId=_data_client_id()), timeout=CONNECT_TIMEOUT)
        try:
            ib.reqMarketDataType(_DELAYED)
            return await operation(ib)
        finally:
            ib.disconnect()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _session()).result(timeout=CALL_TIMEOUT)


async def _daily_closes(ib: Any, contract: Any, days: int) -> list[float]:
    """Daily closing prices for *contract*, oldest-first."""
    bars = await ib.reqHistoricalDataAsync(
        contract,
        endDateTime="",
        durationStr=f"{days} D",
        barSizeSetting="1 day",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=1,
    )
    return [float(b.close) for b in bars]


def _fetch_spy_closes() -> list[float] | None:
    """SPY daily closes (oldest-first) from IB Gateway, or None on failure."""

    async def _op(ib: Any) -> list[float]:
        from ib_async import Stock

        return await _daily_closes(ib, Stock("SPY", "SMART", "USD"), SMA_LOOKBACK + 8)

    try:
        closes = _run_ib(_op)
        if not closes or len(closes) < 2:
            logger.warning("No SPY bars returned from IB Gateway")
            return None
        return closes
    except Exception as exc:
        logger.warning("Failed to fetch SPY bars from IB Gateway: %s", exc)
        return None


def _fetch_vix_value() -> float | None:
    """Latest VIX close via the CBOE index, falling back to the VIXY ETF proxy."""

    async def _op(ib: Any) -> float | None:
        from ib_async import Index, Stock

        for contract in (Index("VIX", "CBOE"), Stock("VIXY", "SMART", "USD")):
            try:
                closes = await _daily_closes(ib, contract, 4)
                if closes:
                    return closes[-1]
            except Exception as exc:
                logger.warning("No bars for %s: %s", contract.symbol, exc)
        return None

    try:
        return _run_ib(_op)
    except Exception as exc:
        logger.warning("Failed to fetch VIX from IB Gateway: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public API — signatures unchanged from the retired Alpaca client
# ---------------------------------------------------------------------------

# Symbols in index_history that are ETFs (IBKR Stock contracts), not CBOE
# cash indexes. Everything the executor trades or tracks per-underlying,
# plus the observation-engine inputs HYG/LQD/RSP (#251).
# AAPL is a common stock, not an ETF, but the same Stock contract applies.
ETF_SYMBOLS = frozenset({"SPY", "IWM", "GLD", "TLT", "HYG", "LQD", "RSP", "AAPL"})


class SpySnapshot:
    """Parsed SPY market data."""

    __slots__ = ("daily_return", "price", "sma20")

    def __init__(self, price: float, sma20: float, daily_return: float) -> None:
        self.price = price
        self.sma20 = sma20
        self.daily_return = daily_return


def snapshot_from_closes(closes: list[float]) -> SpySnapshot | None:
    """Pure math: latest close, SMA20, and daily return from a close series."""
    if len(closes) < 2:
        return None
    price = closes[-1]
    daily_return = (closes[-1] / closes[-2]) - 1.0
    lookback = min(20, len(closes))
    sma20 = sum(closes[-lookback:]) / lookback
    return SpySnapshot(price=price, sma20=sma20, daily_return=daily_return)


def fetch_spy_snapshot() -> SpySnapshot | None:
    """Latest SPY close, 20-day SMA, and daily return — or None on failure."""
    closes = _fetch_spy_closes()
    if closes is None:
        return None
    return snapshot_from_closes(closes)


def fetch_vix_close() -> float | None:
    """Latest VIX daily close (CBOE index, VIXY fallback), or None on failure."""
    return _fetch_vix_value()


def fetch_index_daily_closes(symbol: str, days: int) -> list[tuple[str, float]] | None:
    """Dated daily closes for an index or ETF, oldest-first, or None on failure.

    Feeds the index_history table (VIX / VIX3M plus the ETF underlyings) that
    the V1/V2 regime-engine variants and the per-underlying telemetry (#139)
    read. Dates are ISO strings as reported by IBKR daily bars. ETFs route as
    Stock; everything else as a CBOE cash index.
    """

    async def _op(ib: Any) -> list[tuple[str, float]]:
        from ib_async import Index, Stock

        contract = Stock(symbol, "SMART", "USD") if symbol in ETF_SYMBOLS else Index(symbol, "CBOE")
        duration = "1 Y" if days >= 365 else f"{days} D"
        bars = await ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
        return [(str(b.date), float(b.close)) for b in bars]

    try:
        rows = _run_ib(_op)
        if not rows:
            logger.warning("No %s bars returned from IB Gateway", symbol)
            return None
        return rows
    except Exception as exc:
        logger.warning("Failed to fetch %s bars from IB Gateway: %s", symbol, exc)
        return None


def fetch_market_telemetry() -> dict | None:
    """
    Convenience wrapper: fetch both SPY and VIX in one call.

    Returns {"spy_price", "spy_sma20", "spy_daily_return", "vix_close"} —
    with vix_close 0.0 as a sentinel when only VIX failed — or None when SPY
    itself is unavailable.
    """
    spy = fetch_spy_snapshot()
    if spy is None:
        return None
    vix = fetch_vix_close()
    return {
        "spy_price": spy.price,
        "spy_sma20": spy.sma20,
        "spy_daily_return": spy.daily_return,
        "vix_close": vix if vix is not None else 0.0,
    }


def format_occ_symbol(underlying: str, expiration: str, option_type: str, strike: float) -> str:
    """
    Format option parameters into a standard OCC option symbol.
    Format: [Ticker][YYMMDD][C/P][Strike Price * 1000 (padded to 8 chars)]

    OCC symbols remain the canonical quote-map key across the codebase even
    though IBKR contracts are built from the parsed parts.
    """
    ticker_part = underlying.upper().strip()
    parts = expiration.split("-")
    date_part = f"{parts[0][2:]}{parts[1]}{parts[2]}"
    type_part = "C" if option_type.upper() == "CALL" else "P"
    strike_part = f"{round(strike * 1000):08d}"
    return f"{ticker_part}{date_part}{type_part}{strike_part}"


def parse_occ_symbol(symbol: str) -> dict | None:
    """Inverse of format_occ_symbol. Returns underlying/expiration/right/strike."""
    m = _OCC_RE.match(symbol)
    if not m:
        return None
    ticker, yy, mm, dd, right, strike_part = m.groups()
    return {
        "underlying": ticker,
        "expiration": f"20{yy}{mm}{dd}",  # IBKR contract format YYYYMMDD
        "right": right,
        "strike": int(strike_part) / 1000.0,
    }


def fetch_options_latest_quotes(symbols: list[str]) -> dict[str, float]:
    """
    Fetch delayed quotes (mid-price) for a list of OCC option symbols via IB
    Gateway. Returns a dict mapping OCC symbol to price; {} on failure.
    """
    parsed = [(sym, parse_occ_symbol(sym)) for sym in symbols]
    valid = [(sym, p) for sym, p in parsed if p is not None]
    if not valid:
        return {}

    async def _op(ib: Any) -> dict[str, float]:
        from ib_async import Option

        contracts = [
            Option(p["underlying"], p["expiration"], p["strike"], p["right"], "SMART", currency="USD") for _, p in valid
        ]
        # Expired/unknown contracts come back unqualified (None or conId 0) —
        # skip them; the caller treats missing symbols as unpriceable legs.
        qualified = await ib.qualifyContractsAsync(*contracts)
        pairs = [((sym, p), c) for (sym, p), c in zip(valid, qualified, strict=False) if c is not None and c.conId]
        if not pairs:
            return {}
        # STREAMING requests, not snapshots: under delayed data (type 3, the
        # free tier) IBKR rejects snapshot requests with Error 10091 and
        # returns NaN — reqTickersAsync made most candidates unpriceable
        # (#201). Poll until every ticker has a usable field or the ceiling
        # hits (#230), then cancel every subscription.
        tickers = [ib.reqMktData(c, "", False, False) for _, c in pairs]

        def _has_data(t: Any) -> bool:
            return any(v and v > 0 for v in (t.bid, t.ask, t.last, t.close))

        waited = 0.0
        while waited < OPTION_QUOTE_MAX_WAIT_SECONDS:
            await asyncio.sleep(OPTION_QUOTE_POLL_SECONDS)
            waited += OPTION_QUOTE_POLL_SECONDS
            if all(_has_data(t) for t in tickers):
                break

        quotes: dict[str, float] = {}
        by_conid = {c.conId: sym for (sym, _), c in pairs}
        for t in tickers:
            sym = by_conid.get(t.contract.conId)
            if sym is None:
                continue
            bid = t.bid if t.bid and t.bid > 0 else 0.0
            ask = t.ask if t.ask and t.ask > 0 else 0.0
            if bid > 0 and ask > 0:
                quotes[sym] = round((bid + ask) / 2.0, 2)
            elif ask > 0:
                quotes[sym] = ask
            elif bid > 0:
                quotes[sym] = bid
            elif t.last and t.last > 0:
                quotes[sym] = float(t.last)
            elif t.close and t.close > 0:
                quotes[sym] = float(t.close)
        for _, c in pairs:
            ib.cancelMktData(c)
        return quotes

    try:
        return _run_ib(_op)
    except Exception as exc:
        logger.warning("Failed to fetch option quotes from IB Gateway: %s", exc)
        return {}

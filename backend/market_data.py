"""
market_data.py — Layer B Market Data Fetching

Fetches SPY historical bars (for SMA20 / daily return / closing price) and
VIX closing price from the Alpaca Markets API.

All HTTP calls are isolated here so the rest of the codebase can be tested
without network access (Rule 03: mock external services).

If ALPACA_API_KEY_ID / ALPACA_SECRET_KEY are absent or any request fails,
every public function returns None so callers can fall back to the saved
database state without crashing.
"""

import os
import logging
from datetime import date, timedelta
from typing import Optional, Dict

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (read lazily from environment at call time — never hard-coded)
# ---------------------------------------------------------------------------
ALPACA_DATA_URL: str = os.getenv(
    "ALPACA_DATA_URL", "https://data.alpaca.markets/v2"
)

# How many historical bars to fetch for SMA20 (need at least 21)
SMA_LOOKBACK = 22
REQUEST_TIMEOUT = 10  # seconds


def _is_configured() -> bool:
    """True if both Alpaca credentials are present in the environment."""
    return bool(os.environ.get("ALPACA_API_KEY_ID") and os.environ.get("ALPACA_SECRET_KEY"))


def _headers() -> Dict[str, str]:
    return {
        "APCA-API-KEY-ID": os.environ.get("ALPACA_API_KEY_ID", ""),
        "APCA-API-SECRET-KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_bars(symbol: str, limit: int = SMA_LOOKBACK) -> Optional[list]:
    """
    Fetch recent *daily* bars for *symbol* from Alpaca.
    Returns a list of bar dicts ordered oldest-first, or None on failure.
    """
    if not _is_configured():
        logger.warning("Alpaca credentials not configured — skipping bar fetch for %s", symbol)
        return None

    url = f"{ALPACA_DATA_URL}/stocks/{symbol}/bars"
    # Go back 60 calendar days to guarantee enough trading days for the lookback
    start = (date.today() - timedelta(days=60)).isoformat()
    params = {
        "timeframe": "1Day",
        "start": start,
        "limit": limit,
        "adjustment": "raw",
        "feed": "iex",
    }

    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            resp = client.get(url, headers=_headers(), params=params)
            resp.raise_for_status()
            payload = resp.json()
            bars = payload.get("bars") or []
            if not bars:
                logger.warning("No bars returned for %s", symbol)
                return None
            return bars
    except Exception as exc:
        logger.warning("Failed to fetch bars for %s: %s", symbol, exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class SpySnapshot:
    """Parsed SPY market data."""

    __slots__ = ("price", "sma20", "daily_return")

    def __init__(self, price: float, sma20: float, daily_return: float) -> None:
        self.price = price
        self.sma20 = sma20
        self.daily_return = daily_return


def fetch_spy_snapshot() -> Optional[SpySnapshot]:
    """
    Fetch SPY bars and compute:
    - Latest closing price
    - 20-day SMA of closing prices
    - Daily return (today_close / yesterday_close - 1)

    Returns SpySnapshot or None on failure.
    """
    bars = _get_bars("SPY", limit=SMA_LOOKBACK)
    if not bars or len(bars) < 2:
        return None

    closes = [float(b["c"]) for b in bars]
    price = closes[-1]
    fetched_live = False

    # Try to fetch the live current trade price of SPY
    if _is_configured():
        url = f"{ALPACA_DATA_URL}/stocks/SPY/trades/latest"
        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
                resp = client.get(url, headers=_headers(), params={"feed": "iex"})
                resp.raise_for_status()
                trade_data = resp.json()
                if "trade" in trade_data and "p" in trade_data["trade"]:
                    price = float(trade_data["trade"]["p"])
                    fetched_live = True
        except Exception as exc:
            logger.warning("Failed to fetch latest trade for SPY: %s. Falling back to last daily bar close.", exc)

    if fetched_live:
        # Determine yesterday's close for daily return calculation relative to live trade
        yesterday_close = closes[-1]
        last_bar_date_str = bars[-1].get("t")
        if last_bar_date_str:
            last_bar_date = last_bar_date_str.split("T")[0]
            today_str = date.today().isoformat()
            if last_bar_date == today_str and len(closes) >= 2:
                yesterday_close = closes[-2]
        daily_return = (price / yesterday_close) - 1.0
    else:
        # Fallback to the last two bars return
        daily_return = (closes[-1] / closes[-2]) - 1.0

    # SMA20: average of the last 20 bars (or all bars if fewer than 20)
    lookback = min(20, len(closes))
    sma20 = sum(closes[-lookback:]) / lookback

    return SpySnapshot(price=price, sma20=sma20, daily_return=daily_return)


def fetch_vix_close() -> Optional[float]:
    """
    Fetch the latest VIX daily closing price.
    VIX is available on Alpaca via the symbol 'VIXY' (ETF proxy) or
    directly as an index bar 'VIX' depending on account type.
    We try 'VIX' first, then fall back to 'VIXY'.

    Returns the closing price as a float or None on failure.
    """
    for symbol in ("VIX", "VIXY"):
        bars = _get_bars(symbol, limit=2)
        if bars:
            return float(bars[-1]["c"])
    return None


def fetch_market_telemetry() -> Optional[Dict]:
    """
    Convenience wrapper: fetch both SPY and VIX in one call.

    Returns a dict:
        {
          "spy_price": float,
          "spy_sma20": float,
          "spy_daily_return": float,
          "vix_close": float,
        }
    or None if either fetch fails.
    """
    spy = fetch_spy_snapshot()
    vix = fetch_vix_close()

    if spy is None or vix is None:
        # Return partial data if at least SPY succeeded
        if spy is not None:
            return {
                "spy_price": spy.price,
                "spy_sma20": spy.sma20,
                "spy_daily_return": spy.daily_return,
                "vix_close": 0.0,  # sentinel — caller must handle 0
            }
        return None

    return {
        "spy_price": spy.price,
        "spy_sma20": spy.sma20,
        "spy_daily_return": spy.daily_return,
        "vix_close": vix,
    }


def format_occ_symbol(underlying: str, expiration: str, option_type: str, strike: float) -> str:
    """
    Format option parameters into a standard OCC option symbol.
    Format: [Ticker][YYMMDD][C/P][Strike Price * 1000 (padded to 8 chars)]
    """
    ticker_part = underlying.upper().strip()
    
    # Expiration YYYY-MM-DD -> YYMMDD
    parts = expiration.split("-")
    yy = parts[0][2:]
    mm = parts[1]
    dd = parts[2]
    date_part = f"{yy}{mm}{dd}"
    
    type_part = "C" if option_type.upper() == "CALL" else "P"
    
    # Strike price * 1000 padded to 8 digits
    strike_cents = int(round(strike * 1000))
    strike_part = f"{strike_cents:08d}"
    
    return f"{ticker_part}{date_part}{type_part}{strike_part}"


def fetch_options_latest_quotes(symbols: list[str]) -> dict[str, float]:
    """
    Fetch the latest quotes (mid-price) for a list of OCC option symbols from Alpaca.
    Returns a dict mapping OCC symbol to mid-price (float).
    """
    if not _is_configured() or not symbols:
        return {}

    url = "https://data.alpaca.markets/v1beta1/options/quotes/latest"
    chunk_size = 100
    quotes_map = {}

    try:
        for i in range(0, len(symbols), chunk_size):
            chunk = symbols[i:i + chunk_size]
            params = {
                "symbols": ",".join(chunk),
                "feed": "indicative"
            }
            with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
                resp = client.get(url, headers=_headers(), params=params)
                resp.raise_for_status()
                payload = resp.json()
                quotes = payload.get("quotes") or {}
                for sym, q in quotes.items():
                    bid = q.get("bp") or 0.0
                    ask = q.get("ap") or 0.0
                    if bid > 0 and ask > 0:
                        quotes_map[sym] = round((bid + ask) / 2.0, 2)
                    elif ask > 0:
                        quotes_map[sym] = ask
                    elif bid > 0:
                        quotes_map[sym] = bid
    except Exception as exc:
        logger.warning("Failed to fetch options quotes from Alpaca: %s", exc)

    return quotes_map

"""telemetry.py — per-underlying market-telemetry access (#190).

Every scan input (price, SMA20, IVR) resolves through one proxy-aware key:
XSP is 1/10 SPX and carries no liquid telemetry of its own, so SPY serves
as its same-scale proxy (#139). SPY-scale tickers read the market state's
scalar fields; everything else reads the per-underlying dicts the executor
populates from index_history. None means no telemetry — callers must
suppress, never trade blind.
"""

from backend.models import MarketStateSchema

# Tickers whose telemetry (price, SMA20, IVR) is served by a proxy symbol.
TELEMETRY_PROXY = {"XSP": "SPY"}


def telemetry_key(ticker: str) -> str:
    return TELEMETRY_PROXY.get(ticker, ticker)


def underlying_price(market_state: MarketStateSchema, ticker: str) -> float | None:
    """Current price for *ticker*: spy_price for SPY-scale tickers, else the
    per-underlying dict the executor populates from index_history (#139)."""
    key = telemetry_key(ticker)
    if key == "SPY":
        return market_state.spy_price
    return (market_state.underlying_prices or {}).get(key)


def underlying_sma20(market_state: MarketStateSchema, ticker: str) -> float:
    key = telemetry_key(ticker)
    if key == "SPY":
        return market_state.spy_sma20 or 0.0
    return (market_state.underlying_sma20 or {}).get(key, 0.0)


def trend_label(price: float, sma20: float) -> str:
    """ABOVE_SMA20 / BELOW_SMA20 / ANY for the entry filters' trend gate."""
    if sma20 == 0:
        return "ANY"
    diff_pct = (price - sma20) / sma20 * 100
    if diff_pct > 0:
        return "ABOVE_SMA20"
    if diff_pct < 0:
        return "BELOW_SMA20"
    return "ANY"

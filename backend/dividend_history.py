"""dividend_history.py — free per-share dividend history: the fallback
distribution source for share books on a night the Flex query has no Cash
Transactions section (#1083; see share_distributions.py for the crediting
logic and the Flex-vs-fallback precedence).

Why this exists: PR #1077 (#1074) credits share-book distributions from the
Activity Flex statement's Cash Transactions section. Adding that section
requires the operator to edit the Flex query in IBKR's Client Portal, but the
paper account's Client Portal shows essentially no settings or reporting
menus — there is nothing to click (#1083). Confirmed empirically against the
live paper query (read-only, via the existing weekly audit's own
`fetch_flex_statement`, 2026-10-04): the configured query returns only a
`Trades` section; no `CashTransactions` section is present. This fallback
lets the share book get credited without operator menu-hunting.

Source: Yahoo Finance's public chart endpoint (`events=div`), over httpx
(already a project dependency — no new package). It returns ex-dividend
dates and per-share amounts. It does NOT return a payment date — checked
empirically: each `events.dividends` entry carries only `amount` and `date`,
where `date` is the ex-dividend date. #1077 preferred pay-date crediting;
lacking a free pay-date source, this fallback credits on the EX-DATE instead
(a judgment call, not a hidden limitation — see the #1083 PR). One side
effect: crediting on the ex-date removes the few days' mark dip #1077 noted
between the price drop (ex-date) and the cash arriving (pay date), since now
both land the same day.

Telling "resolved, pays nothing" (e.g. IAUM, a gold trust with no income)
apart from "could not resolve the symbol, or the fetch failed": a resolved
symbol's response always carries price `timestamp` bars for the requested
range; an empty or missing `dividends` map on a response that DOES have bars
is a genuine zero, never a failure to surface."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

logger = logging.getLogger(__name__)

_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_RANGE = "10y"
_USER_AGENT = "Mozilla/5.0 (compatible; basis-dividend-history/1.0)"


class PublicDividendError(RuntimeError):
    """The public dividend-history fetch failed outright (network/HTTP/parse)."""


@dataclass(frozen=True)
class PublicDividend:
    symbol: str
    ex_date: str  # ISO date (UTC calendar date of the ex-dividend event)
    amount_per_share: float


def fetch_public_dividends(symbol: str) -> list[PublicDividend] | None:
    """Every known ex-dividend event for *symbol*, oldest first.

    None means the symbol could not be resolved (no price history at all for
    the requested range) — distinct from a resolved symbol that has simply
    never paid one ([]). Raises PublicDividendError on a genuine fetch
    failure (network, HTTP status, unparseable body)."""
    try:
        resp = httpx.get(
            _YAHOO_CHART_URL.format(symbol=symbol),
            params={"events": "div", "range": _RANGE, "interval": "1d"},
            headers={"User-Agent": _USER_AGENT},
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise PublicDividendError(f"{symbol}: {type(exc).__name__}: {exc}") from exc

    results = ((data or {}).get("chart") or {}).get("result") or []
    if not results:
        return None  # unresolved symbol (Yahoo's own "error" shape, or an empty result list)
    result = results[0]
    if not result.get("timestamp"):
        return None  # resolved response shape but no price bars: still unresolved

    divs = ((result.get("events") or {}).get("dividends")) or {}
    out: list[PublicDividend] = []
    for entry in divs.values():
        ts = entry.get("date")
        amount = entry.get("amount")
        if ts is None or amount is None:
            continue
        ex_date = datetime.fromtimestamp(ts, tz=UTC).date().isoformat()
        out.append(PublicDividend(symbol=symbol, ex_date=ex_date, amount_per_share=float(amount)))
    out.sort(key=lambda d: d.ex_date)
    return out

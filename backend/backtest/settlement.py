"""settlement.py — intrinsic expiry settlement for the replay (#796 PR-3).

Mirrors the production intrinsic-settlement math in
executor._intrinsic_settlement_value (backend/executor.py:871-904): per leg
max(0, S−K) for a CALL / max(0, K−S) for a PUT, LONG adds and SHORT
subtracts, with the DEBIT/CREDIT flip identical — so a worthless spread
settles at exactly 0, never at a stale mark carrying residual time value.

CRITICAL dating rule (#793): SPX chain expire_dates for AM-settled
monthlies are recorded as the LAST-TRADING day (the Thursday before a
Friday AM settlement, or the holiday-shifted equivalent). The replay
settles using THAT day's underlying close — a declared approximation of the
true AM settlement print with the prior session's close. The direction is
conservative-neutral: it neither systematically flatters nor punishes the
position, and the gap (overnight move into the AM print) is symmetric
noise. XSP inherits the same dating via the chain store's ÷10 derived view.

Which close: the underlying resolves through the SAME telemetry_key proxy
production settlement uses (executor.py:887 — XSP settles off SPY-scale
telemetry, not a real-world SPX/10 conversion), read from the ClosesStore
at-or-before the position's expiration date — "at-or-before" IS the
last-trading-day close when the expiry string lands on a non-session day.
"""

from __future__ import annotations


def intrinsic_settlement_value(legs: list[dict], premium_direction: str, underlying_close: float) -> float:
    """Intrinsic value per share at expiry.

    Replicates backend/executor.py:892-904 verbatim (the loop cannot be
    imported: production's version is an async DB reader keyed to
    index_history; only the pure math is mirrored here, cited so drift is
    findable).
    """
    long_val = 0.0
    short_val = 0.0
    for leg in legs:
        strike = leg["strike"]
        intrinsic = (
            max(0.0, underlying_close - strike) if leg["option_type"] == "CALL" else max(0.0, strike - underlying_close)
        )
        if leg["direction"] == "LONG":
            long_val += intrinsic
        else:
            short_val += intrinsic
    new_val = long_val - short_val if premium_direction == "DEBIT" else short_val - long_val
    return round(new_val, 2)

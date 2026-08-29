import math
from typing import Any


def span_bound_max_loss(net: float, width_bound: float, fallback: float) -> float:
    """Per-share max loss derived from a live net price and a strike span:
    width − |net| for a credit (net < 0), |net| for a debit — the ONE home
    for a formula that used to live copied at three sites (entry-fill
    ingestion #686, roll encumbrance #356, and the #887 gate fix), which is
    exactly how the sites drifted apart.

    Guards (#686/#421): a zero-span structure (calendar, straddle/strangle)
    has no width to bound with, and net == 0.0 would either read a $0 fill
    as a full-width debit or zero out a credit's risk entirely — both keep
    *fallback* (the caller's model-side estimate) instead. A BWB's
    width_bound is its TOTAL span, so this OVER-states its true risk —
    callers that must not over-encumber a BWB own that exclusion."""
    if not width_bound or net == 0:
        return fallback
    return width_bound - abs(net) if net < 0 else abs(net)


def capital_at_risk(max_loss_per_share: float, contracts: int) -> float:
    """Capital at risk for a defined-risk position, in dollars.

    Always max loss — the collateral-relevant number — regardless of premium
    direction. (For a debit structure the max loss *should* equal the premium
    paid, but positions are entered manually; using entry_premium here would
    let a data-entry mismatch make the exposure gates and the Layer A
    safeguards disagree about deployed capital.)
    """
    return max_loss_per_share * 100 * contracts


def calculate_position_metrics(
    strategy_type: str,
    legs: list[dict[str, Any]],  # OptionLegSchema as dict
    entry_premium: float,
    premium_direction: str,
) -> dict[str, Any]:
    """
    Calculate max_profit, max_loss, and breakeven points on a raw per-share basis.
    All parameters are raw per-share numbers.
    """
    # Initialize defaults
    max_profit = 0.0
    max_loss = 0.0
    break_even_upside = None
    break_even_downside = None

    if not legs:
        return {
            "max_profit": max_profit,
            "max_loss": max_loss,
            "break_even_upside": break_even_upside,
            "break_even_downside": break_even_downside,
        }

    # Sort legs by strike for easy extraction
    sorted_legs = sorted(legs, key=lambda x: x["strike"])

    if strategy_type == "LONG_STRADDLE":
        # Usually 1 Call, 1 Put at the same strike
        strike = sorted_legs[0]["strike"]
        max_profit = float("inf")  # Unlimited
        max_loss = entry_premium
        break_even_downside = strike - entry_premium
        break_even_upside = strike + entry_premium

    elif strategy_type == "LONG_PUT":
        # Tail hedge (#319): one long put; the debit is the whole risk.
        strike = sorted_legs[0]["strike"]
        max_profit = strike - entry_premium  # underlying at zero
        max_loss = entry_premium
        break_even_downside = strike - entry_premium
        break_even_upside = None

    elif strategy_type == "LONG_STRANGLE":
        # 1 Put (lower strike), 1 Call (higher strike)
        put_strike = min(l["strike"] for l in legs if l["option_type"] == "PUT")
        call_strike = max(l["strike"] for l in legs if l["option_type"] == "CALL")
        max_profit = float("inf")
        max_loss = entry_premium
        break_even_downside = put_strike - entry_premium
        break_even_upside = call_strike + entry_premium

    elif strategy_type == "BULL_CALL_SPREAD":
        # Long Call at lower strike, Short Call at higher strike
        long_call = next(l for l in sorted_legs if l["option_type"] == "CALL" and l["direction"] == "LONG")
        short_call = next(l for l in sorted_legs if l["option_type"] == "CALL" and l["direction"] == "SHORT")
        width = short_call["strike"] - long_call["strike"]

        if premium_direction == "DEBIT":
            max_profit = width - entry_premium
            max_loss = entry_premium
            break_even_upside = long_call["strike"] + entry_premium
        else:
            # If credited by mistake
            max_profit = width + entry_premium
            max_loss = 0.0
            break_even_upside = long_call["strike"] - entry_premium

    elif strategy_type == "BEAR_PUT_SPREAD":
        # Long Put at higher strike, Short Put at lower strike
        long_put = next(l for l in sorted_legs if l["option_type"] == "PUT" and l["direction"] == "LONG")
        short_put = next(l for l in sorted_legs if l["option_type"] == "PUT" and l["direction"] == "SHORT")
        width = long_put["strike"] - short_put["strike"]

        if premium_direction == "DEBIT":
            max_profit = width - entry_premium
            max_loss = entry_premium
            break_even_downside = long_put["strike"] - entry_premium
        else:
            max_profit = width + entry_premium
            max_loss = 0.0
            break_even_downside = long_put["strike"] + entry_premium

    elif strategy_type == "BULL_PUT_SPREAD":
        # Short Put at higher strike, Long Put at lower strike (credit)
        short_put = next(l for l in sorted_legs if l["option_type"] == "PUT" and l["direction"] == "SHORT")
        long_put = next(l for l in sorted_legs if l["option_type"] == "PUT" and l["direction"] == "LONG")
        width = short_put["strike"] - long_put["strike"]

        if premium_direction == "CREDIT":
            max_profit = entry_premium
            max_loss = width - entry_premium
            break_even_downside = short_put["strike"] - entry_premium
        else:
            # If debited by mistake
            max_profit = 0.0
            max_loss = width + entry_premium
            break_even_downside = short_put["strike"] + entry_premium

    elif strategy_type == "BEAR_CALL_SPREAD":
        # Short Call at lower strike, Long Call at higher strike (credit)
        short_call = next(l for l in sorted_legs if l["option_type"] == "CALL" and l["direction"] == "SHORT")
        long_call = next(l for l in sorted_legs if l["option_type"] == "CALL" and l["direction"] == "LONG")
        width = long_call["strike"] - short_call["strike"]

        if premium_direction == "CREDIT":
            max_profit = entry_premium
            max_loss = width - entry_premium
            break_even_upside = short_call["strike"] + entry_premium
        else:
            max_profit = 0.0
            max_loss = width + entry_premium
            break_even_upside = short_call["strike"] - entry_premium

    elif strategy_type == "IRON_CONDOR":
        # 4 legs:
        # Long Put (A), Short Put (B), Short Call (C), Long Call (D)
        long_put = next(l for l in sorted_legs if l["option_type"] == "PUT" and l["direction"] == "LONG")
        short_put = next(l for l in sorted_legs if l["option_type"] == "PUT" and l["direction"] == "SHORT")
        short_call = next(l for l in sorted_legs if l["option_type"] == "CALL" and l["direction"] == "SHORT")
        long_call = next(l for l in sorted_legs if l["option_type"] == "CALL" and l["direction"] == "LONG")

        put_width = short_put["strike"] - long_put["strike"]
        call_width = long_call["strike"] - short_call["strike"]
        max_spread_width = max(put_width, call_width)

        if premium_direction == "CREDIT":
            max_profit = entry_premium
            max_loss = max_spread_width - entry_premium
            break_even_downside = short_put["strike"] - entry_premium
            break_even_upside = short_call["strike"] + entry_premium
        else:
            max_profit = 0.0
            max_loss = max_spread_width + entry_premium

    elif strategy_type == "BROKEN_WING_BUTTERFLY":
        # Put-side BWB entered for a credit (#132): +1 put at U (upper),
        # -2 puts at M (body), +1 put at D (lower), with the lower wing
        # (M-D) wider than the upper (U-M). No upside risk — above U the
        # credit is kept; max profit at S=M; risk only below the body.
        # Legs may arrive as 3 role entries or with the body expanded into
        # duplicates — strikes are selected by role, so both shapes work.
        put_strikes = sorted({l["strike"] for l in legs if l["option_type"] == "PUT"})
        lower, middle, upper = put_strikes[0], put_strikes[1], put_strikes[2]
        narrow = upper - middle
        wide = middle - lower

        if premium_direction == "CREDIT":
            max_profit = narrow + entry_premium
            max_loss = max(0.0, (wide - narrow) - entry_premium)
            # Payoff for D<=S<=M is (U-2M)+S+credit; zero at:
            break_even_downside = middle - narrow - entry_premium
        else:
            # Debited by mistake: the credit-side arithmetic with sign flipped
            max_profit = narrow - entry_premium
            max_loss = (wide - narrow) + entry_premium
            break_even_downside = middle - narrow + entry_premium

    elif strategy_type == "CALENDAR_SPREAD":
        # Long time spread (#133): SELL the front-month, BUY the same strike
        # in a later month, for a net debit. The true peak (underlying at the
        # strike on front expiry) depends on the back leg's remaining value —
        # not analytic without a vol model. Stated conventions:
        #   max_loss   = debit paid (true: both legs long-side defined risk)
        #   max_profit = debit paid (1:1, deliberately conservative)
        #   break-evens = none (vol-dependent; Layer A manages by value)
        max_loss = entry_premium
        max_profit = entry_premium

    # Replace float('inf') with 999999.0 for standard JSON serialization safety if needed
    if math.isinf(max_profit):
        max_profit = 999999.0
    if math.isinf(max_loss):
        max_loss = 999999.0

    return {
        "max_profit": max_profit,
        "max_loss": max_loss,
        "break_even_upside": break_even_upside,
        "break_even_downside": break_even_downside,
    }

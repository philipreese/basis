import math
from typing import Any


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

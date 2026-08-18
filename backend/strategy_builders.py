"""strategy_builders.py — per-strategy leg derivation (#149).

Each strategy registers one builder: BuildContext in, (legs, estimated
limit price per share) out. generate_trade_spec dispatches through
STRATEGY_BUILDERS, so adding a strategy means registering a builder here
plus a metrics branch in pricing.py — opportunity.py stays untouched. An
unknown strategy type is a registry miss, which the caller reports as the
UNKNOWN_STRATEGY hard block.

Limit prices are conservative staging estimates without a live chain —
the executor reprices every combo from live quotes before placement.
"""

import datetime
from collections.abc import Callable
from dataclasses import dataclass

from backend.models import ExecutionSpecs, TradeSpecLeg

# Calendar spreads (#133): the back leg sits one monthly cycle behind the
# front, snapped to a Friday like every other expiration.
CALENDAR_BACK_LEG_DAYS = 28


@dataclass(frozen=True)
class BuildContext:
    """Everything a builder may use, precomputed by generate_trade_spec."""

    price: float  # underlying price (per-underlying telemetry, #139)
    sigma: float  # 1σ move for the target DTE
    specs: ExecutionSpecs
    exp_str: str  # ISO expiration date (the FRONT expiry for time spreads)
    exp_date: datetime.date
    contracts: int
    otm_strike: Callable[[float, int], float]  # (delta, direction ±1) → strike
    nearest_strike: Callable[..., float]  # (price, interval=5.0) → strike


BuilderResult = tuple[list[TradeSpecLeg], float]


def _iron_condor(ctx: BuildContext) -> BuilderResult:
    specs = ctx.specs
    # Wing strikes honor the playbook width on the $1 grid (SPY/XSP list
    # $1 strikes near the money) — a $5 grid would silently widen $3
    # wings past the ADR-0006 per-trade cap (#94).
    short_call = ctx.otm_strike(specs.short_leg_delta, +1)
    long_call = ctx.nearest_strike(short_call + specs.spread_width_dollars, interval=1.0)
    short_put = ctx.otm_strike(specs.short_leg_delta, -1)
    long_put = ctx.nearest_strike(short_put - specs.spread_width_dollars, interval=1.0)
    legs = [
        TradeSpecLeg(
            action="SELL",
            option_type="PUT",
            strike=short_put,
            expiration_date=ctx.exp_str,
            quantity=ctx.contracts,
            delta_target=-specs.short_leg_delta,
        ),
        TradeSpecLeg(
            action="BUY",
            option_type="PUT",
            strike=long_put,
            expiration_date=ctx.exp_str,
            quantity=ctx.contracts,
            delta_target=None,
        ),
        TradeSpecLeg(
            action="SELL",
            option_type="CALL",
            strike=short_call,
            expiration_date=ctx.exp_str,
            quantity=ctx.contracts,
            delta_target=specs.short_leg_delta,
        ),
        TradeSpecLeg(
            action="BUY",
            option_type="CALL",
            strike=long_call,
            expiration_date=ctx.exp_str,
            quantity=ctx.contracts,
            delta_target=None,
        ),
    ]
    # Credit ≈ 1/3 spread width (conservative estimate without live chain)
    return legs, round(specs.spread_width_dollars / 3.0, 2)


def _bull_call_spread(ctx: BuildContext) -> BuilderResult:
    specs = ctx.specs
    buy_strike = ctx.otm_strike(specs.long_leg_delta, +1)  # ATM/near-ATM
    # Sell leg = buy + playbook width. The delta-derived sell leg produced
    # ~$30-wide spreads whose debit blew the per-trade cap (#94); width is
    # the sizing authority for autonomous entries.
    sell_strike = ctx.nearest_strike(buy_strike + specs.spread_width_dollars, interval=1.0)
    legs = [
        TradeSpecLeg(
            action="BUY",
            option_type="CALL",
            strike=buy_strike,
            expiration_date=ctx.exp_str,
            quantity=ctx.contracts,
            delta_target=specs.long_leg_delta,
        ),
        TradeSpecLeg(
            action="SELL",
            option_type="CALL",
            strike=sell_strike,
            expiration_date=ctx.exp_str,
            quantity=ctx.contracts,
            delta_target=specs.short_leg_delta,
        ),
    ]
    return legs, round((sell_strike - buy_strike) * 0.45, 2)  # ~45% of width (debit)


def _bear_put_spread(ctx: BuildContext) -> BuilderResult:
    specs = ctx.specs
    buy_strike = ctx.otm_strike(specs.long_leg_delta, -1)  # ATM/near-ATM put
    # Sell leg = buy − playbook width (see BULL_CALL_SPREAD note, #94)
    sell_strike = ctx.nearest_strike(buy_strike - specs.spread_width_dollars, interval=1.0)
    legs = [
        TradeSpecLeg(
            action="BUY",
            option_type="PUT",
            strike=buy_strike,
            expiration_date=ctx.exp_str,
            quantity=ctx.contracts,
            delta_target=-specs.long_leg_delta,
        ),
        TradeSpecLeg(
            action="SELL",
            option_type="PUT",
            strike=sell_strike,
            expiration_date=ctx.exp_str,
            quantity=ctx.contracts,
            delta_target=-specs.short_leg_delta,
        ),
    ]
    return legs, round((buy_strike - sell_strike) * 0.45, 2)


def _bull_put_spread(ctx: BuildContext) -> BuilderResult:
    specs = ctx.specs
    short_strike = ctx.otm_strike(specs.short_leg_delta, -1)  # OTM put below price
    long_strike = ctx.nearest_strike(short_strike - specs.spread_width_dollars, interval=1.0)
    legs = [
        TradeSpecLeg(
            action="SELL",
            option_type="PUT",
            strike=short_strike,
            expiration_date=ctx.exp_str,
            quantity=ctx.contracts,
            delta_target=-specs.short_leg_delta,
        ),
        TradeSpecLeg(
            action="BUY",
            option_type="PUT",
            strike=long_strike,
            expiration_date=ctx.exp_str,
            quantity=ctx.contracts,
            delta_target=None,
        ),
    ]
    # Credit ≈ 1/3 spread width (conservative estimate without live chain)
    return legs, round((short_strike - long_strike) / 3.0, 2)


def _bear_call_spread(ctx: BuildContext) -> BuilderResult:
    specs = ctx.specs
    short_strike = ctx.otm_strike(specs.short_leg_delta, +1)  # OTM call above price
    long_strike = ctx.nearest_strike(short_strike + specs.spread_width_dollars, interval=1.0)
    legs = [
        TradeSpecLeg(
            action="SELL",
            option_type="CALL",
            strike=short_strike,
            expiration_date=ctx.exp_str,
            quantity=ctx.contracts,
            delta_target=specs.short_leg_delta,
        ),
        TradeSpecLeg(
            action="BUY",
            option_type="CALL",
            strike=long_strike,
            expiration_date=ctx.exp_str,
            quantity=ctx.contracts,
            delta_target=None,
        ),
    ]
    return legs, round((long_strike - short_strike) / 3.0, 2)


def _broken_wing_butterfly(ctx: BuildContext) -> BuilderResult:
    specs = ctx.specs
    # Put-side BWB for a credit (#132): body at the short delta, narrow
    # upper wing = playbook width, lower wing = 2× width (skip-strike).
    # Above the upper strike the credit is kept — no upside risk; the
    # defined risk is (wide − narrow) − credit, below the body.
    width = specs.spread_width_dollars
    body = ctx.otm_strike(specs.short_leg_delta, -1)
    upper = ctx.nearest_strike(body + width, interval=1.0)
    lower = ctx.nearest_strike(body - 2 * width, interval=1.0)
    legs = [
        TradeSpecLeg(
            action="BUY",
            option_type="PUT",
            strike=upper,
            expiration_date=ctx.exp_str,
            quantity=ctx.contracts,
            delta_target=None,
        ),
        TradeSpecLeg(
            action="SELL",
            option_type="PUT",
            strike=body,
            expiration_date=ctx.exp_str,
            quantity=2 * ctx.contracts,
            delta_target=-specs.short_leg_delta,
        ),
        TradeSpecLeg(
            action="BUY",
            option_type="PUT",
            strike=lower,
            expiration_date=ctx.exp_str,
            quantity=ctx.contracts,
            delta_target=None,
        ),
    ]
    # Credit ≈ 1/4 of the narrow wing (conservative, without live chain)
    return legs, round(width / 4.0, 2)


def _calendar_spread(ctx: BuildContext) -> BuilderResult:
    # Long ATM call calendar (#133): SELL the front expiry, BUY the same
    # strike one monthly cycle out. Net debit = the position's entire risk.
    # The position's expiration_date is the FRONT leg — every DTE rule
    # (mandatory exit, staleness) keys off the near-dated risk. The seed
    # races on XSP only: the short front leg is cash-settled, so front-cycle
    # expiry can never assign shares (No-Stock Mandate).
    strike = ctx.nearest_strike(ctx.price)
    back_date = ctx.exp_date + datetime.timedelta(days=CALENDAR_BACK_LEG_DAYS)
    back_date += datetime.timedelta(days=(4 - back_date.weekday()) % 7)  # snap to Friday
    legs = [
        TradeSpecLeg(
            action="SELL",
            option_type="CALL",
            strike=strike,
            expiration_date=ctx.exp_str,
            quantity=ctx.contracts,
            delta_target=0.5,
        ),
        TradeSpecLeg(
            action="BUY",
            option_type="CALL",
            strike=strike,
            expiration_date=back_date.isoformat(),
            quantity=ctx.contracts,
            delta_target=0.5,
        ),
    ]
    # Debit ≈ 10% of the front-DTE 1σ move (rough theta-differential proxy;
    # the executor reprices from live quotes before placing).
    return legs, round(max(ctx.sigma * 0.10, 0.5), 2)


def _long_straddle(ctx: BuildContext) -> BuilderResult:
    atm = ctx.nearest_strike(ctx.price)
    legs = [
        TradeSpecLeg(
            action="BUY",
            option_type="CALL",
            strike=atm,
            expiration_date=ctx.exp_str,
            quantity=ctx.contracts,
            delta_target=0.5,
        ),
        TradeSpecLeg(
            action="BUY",
            option_type="PUT",
            strike=atm,
            expiration_date=ctx.exp_str,
            quantity=ctx.contracts,
            delta_target=-0.5,
        ),
    ]
    # Estimate debit as σ-adjusted; straddle ≈ 0.8 * 1σ move (rough)
    return legs, round(ctx.sigma * 0.8 * 2, 2)  # call + put


def _long_strangle(ctx: BuildContext) -> BuilderResult:
    specs = ctx.specs
    call_strike = ctx.otm_strike(specs.short_leg_delta, +1)
    put_strike = ctx.otm_strike(specs.short_leg_delta, -1)
    legs = [
        TradeSpecLeg(
            action="BUY",
            option_type="CALL",
            strike=call_strike,
            expiration_date=ctx.exp_str,
            quantity=ctx.contracts,
            delta_target=specs.short_leg_delta,
        ),
        TradeSpecLeg(
            action="BUY",
            option_type="PUT",
            strike=put_strike,
            expiration_date=ctx.exp_str,
            quantity=ctx.contracts,
            delta_target=-specs.short_leg_delta,
        ),
    ]
    return legs, round(ctx.sigma * 0.5 * 2, 2)  # strangle cheaper than straddle


STRATEGY_BUILDERS: dict[str, Callable[[BuildContext], BuilderResult]] = {
    "IRON_CONDOR": _iron_condor,
    "BULL_CALL_SPREAD": _bull_call_spread,
    "BEAR_PUT_SPREAD": _bear_put_spread,
    "BULL_PUT_SPREAD": _bull_put_spread,
    "BEAR_CALL_SPREAD": _bear_call_spread,
    "BROKEN_WING_BUTTERFLY": _broken_wing_butterfly,
    "CALENDAR_SPREAD": _calendar_spread,
    "LONG_STRADDLE": _long_straddle,
    "LONG_STRANGLE": _long_strangle,
}

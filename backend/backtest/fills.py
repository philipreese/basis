"""fills.py — the replay fill model (#796 PR-3): worst-side fills off real chains.

Declared assumptions (ADR-0015 §4 — every gap an assumption fills is named
here and stamped into the run log by PR-4):

1. **Worst-side fills.** Entries and exits fill at the touch AGAINST the
   trader: SELL legs at the bid, BUY legs at the ask, per contract × 100.
   NO additional slippage haircut is applied on top — worst-side already
   embodies the full quoted spread cost, and stacking a second haircut on
   it is the double-haircut trap #796 names explicitly.
2. **Commission** is a flat ``COMMISSION_PER_CONTRACT`` ($0.65) per
   leg-contract — an IBKR tiered-ish ballpark. Production commissions come
   from real fill records (FillModel.commission), which do not exist for a
   replay, so a declared constant stands in.
3. **Strike snapping.** The production pipeline derives strikes
   analytically (underlying + VIX) and lets a non-existent strike fail to
   quote; a replay must instead snap each spec leg to the NEAREST listed
   strike in the fill-day snapshot for its expiration. Exact-distance ties
   break AWAY from the money in the leg's own OTM direction (puts snap
   down, calls snap up) — conservative for the credit structures this lab
   trades: a farther-OTM short leg collects LESS credit, never more.
4. **No substitute expirations.** A spec whose target expiration has no
   listing in the fill-day chain is ABANDONED and counted — never quietly
   moved to a nearby expiry the production rules did not choose.
5. **Never partial-fill.** Any leg missing the side it needs (one-sided or
   unquoted after the chain store's load-time filter) abandons the whole
   entry, counted — a spread is one order at the broker, not four.
6. **Marks.** A position marks at the mid when both sides quote; if any
   leg is one-sided or unquoted that day the position KEEPS its prior mark
   and the staleness is counted per position — the same "never invent a
   price" posture as production stale-mark handling (executor.py's
   staleness guards); prices are read, never fabricated.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.backtest.chain_store import ChainSnapshot
from backend.models import TradeSpecLeg

#: Per leg-contract, per side (assumption 2 above).
COMMISSION_PER_CONTRACT = 0.65

# Abandonment reasons — the counted vocabulary, not free text.
NO_SNAPSHOT = "NO_SNAPSHOT"  # chain has no rows at all for the fill day
NO_EXPIRATION = "NO_EXPIRATION"  # target expiry not listed that day (assumption 4)
MISSING_SIDE = "MISSING_SIDE"  # a needed bid/ask is absent (assumption 5)


@dataclass(frozen=True)
class FilledLeg:
    """One leg as actually filled: snapped strike, worst-side price."""

    action: str  # BUY | SELL
    option_type: str  # CALL | PUT
    strike: float  # snapped to the listed grid
    expiration: str  # ISO date
    ratio: int  # combo ratio (BWB body = 2)
    price: float  # per share: ask for BUY, bid for SELL


@dataclass(frozen=True)
class EntryFill:
    """A fully-priced worst-side entry fill for one spread (contracts=1 base).

    net_per_share uses the production sign convention (executor.py:2058):
    BUY = +price, SELL = −price — so a credit structure nets negative.
    """

    legs: tuple[FilledLeg, ...]
    net_per_share: float
    commission: float  # dollars, for `contracts` spreads


@dataclass(frozen=True)
class CloseFill:
    """A worst-side exit fill: LONG legs sell at bid, SHORT legs buy at ask.

    exit_value_per_share is the SIGNED cash flow per share, the same
    convention _order_to_position consumes (executor.py:956-993): negative
    when buying back a credit spread, positive when selling out of a debit.
    """

    exit_value_per_share: float
    commission: float


@dataclass(frozen=True)
class Abandoned:
    """No fill. The entry/exit is abandoned and counted (assumptions 4-5)."""

    reason: str
    detail: str


def listed_strikes(snapshot: ChainSnapshot, expiration: str, right: str) -> list[float]:
    """Strikes listed for (expiration, right) in this snapshot, ascending."""
    return sorted({k[1] for k in snapshot.quotes if k[0] == expiration and k[2] == right})


def snap_strike(snapshot: ChainSnapshot, expiration: str, right: str, target: float) -> float | None:
    """Nearest listed strike for the leg; None when the expiry isn't listed.

    Exact-distance ties break AWAY from the money in the leg's own OTM
    direction — puts snap to the LOWER strike, calls to the HIGHER
    (assumption 3: conservative for credit structures, reduces credit).
    """
    strikes = listed_strikes(snapshot, expiration, right)
    if not strikes:
        return None
    best = min(abs(s - target) for s in strikes)
    tied = [s for s in strikes if abs(s - target) == best]
    if len(tied) == 1:
        return tied[0]
    return min(tied) if right == "P" else max(tied)


def fill_entry(legs: list[TradeSpecLeg], snapshot: ChainSnapshot, contracts: int) -> EntryFill | Abandoned:
    """Price a spec's legs worst-side against the fill-day snapshot."""
    filled: list[FilledLeg] = []
    for leg in legs:
        right = "C" if leg.option_type == "CALL" else "P"
        strike = snap_strike(snapshot, leg.expiration_date, right, leg.strike)
        if strike is None:
            return Abandoned(NO_EXPIRATION, f"{leg.expiration_date} {right} has no listed strikes")
        quote = snapshot.quotes.get((leg.expiration_date, strike, right))
        price = None if quote is None else (quote.ask if leg.action == "BUY" else quote.bid)
        if price is None:
            side = "ask" if leg.action == "BUY" else "bid"
            return Abandoned(MISSING_SIDE, f"{leg.expiration_date} {strike} {right}: no {side}")
        filled.append(
            FilledLeg(
                action=leg.action,
                option_type=leg.option_type,
                strike=strike,
                expiration=leg.expiration_date,
                ratio=max(1, leg.quantity),
                price=price,
            )
        )
    # Sign convention mirrors executor.py:2058 (BUY=+, SELL=−, ratio-weighted).
    net = round(sum((leg.price if leg.action == "BUY" else -leg.price) * leg.ratio for leg in filled), 2)
    leg_contracts = sum(leg.ratio for leg in filled) * contracts
    return EntryFill(
        legs=tuple(filled),
        net_per_share=net,
        commission=round(COMMISSION_PER_CONTRACT * leg_contracts, 2),
    )


def fill_close(position_legs: list[dict], snapshot: ChainSnapshot, contracts: int) -> CloseFill | Abandoned:
    """Worst-side exit for a position's (already expanded, ratio-1) legs.

    Position legs carry the strikes actually filled at entry, so no
    re-snapping: a leg whose exact contract or needed side is missing that
    day means NO fill — the close is abandoned for the day and the
    lifecycle scan re-triggers it on the next (assumption 5).
    """
    total = 0.0
    for leg in position_legs:
        right = "C" if leg["option_type"] == "CALL" else "P"
        quote = snapshot.quotes.get((leg["expiration"], leg["strike"], right))
        if quote is None:
            return Abandoned(NO_EXPIRATION, f"{leg['expiration']} {leg['strike']} {right}: not listed")
        # Closing reverses the held direction: LONG sells (bid), SHORT buys (ask).
        price = quote.bid if leg["direction"] == "LONG" else quote.ask
        if price is None:
            side = "bid" if leg["direction"] == "LONG" else "ask"
            return Abandoned(MISSING_SIDE, f"{leg['expiration']} {leg['strike']} {right}: no {side}")
        total += price if leg["direction"] == "LONG" else -price
    return CloseFill(
        exit_value_per_share=round(total, 2),
        commission=round(COMMISSION_PER_CONTRACT * len(position_legs) * contracts, 2),
    )


def mark_value(position_legs: list[dict], premium_direction: str, snapshot: ChainSnapshot) -> float | None:
    """Mid-based mark per share, or None when ANY leg lacks a two-sided quote.

    Netting mirrors _intrinsic_settlement_value (executor.py:892-904): LONG
    adds, SHORT subtracts, and the DEBIT/CREDIT flip makes the stored value
    the position's own value (DEBIT) or its buyback cost (CREDIT). A None
    return means "keep the prior mark and count the staleness"
    (assumption 6) — a mid is never synthesized from one side.
    """
    long_val = 0.0
    short_val = 0.0
    for leg in position_legs:
        right = "C" if leg["option_type"] == "CALL" else "P"
        quote = snapshot.quotes.get((leg["expiration"], leg["strike"], right))
        if quote is None or quote.bid is None or quote.ask is None:
            return None
        mid = (quote.bid + quote.ask) / 2.0
        if leg["direction"] == "LONG":
            long_val += mid
        else:
            short_val += mid
    value = long_val - short_val if premium_direction == "DEBIT" else short_val - long_val
    return round(value, 2)

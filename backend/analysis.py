"""analysis.py — read models for the Analysis tab (#242).

The lab's output is only as good as our ability to read it. This module
computes report objects from the evidence tables (orders, fills, positions,
post-mortems) — pure reads, no mutation, honest about sample size.

Fill quality (#242): every executor order stores the mid we DECIDED on
(`decision_midpoint`) and the limit we ultimately posted; the fills ledger
holds what the market actually gave. Slippage therefore decomposes into the
ladder concession we chose (limit − mid) and what the market moved on top
(fill − limit). The headline number compares measured slippage per contract
against the ADR-0006 $5 haircut the Live Gate assumes.

Sign convention: prices are per-share and signed like the order rows
(entries: negative = credit; closes: signed cash flow, negative = paying).
Leg fills for a close carry REVERSED sides (the close SELLS the bag), so the
leg-derived net is negated for CLOSE/TP before comparing (#347). "Cost"
numbers are oriented per action so POSITIVE = worse than decided (paid more /
received less) for entries AND closes.
"""

import itertools
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.console import SLIPPAGE_HAIRCUT_PER_CONTRACT, book_summaries
from backend.models import (
    BookModel,
    ClosurePostMortemModel,
    FillModel,
    FillQualityAggregate,
    FillQualityReport,
    FillQualityRow,
    KnobPointSchema,
    KnobSweepSchema,
    LeaderboardReport,
    OrderModel,
    PositionModel,
    RegimeHitRateReport,
    RegimeHitRateRow,
)

logger = logging.getLogger(__name__)


def _net_fill_per_share(fills: list[FillModel], contracts: int) -> float | None:
    """Signed net combo price per share from per-leg fills: buys positive,
    sells negative (matching the limit-price convention where negative =
    credit). Leg quantities are scaled by contracts so ratio legs stay
    honest."""
    if not fills or contracts <= 0:
        return None
    net = 0.0
    for f in fills:
        signed = f.price if f.side == "BOT" else -f.price
        net += signed * (f.quantity / contracts)
    return net


def _aggregate(label: str, rows: list[FillQualityRow]) -> FillQualityAggregate:
    measured = [r for r in rows if r.total_slippage_per_share is not None]
    contracts = sum(r.contracts for r in measured)
    avg = (
        sum(r.total_slippage_per_share * r.contracts for r in measured) / contracts  # type: ignore[operator]
        if contracts
        else None
    )
    return FillQualityAggregate(
        label=label,
        orders=len(rows),
        contracts=sum(r.contracts for r in rows),
        avg_slippage_per_contract=round(avg * 100, 2) if avg is not None else None,
        total_commissions=round(sum(r.commissions for r in rows), 2),
    )


async def fill_quality_report(session: AsyncSession) -> FillQualityReport:
    orders = list(
        (await session.execute(select(OrderModel).where(OrderModel.status.in_(("FILLED", "PARTIAL"))))).scalars()
    )
    fills_by_order: dict[str, list[FillModel]] = {}
    for f in (await session.execute(select(FillModel))).scalars():
        fills_by_order.setdefault(f.order_id, []).append(f)

    rows: list[FillQualityRow] = []
    for o in orders:
        contracts = int((o.combo_legs or {}).get("quantity", 1))
        fills = fills_by_order.get(o.id, [])
        # PARTIAL orders (#347): the fills cover FEWER contracts than the
        # combo quantity, so per-share math against the intended size reads
        # as phantom price improvement. Latched partials are a human's
        # problem (#283) — here they stay listed but unmeasured.
        net_fill = None if o.status == "PARTIAL" else _net_fill_per_share(fills, contracts)
        action = "TP" if o.order_ref.endswith(":tp") else o.action
        # Sign convention per action (#347): closes SELL the bag, so IBKR
        # reverses every leg's side — the raw BOT→+/SLD→− sum comes out
        # opposite to the close order's signed-cash-flow limit. Negate it so
        # net_fill compares against limit/mid directly, and orient slippage
        # with the action so POSITIVE = worse holds for closes too (a credit
        # buy-back filling MORE negative than decided = paid more = worse).
        orient = 1.0 if action == "OPEN" else -1.0
        if net_fill is not None and action != "OPEN":
            net_fill = -net_fill
        rows.append(
            FillQualityRow(
                order_ref=o.order_ref,
                book_id=o.book_id,
                action=action,
                underlying=(o.combo_legs or {}).get("underlying", "?"),
                contracts=contracts,
                decision_midpoint=o.decision_midpoint,
                limit_price=o.limit_price,
                net_fill_per_share=round(net_fill, 4) if net_fill is not None else None,
                ladder_concession_per_share=round(orient * (o.limit_price - o.decision_midpoint), 4),
                market_slippage_per_share=round(orient * (net_fill - o.limit_price), 4)
                if net_fill is not None
                else None,
                total_slippage_per_share=round(orient * (net_fill - o.decision_midpoint), 4)
                if net_fill is not None
                else None,
                commissions=round(sum(f.commission for f in fills), 2),
            )
        )

    measured = [r for r in rows if r.total_slippage_per_share is not None]
    measured_contracts = sum(r.contracts for r in measured)
    avg_per_contract = (
        round(sum(r.total_slippage_per_share * r.contracts for r in measured) / measured_contracts * 100, 2)  # type: ignore[operator]
        if measured_contracts
        else None
    )

    books = sorted({r.book_id for r in rows})
    actions = ["OPEN", "CLOSE", "TP"]
    rows.sort(key=lambda r: -(r.total_slippage_per_share or float("-inf")))
    return FillQualityReport(
        generated_at=datetime.now(UTC).isoformat(),
        orders_analyzed=len(measured),
        orders_awaiting_fills=len(rows) - len(measured),
        haircut_per_contract=SLIPPAGE_HAIRCUT_PER_CONTRACT,
        avg_slippage_per_contract=avg_per_contract,
        total_commissions=round(sum(r.commissions for r in rows), 2),
        by_book=[_aggregate(b, [r for r in rows if r.book_id == b]) for b in books],
        by_action=[_aggregate(a, g) for a in actions if (g := [r for r in rows if r.action == a])],
        rows=rows,
    )


# ---------------------------------------------------------------------------
# Leaderboard + knob sweeps (#243)
# ---------------------------------------------------------------------------

# Each sweep names the books that vary ONE knob (the ADR-0010 one-knob rule)
# in presentation order, so expectancy can be read for direction, not just a
# pairwise difference. Mirrors the seeds.py matrix (#219); a test pins every
# book id here against LAB_BOOKS so a matrix change can't silently orphan it.
KNOB_SWEEPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Short-leg delta (spreads-only)", [("B23", "0.20"), ("B01", "0.30 mix baseline"), ("B24", "0.40")]),
    ("Spread width $", [("B27", "$2"), ("B01", "$3"), ("B13", "$5 (4.5% envelope confound)")]),
    ("Target DTE", [("B07", "24"), ("B01", "38"), ("B25", "52")]),
    ("Profit take %", [("B15", "25%"), ("B01", "50%"), ("B26", "75%")]),
    ("Mandatory exit DTE", [("B17", "7"), ("B01", "21")]),
    ("Engine variant on XSP", [("B01", "V0"), ("B02", "V1"), ("B03", "V2"), ("B19", "V3")]),
    ("Engine variant on SPY", [("B04", "V0"), ("B05", "V1"), ("B06", "V2"), ("B20", "V3")]),
    ("Entry gates", [("B01", "all gates"), ("B12", "no regime"), ("B16", "no IVR")]),
]

# A sweep point speaks only once it has a real sample behind it.
MIN_TRADES_PER_POINT = 5


def _sweep_verdict(points: list[KnobPointSchema]) -> str:
    """Direction of expectancy across the sweep — only claimed when every
    point has a minimum sample; anything else is 'insufficient data'."""
    if len(points) < 2 or any(
        p.expectancy_after_haircut is None or p.closed_trades < MIN_TRADES_PER_POINT for p in points
    ):
        return "insufficient data"
    values = [p.expectancy_after_haircut for p in points]
    diffs = [b - a for a, b in itertools.pairwise(values)]  # type: ignore[operator]
    if all(d >= 0 for d in diffs):
        return "monotonic ↑"
    if all(d <= 0 for d in diffs):
        return "monotonic ↓"
    return "non-monotonic"


async def leaderboard_report(session: AsyncSession, now: datetime | None = None) -> LeaderboardReport:
    summaries = await book_summaries(session, now=now)
    by_id = {s.id: s for s in summaries}
    ranked = sorted(
        summaries,
        key=lambda s: (s.expectancy_after_haircut is None, -(s.expectancy_after_haircut or 0.0), s.id),
    )

    sweeps: list[KnobSweepSchema] = []
    for dimension, spec in KNOB_SWEEPS:
        points = [
            KnobPointSchema(
                book_id=book_id,
                knob_value=knob_value,
                expectancy_after_haircut=by_id[book_id].expectancy_after_haircut,
                closed_trades=by_id[book_id].closed_trades,
            )
            for book_id, knob_value in spec
            if book_id in by_id
        ]
        sweeps.append(KnobSweepSchema(dimension=dimension, points=points, verdict=_sweep_verdict(points)))

    return LeaderboardReport(
        generated_at=datetime.now(UTC).isoformat(),
        min_trades_per_point=MIN_TRADES_PER_POINT,
        ranked=ranked,
        sweeps=sweeps,
    )


# ---------------------------------------------------------------------------
# Regime hit-rate (#244)
# ---------------------------------------------------------------------------


def _hit_rate_row(regime: str, engine_variant: str | None, pnls: list[float]) -> RegimeHitRateRow:
    wins = sum(1 for p in pnls if p > 0.01)
    return RegimeHitRateRow(
        regime=regime,
        engine_variant=engine_variant,
        closed_trades=len(pnls),
        wins=wins,
        win_rate=round(wins / len(pnls), 4) if pnls else None,
        avg_pnl=round(sum(pnls) / len(pnls), 2) if pnls else None,
        total_pnl=round(sum(pnls), 2),
    )


async def regime_hit_rate_report(session: AsyncSession) -> RegimeHitRateReport:
    """Entry-day regime vs closed outcome, overall and per engine variant.

    The regime a trade was DECIDED under is stamped into the position journal
    at entry (#254) — this is the observational complement to the B12 no-gate
    control arm: B12 asks "does gating help?", this asks "when the gate said
    yes, which regimes actually paid?"."""
    pms = list((await session.execute(select(ClosurePostMortemModel))).scalars())
    positions = {p.id: p for p in (await session.execute(select(PositionModel))).scalars()}
    books = {b.id: b for b in (await session.execute(select(BookModel))).scalars()}

    samples: list[tuple[str, str, float]] = []  # (regime, engine_variant, pnl)
    for pm in pms:
        pos = positions.get(pm.position_id)
        if pos is None:
            continue
        regime = (pos.journal or {}).get("entry_regime") or "UNKNOWN"
        book = books.get(pos.book_id)
        engine = (book.config or {}).get("engine_variant", "—") if book is not None else "—"
        samples.append((regime, engine, pm.realized_pnl))

    regimes = sorted({s[0] for s in samples})
    engines = sorted({s[1] for s in samples})
    by_regime = [_hit_rate_row(r, None, [p for reg, _, p in samples if reg == r]) for r in regimes]
    by_engine = [
        _hit_rate_row(r, e, pnls)
        for e in engines
        for r in regimes
        if (pnls := [p for reg, eng, p in samples if reg == r and eng == e])
    ]
    return RegimeHitRateReport(
        generated_at=datetime.now(UTC).isoformat(),
        closed_trades=len(samples),
        by_regime=by_regime,
        by_engine_regime=by_engine,
    )

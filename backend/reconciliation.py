"""reconciliation.py — broker-vs-books drift detection (design §4.4, #66).

The first step of every executor run, ahead of Layer A: prove the broker
agrees with the books before anything else is allowed to happen.

Principles (spec/data-models.md, ADR-0006):
- The DB is truth for attribution; the broker is truth for quantities.
- NEVER auto-adjust book ledgers to match the broker — silent adjustment
  corrupts the Live Gate evidence. Drift latches a global HALT_ENTRIES
  (console-only resume, ADR-0008) and waits for a human.
- Missed fills whose orderRef parses are backfilled append-only, deduped on
  execId; executions with unknown refs are surfaced, not guessed at.

Comparison key: the OCC symbol (canonical across the codebase), computed on
both sides — from broker option contracts and from stored position legs.
Same-direction sharing across books sums before comparing.
"""

import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.book_gates import credit_book_cash
from backend.broker import FillInfo, LegPosition, OpenOrderInfo
from backend.market_data import format_occ_symbol, parse_occ_symbol
from backend.models import FillModel, OrderModel, PositionModel, ReconciliationRunModel
from backend.trading_control import GLOBAL_SCOPE, HALT_ENTRIES, set_control

logger = logging.getLogger(__name__)

ORPHAN = "ORPHAN"
EXTERNAL_CLOSE = "EXTERNAL_CLOSE"
PARTIAL_DRIFT = "PARTIAL_DRIFT"
GHOST_ORDER = "GHOST_ORDER"


@dataclass(frozen=True)
class BrokerSnapshot:
    """Everything reconciliation needs, as plain data — the pipeline builds
    this from a BrokerSession (positions/executions/open_orders); tests build
    it directly."""

    positions: tuple[LegPosition, ...]
    executions: tuple[FillInfo, ...] = ()
    open_orders: tuple[OpenOrderInfo, ...] = ()


@dataclass(frozen=True)
class DriftItem:
    kind: str  # ORPHAN | EXTERNAL_CLOSE | PARTIAL_DRIFT
    key: str  # OCC symbol, or the broker symbol for non-option orphans
    sec_type: str
    broker_qty: float
    expected_qty: float
    unexpected_instrument: bool = False  # No-Stock Mandate violation (P1)


@dataclass(frozen=True)
class ReconciliationResult:
    run_id: int
    result: str  # CLEAN | DRIFT
    drifts: tuple[DriftItem, ...]
    fills_backfilled: int
    unknown_ref_exec_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def clean(self) -> bool:
        return self.result == "CLEAN"


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _backfill_missed_fills(session: AsyncSession, executions: tuple[FillInfo, ...]) -> tuple[int, list[str]]:
    """Insert executions missing from the fills ledger (dedupe on execId).

    Only refs that resolve to one of our orders are ingested — a ':tp'
    suffix maps to its parent order. Unknown refs are returned for the
    drift report, never guessed into a book.
    """
    if not executions:
        return 0, []
    existing = set((await session.execute(select(FillModel.exec_id))).scalars().all())
    backfilled = 0
    unknown: list[str] = []
    for ex in executions:
        if ex.exec_id in existing:
            continue
        ref = ex.order_ref
        base_ref = ref.removesuffix(":tp") if ref else ""
        order = None
        if ref:
            order = (await session.execute(select(OrderModel).filter_by(order_ref=ref))).scalar_one_or_none()
            if order is None and base_ref != ref:
                order = (await session.execute(select(OrderModel).filter_by(order_ref=base_ref))).scalar_one_or_none()
        if order is None:
            unknown.append(ex.exec_id)
            continue
        session.add(
            FillModel(
                exec_id=ex.exec_id,
                order_id=order.id,
                book_id=order.book_id,
                con_id=ex.con_id,
                side=ex.side,
                quantity=ex.quantity,
                price=ex.price,
                commission=ex.commission or 0.0,
                fill_time=_now(),
                raw={"order_ref": ref, "source": "reconciliation_backfill"},
            )
        )
        # Commissions are real money (#276, audit H1): debit the book's cash
        # here, where the exec-id dedupe guarantees exactly-once — every
        # other consumer was ignoring the captured number entirely.
        if ex.commission:
            await credit_book_cash(session, order.book_id, -ex.commission)
        existing.add(ex.exec_id)
        backfilled += 1
    return backfilled, unknown


async def _expected_leg_quantities(session: AsyncSession, today: str | None = None) -> dict[str, float]:
    """Sum open-position leg quantities across ALL books, keyed by OCC symbol.

    Same-direction sharing across books is legitimate (the cross-book netting
    gate blocks opposite-direction sharing before it ever reaches the broker).

    Legs already expired as of *today* (ISO market date; the run is always
    after the close) are excluded — IB purges expired contracts on its own
    overnight schedule, so an expired leg must be reconciliation-neutral on
    both sides or every expiry cycle ends in a false drift halt (#261)."""
    open_positions = (await session.execute(select(PositionModel).filter_by(status="OPEN"))).scalars().all()
    expected: dict[str, float] = {}
    for pos in open_positions:
        for leg in pos.legs:
            if today is not None and leg["expiration"] <= today:
                continue
            occ = format_occ_symbol(
                underlying=pos.underlying,
                expiration=leg["expiration"],
                option_type=leg["option_type"],
                strike=leg["strike"],
            )
            signed = pos.contracts * (1.0 if leg["direction"] == "LONG" else -1.0)
            expected[occ] = expected.get(occ, 0.0) + signed
    return {k: v for k, v in expected.items() if v}


def _classify_drift(
    broker_positions: tuple[LegPosition, ...], expected: dict[str, float], today: str | None = None
) -> list[DriftItem]:
    today_compact = today.replace("-", "") if today else None  # OCC dates are YYYYMMDD
    drifts: list[DriftItem] = []
    broker_by_key: dict[str, LegPosition] = {}
    for p in broker_positions:
        # The mirror of the expected-side exclusion: an expired option IB has
        # not yet purged must not read as an orphan (#261). Unparseable OCC
        # symbols stay in — fail closed, toward drift.
        if p.sec_type == "OPT" and p.occ_symbol and today_compact:
            parsed = parse_occ_symbol(p.occ_symbol)
            if parsed is not None and parsed["expiration"] <= today_compact:
                continue
        if p.sec_type != "OPT" or p.occ_symbol is None:
            # Any non-option position is an orphan AND a No-Stock P1.
            drifts.append(
                DriftItem(
                    kind=ORPHAN,
                    key=p.symbol,
                    sec_type=p.sec_type,
                    broker_qty=p.position,
                    expected_qty=0.0,
                    unexpected_instrument=True,
                )
            )
            continue
        broker_by_key[p.occ_symbol] = p

    for occ, p in broker_by_key.items():
        if occ not in expected:
            drifts.append(DriftItem(kind=ORPHAN, key=occ, sec_type="OPT", broker_qty=p.position, expected_qty=0.0))
        elif p.position != expected[occ]:
            drifts.append(
                DriftItem(
                    kind=PARTIAL_DRIFT, key=occ, sec_type="OPT", broker_qty=p.position, expected_qty=expected[occ]
                )
            )
    for occ, qty in expected.items():
        if occ not in broker_by_key:
            drifts.append(DriftItem(kind=EXTERNAL_CLOSE, key=occ, sec_type="OPT", broker_qty=0.0, expected_qty=qty))
    return drifts


async def _classify_ghost_orders(session: AsyncSession, open_orders: tuple[OpenOrderInfo, ...]) -> list[DriftItem]:
    """Broker open orders wearing our `basis:` tag with no non-terminal DB
    row behind them (#408). After a DB restore (or any drift), a prior DB
    generation's orders rest at IBKR and can fill with no row to receive the
    fill — nothing else in the pipeline looks at them (the sync only queries
    refs the DB already knows). Detection only; cancelling is a human act.
    """
    if not open_orders:
        return []
    live_refs = set(
        (
            await session.execute(
                select(OrderModel.order_ref).filter(OrderModel.status.in_(("STAGED", "SUBMITTED", "PARTIAL")))
            )
        )
        .scalars()
        .all()
    )
    ghosts: list[DriftItem] = []
    for o in open_orders:
        ref = o.order_ref or ""
        if not ref.startswith("basis:"):
            continue  # not ours — a human's own manual order is their business
        # A GTC profit-taker's ':tp' ref rides its parent's row.
        if ref in live_refs or ref.removesuffix(":tp") in live_refs:
            continue
        ghosts.append(DriftItem(kind=GHOST_ORDER, key=ref, sec_type="ORDER", broker_qty=0.0, expected_qty=0.0))
    return ghosts


async def run_reconciliation(
    session: AsyncSession, snapshot: BrokerSnapshot, today: str | None = None
) -> ReconciliationResult:
    """Snapshot → backfill → compare → classify → (on drift) latch global halt.

    Never mutates positions or book ledgers — resolution is a human act
    (EXTERNAL_CLOSE post-mortems record broker settlement values,
    domain-rules.md)."""
    backfilled, unknown = await _backfill_missed_fills(session, snapshot.executions)
    expected = await _expected_leg_quantities(session, today)
    drifts = _classify_drift(snapshot.positions, expected, today)
    drifts.extend(await _classify_ghost_orders(session, snapshot.open_orders))
    result = "CLEAN" if not drifts else "DRIFT"

    run_row = ReconciliationRunModel(
        run_at=_now(),
        broker_snapshot={
            "positions": [asdict(p) for p in snapshot.positions],
            "open_orders": [asdict(o) for o in snapshot.open_orders],
            "unknown_ref_exec_ids": unknown,
        },
        books_expected=expected,
        result=result,
        drift_details=[asdict(d) for d in drifts] if drifts else None,
    )
    session.add(run_row)
    await session.commit()

    if drifts:
        stock_orphans = [d for d in drifts if d.unexpected_instrument]
        ghosts = [d for d in drifts if d.kind == GHOST_ORDER]
        reason_bits = [f"RECONCILIATION_DRIFT: {len(drifts)} discrepancies (run {run_row.id})"]
        if stock_orphans:
            reason_bits.append(f"UNEXPECTED_INSTRUMENT: {', '.join(d.key for d in stock_orphans)} — No-Stock P1")
        if ghosts:
            reason_bits.append(
                f"GHOST_ORDER: {', '.join(d.key for d in ghosts)} — live at the broker with no DB row; "
                "cancel at the broker before they fill"
            )
        await set_control(session, GLOBAL_SCOPE, HALT_ENTRIES, reason="; ".join(reason_bits), actor="reconciliation")
        logger.error("Reconciliation DRIFT — global entries halted: %s", "; ".join(reason_bits))
    return ReconciliationResult(
        run_id=run_row.id,
        result=result,
        drifts=tuple(drifts),
        fills_backfilled=backfilled,
        unknown_ref_exec_ids=tuple(unknown),
    )


async def resolve_reconciliation(session: AsyncSession, run_id: int, resolution: str) -> None:
    """Record the human resolution on a drift run. Resuming entries is a
    separate, console-only act (ADR-0008) — resolution never auto-resumes."""
    run = await session.get(ReconciliationRunModel, run_id)
    if run is None:
        raise ValueError(f"No reconciliation run {run_id}")
    run.resolved_at = _now()
    run.resolution = resolution
    await session.commit()

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
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.book_gates import credit_book_cash
from backend.broker import FillInfo, LegPosition, OpenOrderInfo
from backend.market_data import format_occ_symbol, parse_occ_symbol
from backend.models import AuditEventModel, FillModel, OrderModel, PositionModel, ReconciliationRunModel
from backend.states import ORDER_PENDING_STATUSES, POSITION_OPEN_STATUS
from backend.trading_control import GLOBAL_SCOPE, HALT_ENTRIES, set_control

logger = logging.getLogger(__name__)

ORPHAN = "ORPHAN"
EXTERNAL_CLOSE = "EXTERNAL_CLOSE"
PARTIAL_DRIFT = "PARTIAL_DRIFT"
GHOST_ORDER = "GHOST_ORDER"
# #715: labeled sub-classifications of EXTERNAL_CLOSE/PARTIAL_DRIFT on a
# SHORT option leg — same fail-closed halt, same operator resolution
# requirement, just named instead of anonymous.
ASSIGNMENT_SUSPECTED = "ASSIGNMENT_SUSPECTED"
CASH_SETTLEMENT_SUSPECTED = "CASH_SETTLEMENT_SUSPECTED"

# European-style, cash-settled index products (#130, assignment_defense.py):
# early exercise/assignment is not contractually possible — a short leg here
# can only leave via ordinary expiry settlement (already reconciliation-
# neutral, #261's expired-leg exclusion) or a genuine broker-side anomaly,
# never assignment. A drift on one of these still gets a distinct label, but
# CASH_SETTLEMENT_SUSPECTED, never ASSIGNMENT_SUSPECTED — mislabeling a
# European instrument as "assigned" would send the operator looking for
# something that cannot happen. SPY (and any other American-style
# underlying not in this set) uses ASSIGNMENT_SUSPECTED instead, gated on
# actually seeing a corroborating stock position at the broker.
CASH_SETTLED_UNDERLYINGS = frozenset({"XSP"})

# #473 (Audit II R3, fix-attacker ghost F3): an order the operator (or the
# sync) already cancelled sits briefly in one of these statuses at IBKR
# before it fully clears the open-orders feed. Flagging it as a ghost and
# latching the global halt tells the operator to do what they already did.
CANCEL_IN_FLIGHT_ORDER_STATUSES = frozenset({"PendingCancel", "ApiCancelled"})


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
    kind: str  # ORPHAN | EXTERNAL_CLOSE | PARTIAL_DRIFT | ASSIGNMENT_SUSPECTED | CASH_SETTLEMENT_SUSPECTED
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


async def _audit(session: AsyncSession, event_type: str, book_id: str | None, payload: dict) -> None:
    session.add(
        AuditEventModel(run_at=_now(), book_id=book_id, event_type=event_type, actor="reconciliation", payload=payload)
    )


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
                exec_time=ex.exec_time,
                raw={"order_ref": ref, "source": "reconciliation_backfill"},
            )
        )
        # Commissions are real money (#276, audit H1): debit the book's cash
        # here, where the exec-id dedupe guarantees exactly-once — every
        # other consumer was ignoring the captured number entirely. #685:
        # every other cash mutator (executor fills, resolution, the console
        # close endpoint per #468) pairs its credit_book_cash call with an
        # audit_events row in the same transaction — this was the one
        # remaining cash move invisible to the audit trail.
        if ex.commission:
            await credit_book_cash(session, order.book_id, -ex.commission)
            await _audit(
                session,
                "COMMISSION_DEBITED",
                order.book_id,
                {
                    "exec_id": ex.exec_id,
                    "order_ref": ref,
                    "commission": ex.commission,
                    "source": "reconciliation_backfill",
                },
            )
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
    open_positions = (
        (await session.execute(select(PositionModel).filter_by(status=POSITION_OPEN_STATUS))).scalars().all()
    )
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


def _classify_assignment_or_settlement(
    drifts: list[DriftItem], broker_positions: tuple[LegPosition, ...]
) -> list[DriftItem]:
    """#715 (panel point, Gemini): sub-classify a bare EXTERNAL_CLOSE/
    PARTIAL_DRIFT on a SHORT option leg into a labeled event when the
    pattern matches early assignment or exercise/settlement, instead of
    leaving the operator to reverse-engineer a "6:45 mystery" from an
    anonymous drift. Detection-only: every fail-closed property (global
    halt, no auto-resolve, DriftItem still counted toward DRIFT) is
    unchanged — this only renames what the halt reason and digest say.

    Only a leg that got LESS short (broker_qty closer to flat than
    expected_qty, with expected_qty itself negative) is a candidate — the
    only direction consistent with the short side being assigned or
    exercised away. A leg that grew MORE short, or a LONG leg's drift, has
    no assignment story and stays a bare EXTERNAL_CLOSE/PARTIAL_DRIFT.

    XSP (and any other CASH_SETTLED_UNDERLYINGS member) is European-style —
    early assignment is not contractually possible — so it is always
    CASH_SETTLEMENT_SUSPECTED, never ASSIGNMENT_SUSPECTED, regardless of
    what else is in the broker snapshot. Everything else (SPY) only earns
    ASSIGNMENT_SUSPECTED when a stock position in the assignment-consistent
    direction (short PUT assigned -> LONG stock; short CALL assigned ->
    SHORT stock) is ACTUALLY present at the broker — a bare short-leg
    disappearance with no corroborating stock stays anonymous rather than
    over-claiming a specific cause with no evidence for it.
    """
    stock_by_symbol: dict[str, float] = {p.symbol: p.position for p in broker_positions if p.sec_type != "OPT"}
    reclassified: list[DriftItem] = []
    for d in drifts:
        if d.kind not in (EXTERNAL_CLOSE, PARTIAL_DRIFT) or d.sec_type != "OPT":
            reclassified.append(d)
            continue
        got_less_short = d.expected_qty < 0 and d.broker_qty > d.expected_qty
        parsed = parse_occ_symbol(d.key)
        if not got_less_short or parsed is None:
            reclassified.append(d)
            continue
        underlying = parsed["underlying"]
        if underlying in CASH_SETTLED_UNDERLYINGS:
            reclassified.append(replace(d, kind=CASH_SETTLEMENT_SUSPECTED))
            continue
        stock_qty = stock_by_symbol.get(underlying)
        assignment_consistent = stock_qty is not None and (
            (parsed["right"] == "P" and stock_qty > 0)  # short put assigned -> long stock
            or (parsed["right"] == "C" and stock_qty < 0)  # short call assigned -> short stock
        )
        reclassified.append(replace(d, kind=ASSIGNMENT_SUSPECTED) if assignment_consistent else d)
    return reclassified


async def _classify_ghost_orders(session: AsyncSession, open_orders: tuple[OpenOrderInfo, ...]) -> list[DriftItem]:
    """Broker open orders wearing our `basis:` tag with no non-terminal DB
    row behind them (#408). After a DB restore (or any drift), a prior DB
    generation's orders rest at IBKR and can fill with no row to receive the
    fill — nothing else in the pipeline looks at them (the sync only queries
    refs the DB already knows). Detection only; cancelling is a human act.

    #473 (Audit II R3): a ref must match its OWN row exactly — a GTC
    profit-taker has carried its own row since #409 (place_spread writes it
    before placeOrder), so a resting `:tp` order whose own row is terminal
    (e.g. the parent latched PARTIAL and cancelled it, but the cancel hasn't
    reached the broker yet) IS the ghost the parent's still-live row used to
    mask — the old `ref.removesuffix(":tp") in live_refs` fallback exempted
    exactly that case. Cancel-in-flight broker statuses (PendingCancel,
    ApiCancelled) are excluded separately: an order already cancelled by the
    operator or the sync shouldn't halt the book telling them to do what
    they already did.
    """
    if not open_orders:
        return []
    live_refs = set(
        (await session.execute(select(OrderModel.order_ref).filter(OrderModel.status.in_(ORDER_PENDING_STATUSES))))
        .scalars()
        .all()
    )
    ghosts: list[DriftItem] = []
    for o in open_orders:
        ref = o.order_ref or ""
        if not ref.startswith("basis:"):
            continue  # not ours — a human's own manual order is their business
        if ref in live_refs:
            continue
        if o.status in CANCEL_IN_FLIGHT_ORDER_STATUSES:
            # #473: the operator (or the sync) already cancelled this order —
            # it just hasn't fully cleared IBKR's feed yet. Not a ghost.
            continue
        ghosts.append(DriftItem(kind=GHOST_ORDER, key=ref, sec_type="ORDER", broker_qty=0.0, expected_qty=0.0))
    return ghosts


@dataclass(frozen=True)
class BookComparison:
    """The pure comparison half of a reconciliation run (#827)."""

    expected: dict[str, float]  # OCC symbol -> signed expected leg quantity
    drifts: tuple[DriftItem, ...]


async def compare_books(session: AsyncSession, snapshot: BrokerSnapshot, today: str | None = None) -> BookComparison:
    """Pure broker-vs-books comparison: reads positions and orders, writes
    NOTHING — no reconciliation_runs row, no halt latch, no audit event.

    Split out of run_reconciliation for the afternoon preflight (#827),
    which needs the executor's exact drift verdict hours early but is
    report-only by charter: the evening run alone persists snapshots and
    latches halts. run_reconciliation layers backfill + persistence + the
    halt on top of this same comparison, so the two can never disagree
    about what counts as drift."""
    expected = await _expected_leg_quantities(session, today)
    drifts = _classify_drift(snapshot.positions, expected, today)
    drifts = _classify_assignment_or_settlement(drifts, snapshot.positions)
    drifts.extend(await _classify_ghost_orders(session, snapshot.open_orders))
    return BookComparison(expected=expected, drifts=tuple(drifts))


async def run_reconciliation(
    session: AsyncSession, snapshot: BrokerSnapshot, today: str | None = None
) -> ReconciliationResult:
    """Snapshot → backfill → compare → classify → (on drift) latch global halt.

    Never mutates positions or book ledgers — resolution is a human act
    (EXTERNAL_CLOSE post-mortems record broker settlement values,
    domain-rules.md)."""
    backfilled, unknown = await _backfill_missed_fills(session, snapshot.executions)
    comparison = await compare_books(session, snapshot, today)
    expected = comparison.expected
    drifts = list(comparison.drifts)
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
        assignments = [d for d in drifts if d.kind == ASSIGNMENT_SUSPECTED]
        settlements = [d for d in drifts if d.kind == CASH_SETTLEMENT_SUSPECTED]
        reason_bits = [f"RECONCILIATION_DRIFT: {len(drifts)} discrepancies (run {run_row.id})"]
        if assignments:
            reason_bits.append(
                f"ASSIGNMENT_SUSPECTED: {', '.join(d.key for d in assignments)} — a stock position consistent with "
                "early assignment is at the broker; verify at the broker before resolving"
            )
        if settlements:
            reason_bits.append(
                f"CASH_SETTLEMENT_SUSPECTED: {', '.join(d.key for d in settlements)} — cash-settled underlying, "
                "no early assignment possible; verify exercise/settlement at the broker before resolving"
            )
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


async def latest_reconciliation_run(session: AsyncSession) -> ReconciliationRunModel | None:
    """The run every console surface should treat as "the current recon
    state" (#474, #478): the newest UNRESOLVED drift run if one exists,
    else the newest run overall. A halt from an old DRIFT run persists
    until a human resolves it (ADR-0008), so a merely more recent CLEAN
    snapshot must never shadow it — every reader of "the latest
    reconciliation" (the /api/reconciliation/latest endpoint AND the
    executor-status strip badge) shares this one query so they can't drift
    apart again."""
    run = (
        await session.execute(
            select(ReconciliationRunModel)
            .filter(ReconciliationRunModel.result == "DRIFT", ReconciliationRunModel.resolved_at.is_(None))
            .order_by(ReconciliationRunModel.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if run is not None:
        return run
    return (
        await session.execute(select(ReconciliationRunModel).order_by(ReconciliationRunModel.id.desc()).limit(1))
    ).scalar_one_or_none()

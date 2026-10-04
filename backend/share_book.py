"""share_book.py — executor plumbing for share books (#1054).

The monthly ETF trend book's rules are pure (backend/etf_trend.py); this
module feeds them from the database and carries their orders through the
broker, with the same disciplines the options path keeps:

- Intent first: a share order's row is written STAGED and committed BEFORE
  placeOrder, and a control-state read happens immediately before each
  submission — the entry choke point (assert_entries_allowed) for a
  rebalance order, the flatten's own still-flattening read for a flatten
  sell (#1074).
- The evening run is the only automatic mutator. Orders are placed only by
  the executor's nightly run: the rebalance only on a month's last trading
  day, the flatten sells (#1074) on any evening its scope is in
  FLATTEN_REQUESTED. Fills are booked by its order-state sync, which runs
  before reconciliation, so the books already hold a filled order's shares
  by the time the broker's share count is compared against them.
- share_holdings has two writers: the sync, and a human through the audited
  share-drift resolution (backend/resolution.py, #1074) — which settles a
  held order through this module's own book_fills. The sync books exactly
  the executions recorded against the order — a partial fill is booked as
  what filled, never at the ordered size, and a FILLED verdict whose
  executions cannot be seen is held, never booked from the limit or the
  close. A held order leaves the books short of the broker, so
  reconciliation's SHARE_DRIFT halts the lab loudly until a human looks.
- Commissions are debited here, once, from the executions' commission
  reports. reconciliation._backfill_missed_fills recognizes share refs and
  leaves them to this module, so nothing debits twice.
- Fail closed on data. A held symbol with no close today, a holding that is
  not a whole number of shares, a missing cash-leg close, or a previous
  month's order still pending: the month is skipped and the digest says why.
  A menu asset missing history is simply not trending (its slot goes to the
  cash leg) — the one data gap the rules themselves define.
"""

import logging
import math
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.book_gates import EtfTrendConfig, credit_book_cash, resolve_book_config
from backend.broker import BrokerError, FillInfo, PlacedOrder, ReconcileReport, RefState
from backend.etf_trend import (
    MISSING_HISTORY,
    NOT_TRENDING,
    TRENDING,
    ShareOrderIntent,
    TrendReading,
    buy_limit,
    is_signal_day,
    last_signal_day_on_or_before,
    next_signal_day_after,
    rebalance_orders,
    sell_limit,
    target_shares,
    trend_reading,
)
from backend.models import (
    AuditEventModel,
    BookModel,
    BookMtmHistoryModel,
    IndexHistoryModel,
    ShareHoldingModel,
    ShareHoldingSchema,
    ShareOrderModel,
    TradingControlModel,
)
from backend.stage1 import stake_baseline, stake_window
from backend.states import (
    BOOK_ACTIVE_STATUS,
    SHARE_ORDER_PENDING_STATUSES,
    SHARE_ORDER_PURPOSE_FLATTEN,
    SHARE_ORDER_PURPOSE_REBALANCE,
    SHARE_ORDER_TERMINAL_STATUSES,
)
from backend.trading_control import (
    FLATTEN_REQUESTED,
    GLOBAL_SCOPE,
    TradingHaltedError,
    assert_entries_allowed,
    get_control_state,
)

logger = logging.getLogger(__name__)

SHARE_REF_SUFFIX = "share"
# Float noise only: every share order is whole shares, so any real difference
# between ordered and executed is at least one share.
_QTY_TOLERANCE = 1e-6

# Audit event types written here. Module constants, not states.py members:
# states.py is the vocabulary for ORM status values in query predicates, and
# these are event names (the executor's own ORDER_DAY_EXPIRED_EVENT precedent).
ETF_TREND_SIGNAL = "ETF_TREND_SIGNAL"
ETF_TREND_SKIPPED = "ETF_TREND_SKIPPED"
SHARE_ORDER_SUBMITTED = "SHARE_ORDER_SUBMITTED"
SHARE_ORDER_REJECTED = "SHARE_ORDER_REJECTED"
SHARE_ORDER_EXPIRED = "SHARE_ORDER_EXPIRED"
SHARE_ORDER_HELD = "SHARE_ORDER_HELD"
SHARE_FILL_BOOKED = "SHARE_FILL_BOOKED"
SHARE_WOULD_HAVE_TRADED = "SHARE_WOULD_HAVE_TRADED"
ETF_TREND_STAKE_UNSIZED = "ETF_TREND_STAKE_UNSIZED"
SHARE_FLATTEN_SUBMITTED = "SHARE_FLATTEN_SUBMITTED"
SHARE_FLATTEN_SKIPPED = "SHARE_FLATTEN_SKIPPED"
SHARE_FLATTEN_REJECTED = "SHARE_FLATTEN_REJECTED"


class ShareOrderBroker(Protocol):
    """The one broker call the rebalance needs (BrokerSession satisfies it)."""

    def place_share_order(self, symbol: str, side: str, quantity: int, limit_price: float, ref: str) -> PlacedOrder: ...


def share_order_ref(book_id: str, order_id: str) -> str:
    """`basis:{book}:{id}:share` — the `basis:` tag puts it under the
    ghost-order scan and the Flex audit; the suffix keeps it out of
    executor._position_id_from_ghost_ref, which only maps `...:open`."""
    return f"basis:{book_id}:{order_id}:{SHARE_REF_SUFFIX}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _audit(
    session: AsyncSession, event_type: str, book_id: str | None, payload: dict, actor: str = "executor"
) -> None:
    session.add(AuditEventModel(run_at=_now(), book_id=book_id, event_type=event_type, actor=actor, payload=payload))


# ---------------------------------------------------------------------------
# Readers shared with reconciliation, preflight and the midday pass
# ---------------------------------------------------------------------------


async def pending_share_orders(session: AsyncSession) -> list[ShareOrderModel]:
    """Every share order still awaiting a broker verdict, all books."""
    rows = await session.execute(
        select(ShareOrderModel).filter(ShareOrderModel.status.in_(SHARE_ORDER_PENDING_STATUSES))
    )
    return list(rows.scalars().all())


async def pending_share_deltas(session: AsyncSession) -> dict[str, float]:
    """Per symbol, the net signed quantity of every pending share order —
    the most the broker's share count can legitimately have moved away from
    the books before tonight's sync books it (the sync-pending carve-out,
    reconciliation.drift_is_sync_pending)."""
    deltas: dict[str, float] = {}
    for order in await pending_share_orders(session):
        signed = order.quantity if order.side == "BUY" else -order.quantity
        deltas[order.symbol] = deltas.get(order.symbol, 0.0) + signed
    return {k: v for k, v in deltas.items() if v}


async def _closes_by_symbol(session: AsyncSession, symbols: Iterable[str]) -> dict[str, dict[str, float]]:
    wanted = tuple(sorted(set(symbols)))
    if not wanted:
        return {}
    rows = (
        (await session.execute(select(IndexHistoryModel).filter(IndexHistoryModel.symbol.in_(wanted)))).scalars().all()
    )
    out: dict[str, dict[str, float]] = {s: {} for s in wanted}
    for row in rows:
        out[row.symbol][row.date] = row.close
    return out


async def _book_holdings(session: AsyncSession, book_id: str) -> dict[str, float]:
    rows = (await session.execute(select(ShareHoldingModel).filter_by(book_id=book_id))).scalars().all()
    return {row.symbol: row.quantity for row in rows if abs(row.quantity) > _QTY_TOLERANCE}


async def book_share_value(session: AsyncSession, book_id: str, mark_date: str) -> float | None:
    """Marked value of a book's share holdings on *mark_date*: quantity times
    that exact date's index_history close. None when any held symbol has no
    close on that date — the equity curve takes no point rather than a
    plausible wrong one. 0.0 for a book holding nothing (every options book)."""
    holdings = await _book_holdings(session, book_id)
    if not holdings:
        return 0.0
    closes = await _closes_by_symbol(session, holdings)
    total = 0.0
    for symbol, quantity in holdings.items():
        close = closes.get(symbol, {}).get(mark_date)
        if close is None:
            return None
        total += quantity * close
    return round(total, 2)


async def share_holdings_view(session: AsyncSession, book_id: str, today: str) -> list[ShareHoldingSchema]:
    """The console's holdings rows: each holding at its latest close on or
    before *today* (display only — never an input to a decision)."""
    holdings = await _book_holdings(session, book_id)
    closes = await _closes_by_symbol(session, holdings)
    view: list[ShareHoldingSchema] = []
    for symbol in sorted(holdings):
        dated = [d for d in closes.get(symbol, {}) if d <= today]
        mark_date = max(dated) if dated else None
        mark = closes[symbol][mark_date] if mark_date is not None else None
        view.append(
            ShareHoldingSchema(
                symbol=symbol,
                quantity=holdings[symbol],
                mark=mark,
                mark_date=mark_date,
                value=round(holdings[symbol] * mark, 2) if mark is not None else None,
            )
        )
    return view


# ---------------------------------------------------------------------------
# The order-state sync (runs inside executor._sync_order_states)
# ---------------------------------------------------------------------------


def _record_executions(order: ShareOrderModel, executions: Iterable[FillInfo]) -> None:
    """Append this order's not-yet-recorded executions to its `fills`."""
    fills = list(order.fills or [])
    seen = {f["exec_id"] for f in fills}
    for ex in executions:
        if ex.order_ref != order.order_ref or ex.exec_id in seen:
            continue
        fills.append(
            {
                "exec_id": ex.exec_id,
                "quantity": abs(ex.quantity),
                "price": ex.price,
                "commission": ex.commission or 0.0,
                "exec_time": ex.exec_time,
            }
        )
        seen.add(ex.exec_id)
    order.fills = fills


def _filled_quantity(order: ShareOrderModel) -> float:
    return sum(f["quantity"] for f in order.fills or [])


async def _stamp(session: AsyncSession, order: ShareOrderModel, status: str, **values: object) -> bool:
    """Conditional status UPDATE (the #466 discipline): only a row still
    pending moves. False means something else terminalized it first, and
    the caller skips every side effect."""
    result = await session.execute(
        update(ShareOrderModel)
        .where(ShareOrderModel.id == order.id, ShareOrderModel.status.in_(SHARE_ORDER_PENDING_STATUSES))
        .values(status=status, **values)
    )
    if result.rowcount == 0:
        return False
    order.status = status
    for key, value in values.items():
        setattr(order, key, value)
    return True


async def terminalize_unfilled(session: AsyncSession, order: ShareOrderModel) -> bool:
    """Close a pending order that executed nothing, CANCELLED, through the
    same conditional stamp as the sync (#466). False when it lost a race."""
    return await _stamp(session, order, "CANCELLED", completed_at=_now(), fills=list(order.fills or []))


async def book_fills(
    session: AsyncSession, order: ShareOrderModel, final_status: str, actor: str = "executor"
) -> str | None:
    """Terminalize *order* and book exactly its recorded executions into
    share_holdings and book cash. Returns a digest note, or None when the
    stamp lost a race (nothing booked).

    Two callers, one arithmetic: the evening sync (actor=executor), and the
    console's held-order settlement (resolution.settle_share_order,
    actor=resolution, #1074), which first appends the executions a human
    read off the statement. Neither ever books from the limit or the close."""
    fills = list(order.fills or [])
    quantity = sum(f["quantity"] for f in fills)
    notional = sum(f["quantity"] * f["price"] for f in fills)
    commission = sum(f["commission"] for f in fills)
    avg_price = notional / quantity if quantity > 0 else None
    if not await _stamp(
        session,
        order,
        final_status,
        completed_at=_now(),
        fills=fills,
        filled_quantity=quantity,
        avg_fill_price=avg_price,
        commission=commission,
    ):
        await _audit(
            session,
            SHARE_ORDER_HELD,
            order.book_id,
            {"order_ref": order.order_ref, "reason": "concurrent write"},
            actor,
        )
        return None
    signed = quantity if order.side == "BUY" else -quantity
    holding = await session.get(ShareHoldingModel, (order.book_id, order.symbol))
    if holding is None:
        holding = ShareHoldingModel(book_id=order.book_id, symbol=order.symbol, quantity=0.0, updated_at=_now())
        session.add(holding)
    holding.quantity = holding.quantity + signed
    holding.updated_at = _now()
    cash_delta = -(signed * (avg_price or 0.0)) - commission
    balance = await credit_book_cash(session, order.book_id, cash_delta)
    await _audit(
        session,
        SHARE_FILL_BOOKED,
        order.book_id,
        {
            "order_ref": order.order_ref,
            "symbol": order.symbol,
            "side": order.side,
            "ordered_quantity": order.quantity,
            "filled_quantity": quantity,
            "avg_fill_price": avg_price,
            "commission": commission,
            "cash_delta": round(cash_delta, 2),
            "holding_after": holding.quantity,
            "final_status": final_status,
            "purpose": order.purpose,
        },
        actor,
    )
    note = f"{order.book_id} {order.side} {quantity:g} {order.symbol} @ {avg_price or 0.0:.2f} booked"
    if quantity < order.quantity - _QTY_TOLERANCE:
        note = f"{order.book_id} {order.side} {order.symbol}: {quantity:g} of {order.quantity} filled, booked as filled"
    if balance is not None and balance < 0:
        note += f" — ⚠ {order.book_id} cash is now ${balance:,.2f} (a buy filled without its funding sell)"
    return note


async def sync_share_orders(
    session: AsyncSession,
    pending: list[ShareOrderModel],
    report: ReconcileReport,
    executions: tuple[FillInfo, ...],
    restore_gap_trading_days: int | None,
) -> list[str]:
    """Move every pending share order to its broker verdict. Returns digest
    notes. Mirrors the option sync's arms with the share book's simpler
    truth — no combo legs, no PARTIAL latch — and the same restore-gap hold
    (#542/#650): an UNKNOWN verdict with no prior reconciliation, or after a
    gap of more than one trading day, proves nothing and is held."""
    notes: list[str] = []
    for order in pending:
        _record_executions(order, executions)
        state = report.state(order.order_ref)
        filled = _filled_quantity(order)
        if state is RefState.FILLED:
            if abs(filled - order.quantity) > _QTY_TOLERANCE:
                # The broker says filled, but tonight's executions do not add
                # up to the order (a missed night: reqExecutions is
                # current-day-only). Never book from the limit or the close.
                await _audit(
                    session,
                    SHARE_ORDER_HELD,
                    order.book_id,
                    {
                        "order_ref": order.order_ref,
                        "reason": "FILLED at the broker but executions do not cover the order",
                        "ordered": order.quantity,
                        "executions_quantity": filled,
                    },
                )
                notes.append(
                    f"⚠ {order.order_ref}: FILLED at the broker but only {filled:g} of {order.quantity} shares are "
                    "visible in executions — NOT booked; reconciliation will flag the holding until a human resolves it"
                )
                continue
            note = await book_fills(session, order, "FILLED")
            if note:
                notes.append(note)
        elif state is RefState.CANCELLED:
            if filled > _QTY_TOLERANCE:
                note = await book_fills(session, order, "CANCELLED")
                if note:
                    notes.append(note)
                continue
            reason = report.rejection_reason(order.order_ref)
            final_status = "REJECTED" if reason else "CANCELLED"
            if await _stamp(session, order, final_status, completed_at=_now(), fills=list(order.fills or [])):
                event = SHARE_ORDER_REJECTED if reason else SHARE_ORDER_EXPIRED
                payload: dict[str, object] = {"order_ref": order.order_ref, "symbol": order.symbol}
                if reason:
                    payload["reason"] = reason
                await _audit(session, event, order.book_id, payload)
                notes.append(
                    f"{order.book_id} {order.side} {order.quantity} {order.symbol} "
                    + (
                        f"rejected: {reason}"
                        if reason
                        else (
                            "did not fill (expired) — holding unchanged; the flatten retries next run"
                            if order.purpose == SHARE_ORDER_PURPOSE_FLATTEN
                            else "did not fill (expired) — holding unchanged until next month"
                        )
                    )
                )
        elif state is RefState.OPEN:
            if order.status == "STAGED" and await _stamp(
                session, order, "SUBMITTED", submitted_at=order.submitted_at or _now(), fills=list(order.fills or [])
            ):
                await _audit(
                    session, SHARE_ORDER_SUBMITTED, order.book_id, {"order_ref": order.order_ref, "found": True}
                )
            # Still working its session — the carve-out covers any fills so far.
        else:  # RefState.UNKNOWN
            if order.fills or restore_gap_trading_days is None or restore_gap_trading_days > 1:
                await _audit(
                    session,
                    SHARE_ORDER_HELD,
                    order.book_id,
                    {
                        "order_ref": order.order_ref,
                        "reason": "UNKNOWN at the broker with recorded fills or a restore gap",
                        "gap_trading_days": restore_gap_trading_days,
                    },
                )
                notes.append(f"⚠ {order.order_ref}: unknown at the broker — held, not expired; resolve by hand")
                continue
            if await _stamp(session, order, "CANCELLED", completed_at=_now()):
                await _audit(
                    session,
                    SHARE_ORDER_EXPIRED,
                    order.book_id,
                    {"order_ref": order.order_ref, "symbol": order.symbol, "was": order.status},
                )
                notes.append(f"{order.book_id} {order.side} {order.quantity} {order.symbol} expired unplaced/unfilled")
    return notes


# ---------------------------------------------------------------------------
# The month-end rebalance (runs after Layer C on the evening run)
# ---------------------------------------------------------------------------


@dataclass
class RebalanceResult:
    placed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


async def _skip(session: AsyncSession, result: RebalanceResult, book_id: str, reason: str, signal_date: str) -> None:
    result.notes.append(f"{book_id} ETF trend: month-end rebalance SKIPPED — {reason}")
    # signal_date (#1074): the missed-rebalance digest line finds the skip
    # that explains a missed month-end by this key, never by timestamp.
    await _audit(session, ETF_TREND_SKIPPED, book_id, {"reason": reason, "signal_date": signal_date})
    await session.commit()


async def has_active_share_book(session: AsyncSession) -> bool:
    """Any ACTIVE book configured as a share book (#1074) — gates the nightly
    work only a share book needs (the benchmark's total-return fetch)."""
    books = (await session.execute(select(BookModel).filter(BookModel.status == BOOK_ACTIVE_STATUS))).scalars().all()
    return any(resolve_book_config(b.config).etf_trend is not None for b in books)


async def run_etf_trend_rebalances(session: AsyncSession, broker: ShareOrderBroker, today: date) -> RebalanceResult:
    """On a month's last trading day, rebalance every active share book.
    Any other day: nothing, by the pre-registered rule."""
    result = RebalanceResult()
    if not is_signal_day(today):
        return result
    books = (await session.execute(select(BookModel).filter(BookModel.status == BOOK_ACTIVE_STATUS))).scalars().all()
    for book in sorted(books, key=lambda b: b.id):
        config = resolve_book_config(book.config)
        if config.etf_trend is None:
            continue
        await _rebalance_book(session, broker, book, config.etf_trend, config.stage1_stake, today, result)
    return result


async def _investable(
    session: AsyncSession, result: RebalanceResult, book: BookModel, stake: float | None, equity: float, iso: str
) -> float | None:
    """The capital the month-end targets are sized from (#1074).

    - Unstaked: the whole of current equity.
    - Staked (`stage1_stake`, the real-money cap): the stake plus the P&L the
      book has accrued since its stake window opened — current equity minus
      the equity it carried into the window — so gains compound past the
      stake and losses shrink it. The window start and baseline are
      stage1.stake_window / stake_baseline, the definition the -30% stake
      drawdown halt measures from, so sizing and the halt never disagree.
      Never more than current equity (the book never borrows).
    Fails closed: a staked book whose baseline cannot be determined, or whose
    stake capital is exhausted, rebalances nothing — audited and urgent
    (ETF_TREND_STAKE_UNSIZED), plus the ordinary month-end skip."""
    if stake is None:
        return equity
    window_start, fallback = await stake_window(session, book)
    marks = [
        (row.date, row.mtm)
        for row in (await session.execute(select(BookMtmHistoryModel).filter_by(book_id=book.id))).scalars().all()
    ]
    baseline = stake_baseline(marks, window_start, fallback)
    if baseline is None:
        reason = (
            f"stage-1 stake {stake:,.2f} set but the baseline equity at the stake window start "
            f"({window_start or 'unknown'}) cannot be determined — not sized, nothing placed"
        )
    else:
        investable = min(equity, stake + (equity - baseline))
        if investable > 0:
            return investable
        reason = f"stage-1 stake exhausted (stake {stake:,.2f}, P&L {equity - baseline:,.2f} since {window_start})"
    await _audit(
        session,
        ETF_TREND_STAKE_UNSIZED,
        book.id,
        {"reason": reason, "stake": stake, "window_start": window_start, "baseline": baseline, "equity": equity},
    )
    await _skip(session, result, book.id, reason, iso)
    return None


def _describe_readings(readings: dict[str, TrendReading]) -> str:
    groups: dict[str, list[str]] = {}
    for symbol, reading in readings.items():
        groups.setdefault(reading.status, []).append(symbol)
    parts = [f"trending {', '.join(groups[TRENDING])}" if groups.get(TRENDING) else "nothing trending"]
    if groups.get(NOT_TRENDING):
        parts.append(f"not trending {', '.join(groups[NOT_TRENDING])}")
    if groups.get(MISSING_HISTORY):
        parts.append(f"MISSING HISTORY (fail closed, slot to cash leg) {', '.join(groups[MISSING_HISTORY])}")
    return "; ".join(parts)


async def _rebalance_book(
    session: AsyncSession,
    broker: ShareOrderBroker,
    book: BookModel,
    trend: EtfTrendConfig,
    stake: float | None,
    today: date,
    result: RebalanceResult,
) -> None:
    book_id = book.id
    iso = today.isoformat()
    # The halt is read first (#1074): under a flatten, tonight's own flatten
    # sells are pending too, and the skip reason the missed-rebalance line
    # repeats every night must name the halt, not the orders it caused.
    try:
        await assert_entries_allowed(session, book_id)
    except TradingHaltedError as halt:
        await _skip(session, result, book_id, f"entries halted ({halt.scope}={halt.state}); no other day trades", iso)
        return
    still_pending = [o for o in await pending_share_orders(session) if o.book_id == book_id]
    if still_pending:
        await _skip(
            session,
            result,
            book_id,
            f"{len(still_pending)} earlier share order(s) still pending ({', '.join(o.order_ref for o in still_pending)})",
            iso,
        )
        return

    symbols = (*trend.menu, trend.cash_symbol)
    closes = await _closes_by_symbol(session, symbols)
    readings = {s: trend_reading(s, closes.get(s, {}), today, trend.trend_months) for s in trend.menu}
    holdings = await _book_holdings(session, book_id)
    fractional = sorted(s for s, q in holdings.items() if abs(q - round(q)) > _QTY_TOLERANCE)
    if fractional:
        await _skip(session, result, book_id, f"holding(s) not whole shares: {', '.join(fractional)}", iso)
        return
    closes_today: dict[str, float] = {}
    for symbol in symbols:
        close = closes.get(symbol, {}).get(iso)
        if close is not None and close > 0:
            closes_today[symbol] = close
    unpriced = sorted(s for s in holdings if s not in closes_today)
    if unpriced:
        await _skip(session, result, book_id, f"no close today for held symbol(s) {', '.join(unpriced)}", iso)
        return
    await session.refresh(book, ["cash_balance"])
    cash = book.cash_balance
    equity = cash + sum(q * closes_today[s] for s, q in holdings.items())
    # The book compounds (operator ruling 2026-10-03, #1074): gains are
    # reinvested, losses shrink it. Equity that cannot be computed (a held
    # symbol with no close today) never reaches here: that skip is above.
    investable = await _investable(session, result, book, stake, equity, iso)
    if investable is None:
        return
    try:
        targets = target_shares(readings, closes_today, trend.menu, trend.cash_symbol, investable)
    except ValueError as exc:
        await _skip(session, result, book_id, str(exc), iso)
        return
    current = {s: round(q) for s, q in holdings.items()}
    intents = rebalance_orders(current, targets, closes_today, cash, trend.cash_symbol)
    await _audit(
        session,
        ETF_TREND_SIGNAL,
        book_id,
        {
            "signal_date": iso,
            "readings": {
                s: {"status": r.status, "close": r.close, "average": r.average, "missing": list(r.missing_dates)}
                for s, r in readings.items()
            },
            "equity": round(equity, 2),
            "investable": round(investable, 2),
            "cash": round(cash, 2),
            "current": current,
            "targets": targets,
            "orders": [
                {"symbol": o.symbol, "side": o.side, "quantity": o.quantity, "limit": o.limit_price} for o in intents
            ],
        },
    )
    await session.commit()
    result.notes.append(f"{book_id} ETF trend signal {iso}: {_describe_readings(readings)}")
    if not intents:
        result.notes.append(f"{book_id} ETF trend: holdings already on target — no orders")
        return
    for intent in intents:
        if not await _place_one(session, broker, book, intent, iso, result):
            break


async def _place_one(
    session: AsyncSession,
    broker: ShareOrderBroker,
    book: BookModel,
    intent: ShareOrderIntent,
    signal_date: str,
    result: RebalanceResult,
) -> bool:
    """Stage, re-check the choke point, place. False stops the rest of the
    month's orders (a halt landed, or the broker errored on the order path)."""
    book_id = book.id
    order_id = uuid.uuid4().hex[:12]
    ref = share_order_ref(book_id, order_id)
    order = ShareOrderModel(
        id=order_id,
        book_id=book_id,
        order_ref=ref,
        symbol=intent.symbol,
        side=intent.side,
        quantity=intent.quantity,
        limit_price=intent.limit_price,
        decision_close=intent.decision_close,
        signal_date=signal_date,
        status="STAGED",
        config_hash=book.config_hash,
        created_at=_now(),
        fills=[],
        purpose=SHARE_ORDER_PURPOSE_REBALANCE,
    )
    session.add(order)
    await session.commit()
    try:
        await assert_entries_allowed(session, book_id)
        placed = broker.place_share_order(intent.symbol, intent.side, intent.quantity, intent.limit_price, ref)
    except TradingHaltedError as halt:
        await _stamp(session, order, "CANCELLED", completed_at=_now())
        await _audit(session, SHARE_WOULD_HAVE_TRADED, book_id, {"order_ref": ref, "halt_scope": halt.scope})
        await session.commit()
        result.notes.append(f"{book_id} ETF trend: halted mid-rebalance ({halt.scope}={halt.state}) — rest not placed")
        return False
    except BrokerError as exc:
        await _stamp(session, order, "REJECTED", completed_at=_now())
        await _audit(session, SHARE_ORDER_REJECTED, book_id, {"order_ref": ref, "error": str(exc)})
        await session.commit()
        result.notes.append(
            f"{book_id} ETF trend: {intent.side} {intent.symbol} refused by the broker ({exc}) — rest not placed"
        )
        return False
    await _stamp(
        session,
        order,
        "SUBMITTED",
        submitted_at=_now(),
        ib_order_id=placed.order_id,
        ib_perm_id=placed.perm_id,
    )
    await _audit(
        session,
        SHARE_ORDER_SUBMITTED,
        book_id,
        {
            "order_ref": ref,
            "symbol": intent.symbol,
            "side": intent.side,
            "quantity": intent.quantity,
            "limit": intent.limit_price,
            "decision_close": intent.decision_close,
        },
    )
    await session.commit()
    result.placed.append(ref)
    result.notes.append(
        f"{book_id} ETF trend: {intent.side} {intent.quantity} {intent.symbol} limit {intent.limit_price:.2f}"
    )
    return True


# ---------------------------------------------------------------------------
# FLATTEN_REQUESTED covers shares (#1074, ADR-0011 amendment)
# ---------------------------------------------------------------------------


async def _flatten_scopes(session: AsyncSession) -> tuple[bool, frozenset[str]]:
    """(global flatten?, book scopes in flatten), read fresh. populate_existing
    for the same reason Layer A's own read uses it (#464/#546 F8): a console
    FLATTEN posted mid-run must not be shadowed by a row this run's session
    already loaded."""
    rows = (
        (await session.execute(select(TradingControlModel).execution_options(populate_existing=True))).scalars().all()
    )
    flatten = {row.scope for row in rows if row.state == FLATTEN_REQUESTED}
    return GLOBAL_SCOPE in flatten, frozenset(flatten - {GLOBAL_SCOPE})


async def _still_flattening(session: AsyncSession, book_id: str) -> bool:
    """The flatten's own choke point, read immediately before each order. A
    flatten sell is risk-reducing, so it does not pass assert_entries_allowed
    (which refuses every non-ACTIVE state — including the very flatten being
    acted on); instead it must find its scope STILL in FLATTEN_REQUESTED, so
    an operator who resumes mid-run stops the rest of the sells."""
    if await get_control_state(session, GLOBAL_SCOPE) == FLATTEN_REQUESTED:
        return True
    return await get_control_state(session, book_id) == FLATTEN_REQUESTED


async def _flatten_skip(session: AsyncSession, result: RebalanceResult, book_id: str, symbol: str, reason: str) -> None:
    result.notes.append(f"⚠ FLATTEN {book_id} {symbol}: NOT sold tonight — {reason}")
    await _audit(session, SHARE_FLATTEN_SKIPPED, book_id, {"symbol": symbol, "reason": reason})
    await session.commit()


async def run_share_flatten(
    session: AsyncSession,
    broker: ShareOrderBroker,
    today: date,
    drifted_symbols: frozenset[str] = frozenset(),
) -> RebalanceResult:
    """Sell every share holding in a FLATTEN_REQUESTED scope (#1074).

    ADR-0011's flatten for options, applied to shares, on the same nightly
    cadence: every evening while the scope stays in FLATTEN_REQUESTED, each
    remaining holding gets one whole-share DAY limit order 2% through
    today's close (etf_trend.sell_limit — the rebalance's own band). An
    unfilled order expires, the next night's sync stamps it CANCELLED, and
    the next run sells whatever is still held; a partial fill is booked as
    what filled and the remainder is sold the next night. The flatten itself
    is never cleared here — resuming stays a console act (ADR-0008).

    Fails closed per symbol, each skip audited and named in the digest:
    - share drift on the symbol tonight (the #407 analogue): the books and
      the broker disagree, and the likeliest cause is that the operator
      already sold at the broker (ADR-0011 sends an urgent flatten there), so
      a sell sized from the books could go SHORT;
    - a share order already pending for that book and symbol (the #405
      re-run guard: a same-evening catch-up would sell twice);
    - a holding on a symbol the book is not designated for (reconciliation
      does not count that row either; it is fixed through resolution);
    - no close today to price the limit from;
    - less than one whole share (the order path is whole shares only; a
      fractional remainder is sold by hand and corrected through resolution).
    Runs from the evening run only — never from the 12:30 midday pass, which
    never flattens (ADR-0008 #960 amendment)."""
    result = RebalanceResult()
    flatten_global, flatten_books = await _flatten_scopes(session)
    if not flatten_global and not flatten_books:
        return result
    rows = (await session.execute(select(ShareHoldingModel))).scalars().all()
    targets = sorted(
        (row for row in rows if abs(row.quantity) > _QTY_TOLERANCE and row.book_id != "B00"),
        key=lambda r: (r.book_id, r.symbol),
    )
    targets = [r for r in targets if flatten_global or r.book_id in flatten_books]
    if not targets:
        return result
    books = {b.id: b for b in (await session.execute(select(BookModel))).scalars().all()}
    pending = {(o.book_id, o.symbol) for o in await pending_share_orders(session)}
    closes = await _closes_by_symbol(session, {r.symbol for r in targets})
    iso = today.isoformat()
    for holding in targets:
        book_id, symbol, quantity = holding.book_id, holding.symbol, holding.quantity
        book = books.get(book_id)
        designated = resolve_book_config(book.config).share_symbols if book is not None else ()
        if symbol not in designated:
            await _flatten_skip(
                session,
                result,
                book_id,
                symbol,
                "book is not designated for this symbol — reconciliation ignores the row; correct it through resolution",
            )
            continue
        if symbol in drifted_symbols:
            await _flatten_skip(
                session,
                result,
                book_id,
                symbol,
                "share drift on this symbol tonight (books and broker disagree) — resolve it first; "
                "a sell sized from the books could go short",
            )
            continue
        if (book_id, symbol) in pending:
            await _flatten_skip(session, result, book_id, symbol, "a share order is already pending on it")
            continue
        close = closes.get(symbol, {}).get(iso)
        if close is None or not math.isfinite(close) or close <= 0:
            await _flatten_skip(session, result, book_id, symbol, "no close today to price the limit from")
            continue
        whole = math.floor(abs(quantity) + _QTY_TOLERANCE)
        if whole == 0:
            await _flatten_skip(
                session, result, book_id, symbol, f"holding {quantity:g} is under one whole share — sell it by hand"
            )
            continue
        side = "SELL" if quantity > 0 else "BUY"
        limit = sell_limit(close) if side == "SELL" else buy_limit(close)
        intent = ShareOrderIntent(symbol=symbol, side=side, quantity=whole, limit_price=limit, decision_close=close)
        scope = GLOBAL_SCOPE if flatten_global else book_id
        if await _place_flatten(
            session, broker, book_id, book.config_hash if book else None, intent, iso, scope, result
        ):
            remainder = abs(quantity) - whole
            if remainder > _QTY_TOLERANCE:
                result.notes.append(
                    f"⚠ FLATTEN {book_id} {symbol}: {remainder:g} fractional share(s) left — sell by hand"
                )
    return result


async def _place_flatten(
    session: AsyncSession,
    broker: ShareOrderBroker,
    book_id: str,
    config_hash: str | None,
    intent: ShareOrderIntent,
    today_iso: str,
    scope: str,
    result: RebalanceResult,
) -> bool:
    """Stage, re-read the flatten, place. Intent first, exactly like the
    rebalance's _place_one: the row is committed STAGED before placeOrder."""
    order_id = uuid.uuid4().hex[:12]
    ref = share_order_ref(book_id, order_id)
    order = ShareOrderModel(
        id=order_id,
        book_id=book_id,
        order_ref=ref,
        symbol=intent.symbol,
        side=intent.side,
        quantity=intent.quantity,
        limit_price=intent.limit_price,
        decision_close=intent.decision_close,
        signal_date=today_iso,
        status="STAGED",
        config_hash=config_hash,
        created_at=_now(),
        fills=[],
        purpose=SHARE_ORDER_PURPOSE_FLATTEN,
    )
    session.add(order)
    await session.commit()
    if not await _still_flattening(session, book_id):
        await _stamp(session, order, "CANCELLED", completed_at=_now())
        await _audit(
            session,
            SHARE_FLATTEN_SKIPPED,
            book_id,
            {"symbol": intent.symbol, "order_ref": ref, "reason": "flatten lifted mid-run"},
        )
        await session.commit()
        result.notes.append(f"FLATTEN {book_id} {intent.symbol}: flatten lifted mid-run — not placed")
        return False
    try:
        placed = broker.place_share_order(intent.symbol, intent.side, intent.quantity, intent.limit_price, ref)
    except BrokerError as exc:
        await _stamp(session, order, "REJECTED", completed_at=_now())
        await _audit(session, SHARE_FLATTEN_REJECTED, book_id, {"order_ref": ref, "error": str(exc)})
        await session.commit()
        result.notes.append(f"⚠ FLATTEN {book_id} {intent.side} {intent.symbol} refused by the broker ({exc})")
        return False
    await _stamp(
        session, order, "SUBMITTED", submitted_at=_now(), ib_order_id=placed.order_id, ib_perm_id=placed.perm_id
    )
    await _audit(
        session,
        SHARE_FLATTEN_SUBMITTED,
        book_id,
        {
            "order_ref": ref,
            "symbol": intent.symbol,
            "side": intent.side,
            "quantity": intent.quantity,
            "limit": intent.limit_price,
            "decision_close": intent.decision_close,
            "scope": scope,
            "trigger": "MANUAL",  # a human requested the flatten (ADR-0011)
        },
    )
    await session.commit()
    result.placed.append(ref)
    result.notes.append(
        f"FLATTEN ({scope}): {book_id} {intent.side} {intent.quantity} {intent.symbol} limit {intent.limit_price:.2f}"
    )
    return True


# ---------------------------------------------------------------------------
# A missed or unfilled month-end must be loud (#1074)
# ---------------------------------------------------------------------------


async def rebalance_watch_notes(session: AsyncSession, today: date) -> list[str]:
    """Digest lines for every active share book whose most recent month-end
    rebalance did not happen in full. Repeated every night, from data alone,
    until the next month-end — a missed rebalance is never caught up (the
    pre-registered rule), so the operator hears about it for as long as the
    book is holding last month's positions.

    - Missed: the book has no ETF_TREND_SIGNAL for the last signal day. The
      reason is the ETF_TREND_SKIPPED recorded for that signal day (a halt
      names its scope and state); with no record at all — no run that day, or
      the run stopped before the rebalance — the line says it MAY have been
      missed rather than guessing why.
    - Unfilled: the rebalance ran, but a REBALANCE order from it terminalized
      with less than its quantity filled (its DAY limit was not reached, or
      it was refused), so that slot holds last month's position.
    Signal days before the book was created are never reported."""
    signal = last_signal_day_on_or_before(today)
    following = next_signal_day_after(today).isoformat()
    signal_iso = signal.isoformat()
    notes: list[str] = []
    books = (await session.execute(select(BookModel).filter(BookModel.status == BOOK_ACTIVE_STATUS))).scalars().all()
    for book in sorted(books, key=lambda b: b.id):
        if resolve_book_config(book.config).etf_trend is None or signal_iso < (book.created_at or "")[:10]:
            continue
        events = (
            (
                await session.execute(
                    select(AuditEventModel).filter(
                        AuditEventModel.book_id == book.id,
                        AuditEventModel.event_type.in_((ETF_TREND_SIGNAL, ETF_TREND_SKIPPED)),
                    )
                )
            )
            .scalars()
            .all()
        )
        signals = [
            e for e in events if e.event_type == ETF_TREND_SIGNAL and (e.payload or {}).get("signal_date") == signal_iso
        ]
        if not signals:
            skips = [
                e
                for e in events
                if e.event_type == ETF_TREND_SKIPPED and (e.payload or {}).get("signal_date") == signal_iso
            ]
            reason = (
                f"skipped — {skips[-1].payload.get('reason')}"
                if skips
                else "no rebalance record for that day (no run, or the run stopped before the rebalance) — "
                "it may have been missed"
            )
            notes.append(
                f"⚠ {book.id} missed its month-end rebalance ({signal_iso}): {reason}; "
                f"holding last month's positions until {following}"
            )
            continue
        orders = (
            (
                await session.execute(
                    select(ShareOrderModel).filter(
                        ShareOrderModel.book_id == book.id,
                        ShareOrderModel.signal_date == signal_iso,
                        ShareOrderModel.purpose == SHARE_ORDER_PURPOSE_REBALANCE,
                        ShareOrderModel.status.in_(SHARE_ORDER_TERMINAL_STATUSES),
                    )
                )
            )
            .scalars()
            .all()
        )
        # An intent the rebalance decided on but never staged: a halt or a
        # broker error stopped the month's remaining orders (_place_one).
        staged = {(o.symbol, o.side) for o in orders} | {
            (o.symbol, o.side)
            for o in await pending_share_orders(session)
            if o.book_id == book.id and o.signal_date == signal_iso
        }
        for intent in signals[-1].payload.get("orders") or []:
            if (intent.get("symbol"), intent.get("side")) not in staged:
                notes.append(
                    f"⚠ {book.id} {intent.get('side')} {intent.get('quantity')} {intent.get('symbol')} from the "
                    f"{signal_iso} rebalance was never placed (the rebalance stopped early) — the "
                    f"{intent.get('symbol')} slot holds last month's position until {following}"
                )
        for order in sorted(orders, key=lambda o: o.symbol):
            filled = order.filled_quantity or 0.0
            if filled >= order.quantity - _QTY_TOLERANCE:
                continue
            what = "did not fill" if filled <= _QTY_TOLERANCE else f"filled only {filled:g} of {order.quantity}"
            notes.append(
                f"⚠ {book.id} {order.side} {order.quantity} {order.symbol} from the {signal_iso} rebalance {what} "
                f"({order.status}) — the {order.symbol} slot holds last month's position until {following}"
            )
    return notes

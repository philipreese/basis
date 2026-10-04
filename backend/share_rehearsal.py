"""share_rehearsal.py — operator-triggered PAPER dress rehearsal of the
share-book order path (#1093).

B36's first real share orders go in at a month-end evening run. Before that,
every piece of the share path had been tested against mocks only. This
command runs B36's own code against the real IBKR paper account, a few
shares at a time, through a dedicated ops book (R01, seeds.OPS_BOOKS):

- `place`: buy 1 share of each of 2-3 cheap B36 menu symbols through
  share_book._place_one, the rebalance's own order path (STAGED row
  committed first, the entry choke point read immediately before
  placeOrder, whole-share DAY limit 2% through the last stored close).
- `status`: book R01's fills through share_book.sync_share_orders, the
  evening sync's own arithmetic (share_holdings plus book cash), then run
  reconciliation's read-only comparison (compare_books) and print exactly
  what it sees for R01's symbols.
- `unwind`: latch R01 FLATTEN_REQUESTED and sell everything through
  share_book.run_share_flatten, the #1074 flatten path. The latch stays:
  if a sell does not fill, tonight's evening run retries it exactly as it
  would for B36, and `status` books whatever filled.
- `status` again: R01 flat, reconciliation clean.

Evidence isolation. R01 has status OPS (states.BOOK_OPS_STATUS), so no
ACTIVE-only reader (Layer C, the share rebalance, the marks, the digest's
book rows, fleet NAV) touches it, and console.book_summaries, the null drill
and distribution attribution exclude it by status. Its fills land in R01's
own share_holdings and cash, never B36's. Reconciliation counts R01's
holdings because R01 is designated for B36's symbols; that is the point.

Disciplines:
- Paper only: refuses unless IBKR_TRADING_MODE is paper (the executor's
  guard), and BrokerSession itself refuses any non-D-prefixed account.
- One mutator at a time: takes the executor's own run lock ("executor"), and
  refuses if any other Gateway tenant (midday pass, preflight, fill check,
  restore drill, a nightly launch) is live.
- Never inside a scheduled task's window (BLACKOUT_WINDOWS, ET).
- Never writes index_history: mid-session, IBKR's daily bars include
  today's partial bar, and the nightly's persist skips dates already
  stored, so a partial "close" would stick for every reader. Prices come
  from closes the nightly already stored, and a close older than the
  previous trading day refuses.
- Never writes reconciliation_runs and never latches a halt (the
  preflight/midday discipline): a daytime row would zero the evening run's
  missed-night gap. Drift is reported; tonight's run halts on it if it is
  real.
- Never resumes a scope. RESUME is console-only (ADR-0008): after `unwind`,
  R01 stays FLATTEN_REQUESTED until the operator resumes it from the
  console, and `place` refuses unless R01 and GLOBAL are ACTIVE.
"""

import argparse
import asyncio
import logging
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.anomaly import _market_days_between
from backend.book_gates import resolve_for_book
from backend.broker import BrokerSession
from backend.calendars import is_trading_day
from backend.database import TRADING_MODE
from backend.dates import MARKET_TZ
from backend.etf_trend import ShareOrderIntent, buy_limit
from backend.gateway_lifecycle import stop_gateway_tree_only
from backend.models import (
    AuditEventModel,
    BookModel,
    IndexHistoryModel,
    OrderModel,
    ReconciliationRunModel,
    ShareHoldingModel,
    ShareOrderModel,
)
from backend.reconciliation import ORPHAN, SHARE_DRIFT, BrokerSnapshot, compare_books, drift_is_sync_pending
from backend.run_lock import acquire_run_lock, other_gateway_tenant_active, release_run_lock
from backend.share_book import (
    RebalanceResult,
    _place_one,
    pending_share_deltas,
    pending_share_orders,
    run_share_flatten,
    sync_share_orders,
)
from backend.states import BOOK_OPS_STATUS, ORDER_PENDING_STATUSES
from backend.trading_control import (
    ACTIVE,
    FLATTEN_REQUESTED,
    check_trading_control,
    get_control_state,
    set_control,
)

logger = logging.getLogger(__name__)

REHEARSAL_BOOK_ID = "R01"
# Cheap B36 menu symbols that, as of 2026-10, pay no distribution before
# December: IAUM is a gold trust and pays none; SCHF and SCHH pay around
# their quarter or half-year ends. A distribution earned on a rehearsal share
# has no owner (share_distributions._owners never counts an ops book), so
# avoid the monthly payers (TBIL, UTEN) unless the rehearsal is unwound well
# before their record dates.
DEFAULT_SYMBOLS: tuple[str, ...] = ("IAUM", "SCHF", "SCHH")
MAX_SYMBOLS = 3
REHEARSAL_QUANTITY = 1
# The executor's own lock: one mutator of orders and book cash at a time.
LOCK_NAME = "executor"
ACTOR = "share_rehearsal"
PHASES = ("place", "status", "unwind")

# ET windows the rehearsal never starts in, each opening 15 minutes before
# its scheduled task so a phase (Gateway warmup included) is done before
# the task fires. Inclusive at both ends.
BLACKOUT_WINDOWS: tuple[tuple[dtime, dtime, str], ...] = (
    (dtime(12, 15), dtime(12, 45), "the 12:30 midday exit pass"),
    (dtime(13, 45), dtime(14, 30), "the 14:00 preflight"),
    (dtime(18, 30), dtime(19, 30), "the 18:45 nightly executor run"),
)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_BROKER_UNAVAILABLE = 2
EXIT_DRIFT = 3
# The phase ran, but the share path misbehaved: an order refused by the
# broker or halted mid-run, a flatten sell skipped, a fill held or rejected
# at sync. These are exactly the bugs the rehearsal exists to surface, so
# they must never read as success.
EXIT_SHARE_PATH_PROBLEM = 4

SHARE_REHEARSAL_RUN = "SHARE_REHEARSAL_RUN"

# (broker, gateway proc, launch start time, failure) — midday_exits._open_session's shape.
OpenSession = Callable[[], tuple[Any, subprocess.Popen | None, float | None, str | None]]


@dataclass
class RehearsalReport:
    phase: str
    lines: list[str] = field(default_factory=list)
    refused: str | None = None
    broker_failure: str | None = None
    drift: bool = False
    placed: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if self.refused is not None:
            return EXIT_REFUSED
        if self.broker_failure is not None:
            return EXIT_BROKER_UNAVAILABLE
        if self.drift:
            return EXIT_DRIFT
        if self.problems:
            return EXIT_SHARE_PATH_PROBLEM
        return EXIT_OK

    @property
    def outcome(self) -> str:
        if self.refused is not None:
            return "REFUSED"
        if self.broker_failure is not None:
            return "BROKER_UNAVAILABLE"
        if self.drift:
            return "DRIFT"
        return "SHARE_PATH_PROBLEM" if self.problems else "OK"

    def problem(self, what: str) -> None:
        self.problems.append(what)
        self.lines.append(f"PROBLEM: {what}")

    def refuse(self, reason: str) -> "RehearsalReport":
        self.refused = reason
        self.lines.append(f"REFUSED: {reason}")
        return self


class _Refused(Exception):
    """A precondition failed after the broker session opened."""


def blackout_reason(now: datetime) -> str | None:
    """The scheduled task whose window *now* (ET) falls in, or None."""
    clock = now.astimezone(MARKET_TZ).time()
    for start, end, what in BLACKOUT_WINDOWS:
        if start <= clock <= end:
            return f"{clock:%H:%M} ET is inside {start:%H:%M}-{end:%H:%M}, the window around {what}"
    return None


def previous_trading_day(day: date) -> date:
    prior = day - timedelta(days=1)
    while not is_trading_day(prior):
        prior -= timedelta(days=1)
    return prior


def _default_open_session() -> tuple[Any, subprocess.Popen | None, float | None, str | None]:
    # The midday pass's launcher: IBC Gateway launch, warmup, the #852/#884
    # port poll, then BrokerSession.open() with its paper-account guard.
    from backend.midday_exits import _open_session

    return _open_session(BrokerSession, time.sleep, time.monotonic)


# ---------------------------------------------------------------------------
# Database reads
# ---------------------------------------------------------------------------


async def _holdings(session: AsyncSession, book_id: str) -> dict[str, float]:
    rows = (await session.execute(select(ShareHoldingModel).filter_by(book_id=book_id))).scalars().all()
    return {row.symbol: row.quantity for row in rows if abs(row.quantity) > 1e-6}


async def _r01_pending(session: AsyncSession) -> list[ShareOrderModel]:
    return [o for o in await pending_share_orders(session) if o.book_id == REHEARSAL_BOOK_ID]


async def stored_closes(session: AsyncSession, symbols: tuple[str, ...], today: date) -> tuple[date, dict[str, float]]:
    """The latest date, on or before *today*, on which every symbol has a
    stored close, and those closes. Raises _Refused when there is none, or
    when it is older than the previous trading day (a stale close would
    price a limit nowhere near the market). Reads index_history only; never
    fetches (see the module docstring)."""
    rows = (
        (
            await session.execute(
                select(IndexHistoryModel).filter(
                    IndexHistoryModel.symbol.in_(symbols), IndexHistoryModel.date <= today.isoformat()
                )
            )
        )
        .scalars()
        .all()
    )
    by_symbol: dict[str, dict[str, float]] = {s: {} for s in symbols}
    for row in rows:
        by_symbol[row.symbol][row.date] = row.close
    missing = sorted(s for s, closes in by_symbol.items() if not closes)
    if missing:
        raise _Refused(
            f"no stored close for {', '.join(missing)} — the nightly run backfills index_history; "
            "rehearse after it has run"
        )
    common = set.intersection(*(set(closes) for closes in by_symbol.values()))
    if not common:
        raise _Refused(f"no single date with a stored close for every one of {', '.join(symbols)}")
    latest = max(common)
    floor = previous_trading_day(today).isoformat()
    if latest < floor:
        raise _Refused(f"latest common stored close is {latest}, older than the previous trading day ({floor})")
    return date.fromisoformat(latest), {s: by_symbol[s][latest] for s in symbols}


async def _restore_gap(session: AsyncSession, today: date) -> int | None:
    """The executor's missed-night measure (run_executor_evening): trading
    days since the last reconciliation run, None when there never was one."""
    last = (
        await session.execute(select(ReconciliationRunModel).order_by(ReconciliationRunModel.id.desc()).limit(1))
    ).scalar_one_or_none()
    return _market_days_between(last.run_at, today.isoformat()) if last else None


async def _pending_order_occ(session: AsyncSession) -> set[str]:
    from backend.preflight import _pending_order_occ_symbols

    return await _pending_order_occ_symbols(session)


# ---------------------------------------------------------------------------
# Broker steps shared by the phases
# ---------------------------------------------------------------------------


async def _reconcile_and_sync(session: AsyncSession, broker: Any, today: date, report: RehearsalReport) -> None:
    """broker.reconcile over EVERY pending ref (option and share), so the
    adapter's reconcile-first and duplicate-ref guards see the whole book of
    outstanding orders, then sync R01's pending share orders only. B36's and
    every options book's orders are the evening run's to sync."""
    option_refs = (
        (await session.execute(select(OrderModel.order_ref).filter(OrderModel.status.in_(ORDER_PENDING_STATUSES))))
        .scalars()
        .all()
    )
    share_pending = await pending_share_orders(session)
    verdicts = broker.reconcile(list(option_refs) + [o.order_ref for o in share_pending])
    mine = [o for o in share_pending if o.book_id == REHEARSAL_BOOK_ID]
    if not mine:
        return
    notes = await sync_share_orders(
        session, mine, verdicts, tuple(broker.executions()), await _restore_gap(session, today)
    )
    await session.commit()
    report.lines.extend(f"sync: {note}" for note in notes)
    for note in notes:
        # A held verdict (FILLED without covering executions, UNKNOWN with
        # fills) or a broker rejection is a share-path failure; a plain DAY
        # expiry is not (a 2% limit can miss on a gap) and stays a sync line.
        if note.startswith("⚠") or " rejected: " in note:
            report.problem(f"sync: {note}")


async def _compare(session: AsyncSession, broker: Any, today: date, report: RehearsalReport) -> list[str]:
    """Reconciliation's read-only comparison. Prints what it sees for every
    symbol R01 is designated for, and every drift. Returns the drifted STK
    symbols (the flatten's skip set, as the evening run builds it). Sets
    report.drift for drift that no pending order explains."""
    positions = tuple(broker.positions())
    comparison = await compare_books(
        session, BrokerSnapshot(positions=positions, open_orders=tuple(broker.open_orders())), today=today.isoformat()
    )
    pending_occ = await _pending_order_occ(session)
    pending_shares = await pending_share_deltas(session)
    book = await session.get(BookModel, REHEARSAL_BOOK_ID)
    designated = resolve_for_book(book).share_symbols if book is not None else ()
    r01 = await _holdings(session, REHEARSAL_BOOK_ID)
    broker_shares: dict[str, float] = {}
    for pos in positions:
        if pos.sec_type == "STK":
            broker_shares[pos.symbol] = broker_shares.get(pos.symbol, 0.0) + pos.position
    shown = sorted(s for s in designated if broker_shares.get(s) or comparison.expected_shares.get(s))
    report.lines.append("reconciliation sees (designated symbols with shares on either side):")
    if not shown:
        report.lines.append("  none: no designated book holds shares and the broker shows none")
    for symbol in shown:
        expected = comparison.expected_shares.get(symbol, 0.0)
        report.lines.append(
            f"  {symbol}: broker {broker_shares.get(symbol, 0.0):g}, books expect {expected:g} "
            f"(R01 {r01.get(symbol, 0.0):g}, other designated books {expected - r01.get(symbol, 0.0):g})"
        )
    explained = 0
    for drift in comparison.drifts:
        if drift_is_sync_pending(drift, pending_occ, pending_shares):
            explained += 1
            report.lines.append(
                f"  pending sync: {drift.kind} {drift.key} (broker {drift.broker_qty:g}, books {drift.expected_qty:g})"
                " — a pending order explains it; the next sync books it"
            )
            continue
        report.drift = True
        report.lines.append(
            f"  DRIFT: {drift.kind} {drift.key} (broker {drift.broker_qty:g}, books {drift.expected_qty:g})"
        )
    if report.drift:
        report.lines.append("reconciliation: DRIFT — tonight's run halts entries on this unless it is resolved first")
    elif explained:
        report.lines.append(f"reconciliation: CLEAN once {explained} pending order(s) are synced")
    else:
        report.lines.append("reconciliation: CLEAN")
    return sorted({d.key for d in comparison.drifts if d.sec_type == "STK" and d.kind in (ORPHAN, SHARE_DRIFT)})


async def _describe_r01(session: AsyncSession, report: RehearsalReport) -> None:
    book = await session.get(BookModel, REHEARSAL_BOOK_ID)
    await session.refresh(book, ["cash_balance"])
    holdings = await _holdings(session, REHEARSAL_BOOK_ID)
    pending = await _r01_pending(session)
    report.lines.append(
        f"R01 control {await get_control_state(session, REHEARSAL_BOOK_ID)}, "
        f"global {await get_control_state(session, 'GLOBAL')}, virtual cash {book.cash_balance:,.2f}"
    )
    report.lines.append(
        "R01 holdings: " + (", ".join(f"{s} {q:g}" for s, q in sorted(holdings.items())) if holdings else "none")
    )
    for order in pending:
        report.lines.append(
            f"R01 pending: {order.side} {order.quantity} {order.symbol} limit {order.limit_price:.2f} "
            f"({order.status}, {order.purpose}, {order.order_ref})"
        )


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------


async def _check_place_preconditions(
    session: AsyncSession, symbols: tuple[str, ...], today: date
) -> tuple[date, dict[str, float]]:
    # An early, read-only look so a halted scope never launches Gateway for
    # nothing. The real choke point (assert_entries_allowed, inside
    # _place_one) still runs immediately before each order.
    scope, state = await check_trading_control(session, REHEARSAL_BOOK_ID)
    if state != ACTIVE:
        raise _Refused(
            f"{scope} is {state} — place needs R01 and GLOBAL ACTIVE (after an unwind, resume R01 from the console)"
        )
    if await _r01_pending(session):
        raise _Refused("R01 has share orders still pending — run `status` (or let tonight's sync settle them) first")
    holdings = await _holdings(session, REHEARSAL_BOOK_ID)
    if holdings:
        held = ", ".join(f"{s} {q:g}" for s, q in sorted(holdings.items()))
        raise _Refused(f"R01 still holds {held} — run `unwind` and finish the last rehearsal first")
    return await stored_closes(session, symbols, today)


async def _place(
    session: AsyncSession,
    broker: Any,
    symbols: tuple[str, ...],
    closes: dict[str, float],
    close_date: date,
    today: date,
    report: RehearsalReport,
) -> None:
    await _reconcile_and_sync(session, broker, today, report)
    report.lines.append("pre-trade check:")
    await _compare(session, broker, today, report)
    if report.drift:
        raise _Refused("the account has unexplained drift — resolve it before adding rehearsal shares on top")
    book = await session.get(BookModel, REHEARSAL_BOOK_ID)
    result = RebalanceResult()
    report.lines.append(f"placing (limits 2% through the {close_date.isoformat()} close):")
    for symbol in symbols:
        close = closes[symbol]
        intent = ShareOrderIntent(
            symbol=symbol, side="BUY", quantity=REHEARSAL_QUANTITY, limit_price=buy_limit(close), decision_close=close
        )
        # The rebalance's own path: STAGED committed, then
        # assert_entries_allowed(R01), then place_share_order.
        if not await _place_one(session, broker, book, intent, today.isoformat(), result):
            break
    report.placed.extend(result.placed)
    report.lines.extend(f"  {note}" for note in result.notes)
    if len(result.placed) < len(symbols):
        report.problem(
            f"placed {len(result.placed)} of {len(symbols)} — see the line above; whatever was placed is real, "
            "so run `status`, then `unwind` once it fills"
        )
    else:
        report.lines.append(f"placed {len(result.placed)} order(s); next: `status` once they fill")


async def _status(session: AsyncSession, broker: Any, today: date, report: RehearsalReport) -> None:
    await _reconcile_and_sync(session, broker, today, report)
    await _describe_r01(session, report)
    await _compare(session, broker, today, report)
    holdings = await _holdings(session, REHEARSAL_BOOK_ID)
    pending = await _r01_pending(session)
    if report.drift:
        return
    if pending:
        report.lines.append("verdict: orders in flight — run `status` again after they fill (or expire)")
    elif holdings:
        report.lines.append("verdict: R01 holds its rehearsal shares and reconciles — next: `unwind`")
    elif await get_control_state(session, REHEARSAL_BOOK_ID) == FLATTEN_REQUESTED:
        report.lines.append(
            "verdict: REHEARSAL COMPLETE — R01 is flat and reconciles clean. R01 stays FLATTEN_REQUESTED until you "
            "resume it from the console (this command never resumes a scope, ADR-0008); resume it before the next "
            "rehearsal, or leave it"
        )
    else:
        report.lines.append("verdict: R01 is flat and reconciles clean — nothing in flight")


async def _unwind(session: AsyncSession, broker: Any, today: date, report: RehearsalReport) -> None:
    await _reconcile_and_sync(session, broker, today, report)
    if await _r01_pending(session):
        raise _Refused("R01 has share orders still pending — run `status` after they fill or expire, then unwind")
    holdings = await _holdings(session, REHEARSAL_BOOK_ID)
    if not holdings:
        raise _Refused("R01 holds nothing — nothing to unwind")
    close_date, _ = await stored_closes(session, tuple(sorted(holdings)), today)
    drifted = await _compare(session, broker, today, report)
    await set_control(
        session,
        REHEARSAL_BOOK_ID,
        FLATTEN_REQUESTED,
        reason="share rehearsal unwind (#1093): sell R01's rehearsal shares through the flatten path",
        actor=ACTOR,
    )
    report.lines.append(
        f"R01 latched FLATTEN_REQUESTED; selling (limits 2% through the {close_date.isoformat()} close):"
    )
    # The evening run's own flatten. *today* is the close the limits are
    # priced from: the latest stored one, which before tonight's run is the
    # previous session's (run_share_flatten reads that exact date's close).
    result = await run_share_flatten(session, broker, close_date, frozenset(drifted))
    report.placed.extend(result.placed)
    report.lines.extend(f"  {note}" for note in result.notes)
    if len(result.placed) < len(holdings):
        # A skipped symbol (drift, no close, under one share) or a broker
        # refusal — each already named in the flatten's own line above.
        report.problem(f"flatten placed {len(result.placed)} sell(s) for {len(holdings)} holding(s)")
    report.lines.append(
        "next: `status` once the sells fill. An unfilled sell expires at the close and tonight's run re-places it "
        "(the flatten stays latched)"
    )


async def _foreign_flatten_scopes(session: AsyncSession) -> list[str]:
    from backend.models import TradingControlModel

    rows = (await session.execute(select(TradingControlModel))).scalars().all()
    return sorted(r.scope for r in rows if r.state == FLATTEN_REQUESTED and r.scope != REHEARSAL_BOOK_ID)


async def _write_audit(session_maker: Callable[[], Any], report: RehearsalReport, symbols: tuple[str, ...]) -> None:
    async with session_maker() as session:
        session.add(
            AuditEventModel(
                run_at=datetime.now(UTC).isoformat(),
                book_id=REHEARSAL_BOOK_ID,
                event_type=SHARE_REHEARSAL_RUN,
                actor=ACTOR,
                payload={
                    "phase": report.phase,
                    "outcome": report.outcome,
                    "symbols": list(symbols),
                    "placed": report.placed,
                    "lines": report.lines,
                },
            )
        )
        await session.commit()


async def run_rehearsal(
    phase: str,
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS,
    *,
    session_maker: Callable[[], Any] | None = None,
    open_session: OpenSession | None = None,
    now_et: Callable[[], datetime] = lambda: datetime.now(MARKET_TZ),
) -> RehearsalReport:
    """Run one rehearsal phase. Every refusal is a report, never a raise —
    except the paper-mode guard, which raises exactly as the executor's does."""
    if TRADING_MODE != "paper":
        raise RuntimeError(
            f"share-rehearsal runs only against the PAPER account; IBKR_TRADING_MODE={TRADING_MODE!r}. Refusing to run."
        )
    report = RehearsalReport(phase=phase)
    if phase not in PHASES:
        return report.refuse(f"unknown phase {phase!r} (expected one of {', '.join(PHASES)})")
    if phase == "place" and not (1 <= len(symbols) <= MAX_SYMBOLS and len(set(symbols)) == len(symbols)):
        return report.refuse(f"place takes 1-{MAX_SYMBOLS} distinct symbols, got {list(symbols)}")
    now = now_et()
    today = now.astimezone(MARKET_TZ).date()
    blackout = blackout_reason(now)
    if blackout is not None:
        return report.refuse(blackout)
    if session_maker is None:
        from backend.database import async_session_maker

        session_maker = async_session_maker
    open_session = open_session or _default_open_session

    lock = acquire_run_lock(LOCK_NAME)
    if lock is None:
        return report.refuse("the executor run lock is held — an executor run (or another rehearsal) is live")
    broker: Any = None
    close_date = today
    closes: dict[str, float] = {}
    proc: subprocess.Popen | None = None
    launch_start_time: float | None = None
    try:
        if other_gateway_tenant_active(LOCK_NAME):
            return report.refuse("another Gateway tenant (midday pass, preflight, fill check, drill) is live")
        async with session_maker() as session:
            book = await session.get(BookModel, REHEARSAL_BOOK_ID)
            if book is None or book.status != BOOK_OPS_STATUS:
                return report.refuse(
                    f"{REHEARSAL_BOOK_ID} is missing or not an ops book (status "
                    f"{getattr(book, 'status', None)!r}) — start the backend once so init_db seeds it"
                )
            designated = resolve_for_book(book).share_symbols
            if phase == "place":
                stray = sorted(set(symbols) - set(designated))
                if stray:
                    return report.refuse(f"{', '.join(stray)} not designated for R01 (B36's symbols: {designated})")
            try:
                if phase == "place":
                    close_date, closes = await _check_place_preconditions(session, symbols, today)
                elif phase == "unwind":
                    foreign = await _foreign_flatten_scopes(session)
                    if foreign:
                        raise _Refused(
                            f"{', '.join(foreign)} already FLATTEN_REQUESTED — that flatten is the evening run's; "
                            "unwinding now would act on it too"
                        )
            except _Refused as exc:
                return report.refuse(str(exc))

            broker, proc, launch_start_time, failure = open_session()
            if failure is not None or broker is None:
                report.broker_failure = failure or "broker session unavailable"
                report.lines.append(f"BROKER UNAVAILABLE: {report.broker_failure} — nothing was placed")
                return report
            try:
                if phase == "place":
                    await _place(session, broker, symbols, closes, close_date, today, report)
                elif phase == "status":
                    await _status(session, broker, today, report)
                else:
                    await _unwind(session, broker, today, report)
            except _Refused as exc:
                return report.refuse(str(exc))
        return report
    finally:
        if broker is not None:
            broker.close()
        if proc is not None or launch_start_time is not None:
            if other_gateway_tenant_active(LOCK_NAME):
                logger.warning("Another Gateway tenant is active - leaving Gateway up (#471/#681/#838)")
            else:
                stop_gateway_tree_only(proc, created_after=launch_start_time)
        release_run_lock(lock)
        try:
            await _write_audit(session_maker, report, symbols)
        except Exception as exc:  # the printed report is the deliverable; the audit row is the trail
            logger.warning("SHARE_REHEARSAL_RUN audit write failed: %s", exc)


def _parse_symbols(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return DEFAULT_SYMBOLS
    return tuple(s.strip().upper() for s in raw.split(",") if s.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pixi run share-rehearsal",
        description="PAPER dress rehearsal of the share-book order path through the R01 ops book (#1093).",
    )
    parser.add_argument("phase", choices=PHASES, help="place | status | unwind")
    parser.add_argument(
        "--symbols",
        help=f"place only: comma-separated B36 menu symbols, 1-{MAX_SYMBOLS} (default {','.join(DEFAULT_SYMBOLS)})",
    )
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    from backend.run_logging import setup_run_logging

    setup_run_logging("share_rehearsal")

    async def _run() -> RehearsalReport:
        from backend.database import init_db

        await init_db()
        return await run_rehearsal(args.phase, _parse_symbols(args.symbols))

    report = asyncio.run(_run())
    print(f"share-rehearsal {report.phase}: {report.outcome}")
    for line in report.lines:
        print(line)
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())

"""live_executor.py — Executor (Live) for stage-1 share books (#1065).

The paper pipeline (executor.run_executor_evening) refuses to run in live
mode and stays that way. This is a separate, much narrower pipeline: it runs
ONLY books that hold live authority, carry a stage-1 stake and are share
books (B36-type). There is no options path in live mode at all.

Two keys before anything is transmitted:
1. the process is in live mode: IBKR_TRADING_MODE=live, the database file is
   stamped live, and the connected Gateway manages exactly the one configured
   live account (broker.check_live_accounts);
2. the arm flag IBKR_LIVE_ARM equals the exact token "TRANSMIT", and no
   --dry-run was asked for.
Without the second key the run is a DRY RUN: it does everything up to and
including the whatIf previews against the live Gateway, audits and prints
the would-be orders, and transmits nothing — the BrokerSession itself is
built with transmission locked, so even a code path that tried to place an
order would be refused there.

Order of operations, nightly (after the close):
1. Refusals that need no broker: mode, live database stamp, environment
   (resolve_live_config), a market session still in progress.
2. Open the live session — the account guard refuses a paper account, a
   mismatch, several accounts or none.
3. Sync share orders by orderRef (share_book.sync_share_orders: fills booked
   into share_holdings and book cash from the executions, never the limit).
4. Index history, reconciliation (drift latches the live database's GLOBAL
   halt), the ntfy HALT poll.
5. The post-session anomaly sweep BEFORE any order: tonight's mark and the
   -30% stake drawdown halt (#1071's known limit — a book crossing the line
   must not stage orders the same night).
6. Judge every live book (judge_live_book): an options book, a book without
   a stake, a missing or broken grant, or a config hash that differs from
   the grant's as-raced hash (ADR-0014; the book is also halted) refuses.
7. FLATTEN_REQUESTED (ADR-0011): sells every share holding in scope, any
   book, through the previewing broker.
8. The month-end rebalance, split for an IRA that cannot borrow
   (live_orders): the signal evening places the SELLS only; a later run,
   once every sell is terminal and booked, sizes the BUYS from cash that
   exists. A month with nothing to sell places its buys the same evening.
   Every order is capped, previewed, and refused as a batch on any failure.
"""

import logging
import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.anomaly import _market_days_between, format_anomaly_line, run_post_session_anomalies
from backend.book_gates import BookConfig, resolve_book_config
from backend.broker import (
    BrokerError,
    BrokerSession,
    FillInfo,
    LegPosition,
    OpenOrderInfo,
    PlacedOrder,
    PreviewRejectedError,
    ReconcileReport,
    SharePreview,
)
from backend.calendars import is_trading_day, trading_days_between
from backend.database import TRADING_MODE, async_session_maker
from backend.dates import MARKET_TZ, market_today
from backend.etf_trend import (
    ShareOrderIntent,
    is_signal_day,
    last_signal_day_on_or_before,
    sell_limit,
    target_shares,
    trend_reading,
)
from backend.live_orders import (
    check_buy_preview,
    check_no_debit,
    check_order_caps,
    rebalance_sells,
    size_live_buys,
)
from backend.models import (
    AuditEventModel,
    BookModel,
    BookMtmHistoryModel,
    DbMetaModel,
    LiveGrantModel,
    ReconciliationRunModel,
    ShareHoldingModel,
    ShareOrderModel,
)
from backend.operator import persist_index_history
from backend.reconciliation import ORPHAN, SHARE_DRIFT, BrokerSnapshot, run_reconciliation
from backend.run_lock import acquire_run_lock, release_run_lock
from backend.share_book import (
    ETF_TREND_SIGNAL,
    ETF_TREND_SKIPPED,
    RebalanceResult,
    _book_holdings,
    _closes_by_symbol,
    _flatten_scopes,
    _place_one,
    pending_share_orders,
    rebalance_watch_notes,
    run_share_flatten,
    sync_share_orders,
)
from backend.stage1 import stake_baseline, stake_window
from backend.states import BOOK_ACTIVE_STATUS, LIVE_AUTHORITY_LIVE, SHARE_ORDER_PURPOSE_REBALANCE
from backend.trading_control import (
    ACTIVE,
    HALT_ENTRIES,
    TradingHaltedError,
    apply_ntfy_commands,
    assert_entries_allowed,
    get_control_state,
    set_control,
)

logger = logging.getLogger(__name__)

# --- Environment names (values are never logged, printed or audited) -------
LIVE_ACCOUNT_VAR = "IBKR_LIVE_ACCOUNT_ID"
LIVE_PORT_VAR = "IBKR_LIVE_GATEWAY_PORT"
LIVE_START_SCRIPT_VAR = "IBC_LIVE_START_SCRIPT"
LIVE_ARM_VAR = "IBKR_LIVE_ARM"
# Exact match only — not "truthy". A copied `=1` or `=true` leaves the run dry.
LIVE_ARM_TOKEN = "TRANSMIT"
PAPER_DEFAULT_PORT = 4002

LOCK_NAME = "live_executor"
ACTOR = "live_executor"
# The buy half of a split rebalance must run on the first live run after the
# signal day (that run books the sells' fills). One missed night closes the
# month's buys rather than buying on a stale plan; the cash simply waits for
# the next month-end, and the digest says so every night until then.
LIVE_BUY_PHASE_MAX_LAG_TRADING_DAYS = 1
# Refuse while the regular session (or its closing prints) is in progress:
# index_history keeps the first close it stores for a date, so a daytime run
# would freeze a partial bar in as that day's close.
SESSION_GUARD_START = time(9, 30)
SESSION_GUARD_END = time(16, 30)

# --- Audit event types ------------------------------------------------------
LIVE_RUN_REFUSED = "LIVE_RUN_REFUSED"
LIVE_BROKER_UNAVAILABLE = "LIVE_BROKER_UNAVAILABLE"
LIVE_BOOK_REFUSED = "LIVE_BOOK_REFUSED"
LIVE_HASH_DIVERGENCE = "LIVE_HASH_DIVERGENCE"
LIVE_ORDERS_REFUSED = "LIVE_ORDERS_REFUSED"
LIVE_DRY_RUN_SIGNAL = "LIVE_DRY_RUN_SIGNAL"
LIVE_DRY_RUN_ORDER = "LIVE_DRY_RUN_ORDER"
LIVE_BUYS_DEFERRED = "LIVE_BUYS_DEFERRED"
LIVE_BUY_PHASE_CLOSED = "LIVE_BUY_PHASE_CLOSED"
LIVE_RUN_SUMMARY = "LIVE_RUN_SUMMARY"


class LiveRefusal(RuntimeError):
    """A whole live run refused before (or instead of) trading. The message
    names the rule, never an account id or a secret value."""


@dataclass(frozen=True)
class LiveConfig:
    account_id: str
    host: str
    port: int
    client_id: int
    start_script: str
    armed: bool
    dry_run_requested: bool

    @property
    def transmit(self) -> bool:
        """Both keys turned: armed AND no dry run requested."""
        return self.armed and not self.dry_run_requested


def _port(raw: str | None, name: str) -> int:
    try:
        port = int((raw or "").strip())
    except ValueError as exc:
        raise LiveRefusal(f"{name} is not a port number") from exc
    if not 0 < port < 65536:
        raise LiveRefusal(f"{name} is not a port number")
    return port


def resolve_live_config(
    env: Mapping[str, str], base_env: Mapping[str, str | None], *, overlay_in_use: bool, dry_run: bool
) -> LiveConfig:
    """Everything the live run needs from its environment, or LiveRefusal.

    *env* is the process environment after load_env (base `.env` plus the
    live overlay); *base_env* is the base `.env` file alone — the paper
    processes' view — used only to prove live has its own Gateway session."""
    if not overlay_in_use:
        raise LiveRefusal("no live environment overlay is in use (BASIS_ENV_OVERLAY) — run through the live pixi tasks")
    if (env.get("IBKR_TRADING_MODE") or "").strip().lower() != "live":
        raise LiveRefusal("IBKR_TRADING_MODE is not live in this process's environment")
    account = (env.get(LIVE_ACCOUNT_VAR) or "").strip()
    if not account:
        raise LiveRefusal(f"{LIVE_ACCOUNT_VAR} is not set")
    if account.startswith("D"):
        raise LiveRefusal(f"{LIVE_ACCOUNT_VAR} is a paper (D-prefixed) account id")
    port = _port(env.get(LIVE_PORT_VAR), LIVE_PORT_VAR)
    if env.get("IBKR_GATEWAY_PORT") is None or _port(env.get("IBKR_GATEWAY_PORT"), "IBKR_GATEWAY_PORT") != port:
        raise LiveRefusal(
            f"IBKR_GATEWAY_PORT must equal {LIVE_PORT_VAR} in the live overlay, so market-data fetches reach the "
            "live Gateway too"
        )
    paper_port = _port(base_env.get("IBKR_GATEWAY_PORT") or str(PAPER_DEFAULT_PORT), "the paper IBKR_GATEWAY_PORT")
    if port == paper_port:
        raise LiveRefusal("the live Gateway port equals the paper one — live needs its own Gateway session")
    script = (env.get(LIVE_START_SCRIPT_VAR) or "").strip()
    if not script:
        raise LiveRefusal(f"{LIVE_START_SCRIPT_VAR} is not set")
    if script == (base_env.get("IBC_START_SCRIPT") or "").strip():
        raise LiveRefusal(f"{LIVE_START_SCRIPT_VAR} is the paper IBC start script — live needs its own IBC config")
    try:
        client_id = int((env.get("IBKR_CLIENT_ID") or "17").strip())
    except ValueError as exc:
        raise LiveRefusal("IBKR_CLIENT_ID is not a number") from exc
    return LiveConfig(
        account_id=account,
        host=(env.get("IBKR_GATEWAY_HOST") or "127.0.0.1").strip(),
        port=port,
        client_id=client_id,
        start_script=script,
        armed=env.get(LIVE_ARM_VAR) == LIVE_ARM_TOKEN,
        dry_run_requested=dry_run,
    )


# ---------------------------------------------------------------------------
# The broker surface and the previewing wrapper
# ---------------------------------------------------------------------------


class LiveBroker(Protocol):
    """What the live run calls on a BrokerSession (fakes in tests)."""

    def open(self) -> None: ...
    def close(self) -> None: ...
    def reconcile(self, refs: list[str], since: str | None = None) -> ReconcileReport: ...
    def executions(self, since: str | None = None) -> list[FillInfo]: ...
    def positions(self) -> list[LegPosition]: ...
    def open_orders(self) -> list[OpenOrderInfo]: ...
    def account_cash(self) -> float: ...
    def preview_share_order(self, symbol: str, side: str, quantity: int, limit_price: float) -> SharePreview: ...
    def place_share_order(self, symbol: str, side: str, quantity: int, limit_price: float, ref: str) -> PlacedOrder: ...


def default_broker_factory(config: LiveConfig) -> BrokerSession:
    return BrokerSession(
        live_account_id=config.account_id,
        gateway=(config.host, config.port, config.client_id),
        transmit=config.transmit,
    )


class PreviewingShareBroker:
    """The only object an ARMED live run places orders through. Every
    place_share_order is whatIf-previewed immediately before transmission
    and refused (PreviewRejectedError, a BrokerError — so share_book's own
    REJECTED/audit path handles it) on any preview error, and a BUY also on
    a preview that cannot show the account solvent afterwards."""

    def __init__(self, inner: LiveBroker) -> None:
        self._inner = inner

    def place_share_order(self, symbol: str, side: str, quantity: int, limit_price: float, ref: str) -> PlacedOrder:
        preview = self._inner.preview_share_order(symbol, side, quantity, limit_price)
        if side == "BUY":
            reason = check_buy_preview(preview)
            if reason:
                raise PreviewRejectedError(reason)
        return self._inner.place_share_order(symbol, side, quantity, limit_price, ref)


# ---------------------------------------------------------------------------
# Which books may trade live
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveBookVerdict:
    book_id: str
    eligible: bool
    reason: str | None = None
    diverged: bool = False  # the config hash moved off the grant's as-raced hash


def judge_live_book(book: BookModel, grant: LiveGrantModel | None) -> LiveBookVerdict | None:
    """None for a book without live authority — it is a paper book and the
    live run simply ignores it. For a LIVE book: eligible, or refused with
    the rule it broke. Live mode in this build trades stage-1 SHARE books
    only; an options book with live authority is refused, never traded."""
    if book.live_authority != LIVE_AUTHORITY_LIVE:
        return None
    if book.status != BOOK_ACTIVE_STATUS:
        return LiveBookVerdict(book.id, False, f"book status is {book.status}, not ACTIVE")
    try:
        config = resolve_book_config(book.config)
    except (TypeError, ValueError) as exc:
        return LiveBookVerdict(book.id, False, f"book config does not resolve ({exc})")
    if not config.is_share_book:
        return LiveBookVerdict(book.id, False, "an options book — live mode trades stage-1 share books only")
    if config.stage1_stake is None:
        return LiveBookVerdict(book.id, False, "no stage1_stake — a live book must be staked")
    if not book.promoted_at or book.demotion_policy_version is None:
        return LiveBookVerdict(book.id, False, "the grant record is incomplete (promoted_at / demotion policy)")
    if grant is None:
        return LiveBookVerdict(book.id, False, "no recorded live grant")
    if grant.as_raced_config_hash != book.config_hash:
        return LiveBookVerdict(
            book.id,
            False,
            "config hash differs from the grant's as-raced hash (ADR-0014) — revert the config or record a new grant",
            diverged=True,
        )
    if not math.isclose(grant.stake, config.stage1_stake):
        return LiveBookVerdict(book.id, False, "the stake differs from the grant's stake")
    return LiveBookVerdict(book.id, True)


async def latest_grant(session: AsyncSession, book_id: str) -> LiveGrantModel | None:
    return (
        await session.execute(
            select(LiveGrantModel).filter_by(book_id=book_id).order_by(LiveGrantModel.id.desc()).limit(1)
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass
class LiveRunSummary:
    run_started_at: str
    run_date: str
    transmit: bool
    broker_ok: bool = True
    notes: list[str] = field(default_factory=list)
    urgent: list[str] = field(default_factory=list)
    would_place: list[str] = field(default_factory=list)
    placed: list[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _audit(session: AsyncSession, event_type: str, book_id: str | None, payload: dict) -> None:
    session.add(AuditEventModel(run_at=_now(), book_id=book_id, event_type=event_type, actor=ACTOR, payload=payload))


def session_in_progress(now: datetime) -> bool:
    """True from the open until half an hour after the close on a trading day."""
    local = now.astimezone(MARKET_TZ)
    return is_trading_day(local.date()) and SESSION_GUARD_START <= local.time() < SESSION_GUARD_END


async def assert_live_database(session_maker: Callable[[], AsyncSession]) -> None:
    """The database file must be stamped live (ADR-0006 #204). init_db
    already refuses a mismatch at startup; this re-reads the stamp itself, so
    a live run against an unstamped or paper file refuses even if called
    some other way."""
    async with session_maker() as session:
        row = await session.get(DbMetaModel, "trading_mode")
    if row is None or row.value != "live":
        raise LiveRefusal("the database is not stamped live — refusing to trade")


async def run_live_executor(
    config: LiveConfig,
    *,
    session_maker: Callable[[], AsyncSession] | None = None,
    broker_factory: Callable[[LiveConfig], LiveBroker] | None = None,
    today: date | None = None,
    now: datetime | None = None,
    rehearse: bool = False,
) -> LiveRunSummary:
    """One live run. Raises LiveRefusal for a run-level refusal; everything
    narrower (a book, a batch of orders) is refused, audited and named in the
    summary while the run carries on.

    rehearse (dry run only): evaluate the most recent month-end as if tonight
    were that signal evening, so the operator can preview a full rebalance
    against the live Gateway on any day."""
    if TRADING_MODE != "live":
        raise LiveRefusal(f"the process is in {TRADING_MODE!r} mode — the live executor runs only in live mode")
    if rehearse and config.transmit:
        raise LiveRefusal("a rehearsal is dry-run only — pass --dry-run or unset the arm flag")
    session_maker = session_maker or async_session_maker
    broker_factory = broker_factory or default_broker_factory
    now = now or datetime.now(UTC)
    today = today or market_today()
    await assert_live_database(session_maker)
    if session_in_progress(now):
        raise LiveRefusal("the market session is in progress — the live executor runs after the close")
    summary = LiveRunSummary(run_started_at=now.isoformat(), run_date=today.isoformat(), transmit=config.transmit)
    if not config.transmit:
        summary.notes.append(
            "DRY RUN — nothing is transmitted"
            + ("" if config.armed else f" ({LIVE_ARM_VAR} is not set to the arm token)")
        )
    if not is_trading_day(today) and not rehearse:
        summary.notes.append(f"MARKET HOLIDAY: {today.isoformat()} — no live run")
        return summary

    lock = acquire_run_lock(LOCK_NAME)
    if lock is None:
        summary.urgent.append("live run lock held — another live run is in progress; aborted without trading")
        return summary
    broker = broker_factory(config)
    try:
        try:
            broker.open()
        except BrokerError as exc:
            summary.broker_ok = False
            summary.urgent.append(f"live broker unavailable or refused: {exc}")
            async with session_maker() as session:
                await _audit(session, LIVE_BROKER_UNAVAILABLE, None, {"error": str(exc), "kind": type(exc).__name__})
                await session.commit()
            return summary
        try:
            async with session_maker() as session:
                await _run_session(session, broker, config, summary, today, rehearse)
        finally:
            broker.close()
    finally:
        release_run_lock(lock)
    return summary


async def _run_session(
    session: AsyncSession,
    broker: LiveBroker,
    config: LiveConfig,
    summary: LiveRunSummary,
    today: date,
    rehearse: bool,
) -> None:
    iso = today.isoformat()
    # 3. Order sync. The same restore-gap hold as the paper sync (#542/#650):
    # no reconciliation baseline, or a gap of more than one trading day,
    # holds UNKNOWN verdicts instead of expiring them.
    last_recon = (
        await session.execute(select(ReconciliationRunModel).order_by(ReconciliationRunModel.id.desc()).limit(1))
    ).scalar_one_or_none()
    gap = _market_days_between(last_recon.run_at, iso) if last_recon else None
    pending = await pending_share_orders(session)
    report = broker.reconcile([o.order_ref for o in pending])
    executions = tuple(broker.executions())
    summary.notes.extend(await sync_share_orders(session, pending, report, executions, gap))
    await session.commit()

    # 4. Data, reconciliation, the remote HALT channel.
    await persist_index_history(session)
    snapshot = BrokerSnapshot(
        positions=tuple(broker.positions()), executions=executions, open_orders=tuple(broker.open_orders())
    )
    recon = await run_reconciliation(session, snapshot, today=iso)
    if not recon.clean:
        summary.urgent.append(
            f"reconciliation DRIFT ({len(recon.drifts)} item(s), run #{recon.run_id}) — the live GLOBAL halt is "
            "latched; the live account must hold only what the live books hold, plus cash"
        )
    await apply_ntfy_commands(session)

    # 5. The sweep BEFORE any order: tonight's mark and the stake drawdown halt.
    # A rehearsal places nothing and may run on a weekend, so it writes no mark.
    if rehearse:
        summary.notes.append("rehearsal: the anomaly sweep (marks, drawdown halt) is not run")
    else:
        findings = await run_post_session_anomalies(session, iso, since=summary.run_started_at)
        summary.urgent.extend(format_anomaly_line(f) for f in findings)

    # 6. Judge the live books.
    eligible: list[tuple[BookModel, BookConfig]] = []
    books = (await session.execute(select(BookModel).order_by(BookModel.id))).scalars().all()
    for book in books:
        verdict = judge_live_book(book, await latest_grant(session, book.id))
        if verdict is None:
            continue
        if verdict.eligible:
            eligible.append((book, resolve_book_config(book.config)))
            continue
        await _refuse_book(session, book, verdict, summary)
    if not eligible:
        summary.notes.append("no live book is eligible tonight — nothing to rebalance")

    # 7. FLATTEN_REQUESTED covers every share holding in scope, live authority
    # or not: selling reduces risk, and a demoted book may still hold shares.
    drifted = frozenset(d.key for d in recon.drifts if d.sec_type == "STK" and d.kind in (ORPHAN, SHARE_DRIFT))
    if config.transmit:
        flatten = await run_share_flatten(session, PreviewingShareBroker(broker), today, drifted)
        summary.placed.extend(flatten.placed)
        summary.notes.extend(flatten.notes)
    else:
        await _dry_run_flatten(session, broker, today, drifted, summary)

    # 8. The month-end rebalance, per eligible book.
    for book, book_config in eligible:
        await _rebalance_live_book(session, broker, book, book_config, today, summary, config.transmit, rehearse)

    summary.notes.extend(await rebalance_watch_notes(session, today))
    await _audit(
        session,
        LIVE_RUN_SUMMARY,
        None,
        {
            "transmit": config.transmit,
            "armed": config.armed,
            "rehearse": rehearse,
            "eligible_books": [b.id for b, _ in eligible],
            "placed": list(summary.placed),
            "would_place": list(summary.would_place),
            "urgent": list(summary.urgent),
        },
    )
    await session.commit()


async def _refuse_book(
    session: AsyncSession, book: BookModel, verdict: LiveBookVerdict, summary: LiveRunSummary
) -> None:
    summary.urgent.append(f"{book.id} has live authority but is REFUSED: {verdict.reason}")
    await _audit(session, LIVE_BOOK_REFUSED, book.id, {"reason": verdict.reason})
    if verdict.diverged:
        # ADR-0014's amendment: a live book whose config moved off its
        # as-raced hash enters a book-scoped entries halt. Only an ACTIVE
        # scope is moved — never downgrade a FLATTEN_REQUESTED.
        await _audit(session, LIVE_HASH_DIVERGENCE, book.id, {"current_hash": book.config_hash})
        if await get_control_state(session, book.id) == ACTIVE:
            await set_control(
                session,
                book.id,
                HALT_ENTRIES,
                reason="live book config hash diverged from its grant (ADR-0014) — revert, or record a new grant",
                actor=ACTOR,
            )
    await session.commit()


# ---------------------------------------------------------------------------
# Previews, caps, and acting on a batch
# ---------------------------------------------------------------------------


def _describe(intent: ShareOrderIntent) -> str:
    return f"{intent.side} {intent.quantity} {intent.symbol} limit {intent.limit_price:.2f}"


async def _preview_all(
    broker: LiveBroker, intents: list[ShareOrderIntent]
) -> tuple[list[tuple[ShareOrderIntent, SharePreview]], str | None]:
    """Preview every intent; the first failure is the batch's refusal reason."""
    previews: list[tuple[ShareOrderIntent, SharePreview]] = []
    for intent in intents:
        try:
            preview = broker.preview_share_order(intent.symbol, intent.side, intent.quantity, intent.limit_price)
        except BrokerError as exc:
            return previews, f"{_describe(intent)}: preview refused ({exc})"
        if intent.side == "BUY":
            reason = check_buy_preview(preview)
            if reason:
                return previews, f"{_describe(intent)}: {reason}"
        previews.append((intent, preview))
    return previews, None


async def _gate_batch(
    session: AsyncSession,
    broker: LiveBroker,
    book: BookModel,
    intents: list[ShareOrderIntent],
    *,
    stake: float,
    investable: float,
    holdings: dict[str, int],
    book_cash: float,
    broker_cash: float | None,
    summary: LiveRunSummary,
    phase: str,
) -> list[tuple[ShareOrderIntent, SharePreview]] | None:
    """Caps, then every preview, then the no-debit rule. None (refused,
    audited, urgent) on the first failure — nothing in the batch is placed."""
    reason = check_order_caps(intents, stake, investable, holdings)
    previews: list[tuple[ShareOrderIntent, SharePreview]] = []
    if reason is None:
        previews, reason = await _preview_all(broker, intents)
    if reason is None:
        reason = check_no_debit([(i, p) for i, p in previews if i.side == "BUY"], book_cash, broker_cash)
    if reason is None:
        return previews
    summary.urgent.append(f"{book.id} live {phase} REFUSED, nothing placed: {reason}")
    await _audit(
        session,
        LIVE_ORDERS_REFUSED,
        book.id,
        {"phase": phase, "reason": reason, "orders": [_describe(i) for i in intents], "dry_run": not summary.transmit},
    )
    await session.commit()
    return None


async def _record_dry_run(
    session: AsyncSession,
    book_id: str,
    previews: list[tuple[ShareOrderIntent, SharePreview]],
    summary: LiveRunSummary,
    phase: str,
) -> None:
    for intent, preview in previews:
        line = f"{book_id} {phase}: would {_describe(intent)} (preview OK, commission max {preview.commission_max})"
        summary.would_place.append(line)
        await _audit(
            session,
            LIVE_DRY_RUN_ORDER,
            book_id,
            {
                "phase": phase,
                "symbol": intent.symbol,
                "side": intent.side,
                "quantity": intent.quantity,
                "limit": intent.limit_price,
                "decision_close": intent.decision_close,
                "init_margin_change": preview.init_margin_change,
                "equity_with_loan_after": preview.equity_with_loan_after,
                "init_margin_after": preview.init_margin_after,
                "commission_max": preview.commission_max,
            },
        )
    await session.commit()


async def _place_batch(
    session: AsyncSession,
    broker: LiveBroker,
    book: BookModel,
    intents: list[ShareOrderIntent],
    signal_iso: str,
    summary: LiveRunSummary,
) -> None:
    """Armed only: each order through share_book._place_one (STAGED row
    committed first, the choke-point control read immediately before
    placement) and the previewing broker. A halt or a broker error stops the
    rest, exactly as on paper."""
    result = RebalanceResult()
    wrapped = PreviewingShareBroker(broker)
    for intent in intents:
        if not await _place_one(session, wrapped, book, intent, signal_iso, result):
            # _place_one's last note names the halt or the broker refusal
            # that stopped the rest of the batch.
            summary.urgent.append(f"{book.id} live batch stopped: {result.notes.pop()}")
            break
    summary.placed.extend(result.placed)
    summary.notes.extend(result.notes)


async def _broker_cash(broker: LiveBroker, summary: LiveRunSummary, book_id: str) -> float | None:
    try:
        return broker.account_cash()
    except BrokerError as exc:
        summary.notes.append(f"{book_id}: broker cash unavailable ({exc}) — no buys tonight")
        return None


# ---------------------------------------------------------------------------
# Flatten (dry run) — the armed flatten is share_book.run_share_flatten itself
# ---------------------------------------------------------------------------


async def _dry_run_flatten(
    session: AsyncSession, broker: LiveBroker, today: date, drifted: frozenset[str], summary: LiveRunSummary
) -> None:
    """What run_share_flatten would sell tonight, previewed, never placed."""
    flatten_global, flatten_books = await _flatten_scopes(session)
    if not flatten_global and not flatten_books:
        return
    rows = (await session.execute(select(ShareHoldingModel))).scalars().all()
    books = {b.id: b for b in (await session.execute(select(BookModel))).scalars().all()}
    held = [r for r in rows if r.quantity >= 1 and (flatten_global or r.book_id in flatten_books)]
    closes = await _closes_by_symbol(session, {r.symbol for r in held})
    iso = today.isoformat()
    for row in sorted(held, key=lambda r: (r.book_id, r.symbol)):
        book = books.get(row.book_id)
        designated = resolve_book_config(book.config).share_symbols if book is not None else ()
        close = closes.get(row.symbol, {}).get(iso)
        if row.symbol not in designated or row.symbol in drifted or close is None or close <= 0:
            summary.would_place.append(f"FLATTEN {row.book_id} {row.symbol}: would be SKIPPED (see the armed rules)")
            continue
        intent = ShareOrderIntent(row.symbol, "SELL", math.floor(row.quantity), sell_limit(close), close)
        previews, reason = await _preview_all(broker, [intent])
        if reason:
            summary.urgent.append(f"FLATTEN {row.book_id} dry run: {reason}")
            continue
        await _record_dry_run(session, row.book_id, previews, summary, "flatten")


# ---------------------------------------------------------------------------
# The split month-end rebalance
# ---------------------------------------------------------------------------


async def _live_investable(session: AsyncSession, book: BookModel, stake: float, equity: float) -> float | None:
    """share_book._investable's sizing (#1074) without its audit side
    effects: stake plus P&L since the stake window opened, never more than
    equity. None when the baseline is unknown or the stake is exhausted."""
    window_start, fallback = await stake_window(session, book)
    marks = [
        (row.date, row.mtm)
        for row in (await session.execute(select(BookMtmHistoryModel).filter_by(book_id=book.id))).scalars().all()
    ]
    baseline = stake_baseline(marks, window_start, fallback)
    if baseline is None:
        return None
    investable = min(equity, stake + (equity - baseline))
    return investable if investable > 0 else None


async def _skip(
    session: AsyncSession, summary: LiveRunSummary, book_id: str, reason: str, signal_iso: str, transmit: bool
) -> None:
    summary.notes.append(f"{book_id} live rebalance SKIPPED — {reason}")
    # Only a real (armed) run writes the paper vocabulary's skip, which the
    # missed-rebalance digest line and the clean-rebalance count read.
    event = ETF_TREND_SKIPPED if transmit else "LIVE_DRY_RUN_SKIPPED"
    await _audit(session, event, book_id, {"reason": reason, "signal_date": signal_iso, "live": True})
    await session.commit()


async def _rebalance_live_book(
    session: AsyncSession,
    broker: LiveBroker,
    book: BookModel,
    config: BookConfig,
    today: date,
    summary: LiveRunSummary,
    transmit: bool,
    rehearse: bool,
) -> None:
    if is_signal_day(today):
        await _signal_phase(session, broker, book, config, today, summary, transmit)
    elif rehearse:
        await _signal_phase(session, broker, book, config, last_signal_day_on_or_before(today), summary, transmit)
    else:
        await _buy_phase(session, broker, book, config, today, summary, transmit)


async def _common_checks(
    session: AsyncSession,
    book: BookModel,
    symbols: tuple[str, ...],
    close_iso: str,
    iso: str,
    summary: LiveRunSummary,
    transmit: bool,
) -> tuple[dict[str, int], dict[str, float]] | None:
    """The halt, no pending orders, whole-share holdings, and a close dated
    *close_iso* for every held symbol. (holdings, closes_today) or None after
    a skip, which is recorded against the signal date *iso*."""
    try:
        await assert_entries_allowed(session, book.id, actor=ACTOR)
    except TradingHaltedError as halt:
        await _skip(session, summary, book.id, f"entries halted ({halt.scope}={halt.state})", iso, transmit)
        return None
    still_pending = [o for o in await pending_share_orders(session) if o.book_id == book.id]
    if still_pending:
        await _skip(session, summary, book.id, f"{len(still_pending)} share order(s) still pending", iso, transmit)
        return None
    raw = await _book_holdings(session, book.id)
    if any(abs(q - round(q)) > 1e-6 or q < 0 for q in raw.values()):
        await _skip(session, summary, book.id, "a holding is not a whole, non-negative share count", iso, transmit)
        return None
    holdings = {s: round(q) for s, q in raw.items()}
    closes = await _closes_by_symbol(session, (*symbols, *holdings))
    closes_today = {
        s: c
        for s in (*symbols, *holdings)
        if (c := closes.get(s, {}).get(close_iso)) is not None and math.isfinite(c) and c > 0
    }
    unpriced = sorted(s for s in holdings if s not in closes_today)
    if unpriced:
        await _skip(session, summary, book.id, f"no close for held symbol(s) {', '.join(unpriced)}", iso, transmit)
        return None
    return holdings, closes_today


async def _signal_phase(
    session: AsyncSession,
    broker: LiveBroker,
    book: BookModel,
    config: BookConfig,
    signal_day: date,
    summary: LiveRunSummary,
    transmit: bool,
) -> None:
    trend = config.etf_trend
    stake = config.stage1_stake
    assert trend is not None and stake is not None  # judge_live_book guarantees both
    iso = signal_day.isoformat()
    symbols = (*trend.menu, trend.cash_symbol)
    checked = await _common_checks(session, book, symbols, iso, iso, summary, transmit)
    if checked is None:
        return
    holdings, closes_today = checked
    all_closes = await _closes_by_symbol(session, trend.menu)
    readings = {s: trend_reading(s, all_closes.get(s, {}), signal_day, trend.trend_months) for s in trend.menu}
    await session.refresh(book, ["cash_balance"])
    cash = book.cash_balance
    equity = cash + sum(q * closes_today[s] for s, q in holdings.items())
    investable = await _live_investable(session, book, stake, equity)
    if investable is None:
        await _skip(session, summary, book.id, "stake baseline unknown or stake exhausted — not sized", iso, transmit)
        return
    try:
        targets = target_shares(readings, closes_today, trend.menu, trend.cash_symbol, investable)
    except ValueError as exc:
        await _skip(session, summary, book.id, str(exc), iso, transmit)
        return
    sells = rebalance_sells(holdings, targets, closes_today, trend.cash_symbol)
    broker_cash: float | None = None
    if sells:
        tonight = sells
    else:
        broker_cash = await _broker_cash(broker, summary, book.id)
        if broker_cash is None:
            await _skip(session, summary, book.id, "broker cash unavailable — buys not sized", iso, transmit)
            return
        tonight = size_live_buys(holdings, targets, closes_today, min(cash, broker_cash), trend.cash_symbol)
    signal_payload = {
        "signal_date": iso,
        "readings": {
            s: {"status": r.status, "close": r.close, "average": r.average, "missing": list(r.missing_dates)}
            for s, r in readings.items()
        },
        "equity": round(equity, 2),
        "investable": round(investable, 2),
        "cash": round(cash, 2),
        "current": holdings,
        "targets": targets,
        "orders": [
            {"symbol": o.symbol, "side": o.side, "quantity": o.quantity, "limit": o.limit_price} for o in tonight
        ],
        "live": True,
        # Sells go first; the buys are sized on the next run from booked fills.
        "live_buys_deferred": bool(sells),
    }
    phase = "sells (buys follow once they fill)" if sells else "buys"
    previews = await _gate_batch(
        session,
        broker,
        book,
        tonight,
        stake=stake,
        investable=investable,
        holdings=holdings,
        book_cash=cash,
        broker_cash=broker_cash,
        summary=summary,
        phase=phase,
    )
    if not transmit:
        await _audit(session, LIVE_DRY_RUN_SIGNAL, book.id, signal_payload)
        await session.commit()
        if previews is not None:
            await _record_dry_run(session, book.id, previews, summary, phase)
        if sells:
            await _dry_run_estimated_buys(
                session, broker, book, holdings, targets, closes_today, sells, cash, trend.cash_symbol, summary
            )
        if not tonight:
            summary.notes.append(f"{book.id} live (dry run): holdings already on target — no orders")
        return
    # A refused batch is not a signal that ran: skip, so the missed-rebalance
    # line names the refusal every night until the next month-end.
    if previews is None:
        await _skip(session, summary, book.id, "the live order batch was refused (see the urgent push)", iso, transmit)
        return
    await _audit(session, ETF_TREND_SIGNAL, book.id, signal_payload)
    if sells:
        await _audit(session, LIVE_BUYS_DEFERRED, book.id, {"signal_date": iso, "targets": targets})
    await session.commit()
    if not tonight:
        summary.notes.append(f"{book.id} live: holdings already on target — no orders")
        return
    await _place_batch(session, broker, book, tonight, iso, summary)


async def _dry_run_estimated_buys(
    session: AsyncSession,
    broker: LiveBroker,
    book: BookModel,
    holdings: dict[str, int],
    targets: dict[str, int],
    closes_today: dict[str, float],
    sells: list[ShareOrderIntent],
    cash: float,
    cash_symbol: str,
    summary: LiveRunSummary,
) -> None:
    """Dry run only: the buys the next run would size if every sell filled at
    its limit — previewed so the operator sees the whole month, labelled an
    estimate. An armed run never sizes buys from unfilled sells."""
    broker_cash = await _broker_cash(broker, summary, book.id)
    if broker_cash is None:
        return
    after = dict(holdings)
    for sell in sells:
        after[sell.symbol] = after.get(sell.symbol, 0) - sell.quantity
    proceeds = sum(s.quantity * s.limit_price for s in sells)
    estimated = size_live_buys(after, targets, closes_today, min(cash, broker_cash) + proceeds, cash_symbol)
    previews, reason = await _preview_all(broker, estimated)
    if reason:
        summary.notes.append(f"{book.id} dry run, ESTIMATED buys (sized before the sells fill): {reason}")
    await _record_dry_run(session, book.id, previews, summary, "estimated buys (after sells fill)")


async def _buy_phase(
    session: AsyncSession,
    broker: LiveBroker,
    book: BookModel,
    config: BookConfig,
    today: date,
    summary: LiveRunSummary,
    transmit: bool,
) -> None:
    """The second half of a split rebalance: the buys, sized from cash that
    exists after the signal day's sells terminalized and were booked."""
    trend = config.etf_trend
    stake = config.stage1_stake
    assert trend is not None and stake is not None
    signal = last_signal_day_on_or_before(today)
    signal_iso = signal.isoformat()
    events = (
        (
            await session.execute(
                select(AuditEventModel)
                .filter(
                    AuditEventModel.book_id == book.id,
                    AuditEventModel.event_type.in_((ETF_TREND_SIGNAL, LIVE_BUY_PHASE_CLOSED)),
                )
                .order_by(AuditEventModel.id)
            )
        )
        .scalars()
        .all()
    )
    for_signal = [e for e in events if (e.payload or {}).get("signal_date") == signal_iso]
    plans = [e for e in for_signal if e.event_type == ETF_TREND_SIGNAL and (e.payload or {}).get("live_buys_deferred")]
    if not plans or any(e.event_type == LIVE_BUY_PHASE_CLOSED for e in for_signal):
        return
    bought = (
        await session.execute(
            select(ShareOrderModel.id).filter(
                ShareOrderModel.book_id == book.id,
                ShareOrderModel.signal_date == signal_iso,
                ShareOrderModel.side == "BUY",
                ShareOrderModel.purpose == SHARE_ORDER_PURPOSE_REBALANCE,
            )
        )
    ).first()
    if bought is not None:
        return
    lag = trading_days_between(signal, today)
    if lag > LIVE_BUY_PHASE_MAX_LAG_TRADING_DAYS:
        await _close_buy_phase(
            session,
            summary,
            book.id,
            signal_iso,
            f"the buy run is {lag} trading days after the signal — too late; the cash waits for the next month-end",
            transmit,
            urgent=True,
        )
        return
    raw_targets = plans[-1].payload.get("targets") or {}
    if not isinstance(raw_targets, dict) or not all(
        isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in raw_targets.values()
    ):
        await _close_buy_phase(
            session, summary, book.id, signal_iso, "the recorded plan's targets are malformed", transmit, urgent=True
        )
        return
    targets: dict[str, int] = dict(raw_targets)
    checked = await _common_checks(session, book, tuple(targets), today.isoformat(), signal_iso, summary, transmit)
    if checked is None:
        return
    holdings, closes_today = checked
    missing = sorted(s for s, t in targets.items() if t > holdings.get(s, 0) and s not in closes_today)
    if missing:
        await _skip(session, summary, book.id, f"no close today for {', '.join(missing)}", signal_iso, transmit)
        return
    await session.refresh(book, ["cash_balance"])
    cash = book.cash_balance
    equity = cash + sum(q * closes_today[s] for s, q in holdings.items())
    investable = await _live_investable(session, book, stake, equity)
    if investable is None:
        await _skip(session, summary, book.id, "stake baseline unknown or stake exhausted", signal_iso, transmit)
        return
    broker_cash = await _broker_cash(broker, summary, book.id)
    if broker_cash is None:
        return
    buys = size_live_buys(holdings, targets, closes_today, min(cash, broker_cash), trend.cash_symbol)
    if not buys:
        await _close_buy_phase(
            session, summary, book.id, signal_iso, "nothing to buy within the cash available", transmit
        )
        return
    previews = await _gate_batch(
        session,
        broker,
        book,
        buys,
        stake=stake,
        investable=investable,
        holdings=holdings,
        book_cash=cash,
        broker_cash=broker_cash,
        summary=summary,
        phase="buys (after the sells filled)",
    )
    if previews is None:
        return
    if not transmit:
        await _record_dry_run(session, book.id, previews, summary, "buys (after the sells filled)")
        return
    await _place_batch(session, broker, book, buys, signal_iso, summary)
    await _close_buy_phase(session, summary, book.id, signal_iso, "buys placed", transmit)


async def _close_buy_phase(
    session: AsyncSession,
    summary: LiveRunSummary,
    book_id: str,
    signal_iso: str,
    reason: str,
    transmit: bool,
    urgent: bool = False,
) -> None:
    line = f"{book_id} live buys for the {signal_iso} rebalance: {reason}"
    (summary.urgent if urgent else summary.notes).append(line)
    if not transmit:
        return  # a dry run never closes a real month's buy phase
    await _audit(session, LIVE_BUY_PHASE_CLOSED, book_id, {"signal_date": signal_iso, "reason": reason})
    await session.commit()


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------


def compose_live_digest(summary: LiveRunSummary) -> tuple[str, str, str]:
    """(title, body, priority) for the live run's own push — separate from
    the paper digest, and titled so a dry run can never be mistaken for an
    armed one."""
    mode = "ARMED" if summary.transmit else "DRY RUN"
    title = f"basis LIVE {mode} {summary.run_date}"
    lines: list[str] = []
    if summary.urgent:
        lines.append("URGENT:")
        lines.extend(f"- {u}" for u in summary.urgent)
    if summary.would_place:
        lines.append("Would place (nothing transmitted):")
        lines.extend(f"- {w}" for w in summary.would_place)
    if summary.placed:
        lines.append(f"Placed {len(summary.placed)} order(s).")
    lines.extend(summary.notes)
    if not summary.broker_ok:
        lines.append("Broker session not opened — nothing ran.")
    return title, "\n".join(lines) or "Nothing to do.", ("urgent" if summary.urgent else "default")


def live_mode_env_ok() -> bool:
    """The import-order cross-check: the mode the database module captured
    at import and the mode in the environment now must both be live."""
    return TRADING_MODE == "live" and (os.environ.get("IBKR_TRADING_MODE") or "").strip().lower() == "live"

"""executor.py — the Executor (Paper) nightly pipeline (design §7 item 11, #70).

Order of operations, per the sequencing rule "manage what you hold before
adding risk" and the reconciliation-first mandate:

1. Open the broker session (paper-only guard; unreachable Gateway = audited
   failure, no orders, heartbeat still written — silent non-operation is the
   worst failure mode).
2. Sync order state by orderRef: yesterday's fills become positions with
   book-cash adjustment; cancelled/expired orders release their encumbrance;
   STAGED intents absent at the broker EXPIRE (resolved decision 4 — the
   evening's prices are stale by the next session).
3. Reconciliation (backfill + drift classification). Drift latches a global
   HALT_ENTRIES; exits still run.
4. Market refresh, index history, all regime-variant readings, ntfy HALT poll.
5. Layer A: P1 positions get closing SELL combos at a marketable limit.
6. Layer C per lab book: variant regime → scan → spec → live-quote pricing →
   book gates → stage (encumber) → control check at the choke point → place
   with a GTC profit-taker resting server-side at IBKR.
7. Heartbeat.

Entries are DAY limits placed after hours: they work the NEXT trading
session and IBKR expires them at that session's close — which implements
UNFILLED_ENTRY (entries never rest beyond one session) without a resting
cancel. GTC belongs to profit-taker children only.

Timing note: the nightly cadence means the close-order escalation ladder
advances one rung per evening (mid + growing concession), not per 5 minutes
— the supervision spec's intraday ladder applies to human-initiated FLATTEN.
"""

import asyncio
import logging
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.anomaly import DUPLICATE_ORDER, _market_days_between, check_duplicate_order, run_post_session_anomalies
from backend.book_gates import (
    PENDING_ORDER_STATUSES,
    BookConfig,
    CandidateOrder,
    Envelope,
    evaluate_book_gates,
    release_order,
    resolve_book_config,
    stage_order,
)
from backend.broker import BrokerError, BrokerSession, RefState, SpreadOrder
from backend.calendars import is_trading_day, stale_calendars
from backend.console import heartbeat_path
from backend.database import async_session_maker
from backend.dates import market_evening_window_start, market_today
from backend.market_data import fetch_options_latest_quotes, format_occ_symbol
from backend.models import (
    AuditEventModel,
    BookModel,
    ClosurePostMortemModel,
    FillModel,
    MarketStateModel,
    OrderModel,
    PlaybookDefinitionModel,
    PlaybookDefinitionSchema,
    PortfolioConfigModel,
    PortfolioConfigSchema,
    PositionModel,
    ReconciliationRunModel,
    TradingControlModel,
)
from backend.observation import calculate_dte, run_lifecycle_scan
from backend.operator import (
    persist_index_history,
    refresh_market_state,
    refresh_position_values,
)
from backend.opportunity import generate_trade_spec, scan_opportunities
from backend.reconciliation import BrokerSnapshot, _backfill_missed_fills, run_reconciliation
from backend.regime_variants import INSUFFICIENT_DATA, persist_regime_readings, underlying_telemetry
from backend.run_lock import acquire_run_lock, release_run_lock
from backend.telemetry import telemetry_key
from backend.trading_control import (
    FLATTEN_REQUESTED,
    GLOBAL_SCOPE,
    HALT_ENTRIES,
    TradingHaltedError,
    apply_ntfy_commands,
    assert_entries_allowed,
    set_control,
)

logger = logging.getLogger(__name__)

CLOSE_CONCESSION_PER_RUNG = 0.15  # each evening a close reworks 15% closer to natural
MAX_CLOSE_RUNGS = 5  # beyond this the ladder stops conceding and escalates to a human (#280)
STALE_MARK_MAX_HOURS = 30.0  # a close limit needs a mark fresher than one missed session (#280)


@dataclass(frozen=True)
class BlockedEntry:
    """One blocked entry crossing the executor→digest seam as data — the
    digest formats and groups these; nobody re-parses a string. book_id None
    means the block applies run-wide (e.g. stale telemetry)."""

    book_id: str | None
    reason: str


@dataclass
class ExecutorRunSummary:
    broker_ok: bool = True
    reconciliation: str = "SKIPPED"
    # UTC ISO timestamp of run start (#259): "tonight's events" everywhere is
    # run_at >= this, never a date-prefix match that breaks at UTC midnight.
    run_started_at: str = ""
    # The market date this run ran under (America/New_York, computed once).
    run_date: str = ""
    positions_created: list[str] = field(default_factory=list)
    intents_expired: list[str] = field(default_factory=list)
    closes_placed: list[str] = field(default_factory=list)
    entries_placed: list[str] = field(default_factory=list)
    entries_blocked: list[BlockedEntry] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _audit(session: AsyncSession, event_type: str, book_id: str | None, payload: dict) -> None:
    session.add(
        AuditEventModel(run_at=_now(), book_id=book_id, event_type=event_type, actor="executor", payload=payload)
    )


def _write_heartbeat(summary: ExecutorRunSummary) -> None:
    """The dead-man watchdog (#72) checks this file's timestamp."""
    import json

    heartbeat_path().write_text(
        json.dumps(
            {
                "at": _now(),
                "broker_ok": summary.broker_ok,
                "reconciliation": summary.reconciliation,
                "entries_placed": len(summary.entries_placed),
                "closes_placed": len(summary.closes_placed),
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Phase 2 — order-state sync
# ---------------------------------------------------------------------------


async def _sync_order_states(session: AsyncSession, broker, summary: ExecutorRunSummary) -> None:
    pending = (
        (await session.execute(select(OrderModel).filter(OrderModel.status.in_(PENDING_ORDER_STATUSES))))
        .scalars()
        .all()
    )
    if not pending:
        broker.reconcile([])
        return
    report = broker.reconcile([o.order_ref for o in pending])
    executions = tuple(broker.executions())
    await _backfill_missed_fills(session, executions)

    # Entries before closes: an entry and its profit-taker child (#258) can
    # both fill the same day, and the child's CLOSE can only settle after the
    # parent's fill has created and linked the position.
    for order in sorted(pending, key=lambda o: o.action != "OPEN"):
        if order.status == "PARTIAL":
            continue  # latched for a human (#283) — never re-processed, never re-alerted
        state = report.state(order.order_ref)
        if state is RefState.FILLED:
            await _order_to_position(session, order, summary)
        elif state is RefState.CANCELLED:
            # A cancelled order that EXECUTED something first is a partial
            # fill (#283, audit M1): booking it at full intended size would
            # corrupt cash and reconciliation both. Latch PARTIAL (keeps its
            # encumbrance), halt the book, and leave correction to a human —
            # the no-auto-adjust principle applies to sizes too.
            fills = (await session.execute(select(FillModel).filter_by(order_id=order.id))).scalars().all()
            if fills:
                order.status = "PARTIAL"
                await _audit(
                    session,
                    "PARTIAL_FILL",
                    order.book_id,
                    {"order_ref": order.order_ref, "executions": len(fills)},
                )
                await set_control(
                    session,
                    order.book_id,
                    HALT_ENTRIES,
                    reason=f"PARTIAL_FILL: {order.order_ref} cancelled with {len(fills)} execution(s)",
                    actor="anomaly",
                )
                continue
            order.status = "CANCELLED"
            order.completed_at = _now()
            await _audit(session, "ORDER_EXPIRED_AT_BROKER", order.book_id, {"order_ref": order.order_ref})
        elif state is RefState.UNKNOWN and order.status == "STAGED":
            # Crash before submission: expire, never resubmit at stale prices.
            order.status = "CANCELLED"
            order.completed_at = _now()
            summary.intents_expired.append(order.order_ref)
            await _audit(session, "INTENT_EXPIRED", order.book_id, {"order_ref": order.order_ref})
        elif state is RefState.UNKNOWN:
            order.status = "CANCELLED"
            order.completed_at = _now()
            # A resting order on an expired position vanished WITH its
            # contracts — IB purges both together. Expected, not urgent (#261).
            pos = await session.get(PositionModel, order.position_id) if order.position_id else None
            if pos is not None and pos.expiration_date and pos.expiration_date <= summary.run_date:
                await _audit(session, "ORDER_EXPIRED_AT_BROKER", order.book_id, {"order_ref": order.order_ref})
            else:
                await _audit(
                    session, "ORDER_LOST_AT_BROKER", order.book_id, {"order_ref": order.order_ref, "was": "SUBMITTED"}
                )
        # OPEN: still working its next-session window — leave it counted.
    await session.commit()


def _post_mortem(
    pos: PositionModel, exit_value_per_share: float, exit_trigger: str, exit_date: str
) -> ClosurePostMortemModel:
    """The expectancy evidence row (ADR-0006): every executor-side closure
    writes one, or the Live Gate's per-trade record silently never accrues.
    Realized P&L uses the same convention as the console close endpoint."""
    if pos.premium_direction == "DEBIT":
        realized = (exit_value_per_share - pos.entry_premium) * 100 * pos.contracts
    else:
        realized = (pos.entry_premium - exit_value_per_share) * 100 * pos.contracts
    realized = round(realized, 2)
    outcome = "WIN" if realized > 0.01 else "LOSS" if realized < -0.01 else "BREAKEVEN"
    return ClosurePostMortemModel(
        id=str(uuid.uuid4()),
        position_id=pos.id,
        outcome=outcome,
        realized_pnl=realized,
        actual_underlying_move_pct=0.0,  # not tracked on autonomous exits
        exit_date=exit_date,
        exit_trigger=exit_trigger,
        lesson_tags=[],
        user_override_logged=False,
        playbook_id=pos.playbook_id,
        playbook_version=pos.playbook_version,
    )


async def _settle_expired(session: AsyncSession, summary: ExecutorRunSummary) -> None:
    """Cash-settle OPEN positions whose expiration has passed (#261, audit C4).

    Runs after the fill sync (a final-day close fill must settle as a fill,
    not an expiry) and before reconciliation/Layer A. Settlement value is the
    LAST MARK (current_value_per_share): quotes for expired contracts are
    gone, index_history has no XSP, and the mark came from real option quotes
    on the final priced evening. A mark carries residual time value, so
    credit buy-backs settle slightly rich — a conservative expectancy bias.
    Any order still resting on the position died with its contracts at IB."""
    cutoff = summary.run_date
    rows = (await session.execute(select(PositionModel).filter_by(status="OPEN"))).scalars().all()
    settled = 0
    for pos in rows:
        if pos.book_id == "B00" or not pos.expiration_date or pos.expiration_date > cutoff:
            continue
        value = pos.current_value_per_share
        book = await session.get(BookModel, pos.book_id)
        if book is not None:
            book.cash_balance += (value if pos.premium_direction == "DEBIT" else -value) * 100 * pos.contracts
        pos.status = "EXPIRED"
        session.add(_post_mortem(pos, value, "EXPIRY", cutoff))
        resting = (
            (
                await session.execute(
                    select(OrderModel).filter(
                        OrderModel.position_id == pos.id, OrderModel.status.in_(PENDING_ORDER_STATUSES)
                    )
                )
            )
            .scalars()
            .all()
        )
        for stale in resting:
            stale.status = "CANCELLED"
            stale.completed_at = _now()
            await _audit(session, "ORDER_EXPIRED_AT_BROKER", pos.book_id, {"order_ref": stale.order_ref})
        await _audit(
            session,
            "POSITION_EXPIRED",
            pos.book_id,
            {"position_id": pos.id, "settled_value_per_share": value, "expiration": pos.expiration_date},
        )
        settled += 1
    if settled:
        summary.notes.append(f"{settled} position(s) cash-settled at expiry (at last mark)")
    await session.commit()


async def _order_to_position(session: AsyncSession, order: OrderModel, summary: ExecutorRunSummary) -> None:
    """A filled entry order becomes a PositionModel; a filled close order
    closes its position. Book cash adjusts by the order's limit economics
    (fill-price refinement rides on the fills ledger; positions reprice
    nightly from live quotes)."""
    meta = order.combo_legs or {}
    quantity = int(meta.get("quantity", 1))
    book = await session.get(BookModel, order.book_id)

    if order.action == "CLOSE":
        if order.position_id:
            pos = await session.get(PositionModel, order.position_id)
            if pos is not None and pos.status == "OPEN":
                pos.status = "CLOSED"
                # The exit price IS the final mark (#280, audit H4): console
                # realized P&L recomputes from current_value_per_share, which
                # must agree with the post-mortem, not a stale quote.
                pos.current_value_per_share = abs(order.limit_price)
                pos.last_priced_at = _now()
                # Every executor closure writes its expectancy row (#261);
                # the trigger was stamped when the close was staged.
                exit_date = summary.run_date or market_today().isoformat()
                session.add(_post_mortem(pos, abs(order.limit_price), meta.get("exit_trigger", "MANUAL"), exit_date))
        if book is not None:
            # SELL-the-bag convention: the close's limit_price IS the signed
            # cash flow per share — negative when buying back a credit spread
            # (cash out), positive when selling out of a debit spread (cash
            # in). The old `* -1` inverted this and CREDITED every buy-back
            # cost, inflating the book by 2× the exit value per close (#257).
            book.cash_balance += order.limit_price * 100 * quantity
        order.status = "FILLED"
        order.completed_at = _now()
        await _audit(session, "CLOSE_FILLED", order.book_id, {"order_ref": order.order_ref})
        return

    pos_id = f"pos_{order.id}"
    if await session.get(PositionModel, pos_id) is None:
        legs = meta.get("legs", [])
        net = order.limit_price  # negative = credit
        max_loss_ps = order.encumbered_risk / (100 * quantity) if quantity else 0.0
        session.add(
            PositionModel(
                id=pos_id,
                underlying=meta.get("underlying", "?"),
                strategy_type=meta.get("strategy_type", "?"),
                execution_mode="PAPER",
                legs=[
                    {
                        "option_type": leg["option_type"],
                        "direction": leg["direction"],
                        "strike": leg["strike"],
                        "expiration": leg["expiration"],
                        "delta": 0.0,
                        "theta": 0.0,
                        "vega": 0.0,
                        "gamma": 0.0,
                    }
                    for leg in legs
                ],
                entry_date=market_today().isoformat(),
                expiration_date=meta.get("expiration_date", ""),
                entry_premium=abs(net),
                premium_direction="CREDIT" if net < 0 else "DEBIT",
                current_value_per_share=abs(net),
                contracts=quantity,
                max_profit=abs(net) if net < 0 else 999999.0,
                max_loss=max_loss_ps,
                notes=f"Executor entry {order.order_ref}",
                rolls=0,
                status="OPEN",
                journal={
                    "core_thesis_rationale": f"Autonomous entry per playbook (order {order.order_ref})",
                    "structural_invalidation": "Playbook exit rules govern",
                    "expected_underlying_move_pct": 0.0,
                    "pre_trade_emotional_state": "Calm",
                    "pre_trade_confidence_rating": 3,
                    # The regime this entry was decided under (B28, #254).
                    "entry_regime": meta.get("entry_regime", ""),
                },
                playbook_id=meta.get("playbook_id"),
                playbook_version=meta.get("playbook_version"),
                playbook_snapshot=meta.get("playbook_snapshot"),
                # The config fingerprint this trade raced under (#284, M5):
                # a mid-race config change must split the evidence, not pool it.
                config_hash=book.config_hash if book is not None else None,
                book_id=order.book_id,
            )
        )
        order.position_id = pos_id
        # Adopt the profit-taker child (#258): it was staged before the
        # position existed, so its fill can only settle once it knows whose
        # exit it is.
        tp = (
            await session.execute(select(OrderModel).filter_by(order_ref=f"{order.order_ref}:tp"))
        ).scalar_one_or_none()
        if tp is not None:
            tp.position_id = pos_id
        if book is not None:
            book.cash_balance += -net * 100 * quantity  # credit received (or debit paid)
        summary.positions_created.append(pos_id)
        await _audit(session, "ENTRY_FILLED", order.book_id, {"order_ref": order.order_ref, "position_id": pos_id})
    order.status = "FILLED"
    order.completed_at = _now()


# ---------------------------------------------------------------------------
# Phase 5 — Layer A closes
# ---------------------------------------------------------------------------


async def _layer_a_closes(
    session: AsyncSession,
    broker,
    state: MarketStateModel,
    summary: ExecutorRunSummary,
    today: date,
    readings: dict[str, str] | None = None,
) -> None:
    open_positions = (await session.execute(select(PositionModel).filter_by(status="OPEN"))).scalars().all()
    # Non-SPY-scale closes for the ex-div assignment defense (#130).
    non_spy = sorted({p.underlying for p in open_positions if p.underlying not in ("SPY", "XSP")})
    prices, _, _ = await underlying_telemetry(session, non_spy)
    # FLATTEN_REQUESTED (#281): the kill switch's third state finally does
    # something — every OPEN position in a flattened scope closes tonight,
    # regardless of what the lifecycle scan thinks. Entries in that scope are
    # already blocked (any non-ACTIVE state fails the choke point).
    controls = {row.scope: row.state for row in (await session.execute(select(TradingControlModel))).scalars().all()}
    flatten_global = controls.get(GLOBAL_SCOPE) == FLATTEN_REQUESTED
    book_configs: dict[str, BookConfig] = {}
    for pos in open_positions:
        if pos.book_id == "B00":
            continue  # legacy/manual book is never traded by the executor
        if flatten_global or controls.get(pos.book_id) == FLATTEN_REQUESTED:
            # Same ladder, same stale-mark guard as every other close — a
            # flatten is a limit order placed tonight, not a market order.
            scope = GLOBAL_SCOPE if flatten_global else pos.book_id
            scan = {"priority": "P1_FLATTEN", "reason": f"FLATTEN_REQUESTED on {scope}"}
        else:
            scan = run_lifecycle_scan(
                pos.to_schema(),
                current_regime=state.current_regime,
                spy_price=state.spy_price,
                catalyst_dates=state.catalyst_dates or [],
                today=today,
                underlying_prices=prices,
            )
        if not scan["priority"].startswith("P1"):
            # B28's regime-flip exit (#254): a flagged book closes positions
            # whose current variant regime left the state they were entered
            # under — the exit-side question no entry gate can ask.
            if pos.book_id not in book_configs:
                book = await session.get(BookModel, pos.book_id)
                book_configs[pos.book_id] = resolve_book_config(book.config if book else None)
            cfg = book_configs[pos.book_id]
            entry_regime = (pos.journal or {}).get("entry_regime") or ""
            current = (readings or {}).get(cfg.variant or "V0")
            # Mandatory time exit (#260, audit C3): the scan classifies the
            # DTE rule as P2 ("review") — right for the manual workbench,
            # meaningless in an unattended pipeline where nobody reviews.
            # "Mandatory" means the executor closes. The threshold comes from
            # the position's own frozen playbook snapshot, so per-book exit
            # overrides (B26's 75% PT arm, a future DTE arm) are honored.
            exit_dte = ((pos.playbook_snapshot or {}).get("exit_rules") or {}).get("mandatory_exit_dte", 21)
            dte = calculate_dte(pos.expiration_date, today)
            if (
                cfg.exit_on_regime_flip
                and entry_regime
                and current
                and current != INSUFFICIENT_DATA
                and current != entry_regime
            ):
                scan = {
                    "priority": "P1_REGIME_FLIP",
                    "reason": f"REGIME_FLIP: entered under {entry_regime}, now {current}",
                }
            elif dte <= exit_dte:
                scan = {
                    "priority": "P1_TIME_EXIT",
                    "reason": f"TIME_EXIT: {dte} DTE <= mandatory {exit_dte} DTE",
                }
            else:
                continue
        # Stale-mark guard (#280, audit M3): entries are stale-guarded, exits
        # were not — a close limit derived from a mark of unknown age chases
        # the market with garbage. Skip the close, alert, retry tomorrow once
        # repricing works. (Tonight's reprice ran BEFORE Layer A, so a fresh
        # mark is minutes old; anything beyond one missed session is stale.)
        mark_age_ok = False
        if pos.last_priced_at:
            try:
                priced = datetime.fromisoformat(pos.last_priced_at)
                mark_age_ok = (datetime.now(UTC) - priced).total_seconds() <= STALE_MARK_MAX_HOURS * 3600
            except ValueError:
                mark_age_ok = False
        if not mark_age_ok:
            await _audit(
                session,
                "STALE_MARK_CLOSE_SKIPPED",
                pos.book_id,
                {"position_id": pos.id, "last_priced_at": pos.last_priced_at, "reason": scan["reason"]},
            )
            await session.commit()
            continue
        prior_closes = (
            (await session.execute(select(OrderModel).filter_by(position_id=pos.id, action="CLOSE"))).scalars().all()
        )
        tp_rows = [o for o in prior_closes if o.order_ref.endswith(":tp")]
        rung = len(prior_closes) - len(tp_rows)
        # Ladder cap (#280): concessions grew without bound — beyond
        # MAX_CLOSE_RUNGS evenings the market is telling us something a
        # bigger concession won't fix. Stop conceding, escalate to a human.
        if rung >= MAX_CLOSE_RUNGS:
            await _audit(
                session,
                "CLOSE_LADDER_EXHAUSTED",
                pos.book_id,
                {"position_id": pos.id, "rungs": rung, "reason": scan["reason"]},
            )
            await session.commit()
            continue
        # The resting GTC profit-taker must come down before a manual close
        # goes up (#258) — two live exits on the same legs is a double-close
        # waiting to happen. Cancel-first: if the close placement then fails,
        # the position is briefly unprotected and Layer A retries tomorrow.
        # The TP row is not an escalation rung — it never chased the market.
        for tp in tp_rows:
            if tp.status in PENDING_ORDER_STATUSES:
                found = broker.cancel_by_ref(tp.order_ref)
                tp.status = "CANCELLED"
                tp.completed_at = _now()
                await _audit(
                    session, "TP_CANCELLED", pos.book_id, {"order_ref": tp.order_ref, "found_at_broker": found}
                )
        concession = 1.0 + CLOSE_CONCESSION_PER_RUNG * rung
        # SELL-the-bag convention: closing a credit position pays (negative
        # price); closing a debit position receives (positive price).
        if pos.premium_direction == "CREDIT":
            limit_price = round(-pos.current_value_per_share * concession, 2)
        else:
            limit_price = round(pos.current_value_per_share / concession, 2)
        # The closing bag MIRRORS the entry bag (SHORT leg = SELL, LONG = BUY);
        # the SELL order action on the bag is what reverses the position.
        # Duplicate leg entries (a BWB body stores its ratio expanded, #132)
        # re-aggregate into one combo leg with the summed ratio — IBKR combos
        # take a ratio per conId, not repeated identical legs.
        leg_counts: dict[tuple[str, str], int] = {}
        for leg in pos.legs:
            key = (
                format_occ_symbol(pos.underlying, leg["expiration"], leg["option_type"], leg["strike"]),
                "SELL" if leg["direction"] == "SHORT" else "BUY",
            )
            leg_counts[key] = leg_counts.get(key, 0) + 1
        legs = tuple((occ, action, n) for (occ, action), n in leg_counts.items())
        order_id = f"o_{uuid.uuid4().hex[:8]}"
        ref = f"basis:{pos.book_id}:{order_id}:close"
        spread = SpreadOrder(legs=legs, quantity=pos.contracts, net_limit_price=limit_price, underlying=pos.underlying)
        # The post-mortem trigger travels on the order (#261): the scan that
        # justified this close won't be re-runnable when the fill lands.
        reason = scan["reason"]
        if scan["priority"] == "P1_REGIME_FLIP":
            trigger = "REGIME_FLIP"
        elif scan["priority"] == "P1_TIME_EXIT":
            trigger = "TIME_RULE"
        elif scan["priority"] == "P1_FLATTEN":
            trigger = "MANUAL"  # a human requested the flatten (#281)
        elif reason.startswith("Profit target"):
            trigger = "PROFIT_TARGET"
        elif reason.startswith("Loss limit"):
            trigger = "LOSS_LIMIT"
        else:
            trigger = "ASSIGNMENT_RISK"  # the only remaining P1 (ex-div defense)
        order = OrderModel(
            id=order_id,
            book_id=pos.book_id,
            position_id=pos.id,
            order_ref=ref,
            ib_order_id=None,
            ib_perm_id=None,
            action="CLOSE",
            combo_legs={"legs": [dict(l) for l in pos.legs], "quantity": pos.contracts, "exit_trigger": trigger},
            order_type="LIMIT",
            limit_price=limit_price,
            decision_midpoint=limit_price,
            status="STAGED",
            submitted_at=None,
            completed_at=None,
            encumbered_risk=0.0,  # closes reduce risk — no encumbrance
        )
        session.add(order)
        await session.commit()
        try:
            placed = broker.close_spread(spread, ref)
        except BrokerError as exc:
            order.status = "REJECTED"
            order.completed_at = _now()
            await _audit(session, "CLOSE_REJECTED", pos.book_id, {"order_ref": ref, "error": str(exc)})
            await session.commit()
            continue
        order.status = "SUBMITTED"
        order.submitted_at = _now()
        order.ib_order_id = placed.order_id
        order.ib_perm_id = placed.perm_id
        summary.closes_placed.append(ref)
        await _audit(
            session,
            "CLOSE_SUBMITTED",
            pos.book_id,
            {"order_ref": ref, "reason": scan["reason"], "rung": rung, "limit": limit_price},
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Phase 6 — Layer C entries per book
# ---------------------------------------------------------------------------


def _book_scan_config(base: PortfolioConfigModel, envelope: Envelope) -> PortfolioConfigSchema:
    """Clone the portfolio config with the book's envelope numbers so the
    Layer C scan gates and the book gates agree (the book gates remain the
    authority; this keeps the scan from pre-blocking at the wrong caps).
    Both layers read the same resolved Envelope, so they can never drift."""
    schema = base.to_schema()
    risk = schema.risk_profile.model_copy(
        update={
            "max_simultaneous_positions": envelope.max_positions,
            "max_capital_deployed_pct": envelope.max_deployed_pct,
            "max_trade_risk_dollars": envelope.basis * envelope.max_loss_pct_per_trade / 100.0,
            "max_trade_risk_pct": envelope.max_loss_pct_per_trade,
        }
    )
    return schema.model_copy(update={"risk_profile": risk})


def _book_playbooks(playbooks: list[PlaybookDefinitionSchema], config: BookConfig) -> list[PlaybookDefinitionSchema]:
    """Apply a book's playbook selection and overrides (#136 experiment arms).

    playbook_ids: optional whitelist — the book scans only those.
    playbook_overrides: optional dot-keyed field overrides applied to every
    selected playbook (e.g. {"execution_specs.target_dte": 24}), revalidated
    through the schema so a bad override fails loudly at scan time, not at
    order time. Both feed the book's config_hash, so every arm is
    fingerprinted (ADR-0003 pattern).
    """
    ids = config.playbook_ids
    selected = [pb for pb in playbooks if not ids or pb.id in ids]
    overrides: dict = dict(config.playbook_overrides)
    # The book's underlying becomes the playbook's ticker (#139), so strike
    # derivation, trend, and IVR all resolve per book (via telemetry_key —
    # XSP proxies to SPY). Placement no longer needs its own substitution.
    if config.underlying:
        overrides["underlying_ticker"] = config.underlying
    if not overrides:
        return selected
    adjusted = []
    for pb in selected:
        data = pb.model_dump()
        for dotted, value in overrides.items():
            node = data
            *path, last = dotted.split(".")
            for key in path:
                node = node[key]
            node[last] = value
        adjusted.append(PlaybookDefinitionSchema(**data))
    return adjusted


async def _layer_c_entries(
    session: AsyncSession,
    broker,
    state: MarketStateModel,
    readings: dict[str, str],
    telemetry_live: bool,
    summary: ExecutorRunSummary,
    today: date,
) -> None:
    if not telemetry_live:
        summary.entries_blocked.append(BlockedEntry(None, "STALE_DATA — live telemetry unavailable, no new entries"))
        await _audit(session, "ENTRIES_BLOCKED_STALE_DATA", None, {"scope": "ALL"})
        await session.commit()
        return

    playbooks = [pb.to_schema() for pb in (await session.execute(select(PlaybookDefinitionModel))).scalars().all()]
    config_model = (await session.execute(select(PortfolioConfigModel).filter_by(id=1))).scalar_one_or_none()
    if config_model is None:
        summary.notes.append("No portfolio config — Layer C skipped")
        return
    books = (
        (await session.execute(select(BookModel).filter(BookModel.status == "ACTIVE", BookModel.id != "B00")))
        .scalars()
        .all()
    )

    configs = {b.id: resolve_book_config(b.config) for b in books}
    # Per-underlying telemetry (#139): prices/SMA20/pseudo-IVR for every
    # non-SPY-scale underlying any active book trades, from index_history.
    non_spy = sorted({u for cfg in configs.values() if (u := cfg.underlying) is not None and telemetry_key(u) != "SPY"})
    prices, smas, pseudo_ivrs = await underlying_telemetry(session, non_spy)

    for book in books:
        book_config = configs[book.id]
        variant = book_config.variant or "V0"
        regime = readings.get(variant)
        if regime is None or regime == INSUFFICIENT_DATA:
            summary.entries_blocked.append(BlockedEntry(book.id, f"variant {variant} reading unavailable"))
            await _audit(session, "ENTRIES_BLOCKED_STALE_DATA", book.id, {"variant": variant})
            await session.commit()
            continue

        book_positions = [
            p.to_schema()
            for p in (await session.execute(select(PositionModel).filter_by(book_id=book.id))).scalars().all()
        ]
        state_schema = state.to_schema().model_copy(
            update={
                "current_regime": regime,
                "underlying_prices": prices,
                "underlying_sma20": smas,
                # Pseudo-IVRs supplement, never overwrite, real IVR entries.
                "underlying_ivrs": {**pseudo_ivrs, **(state.underlying_ivrs or {})},
            }
        )
        scan_config = _book_scan_config(config_model, book_config.envelope)
        scan = scan_opportunities(
            playbooks=_book_playbooks(playbooks, book_config),
            market_state=state_schema,
            positions=book_positions,
            portfolio_config=scan_config,
            today=today,
            # Control books (ADR-0009): B12 ignores the regime gate, B16 the
            # IVR gates — they exist to measure whether those gates earn keep.
            enforce_regime=not book_config.ignore_regime,
            enforce_ivr=not book_config.ignore_ivr,
            book_mode=True,
        )
        if scan.portfolio_blocked:
            await _audit(session, "SCAN_BLOCKED", book.id, {"reason": scan.block_reason})
            await session.commit()
            continue
        for candidate in scan.candidates:
            if not candidate.eligible:
                continue
            spec_result = generate_trade_spec(
                candidate.playbook, state_schema, book_positions, scan_config, contracts=1, today=today
            )
            if spec_result.spec is None:
                await _audit(
                    session,
                    "SPEC_HARD_BLOCKED",
                    book.id,
                    {"playbook": candidate.playbook.id, "blocks": [b.check for b in spec_result.hard_blocks]},
                )
                await session.commit()
                continue
            if not await _try_place_entry(
                session, broker, book, spec_result.spec, candidate.playbook, summary, entry_regime=regime
            ):
                await _audit(session, "ENTRY_PHASE_ABORTED", None, {"after": f"{book.id}:{candidate.playbook.id}"})
                await session.commit()
                return


@dataclass(frozen=True)
class ComboLeg:
    """One leg of a combo order. ratio is the combo multiplier — BWB bodies
    carry 2 (#132); position legs expand it into duplicates separately."""

    occ: str
    action: str  # "BUY" | "SELL"
    direction: str  # "LONG" | "SHORT"
    ratio: int


async def _try_place_entry(
    session: AsyncSession, broker, book: BookModel, spec, playbook, summary, entry_regime: str = ""
) -> bool:
    """Returns False only when the submission phase must abort (order-path
    broker error, design §3.2); every per-candidate skip returns True.
    entry_regime is stamped into the order meta so the position remembers the
    regime it was entered under (B28's regime-flip exit, #254)."""
    underlying = resolve_book_config(book.config).underlying or spec.underlying
    legs_meta = []
    combo: list[ComboLeg] = []
    for leg in spec.legs:
        strike = round(leg.strike)  # XSP strikes are integer-spaced; SPY specs derive on $5 grid
        occ = format_occ_symbol(underlying, leg.expiration_date, leg.option_type, strike)
        direction = "LONG" if leg.action == "BUY" else "SHORT"
        # Combo ratio: BWB bodies carry quantity 2 (#132); everything else 1.
        ratio = max(1, leg.quantity)
        combo.append(ComboLeg(occ=occ, action=leg.action, direction=direction, ratio=ratio))
        # Position legs expand the ratio into duplicate entries so the
        # reconciliation leg-quantity sum matches the broker exactly.
        legs_meta.extend(
            [
                {
                    "occ": occ,
                    "option_type": leg.option_type,
                    "direction": direction,
                    "strike": float(strike),
                    "expiration": leg.expiration_date,
                }
            ]
            * ratio
        )

    quotes = fetch_options_latest_quotes([leg.occ for leg in combo])
    if any(leg.occ not in quotes for leg in combo):
        summary.entries_blocked.append(BlockedEntry(book.id, f"{playbook.id} unpriceable ({underlying})"))
        await _audit(session, "CANDIDATE_UNPRICEABLE", book.id, {"playbook": playbook.id, "underlying": underlying})
        await session.commit()
        return True
    net_mid = round(sum((quotes[leg.occ] if leg.action == "BUY" else -quotes[leg.occ]) * leg.ratio for leg in combo), 2)
    if net_mid == 0.0:
        await _audit(session, "CANDIDATE_UNPRICEABLE", book.id, {"playbook": playbook.id, "reason": "zero mid"})
        await session.commit()
        return True
    # Quote sanity bound (#282, audit H8): a same-expiry spread's value can
    # never exceed its widest same-type strike span — a mid beyond it is a
    # stale close or a broken quote, and must be skipped, never traded.
    # Calendars (same strike, two expiries) have span 0 → no bound applies.
    spans = []
    for opt_type in ("CALL", "PUT"):
        strikes = [leg["strike"] for leg in legs_meta if leg["option_type"] == opt_type]
        if len(strikes) >= 2:
            spans.append(max(strikes) - min(strikes))
    width_bound = max(spans) if spans else 0.0
    if width_bound and abs(net_mid) >= width_bound:
        await _audit(
            session,
            "CANDIDATE_UNPRICEABLE",
            book.id,
            {"playbook": playbook.id, "reason": f"absurd quote: |{net_mid}| >= {width_bound} width"},
        )
        await session.commit()
        return True

    candidate_order = CandidateOrder(
        book_id=book.id,
        strategy_type=spec.strategy_type,
        expiration_date=spec.expiration_date,
        legs=tuple((leg.occ, leg.direction) for leg in combo),
        max_loss_per_share=spec.max_loss_dollars / 100.0,
        contracts=1,
    )
    if await check_duplicate_order(session, book.id, candidate_order.legs, market_evening_window_start(market_today())):
        # An identical entry already went out tonight — logic bug, not market
        # condition. Block it and latch the global halt (supervision.md).
        summary.entries_blocked.append(BlockedEntry(book.id, f"{playbook.id} DUPLICATE_ORDER"))
        await _audit(session, DUPLICATE_ORDER, book.id, {"playbook": playbook.id})
        await session.commit()
        await set_control(
            session, "GLOBAL", HALT_ENTRIES, reason=f"{DUPLICATE_ORDER}: {playbook.id} in {book.id}", actor="anomaly"
        )
        return True

    decision = await evaluate_book_gates(session, candidate_order)
    if not decision.allowed:
        summary.entries_blocked.append(
            BlockedEntry(book.id, f"{playbook.id} gated ({', '.join(decision.blocked_by())})")
        )
        return True

    order_id = f"o_{uuid.uuid4().hex[:8]}"
    ref = f"basis:{book.id}:{order_id}:open"
    await stage_order(
        session,
        candidate_order,
        order_id=order_id,
        order_ref=ref,
        limit_price=net_mid,
        decision_midpoint=net_mid,
        combo_legs={
            "legs": legs_meta,
            "quantity": 1,
            "strategy_type": spec.strategy_type,
            "expiration_date": spec.expiration_date,
            "underlying": underlying,
            "playbook_id": playbook.id,
            # Frozen contract (#260): the position must be exited under the
            # rules it was ENTERED under, even if the playbook row (or a
            # book's overrides) changes mid-flight. This is the book-resolved
            # playbook — B15/B26-style exit overrides are already applied.
            "playbook_version": playbook.version,
            "playbook_snapshot": playbook.model_dump(),
            "entry_regime": entry_regime,
        },
    )
    pct = playbook.exit_rules.profit_take_pct / 100.0
    tp_price = round(net_mid * (1 - pct) if net_mid < 0 else net_mid * (1 + pct), 2)
    spread = SpreadOrder(
        legs=tuple((leg.occ, leg.action, leg.ratio) for leg in combo),
        quantity=1,
        net_limit_price=net_mid,
        underlying=underlying,
    )
    try:
        await assert_entries_allowed(session, book.id)
        placed = broker.place_spread(spread, ref, profit_target_price=tp_price)
    except TradingHaltedError as halt:
        summary.entries_blocked.append(BlockedEntry(book.id, f"{playbook.id} halted ({halt.scope}={halt.state})"))
        await _audit(
            session,
            "WOULD_HAVE_TRADED",
            book.id,
            {"order_ref": ref, "playbook": playbook.id, "halt_scope": halt.scope},
        )
        await release_order(session, order_id, "CANCELLED")
        return True
    except BrokerError as exc:
        # 162/competing-session policy (#68, design §3.2): a broker error on
        # the ORDER path aborts the rest of the submission phase — never
        # fail-soft where orders are concerned. (Data-path failures already
        # fail soft to stored data upstream.) REPEATED_REJECTION still
        # latches the halt if this recurs across sessions.
        summary.entries_blocked.append(BlockedEntry(book.id, f"{playbook.id} rejected — submission phase aborted"))
        await _audit(session, "ORDER_REJECTED", book.id, {"order_ref": ref, "error": str(exc)})
        await release_order(session, order_id, "REJECTED")
        return False
    order = await session.get(OrderModel, order_id)
    order.status = "SUBMITTED"
    order.submitted_at = _now()
    order.ib_order_id = placed.order_id
    order.ib_perm_id = placed.perm_id
    # The GTC profit-taker child is a REAL order resting at IB (#258, audit
    # C1): it can fill any future morning, and a fill with no row here is
    # invisible to the sync — the position would stay OPEN into reconciliation
    # drift and a global halt. Its fill settles through the normal CLOSE path
    # (limit_price is the signed per-share cash flow, same convention).
    session.add(
        OrderModel(
            id=f"{order_id}_tp",
            book_id=book.id,
            position_id=None,  # linked when the parent's fill creates the position
            order_ref=f"{ref}:tp",
            ib_order_id=None,
            ib_perm_id=None,
            action="CLOSE",
            combo_legs={
                "legs": legs_meta,
                "quantity": 1,
                "strategy_type": spec.strategy_type,
                "exit_trigger": "PROFIT_TARGET",
            },
            order_type="LIMIT",
            limit_price=tp_price,
            decision_midpoint=tp_price,
            status="SUBMITTED",
            submitted_at=_now(),
            completed_at=None,
            encumbered_risk=0.0,  # closes reduce risk — no encumbrance
        )
    )
    summary.entries_placed.append(ref)
    await _audit(
        session,
        "ORDER_SUBMITTED",
        book.id,
        {"order_ref": ref, "playbook": playbook.id, "limit": net_mid, "profit_target": tp_price},
    )
    await session.commit()
    return True


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


async def run_executor_evening(
    session_maker=None, broker_factory=None, today: date | None = None
) -> ExecutorRunSummary:
    session_maker = session_maker or async_session_maker
    broker_factory = broker_factory or BrokerSession
    today = today or market_today()
    summary = ExecutorRunSummary(run_started_at=_now(), run_date=today.isoformat())

    # Holiday guard (#68, design §3.3): write the heartbeat and exit without
    # trading — silent non-operation is only acceptable when announced. The
    # gateway lifecycle also skips launching Gateway on these days.
    if not is_trading_day(today):
        summary.notes.append(f"MARKET HOLIDAY: {today.isoformat()} — no trading, heartbeat written")
        async with session_maker() as session:
            await _audit(session, "EXECUTOR_HOLIDAY_SKIP", None, {"date": today.isoformat()})
            await session.commit()
        _write_heartbeat(summary)
        return summary

    # One run at a time (#275, audit H5): a concurrent manual run would place
    # duplicate live closes and double-adjust cash. A held lock aborts THIS
    # run loudly and leaves the live run's heartbeat alone.
    lock = acquire_run_lock("executor")
    if lock is None:
        summary.notes.append("RUN LOCK HELD — another executor run is in progress; aborted without trading")
        async with session_maker() as session:
            await _audit(session, "RUN_LOCK_HELD", None, {})
            await session.commit()
        logger.error("Executor run lock held — aborting this run")
        return summary

    broker = broker_factory()
    try:
        broker.open()
    except BrokerError as exc:
        summary.broker_ok = False
        summary.notes.append(f"Broker unavailable: {exc}")
        async with session_maker() as session:
            await _audit(session, "EXECUTOR_BROKER_UNAVAILABLE", None, {"error": str(exc)})
            await session.commit()
        _write_heartbeat(summary)
        release_run_lock(lock)
        return summary

    try:
        async with session_maker() as session:
            # Missed-night detection (#283, audit M2): reqExecutions is
            # current-day-only, so a skipped night's fills are NOT here and
            # never will be — the weekly Flex audit is the recovery path.
            # Pretending continuity would be silently wrong books.
            last_recon = (
                await session.execute(
                    select(ReconciliationRunModel).order_by(ReconciliationRunModel.id.desc()).limit(1)
                )
            ).scalar_one_or_none()
            if last_recon and _market_days_between(last_recon.run_at, today.isoformat()) > 1:
                summary.notes.append(
                    f"⚠ MISSED NIGHT(S): last run {last_recon.run_at[:16]} — fills from the gap are NOT in the "
                    "books; run the Flex audit (pixi run flex-audit) to reconcile before trusting P&L"
                )
                await _audit(session, "MISSED_NIGHT_GAP", None, {"last_run_at": last_recon.run_at})
                await session.commit()
            await _sync_order_states(session, broker, summary)
            await _settle_expired(session, summary)
            snapshot = BrokerSnapshot(
                positions=tuple(broker.positions()),
                executions=tuple(broker.executions()),
                open_orders=tuple(broker.open_orders()),
            )
            recon = await run_reconciliation(session, snapshot, today=today.isoformat())
            summary.reconciliation = recon.result

            await apply_ntfy_commands(session)
            await refresh_position_values(session)
            state, telemetry_live = await refresh_market_state(session)
            await persist_index_history(session)
            readings = await persist_regime_readings(session, today)
            if state is None:
                summary.notes.append("No market state — run aborted after reconciliation")
                return summary

            for label in stale_calendars(today):
                summary.notes.append(
                    f"CALENDAR STALE ({label}): extend the table in backend/calendars.py before coverage lapses"
                )
            await _layer_a_closes(session, broker, state, summary, today, readings)
            await _layer_c_entries(session, broker, state, readings, telemetry_live, summary, today)
            findings = await run_post_session_anomalies(session, today.isoformat(), since=summary.run_started_at)
            summary.anomalies.extend(f"{f.rule}({f.scope}): {f.detail}" for f in findings)
    finally:
        broker.close()
        _write_heartbeat(summary)
        release_run_lock(lock)
    return summary


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    from backend.run_logging import setup_run_logging

    setup_run_logging("executor")
    from backend.database import init_db

    await init_db()
    summary = await run_executor_evening()
    logger.info(
        "Executor run complete: broker_ok=%s reconciliation=%s entries=%d closes=%d blocked=%d",
        summary.broker_ok,
        summary.reconciliation,
        len(summary.entries_placed),
        len(summary.closes_placed),
        len(summary.entries_blocked),
    )

    # Digest + urgent tiering (#72): the nightly summary batches everything;
    # interrupt-worthy events additionally go out as a separate urgent push.
    from backend.digest import compose_executor_digest, urgent_events
    from backend.operator import send_ntfy_with_retry

    # The run's own date and start time (#259) — never recomputed here, so a
    # pipeline that crosses midnight UTC still reports its own events.
    async with async_session_maker() as session:
        title, body, priority = await compose_executor_digest(
            session, summary, summary.run_date, since=summary.run_started_at
        )
        urgent = await urgent_events(session, summary.run_started_at)
    pushed = send_ntfy_with_retry(title, body, priority)
    urgent_pushed = send_ntfy_with_retry("⛔ basis executor alerts", "\n".join(urgent), "urgent") if urgent else None
    # The digest is evidence too (#277, audit H2): scheduled-task stdout
    # vanishes and send_ntfy fails soft, so the composed text and its
    # delivery outcome are persisted where the console can show them.
    async with async_session_maker() as session:
        await _audit(
            session,
            "DIGEST_COMPOSED",
            None,
            {"title": title, "body": body, "priority": priority, "pushed": pushed, "urgent_pushed": urgent_pushed},
        )
        await session.commit()
    print(f"\n{title}\n{body}")


if __name__ == "__main__":
    asyncio.run(main())

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

from backend.anomaly import DUPLICATE_ORDER, check_duplicate_order, run_post_session_anomalies
from backend.book_gates import (
    DEFAULT_ENVELOPE,
    PENDING_ORDER_STATUSES,
    CandidateOrder,
    evaluate_book_gates,
    release_order,
    stage_order,
)
from backend.broker import BrokerError, BrokerSession, RefState, SpreadOrder
from backend.console import heartbeat_path
from backend.database import async_session_maker
from backend.market_data import fetch_options_latest_quotes, format_occ_symbol
from backend.models import (
    AuditEventModel,
    BookModel,
    MarketStateModel,
    OrderModel,
    PlaybookDefinitionModel,
    PortfolioConfigModel,
    PositionModel,
)
from backend.observation import run_lifecycle_scan
from backend.operator import (
    persist_index_history,
    refresh_market_state,
    refresh_position_values,
)
from backend.opportunity import generate_trade_spec, scan_opportunities
from backend.reconciliation import BrokerSnapshot, _backfill_missed_fills, run_reconciliation
from backend.regime_variants import INSUFFICIENT_DATA, persist_regime_readings
from backend.trading_control import TradingHaltedError, apply_ntfy_commands, assert_entries_allowed

logger = logging.getLogger(__name__)

CLOSE_CONCESSION_PER_RUNG = 0.15  # each evening a close reworks 15% closer to natural


@dataclass
class ExecutorRunSummary:
    broker_ok: bool = True
    reconciliation: str = "SKIPPED"
    positions_created: list[str] = field(default_factory=list)
    intents_expired: list[str] = field(default_factory=list)
    closes_placed: list[str] = field(default_factory=list)
    entries_placed: list[str] = field(default_factory=list)
    entries_blocked: list[str] = field(default_factory=list)
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

    for order in pending:
        state = report.state(order.order_ref)
        if state is RefState.FILLED:
            await _order_to_position(session, order, summary)
        elif state is RefState.CANCELLED:
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
            await _audit(
                session, "ORDER_LOST_AT_BROKER", order.book_id, {"order_ref": order.order_ref, "was": "SUBMITTED"}
            )
        # OPEN: still working its next-session window — leave it counted.
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
        if book is not None:
            book.cash_balance += order.limit_price * 100 * quantity * -1  # SELL: negative price = cash out
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
                entry_date=_now()[:10],
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
                },
                book_id=order.book_id,
            )
        )
        order.position_id = pos_id
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
    session: AsyncSession, broker, state: MarketStateModel, summary: ExecutorRunSummary, today: date
) -> None:
    open_positions = (await session.execute(select(PositionModel).filter_by(status="OPEN"))).scalars().all()
    for pos in open_positions:
        if pos.book_id == "B00":
            continue  # legacy/manual book is never traded by the executor
        scan = run_lifecycle_scan(
            pos.to_schema(),
            current_regime=state.current_regime,
            spy_price=state.spy_price,
            catalyst_dates=state.catalyst_dates or [],
        )
        if not scan["priority"].startswith("P1"):
            continue
        prior_closes = (
            (await session.execute(select(OrderModel).filter_by(position_id=pos.id, action="CLOSE"))).scalars().all()
        )
        rung = len(prior_closes)
        concession = 1.0 + CLOSE_CONCESSION_PER_RUNG * rung
        # SELL-the-bag convention: closing a credit position pays (negative
        # price); closing a debit position receives (positive price).
        if pos.premium_direction == "CREDIT":
            limit_price = round(-pos.current_value_per_share * concession, 2)
        else:
            limit_price = round(pos.current_value_per_share / concession, 2)
        # The closing bag MIRRORS the entry bag (SHORT leg = SELL, LONG = BUY);
        # the SELL order action on the bag is what reverses the position.
        legs = tuple(
            (
                format_occ_symbol(pos.underlying, leg["expiration"], leg["option_type"], leg["strike"]),
                "SELL" if leg["direction"] == "SHORT" else "BUY",
                1,
            )
            for leg in pos.legs
        )
        order_id = f"o_{uuid.uuid4().hex[:8]}"
        ref = f"basis:{pos.book_id}:{order_id}:close"
        spread = SpreadOrder(legs=legs, quantity=pos.contracts, net_limit_price=limit_price, underlying=pos.underlying)
        order = OrderModel(
            id=order_id,
            book_id=pos.book_id,
            position_id=pos.id,
            order_ref=ref,
            ib_order_id=None,
            ib_perm_id=None,
            action="CLOSE",
            combo_legs={"legs": [dict(l) for l in pos.legs], "quantity": pos.contracts},
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


def _book_scan_config(base: PortfolioConfigModel, envelope: dict) -> object:
    """Clone the portfolio config with the book's envelope numbers so the
    Layer C scan gates and the book gates agree (the book gates remain the
    authority; this keeps the scan from pre-blocking at the wrong caps).
    Fallbacks come from DEFAULT_ENVELOPE so the two layers can never drift."""
    schema = base.to_schema()
    merged = {**DEFAULT_ENVELOPE, **envelope}
    basis = float(merged["basis"])
    risk = schema.risk_profile.model_copy(
        update={
            "max_simultaneous_positions": int(merged["max_positions"]),
            "max_capital_deployed_pct": float(merged["max_deployed_pct"]),
            "max_trade_risk_dollars": basis * float(merged["max_loss_pct_per_trade"]) / 100.0,
            "max_trade_risk_pct": float(merged["max_loss_pct_per_trade"]),
        }
    )
    return schema.model_copy(update={"risk_profile": risk})


def _book_playbooks(playbooks: list, book_config: dict) -> list:
    """Apply a book's playbook selection and overrides (#136 experiment arms).

    config["playbook_ids"]: optional whitelist — the book scans only those.
    config["playbook_overrides"]: optional dot-keyed field overrides applied
    to every selected playbook (e.g. {"execution_specs.target_dte": 24}),
    revalidated through the schema so a bad override fails loudly at scan
    time, not at order time. Both feed the book's config_hash, so every arm
    is fingerprinted (ADR-0003 pattern).
    """
    from backend.models import PlaybookDefinitionSchema

    ids = book_config.get("playbook_ids")
    selected = [pb for pb in playbooks if not ids or pb.id in ids]
    overrides: dict = book_config.get("playbook_overrides") or {}
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
        summary.entries_blocked.append("ALL: STALE_DATA — live telemetry unavailable, no new entries")
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
    for book in books:
        variant = (book.config or {}).get("engine_variant", "V0")
        regime = readings.get(variant)
        if regime is None or regime == INSUFFICIENT_DATA:
            summary.entries_blocked.append(f"{book.id}: variant {variant} reading unavailable")
            await _audit(session, "ENTRIES_BLOCKED_STALE_DATA", book.id, {"variant": variant})
            await session.commit()
            continue

        book_positions = [
            p.to_schema()
            for p in (await session.execute(select(PositionModel).filter_by(book_id=book.id))).scalars().all()
        ]
        state_schema = state.to_schema().model_copy(update={"current_regime": regime})
        book_config = book.config or {}
        envelope = book_config.get("envelope", {})
        scan_config = _book_scan_config(config_model, envelope)
        scan = scan_opportunities(
            playbooks=_book_playbooks(playbooks, book_config),
            market_state=state_schema,
            positions=book_positions,
            portfolio_config=scan_config,
            today=today,
            # Control books (ADR-0009): B12 ignores the regime gate, B16 the
            # IVR gates — they exist to measure whether those gates earn keep.
            enforce_regime=not book_config.get("ignore_regime", False),
            enforce_ivr=not book_config.get("ignore_ivr", False),
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
            await _try_place_entry(session, broker, book, spec_result.spec, candidate.playbook, summary)


async def _try_place_entry(session: AsyncSession, broker, book: BookModel, spec, playbook, summary) -> None:
    underlying = (book.config or {}).get("underlying", spec.underlying)
    legs_meta = []
    occ_by_leg = []
    for leg in spec.legs:
        strike = round(leg.strike)  # XSP strikes are integer-spaced; SPY specs derive on $5 grid
        occ = format_occ_symbol(underlying, leg.expiration_date, leg.option_type, strike)
        direction = "LONG" if leg.action == "BUY" else "SHORT"
        occ_by_leg.append((occ, leg.action, direction))
        legs_meta.append(
            {
                "occ": occ,
                "option_type": leg.option_type,
                "direction": direction,
                "strike": float(strike),
                "expiration": leg.expiration_date,
            }
        )

    quotes = fetch_options_latest_quotes([occ for occ, _, _ in occ_by_leg])
    if any(occ not in quotes for occ, _, _ in occ_by_leg):
        summary.entries_blocked.append(f"{book.id}: {playbook.id} unpriceable ({underlying})")
        await _audit(session, "CANDIDATE_UNPRICEABLE", book.id, {"playbook": playbook.id, "underlying": underlying})
        await session.commit()
        return
    net_mid = round(sum(quotes[occ] if action == "BUY" else -quotes[occ] for occ, action, _ in occ_by_leg), 2)
    if net_mid == 0.0:
        await _audit(session, "CANDIDATE_UNPRICEABLE", book.id, {"playbook": playbook.id, "reason": "zero mid"})
        await session.commit()
        return

    candidate_order = CandidateOrder(
        book_id=book.id,
        strategy_type=spec.strategy_type,
        expiration_date=spec.expiration_date,
        legs=tuple((occ, direction) for occ, _, direction in occ_by_leg),
        max_loss_per_share=spec.max_loss_dollars / 100.0,
        contracts=1,
    )
    if await check_duplicate_order(session, book.id, candidate_order.legs, _now()[:10]):
        # An identical entry already went out tonight — logic bug, not market
        # condition. Block it and latch the global halt (supervision.md).
        summary.entries_blocked.append(f"{book.id}: {playbook.id} DUPLICATE_ORDER")
        await _audit(session, DUPLICATE_ORDER, book.id, {"playbook": playbook.id})
        await session.commit()
        from backend.trading_control import HALT_ENTRIES, set_control

        await set_control(
            session, "GLOBAL", HALT_ENTRIES, reason=f"{DUPLICATE_ORDER}: {playbook.id} in {book.id}", actor="anomaly"
        )
        return

    decision = await evaluate_book_gates(session, candidate_order)
    if not decision.allowed:
        summary.entries_blocked.append(f"{book.id}: {playbook.id} gated ({', '.join(decision.blocked_by())})")
        return

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
        },
    )
    pct = playbook.exit_rules.profit_take_pct / 100.0
    tp_price = round(net_mid * (1 - pct) if net_mid < 0 else net_mid * (1 + pct), 2)
    spread = SpreadOrder(
        legs=tuple((occ, action, 1) for occ, action, _ in occ_by_leg),
        quantity=1,
        net_limit_price=net_mid,
        underlying=underlying,
    )
    try:
        await assert_entries_allowed(session, book.id)
        placed = broker.place_spread(spread, ref, profit_target_price=tp_price)
    except TradingHaltedError as halt:
        summary.entries_blocked.append(f"{book.id}: {playbook.id} halted ({halt.scope}={halt.state})")
        await _audit(
            session,
            "WOULD_HAVE_TRADED",
            book.id,
            {"order_ref": ref, "playbook": playbook.id, "halt_scope": halt.scope},
        )
        await release_order(session, order_id, "CANCELLED")
        return
    except BrokerError as exc:
        await _audit(session, "ORDER_REJECTED", book.id, {"order_ref": ref, "error": str(exc)})
        await release_order(session, order_id, "REJECTED")
        return
    order = await session.get(OrderModel, order_id)
    order.status = "SUBMITTED"
    order.submitted_at = _now()
    order.ib_order_id = placed.order_id
    order.ib_perm_id = placed.perm_id
    summary.entries_placed.append(ref)
    await _audit(
        session,
        "ORDER_SUBMITTED",
        book.id,
        {"order_ref": ref, "playbook": playbook.id, "limit": net_mid, "profit_target": tp_price},
    )
    await session.commit()


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


async def run_executor_evening(
    session_maker=None, broker_factory=None, today: date | None = None
) -> ExecutorRunSummary:
    session_maker = session_maker or async_session_maker
    broker_factory = broker_factory or BrokerSession
    today = today or datetime.now(UTC).date()
    summary = ExecutorRunSummary()

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
        return summary

    try:
        async with session_maker() as session:
            await _sync_order_states(session, broker, summary)
            snapshot = BrokerSnapshot(
                positions=tuple(broker.positions()),
                executions=tuple(broker.executions()),
                open_orders=tuple(broker.open_orders()),
            )
            recon = await run_reconciliation(session, snapshot)
            summary.reconciliation = recon.result

            await apply_ntfy_commands(session)
            await refresh_position_values(session)
            state, telemetry_live = await refresh_market_state(session)
            await persist_index_history(session)
            readings = await persist_regime_readings(session, today)
            if state is None:
                summary.notes.append("No market state — run aborted after reconciliation")
                return summary

            await _layer_a_closes(session, broker, state, summary, today)
            await _layer_c_entries(session, broker, state, readings, telemetry_live, summary, today)
            findings = await run_post_session_anomalies(session, today.isoformat())
            summary.anomalies.extend(f"{f.rule}({f.scope}): {f.detail}" for f in findings)
    finally:
        broker.close()
        _write_heartbeat(summary)
    return summary


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
    from backend.operator import send_ntfy

    today = datetime.now(UTC).date().isoformat()
    async with async_session_maker() as session:
        title, body, priority = await compose_executor_digest(session, summary, today)
        urgent = await urgent_events(session, today)
    send_ntfy(title, body, priority)
    if urgent:
        send_ntfy("⛔ basis executor alerts", "\n".join(urgent), "urgent")
    print(f"\n{title}\n{body}")


if __name__ == "__main__":
    asyncio.run(main())

"""attention.py — GET /api/attention composition (#890, DESIGN-890.md §1).

The "what needs you" triage surface: every field is a read of something that
already exists (TradingControlModel rows, the reconciliation run, PARTIAL
orders, the latest Flex audit, executor_status, the urgent audit-event
vocabulary) — no new persisted state, no new business logic. This module
only aggregates those existing queries/compose functions into one
operator-facing shape, mirroring compose_observation's "the route loads,
this composes" split.
"""

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.broker import first_needs_human_instruction
from backend.console import executor_status
from backend.digest import is_urgent_event_type
from backend.labels import book_label, order_label
from backend.models import (
    NON_ALARM_ACTION_KINDS,
    AttentionAction,
    AttentionActionKind,
    AttentionResponse,
    AuditEventModel,
    BookModel,
    BrokerErrorItem,
    DeliveryGapItem,
    FlexDiscrepancyItem,
    HaltItem,
    MarketStateSchema,
    OrderModel,
    PartialOrderItem,
    PortfolioConfigSchema,
    PositionActionItem,
    PositionSchema,
    ReconciliationDriftItem,
    ReconciliationRunModel,
    TradingControlModel,
    UnresolvedUrgentEvent,
)
from backend.observation import REGIME_REVIEW_PRIORITY, compose_observation
from backend.reconciliation import latest_reconciliation_run
from backend.states import LIVE_AUTHORITY_LIVE, ORDER_PARTIAL_STATUS
from backend.trading_control import ACTIVE, GLOBAL_SCOPE, sentinel_halt_active, sentinel_path

# #1132: demote opinions, never alarms. Home is the operator's first screen,
# and a wall of regime-conflict reviews from paper-only options practice books
# buried the one item that mattered. Exactly ONE kind is demoted — a
# REGIME_REVIEW_PRIORITY position flag on a practice book — and it moves to
# practice_reviews (a one-line count on Home, never in problem_count).
#
# Every other AttentionResponse field is NEVER demoted, whatever book it comes
# from: halts (operator, anomaly — zombie fills, duplicate orders, envelope
# breaches latch here — and stake-drawdown halts), the sentinel, reconciliation
# drift, partial orders, Flex discrepancies, delivery gaps, broker errors and
# every urgent audit event (hard-limit/gate breaches, DUPLICATE_ORDER,
# ZOMBIE_FILL, STAKE_DRAWDOWN_HALT, …). Within p1_actions every P1 and every
# P2 — CLOSE SOON (the DTE exit rule) stays in full; so does any flag on B00
# (the manual lane) or on a book holding LIVE authority (real money is never
# "practice"). test_attention.py pins both sets against model_fields, so a new
# field fails until it is classified here.
NEVER_DEMOTED_FIELDS: frozenset[str] = frozenset(
    {
        "sentinel_halt",
        "halts",
        "reconciliation_drift",
        "partial_orders",
        "flex_discrepancies",
        "delivery_gaps",
        "broker_errors",
        "unresolved_urgent_events",
    }
)
# p1_actions keeps every position flag EXCEPT the demoted kind, which lands in
# practice_reviews instead — the split is _is_practice_review below.
POSITION_FLAG_FIELDS: frozenset[str] = frozenset({"p1_actions", "practice_reviews"})
_MANUAL_BOOK_ID = "B00"


async def _practice_book_ids(session: AsyncSession) -> frozenset[str]:
    """Books whose advisory flags may be demoted: paper-only executor books.
    Fails closed — a book missing from the books table, B00, or any book
    holding LIVE authority is NOT in the set, so its flags stay in full."""
    rows = (await session.execute(select(BookModel.id, BookModel.live_authority))).all()
    return frozenset(
        book_id for book_id, authority in rows if book_id != _MANUAL_BOOK_ID and authority != LIVE_AUTHORITY_LIVE
    )


def _is_practice_review(item: PositionActionItem, practice_book_ids: frozenset[str]) -> bool:
    return item.priority == REGIME_REVIEW_PRIORITY and item.book_id in practice_book_ids


# "UNKNOWN_ORDER_REF ref (exec 0001.1)", "MISSING_FROM_LEDGER exec 0001.1 ref
# ...", "FILL_MISMATCH exec 0001.1: ..." — every Flex discrepancy line that
# names an execution spells it as "exec <id>"; NO_ORDER_REFS_IN_EXPORT is the
# one kind that never does, and stays un-ackable by design (flex_audit.py).
_EXEC_ID_PATTERN = re.compile(r"exec ([^\s):]+)")


async def _sentinel_halt_item() -> HaltItem | None:
    if not sentinel_halt_active():
        return None
    since = datetime.now(UTC).isoformat()
    try:
        since = datetime.fromtimestamp(sentinel_path().stat().st_mtime, UTC).isoformat()
    except OSError:
        pass  # file vanished between the exists() check and stat() — "now" is still honest
    return HaltItem(
        scope="SENTINEL",
        scope_label="HALT sentinel file",
        state="HALT_ENTRIES",
        reason="HALT sentinel file present in the project root",
        actor="system",
        since=since,
        action=AttentionAction(kind=AttentionActionKind.ACKNOWLEDGE_ONLY, label="Seen"),
    )


async def _halt_items(session: AsyncSession) -> list[HaltItem]:
    items: list[HaltItem] = []
    sentinel = await _sentinel_halt_item()
    if sentinel is not None:
        items.append(sentinel)
    rows = (await session.execute(select(TradingControlModel))).scalars().all()
    for row in sorted(rows, key=lambda r: r.scope):
        if row.state == ACTIVE:
            continue
        scope_label = GLOBAL_SCOPE if row.scope == GLOBAL_SCOPE else await book_label(session, row.scope)
        items.append(
            HaltItem(
                scope=row.scope,
                scope_label=scope_label,
                state=row.state,
                reason=row.reason,
                actor=row.actor,
                since=row.changed_at,
                action=AttentionAction(
                    kind=AttentionActionKind.ACK_HALT,
                    label="Review + Resume",
                    requires_reason=True,
                    endpoint="/api/trading-control",
                    target={"scope": row.scope},
                ),
            )
        )
    return items


async def _position_action_items(
    positions: list[PositionSchema],
    config: PortfolioConfigSchema,
    state: MarketStateSchema,
    close_in_flight: dict[str, list[dict[str, Any]]],
) -> list[PositionActionItem]:
    observation = compose_observation(config, positions, state, close_in_flight)
    book_ids = {p.id: p.book_id for p in positions}
    items: list[PositionActionItem] = []
    for scanned in observation["scanned_positions"]:
        if not scanned["priority"].startswith(("P1", "P2")):
            continue
        in_flight = scanned["close_in_flight"]
        action = (
            AttentionAction(kind=AttentionActionKind.ACKNOWLEDGE_ONLY, label="Close in flight")
            if in_flight
            else AttentionAction(
                kind=AttentionActionKind.CLOSE_POSITION,
                label="Close now",
                endpoint=f"/api/positions/{scanned['position_id']}/close",
                target={"position_id": scanned["position_id"]},
            )
        )
        items.append(
            PositionActionItem(
                position_id=scanned["position_id"],
                book_id=book_ids.get(scanned["position_id"], "B00"),
                underlying=scanned["underlying"],
                strategy_type=scanned["strategy_type"],
                priority=scanned["priority"],
                reason=scanned["reason"],
                close_in_flight=in_flight,
                action=action,
            )
        )
    return items


async def _reconciliation_drift_item(session: AsyncSession) -> ReconciliationDriftItem | None:
    run = await latest_reconciliation_run(session)
    if run is None or run.result != "DRIFT":
        return None
    drift_details = run.drift_details or []
    resolved = run.resolved_at is not None
    action = (
        None
        if resolved
        else AttentionAction(
            kind=AttentionActionKind.RESOLVE_RECONCILIATION,
            label="Resolve drift",
            requires_reason=True,
            endpoint=f"/api/reconciliation/{run.id}/resolve",
            target={"run_id": str(run.id)},
        )
    )
    return ReconciliationDriftItem(
        run_id=run.id,
        run_at=run.run_at,
        drift_count=len(drift_details),
        drift_summary=[f"{d.get('kind')}: {d.get('key')}" for d in drift_details],
        resolved=resolved,
        action=action,
    )


async def _partial_order_items(session: AsyncSession) -> list[PartialOrderItem]:
    rows = (await session.execute(select(OrderModel).filter(OrderModel.status == ORDER_PARTIAL_STATUS))).scalars().all()
    items: list[PartialOrderItem] = []
    for order in rows:
        label = await order_label(session, order.book_id, order.combo_legs)
        items.append(
            PartialOrderItem(
                order_ref=order.order_ref,
                book_id=order.book_id,
                label=label,
                action=AttentionAction(
                    kind=AttentionActionKind.RESOLVE_PARTIAL_ORDER,
                    label="Resolve partial order",
                    requires_reason=True,
                    endpoint="/api/resolution/partial-order",
                    target={"order_ref": order.order_ref},
                ),
            )
        )
    return items


async def _flex_discrepancy_items(session: AsyncSession) -> list[FlexDiscrepancyItem]:
    latest = (
        await session.execute(
            select(AuditEventModel).filter_by(event_type="FLEX_AUDIT").order_by(AuditEventModel.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if latest is None:
        return []
    items: list[FlexDiscrepancyItem] = []
    for description in latest.payload.get("discrepancies", []):
        match = _EXEC_ID_PATTERN.search(description)
        exec_id = match.group(1) if match else None
        action = (
            AttentionAction(
                kind=AttentionActionKind.FLEX_ACK,
                label="Acknowledge discrepancy",
                requires_reason=True,
                endpoint="/api/resolution/flex-ack",
                target={"exec_ids": [exec_id]},
            )
            if exec_id is not None
            else AttentionAction(kind=AttentionActionKind.ACKNOWLEDGE_ONLY, label="Seen")
        )
        items.append(FlexDiscrepancyItem(exec_id=exec_id, description=description, action=action))
    return items


async def _delivery_gap_items(session: AsyncSession) -> list[DeliveryGapItem]:
    status = await executor_status(session)
    items: list[DeliveryGapItem] = []
    if status.last_digest_pushed is False:
        items.append(
            DeliveryGapItem(
                kind="digest",
                since=status.last_digest_at,
                action=AttentionAction(kind=AttentionActionKind.ACKNOWLEDGE_ONLY, label="Seen"),
            )
        )
    if status.last_urgent_pushed is False:
        items.append(
            DeliveryGapItem(
                kind="urgent_push",
                since=status.last_digest_at,
                action=AttentionAction(kind=AttentionActionKind.ACKNOWLEDGE_ONLY, label="Seen"),
            )
        )
    return items


async def _urgent_lookback_since(session: AsyncSession, now: datetime) -> str:
    """Ratified owner ruling (DESIGN-890.md §6.3): since the last resolved
    reconciliation run, or 24h, whichever is more recent — a live-polled
    endpoint has no natural "since a nightly run" boundary, so this bounds
    the catch-all buckets to what a human hasn't already cleared, without
    ever showing less than a day of history when nothing's been resolved
    lately."""
    last_resolved = (
        await session.execute(
            select(ReconciliationRunModel)
            .filter(ReconciliationRunModel.resolved_at.is_not(None))
            .order_by(ReconciliationRunModel.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    fallback = (now - timedelta(hours=24)).isoformat()
    if last_resolved is None or last_resolved.resolved_at is None:
        return fallback
    return max(last_resolved.resolved_at, fallback)


def _broker_error_instruction(payload: dict) -> str:
    """Mirrors digest.compose_executor_digest's own NEEDS_HUMAN_BROKER_ERRORS
    lookup — reads the same classification, does not invent a new one."""
    instruction = first_needs_human_instruction(err.get("code") for err in payload.get("api_errors", []))
    return instruction or payload.get("error") or "IB Gateway unreachable — no orders were possible tonight"


async def _broker_error_and_urgent_items(
    session: AsyncSession, since: str
) -> tuple[list[BrokerErrorItem], list[UnresolvedUrgentEvent]]:
    rows = (await session.execute(select(AuditEventModel).filter(AuditEventModel.run_at >= since))).scalars().all()

    broker_errors: list[BrokerErrorItem] = []
    broker_error_ids: set[int] = set()
    for e in rows:
        if e.event_type == "EXECUTOR_BROKER_UNAVAILABLE":
            broker_error_ids.add(e.id)
            broker_errors.append(
                BrokerErrorItem(
                    book_id=e.book_id,
                    at=e.run_at,
                    instruction=_broker_error_instruction(e.payload),
                    action=AttentionAction(kind=AttentionActionKind.ACKNOWLEDGE_ONLY, label="Seen"),
                )
            )

    urgent_items: list[UnresolvedUrgentEvent] = []
    label_cache: dict[str, str] = {}
    for e in rows:
        if e.id in broker_error_ids or not is_urgent_event_type(e.event_type):
            continue
        book_label_value = None
        if e.book_id:
            if e.book_id not in label_cache:
                label_cache[e.book_id] = await book_label(session, e.book_id)
            book_label_value = label_cache[e.book_id]
        detail = (
            e.payload.get("reason")
            or e.payload.get("detail")
            or e.payload.get("error")
            or e.payload.get("order_ref")
            or ""
        )
        urgent_items.append(
            UnresolvedUrgentEvent(
                id=e.id,
                run_at=e.run_at,
                book_label=book_label_value,
                event_type=e.event_type,
                detail=detail,
                action=AttentionAction(kind=AttentionActionKind.ACKNOWLEDGE_ONLY, label="Seen"),
            )
        )
    return broker_errors, urgent_items


def _headline(problem_count: int) -> str:
    if problem_count == 0:
        return "All clear"
    if problem_count == 1:
        return "1 thing needs you"
    return f"{problem_count} things need you"


def _problem_count(
    halts: list[HaltItem],
    p1_actions: list[PositionActionItem],
    reconciliation_drift: ReconciliationDriftItem | None,
    partial_orders: list[PartialOrderItem],
    flex_discrepancies: list[FlexDiscrepancyItem],
) -> int:
    """Every item whose action kind is NOT in NON_ALARM_ACTION_KINDS
    (ACKNOWLEDGE_ONLY, VIEW_ONLY) carries alarm weight (#915) — the frontend's
    actionable/informational split reads the same set, so the two stay in
    lockstep by construction rather than by two independently maintained
    kind exclusions."""
    return (
        sum(1 for h in halts if h.action.kind not in NON_ALARM_ACTION_KINDS)
        + sum(1 for p in p1_actions if p.action.kind not in NON_ALARM_ACTION_KINDS)
        + (1 if reconciliation_drift is not None and reconciliation_drift.action is not None else 0)
        + len(partial_orders)
        + sum(1 for f in flex_discrepancies if f.action.kind not in NON_ALARM_ACTION_KINDS)
    )


async def compose_attention(
    session: AsyncSession,
    config: PortfolioConfigSchema,
    positions: list[PositionSchema],
    state: MarketStateSchema,
    close_in_flight: dict[str, list[dict[str, Any]]] | None = None,
    now: datetime | None = None,
) -> AttentionResponse:
    now = now or datetime.now(UTC)
    close_in_flight = close_in_flight or {}

    halts = await _halt_items(session)
    position_flags = await _position_action_items(positions, config, state, close_in_flight)
    practice_book_ids = await _practice_book_ids(session)
    practice_reviews = [p for p in position_flags if _is_practice_review(p, practice_book_ids)]
    p1_actions = [p for p in position_flags if not _is_practice_review(p, practice_book_ids)]
    reconciliation_drift = await _reconciliation_drift_item(session)
    partial_orders = await _partial_order_items(session)
    flex_discrepancies = await _flex_discrepancy_items(session)
    delivery_gaps = await _delivery_gap_items(session)
    since = await _urgent_lookback_since(session, now)
    broker_errors, unresolved_urgent_events = await _broker_error_and_urgent_items(session, since)

    problem_count = _problem_count(halts, p1_actions, reconciliation_drift, partial_orders, flex_discrepancies)

    return AttentionResponse(
        generated_at=now.isoformat(),
        status="attention" if problem_count > 0 else "ok",
        headline=_headline(problem_count),
        problem_count=problem_count,
        sentinel_halt=sentinel_halt_active(),
        halts=halts,
        p1_actions=p1_actions,
        practice_reviews=practice_reviews,
        reconciliation_drift=reconciliation_drift,
        partial_orders=partial_orders,
        flex_discrepancies=flex_discrepancies,
        delivery_gaps=delivery_gaps,
        broker_errors=broker_errors,
        unresolved_urgent_events=unresolved_urgent_events,
    )

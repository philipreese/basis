"""research.py — the research brief's ledgers and the operator picks book (#1131).

Design: spec/research-brief.md (ADR-0018). The AI reads a frozen snapshot
(backend/research_snapshot.py) and writes a brief of candidates; every
candidate enters the AI shortlist's paper book; the operator marks PICK or
PASS on each, and buys the picks BY HAND in the live account. This module
writes those ledgers and attributes the operator's shares so reconciliation
expects them.

Invariants this module protects:

- Append-only. Every research row is written once and never edited
  (models._APPEND_ONLY_MODELS); a later fact is a new row linked to the
  earlier one. The track record is measured from rows written before the
  outcomes were known.
- A brief reads only a COMPLETE snapshot whose bytes still hash to what was
  recorded, and every candidate must be priced in that snapshot. A partial
  or altered snapshot can never feed the track record.
- The picks book's holdings ARE the signed net of its fill ledger. Nothing
  else writes them (they are not share_holdings rows), and reconciliation is
  their only automated reader (picks_book_expected_shares). So a recorded
  buy is expected, never drift, and never freezes B36; an unrecorded buy is
  an ordinary No-Stock P1 and halts as always (fail closed).
- A fill is never refused for being over the cap. The trade already
  happened at the broker; refusing the record would only turn it into drift
  that halts the whole account. It is recorded, and the book is halted.
- The cap is private (BASIS_MANUAL_CAP_<book> in .env.live), never in the
  repo. Unset or malformed reads as "no room": a PICK is refused.
- Writes run only against the live database: the picks are real money in
  the live account, and a pick fill recorded in the paper database would
  make paper reconciliation expect shares the paper account never holds.
"""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import (
    AuditEventModel,
    BookModel,
    OperatorPickFillModel,
    OperatorPickFillRequest,
    OperatorPickFillResult,
    OperatorPickFillSchema,
    OperatorPickModel,
    OperatorPickRequest,
    OperatorPickSchema,
    PicksBookView,
    PicksHoldingSchema,
    ResearchBriefCreate,
    ResearchBriefModel,
    ResearchBriefSchema,
    ResearchCandidateModel,
    ResearchCandidateSchema,
    ResearchShortlistPositionModel,
    ResearchSnapshotModel,
    ResearchSnapshotSchema,
)
from backend.seeds import PICKS_BOOK_ID
from backend.states import (
    BOOK_MANUAL_STATUS,
    PICK_DECISION_PICK,
    PICK_FILL_BUY,
    PICK_FILL_SELL,
    RESEARCH_KINDS,
    SNAPSHOT_COMPLETE_STATUS,
    SNAPSHOT_INCOMPLETE_STATUS,
    SNAPSHOT_STATUSES,
)
from backend.trading_control import ACTIVE, HALT_ENTRIES, check_trading_control, set_control

# The private hard cap of a manual book, read from the live overlay. A
# distinct prefix from BASIS_LIVE_STAKE_*: that one is merged into the book
# config as stage1_stake and arms the stage-1 machinery, which a manual book
# must never carry.
MANUAL_CAP_VAR_PREFIX = "BASIS_MANUAL_CAP_"
PICKS_CAP_BREACH = "PICKS_CAP_BREACH"
SHORTLIST_WEIGHT = 1.0  # equal weight: every candidate is one unit
_QTY_TOLERANCE = 1e-6


class ResearchError(ValueError):
    """A research write was refused. The message says why; nothing was written."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def manual_cap_var(book_id: str) -> str:
    return f"{MANUAL_CAP_VAR_PREFIX}{book_id}"


def private_manual_cap(book_id: str, env: Mapping[str, str] | None = None) -> float | None:
    """The manual book's private hard cap, or None when unset. A set but
    malformed value raises ValueError, never a silent "no cap". Messages
    name the setting, never its value."""
    name = manual_cap_var(book_id)
    raw = (os.environ if env is None else env).get(name)
    if raw is None or not raw.strip():
        return None
    try:
        cap = float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} is not a number") from exc
    if not math.isfinite(cap) or cap <= 0:
        raise ValueError(f"{name} must be a finite, positive number")
    return cap


def _cap_or_none(book_id: str) -> float | None:
    try:
        return private_manual_cap(book_id)
    except ValueError:
        return None


def _require_live() -> None:
    from backend.database import TRADING_MODE  # lazy: database imports the seeds chain

    if TRADING_MODE != "live":
        raise ResearchError(
            "research records are written to the live database only (the picks are real money in the "
            "live account) — use the live console or a live-overlay task"
        )


# ---------------------------------------------------------------------------
# Holdings: the picks book's shares are the net of its fill ledger
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Holding:
    symbol: str
    quantity: float
    cost_basis: float  # net shares x average buy cost (commissions included)


def holdings_from_fills(fills: list[OperatorPickFillModel]) -> dict[str, Holding]:
    """Average-cost holdings per symbol, in fill order. A buy adds its shares
    and its cost (price x shares + commission); a sell removes shares at the
    running average cost. A symbol sold out drops from the result."""
    qty: dict[str, float] = {}
    cost: dict[str, float] = {}
    for f in sorted(fills, key=lambda r: r.id):
        held = qty.get(f.symbol, 0.0)
        basis = cost.get(f.symbol, 0.0)
        if f.side == PICK_FILL_BUY:
            qty[f.symbol] = held + f.quantity
            cost[f.symbol] = basis + f.quantity * f.price + f.commission
        else:
            avg = basis / held if held > _QTY_TOLERANCE else 0.0
            qty[f.symbol] = held - f.quantity
            cost[f.symbol] = max(0.0, basis - avg * f.quantity)
    return {
        s: Holding(symbol=s, quantity=q, cost_basis=cost[s]) for s, q in sorted(qty.items()) if abs(q) > _QTY_TOLERANCE
    }


async def _manual_book_ids(session: AsyncSession) -> frozenset[str]:
    rows = (await session.execute(select(BookModel.id, BookModel.status))).all()
    return frozenset(book_id for book_id, status in rows if status == BOOK_MANUAL_STATUS)


async def picks_book_expected_shares(session: AsyncSession) -> dict[str, float]:
    """What reconciliation must EXPECT the broker to hold for the manual
    books: symbol -> net recorded shares. Only fills on a book that exists
    with status MANUAL count, so a fill row on any other book (or one whose
    book row is missing) is ignored and the broker's shares read as drift
    — fail closed."""
    manual = await _manual_book_ids(session)
    if not manual:
        return {}
    fills = (
        (await session.execute(select(OperatorPickFillModel).filter(OperatorPickFillModel.book_id.in_(manual))))
        .scalars()
        .all()
    )
    return {s: h.quantity for s, h in holdings_from_fills(list(fills)).items()}


async def picks_exec_ids(session: AsyncSession) -> frozenset[str]:
    """IBKR execIds the operator recorded against picks: the hand-placed
    executions reconciliation should treat as known, not unknown-ref."""
    rows = (
        await session.execute(select(OperatorPickFillModel.exec_id).filter(OperatorPickFillModel.exec_id.is_not(None)))
    ).scalars()
    return frozenset(r for r in rows if r)


async def _book_fills(session: AsyncSession, book_id: str) -> list[OperatorPickFillModel]:
    return list((await session.execute(select(OperatorPickFillModel).filter_by(book_id=book_id))).scalars().all())


def committed_cost(holdings: dict[str, Holding]) -> float:
    return sum(h.cost_basis for h in holdings.values())


# ---------------------------------------------------------------------------
# Snapshots and briefs
# ---------------------------------------------------------------------------


async def record_snapshot(
    session: AsyncSession,
    *,
    snapshot_id: str,
    kind: str,
    as_of: str,
    created_at: str,
    path: str,
    content_hash: str,
    status: str,
    reasons: list[str],
    counts: dict[str, int],
) -> ResearchSnapshotModel:
    """Write a snapshot's one row. COMPLETE must carry no reasons and
    INCOMPLETE must carry at least one: a partial snapshot always says why."""
    _require_live()
    if kind not in RESEARCH_KINDS:
        raise ResearchError(f"unknown snapshot kind {kind!r}")
    if status not in SNAPSHOT_STATUSES:
        raise ResearchError(f"unknown snapshot status {status!r}")
    if status == SNAPSHOT_COMPLETE_STATUS and reasons:
        raise ResearchError("a COMPLETE snapshot cannot carry reasons — it would hide a partial one")
    if status == SNAPSHOT_INCOMPLETE_STATUS and not reasons:
        raise ResearchError("an INCOMPLETE snapshot must say why")
    row = ResearchSnapshotModel(
        id=snapshot_id,
        kind=kind,
        as_of=as_of,
        created_at=created_at,
        path=path,
        content_hash=content_hash,
        status=status,
        reasons=list(reasons),
        counts=dict(counts),
    )
    session.add(row)
    await session.commit()
    return row


async def record_brief(session: AsyncSession, brief: ResearchBriefCreate) -> ResearchBriefModel:
    """Write a brief, its candidates, and one equal-weight shortlist position
    per candidate, entered at the snapshot's frozen close.

    Refused (nothing written) when the snapshot is missing, INCOMPLETE, of
    another kind, or no longer hashes to its recorded content_hash; or when a
    candidate repeats, or is not priced in the snapshot (no hallucinated
    tickers, and no price the AI could not have seen)."""
    from backend.research_snapshot import SnapshotIntegrityError, load_snapshot_prices

    _require_live()
    snapshot = await session.get(ResearchSnapshotModel, brief.snapshot_id)
    if snapshot is None:
        raise ResearchError(f"no snapshot {brief.snapshot_id!r}")
    if snapshot.status != SNAPSHOT_COMPLETE_STATUS:
        raise ResearchError(f"snapshot {snapshot.id} is {snapshot.status} — a brief needs a COMPLETE snapshot")
    if snapshot.kind != brief.kind:
        raise ResearchError(f"a {brief.kind} brief cannot read a {snapshot.kind} snapshot")
    try:
        prices = load_snapshot_prices(snapshot.path, snapshot.content_hash)
    except SnapshotIntegrityError as exc:
        raise ResearchError(f"snapshot {snapshot.id} failed its integrity check: {exc}") from exc
    symbols = [c.symbol.strip().upper() for c in brief.candidates]
    if len(set(symbols)) != len(symbols):
        raise ResearchError("a brief lists each candidate once")
    unpriced = [s for s in symbols if s not in prices]
    if unpriced:
        raise ResearchError(f"not priced in snapshot {snapshot.id}: {', '.join(unpriced)}")

    now = _now()
    row = ResearchBriefModel(
        snapshot_id=snapshot.id,
        kind=brief.kind,
        model_id=brief.model_id,
        prompt_hash=brief.prompt_hash,
        summary=brief.summary,
        created_at=now,
    )
    session.add(row)
    await session.flush()
    for symbol, c in zip(symbols, brief.candidates, strict=True):
        candidate = ResearchCandidateModel(
            brief_id=row.id,
            symbol=symbol,
            thesis=c.thesis,
            risks=c.risks,
            proves_wrong=c.proves_wrong,
            snapshot_price=prices[symbol],
        )
        session.add(candidate)
        await session.flush()
        session.add(
            ResearchShortlistPositionModel(
                candidate_id=candidate.id,
                brief_id=row.id,
                symbol=symbol,
                opened_on=snapshot.as_of,
                entry_price=prices[symbol],
                weight=SHORTLIST_WEIGHT,
                created_at=now,
            )
        )
    await session.commit()
    return row


# ---------------------------------------------------------------------------
# The operator's picks
# ---------------------------------------------------------------------------


async def _picks_book(session: AsyncSession) -> BookModel:
    book = await session.get(BookModel, PICKS_BOOK_ID)
    if book is None or book.status != BOOK_MANUAL_STATUS:
        raise ResearchError(
            f"the picks book {PICKS_BOOK_ID} is missing or not a MANUAL book — start the backend once so init_db seeds it"
        )
    return book


async def record_pick(session: AsyncSession, req: OperatorPickRequest) -> OperatorPickModel:
    """Mark PICK or PASS on a candidate, timestamped now by the server.

    One decision per candidate. A PASS is always recorded. A PICK is refused
    while the picks book (or GLOBAL) is not ACTIVE — it is seeded halted and
    waits for an operator RESUME — or while the private cap leaves no room
    (unset or malformed reads as none)."""
    _require_live()
    book = await _picks_book(session)
    candidate = await session.get(ResearchCandidateModel, req.candidate_id)
    if candidate is None:
        raise ResearchError(f"no candidate {req.candidate_id}")
    existing = (
        await session.execute(select(OperatorPickModel).filter_by(candidate_id=req.candidate_id))
    ).scalar_one_or_none()
    if existing is not None:
        raise ResearchError(f"candidate {req.candidate_id} already has a decision ({existing.decision})")
    if req.decision == PICK_DECISION_PICK:
        scope, state = await check_trading_control(session, book.id)
        if state != ACTIVE:
            raise ResearchError(f"{scope} is {state} — a PICK waits for an operator RESUME of {book.id}")
        cap = _cap_or_none(book.id)
        if cap is None:
            raise ResearchError(f"{manual_cap_var(book.id)} is unset or malformed in .env.live — no PICK without a cap")
        if committed_cost(holdings_from_fills(await _book_fills(session, book.id))) >= cap:
            raise ResearchError(f"{book.id} has no room left under its private cap")
    pick = OperatorPickModel(
        candidate_id=candidate.id,
        book_id=book.id,
        symbol=candidate.symbol,
        decision=req.decision,
        decided_at=_now(),
        note=req.note,
    )
    session.add(pick)
    await session.commit()
    return pick


async def record_pick_fill(session: AsyncSession, pick_id: int, req: OperatorPickFillRequest) -> OperatorPickFillResult:
    """Record one hand-placed execution on a PICK.

    Refused (nothing written) for a PASS or unknown pick, a duplicate
    exec_id, or a SELL of more shares than the pick holds — the broker could
    not have done that without short stock, so the record would be wrong.
    Never refused for the cap: an over-cap BUY is recorded, then the book is
    halted and a PICKS_CAP_BREACH urgent event is written."""
    _require_live()
    pick = await session.get(OperatorPickModel, pick_id)
    if pick is None:
        raise ResearchError(f"no pick {pick_id}")
    if pick.decision != PICK_DECISION_PICK:
        raise ResearchError(f"pick {pick_id} is a {pick.decision} — only a PICK can carry fills")
    if req.exec_id is not None:
        dup = (
            await session.execute(select(OperatorPickFillModel.id).filter_by(exec_id=req.exec_id))
        ).scalar_one_or_none()
        if dup is not None:
            raise ResearchError(f"execution {req.exec_id} is already recorded (fill {dup})")
    if req.side == PICK_FILL_SELL:
        own = (await session.execute(select(OperatorPickFillModel).filter_by(pick_id=pick.id))).scalars().all()
        held = holdings_from_fills(list(own)).get(pick.symbol)
        held_qty = held.quantity if held is not None else 0.0
        if req.quantity > held_qty + _QTY_TOLERANCE:
            raise ResearchError(
                f"pick {pick_id} holds {held_qty:g} {pick.symbol}; cannot record a sell of {req.quantity:g}"
            )

    fill = OperatorPickFillModel(
        pick_id=pick.id,
        book_id=pick.book_id,
        symbol=pick.symbol,
        side=req.side,
        quantity=req.quantity,
        price=req.price,
        commission=req.commission,
        executed_at=req.executed_at,
        recorded_at=_now(),
        exec_id=req.exec_id,
    )
    session.add(fill)
    await session.flush()

    breached = False
    note: str | None = None
    if req.side == PICK_FILL_BUY:
        cap = _cap_or_none(pick.book_id)
        committed = committed_cost(holdings_from_fills(await _book_fills(session, pick.book_id)))
        if cap is None or committed > cap:
            breached = True
            reason = (
                f"{pick.book_id} is over its private cap after a {pick.symbol} buy — no new PICK until reviewed"
                if cap is not None
                else f"{pick.book_id} recorded a buy with {manual_cap_var(pick.book_id)} unset or malformed"
            )
            note = reason
            session.add(
                AuditEventModel(
                    run_at=_now(),
                    book_id=pick.book_id,
                    event_type=PICKS_CAP_BREACH,
                    actor="research",
                    payload={"pick_id": pick.id, "fill_id": fill.id, "symbol": pick.symbol, "reason": reason},
                )
            )
            await set_control(session, pick.book_id, HALT_ENTRIES, reason=reason, actor="research")
    await session.commit()
    return OperatorPickFillResult(fill=fill_schema(fill), cap_breached=breached, note=note)


# ---------------------------------------------------------------------------
# Read views
# ---------------------------------------------------------------------------


def snapshot_schema(row: ResearchSnapshotModel) -> ResearchSnapshotSchema:
    return ResearchSnapshotSchema(
        id=row.id,
        kind=row.kind,
        as_of=row.as_of,
        created_at=row.created_at,
        path=row.path,
        content_hash=row.content_hash,
        status=row.status,
        reasons=list(row.reasons or []),
        counts=dict(row.counts or {}),
    )


def pick_schema(row: OperatorPickModel) -> OperatorPickSchema:
    return OperatorPickSchema(
        id=row.id,
        candidate_id=row.candidate_id,
        book_id=row.book_id,
        symbol=row.symbol,
        decision=row.decision,
        decided_at=row.decided_at,
        note=row.note,
    )


def fill_schema(row: OperatorPickFillModel) -> OperatorPickFillSchema:
    return OperatorPickFillSchema(
        id=row.id,
        pick_id=row.pick_id,
        book_id=row.book_id,
        symbol=row.symbol,
        side=row.side,
        quantity=row.quantity,
        price=row.price,
        commission=row.commission,
        executed_at=row.executed_at,
        recorded_at=row.recorded_at,
        exec_id=row.exec_id,
    )


async def list_snapshots(session: AsyncSession, limit: int = 30) -> list[ResearchSnapshotSchema]:
    rows = (
        (
            await session.execute(
                select(ResearchSnapshotModel).order_by(ResearchSnapshotModel.created_at.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [snapshot_schema(r) for r in rows]


async def list_briefs(session: AsyncSession, limit: int = 30) -> list[ResearchBriefSchema]:
    briefs = (
        (await session.execute(select(ResearchBriefModel).order_by(ResearchBriefModel.id.desc()).limit(limit)))
        .scalars()
        .all()
    )
    out: list[ResearchBriefSchema] = []
    for b in briefs:
        candidates = (
            (
                await session.execute(
                    select(ResearchCandidateModel).filter_by(brief_id=b.id).order_by(ResearchCandidateModel.id)
                )
            )
            .scalars()
            .all()
        )
        out.append(
            ResearchBriefSchema(
                id=b.id,
                snapshot_id=b.snapshot_id,
                kind=b.kind,
                model_id=b.model_id,
                prompt_hash=b.prompt_hash,
                summary=b.summary,
                created_at=b.created_at,
                candidates=[
                    ResearchCandidateSchema(
                        id=c.id,
                        brief_id=c.brief_id,
                        symbol=c.symbol,
                        thesis=c.thesis,
                        risks=c.risks,
                        proves_wrong=c.proves_wrong,
                        snapshot_price=c.snapshot_price,
                    )
                    for c in candidates
                ],
            )
        )
    return out


async def picks_book_view(session: AsyncSession) -> PicksBookView:
    book = await _picks_book(session)
    _, state = await check_trading_control(session, book.id)
    holdings = holdings_from_fills(await _book_fills(session, book.id))
    committed = committed_cost(holdings)
    cap = _cap_or_none(book.id)
    picks = (
        (await session.execute(select(OperatorPickModel).filter_by(book_id=book.id).order_by(OperatorPickModel.id)))
        .scalars()
        .all()
    )
    return PicksBookView(
        book_id=book.id,
        control_state=state,
        cap_configured=cap is not None,
        committed_cost=committed,
        cap_headroom=(max(0.0, cap - committed) if cap is not None else None),
        holdings=[
            PicksHoldingSchema(symbol=h.symbol, quantity=h.quantity, cost_basis=h.cost_basis) for h in holdings.values()
        ],
        picks=[pick_schema(p) for p in picks],
    )

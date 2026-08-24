"""book_gates.py — per-book risk-envelope gates and capital encumbrance (#67).

Shared-capital isolation is arithmetic (design §4.1): every gate evaluates
against the book's VIRTUAL ledger — the $10K book basis — never the paper
account's real balance. Every evaluation, pass or block, is written to the
append-only gate_events table, which is what makes the Live Gate's "zero
breaches" criterion a table scan (ADR-0006).

Encumbrance: capital reserved by a staged/pending OPEN order counts toward
the deployed gate until the order reaches a terminal status. Without it, two
same-evening candidates in one book could both pass the deployed gate.
Persisted on the orders row so a crash cannot forget a reservation.
"""

import logging
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import AuditEventModel, BookModel, GateEventModel, OrderModel, PositionModel
from backend.pricing import capital_at_risk
from backend.states import ORDER_PENDING_STATUSES, POSITION_OPEN_STATUS

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Envelope:
    """ADR-0006 risk envelope; defaults here, a book's config {"envelope": {...}}
    overrides per field. max_positions raised 4 → 8 for the accelerated cadence
    (#136, ADR-0009): 8 × ~$250 max loss = 20% deployed, still far under the 50%
    cap — the old 4 was the binding constraint on trade-count accumulation, not
    a risk limit."""

    basis: float = 10_000.0
    max_loss_pct_per_trade: float = 2.5
    max_deployed_pct: float = 50.0
    max_positions: int = 8
    max_same_strategy_expiry: int = 2


_ENVELOPE_INT_FIELDS = frozenset({"max_positions", "max_same_strategy_expiry"})


@dataclass(frozen=True)
class BookConfig:
    """A book's config dict resolved once into typed fields — the only way any
    module reads book.config. variant/underlying stay optional: display callers
    render a fallback, and B00-legacy configs predate them."""

    envelope: Envelope = Envelope()
    variant: str | None = None
    underlying: str | None = None
    ignore_regime: bool = False
    ignore_ivr: bool = False
    playbook_ids: tuple[str, ...] | None = None
    playbook_overrides: dict[str, object] = field(default_factory=dict)
    # B28's exit-side knob (#254): close positions whose current variant
    # regime no longer matches the regime they were entered under.
    exit_on_regime_flip: bool = False
    # B29's entry-side knob (#316): entries require at least this many raced
    # engines to agree with this book's own reading. 0 = no consensus gate.
    require_consensus: int = 0
    # B31's knob (#318): when the mandatory time exit fires on a LOSING
    # position, stage a roll-out entry (same strikes, next cycle) alongside
    # the close instead of just walking away.
    roll_time_exits: bool = False
    # B32's knob (#411): skip an entry while an open position from the SAME
    # playbook is not yet in its exit window (DTE > mandatory_exit_dte).
    # Without it an always-on playbook with max_positions 2 fills BOTH slots
    # in steady state — ADR-0012's second slot exists for roll-night overlap
    # only, not double bleed.
    dedup_playbook_entries: bool = False


def resolve_book_config(config: dict | None) -> BookConfig:
    """Resolve a raw book.config dict. Unknown envelope keys raise so a typo in
    a seeded book fails loudly at resolve time instead of silently merging and
    silently doing nothing. Top-level keys stay permissive (B00 legacy)."""
    cfg = config or {}
    env_overrides = cfg.get("envelope") or {}
    valid = {f.name for f in fields(Envelope)}
    unknown = set(env_overrides) - valid
    if unknown:
        raise ValueError(f"Unknown envelope key(s) {sorted(unknown)} — valid keys: {sorted(valid)}")
    envelope = replace(
        Envelope(),
        **{k: (int(v) if k in _ENVELOPE_INT_FIELDS else float(v)) for k, v in env_overrides.items()},
    )
    ids = cfg.get("playbook_ids")
    return BookConfig(
        envelope=envelope,
        variant=cfg.get("engine_variant"),
        underlying=cfg.get("underlying"),
        ignore_regime=bool(cfg.get("ignore_regime", False)),
        ignore_ivr=bool(cfg.get("ignore_ivr", False)),
        playbook_ids=tuple(ids) if ids else None,
        playbook_overrides=dict(cfg.get("playbook_overrides") or {}),
        exit_on_regime_flip=bool(cfg.get("exit_on_regime_flip", False)),
        require_consensus=int(cfg.get("require_consensus", 0)),
        roll_time_exits=bool(cfg.get("roll_time_exits", False)),
        dedup_playbook_entries=bool(cfg.get("dedup_playbook_entries", False)),
    )


# Order statuses whose encumbrance still counts (non-terminal, capital
# reserved). #674: this name is kept as the re-exported alias every other
# module already imports (executor.py et al.) — the actual vocabulary lives
# in backend/states.py now, centralized.
PENDING_ORDER_STATUSES = ORDER_PENDING_STATUSES

# ADR-0006 Live Gate: ≥30 closed paper trades per book config before live money
LIVE_GATE_TRADES = 30

PASS = "PASS"
BLOCK = "BLOCK"


@dataclass(frozen=True)
class CandidateOrder:
    """The gate-relevant shape of a would-be entry, independent of playbook."""

    book_id: str
    strategy_type: str
    expiration_date: str  # ISO date
    legs: tuple[tuple[str, str], ...]  # (occ_symbol, "LONG" | "SHORT")
    max_loss_per_share: float
    contracts: int

    @property
    def risk_dollars(self) -> float:
        return capital_at_risk(self.max_loss_per_share, self.contracts)


@dataclass(frozen=True)
class GateOutcome:
    gate: str
    result: str  # PASS | BLOCK
    detail: str


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    outcomes: tuple[GateOutcome, ...]

    def blocked_by(self) -> tuple[str, ...]:
        return tuple(o.gate for o in self.outcomes if o.result == BLOCK)


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _audit(session: AsyncSession, event_type: str, book_id: str | None, payload: dict) -> None:
    session.add(
        AuditEventModel(run_at=_now(), book_id=book_id, event_type=event_type, actor="book_gates", payload=payload)
    )


async def credit_book_cash(session: AsyncSession, book_id: str, delta: float) -> float | None:
    """The ONLY way to move book cash (#462): a SQL-side increment.

    Every former `book.cash_balance += x` was a read-modify-write on a
    possibly-stale ORM instance — the executor's night-long session and the
    console's request sessions interleave, and the last flush silently erased
    the other side's movement while its audit row still claimed it landed
    (reconciliation compares leg quantities, never cash, so nothing flagged
    it). An increment computed IN SQL is commutative and interleaving-proof.

    Returns the new balance (fresh-read), or None when the book is unknown.
    """
    result = await session.execute(
        update(BookModel).where(BookModel.id == book_id).values(cash_balance=BookModel.cash_balance + delta)
    )
    if result.rowcount == 0:
        return None
    book = await session.get(BookModel, book_id)
    if book is not None:
        # Refresh the identity-map instance so later same-session reads
        # (MTM sweep, digest) see the post-increment value, not the stale
        # attribute. refresh() is async-safe; lazy expiry is not.
        await session.refresh(book, ["cash_balance"])
        return book.cash_balance
    return None


async def _book_open_positions(session: AsyncSession, book_id: str) -> list[PositionModel]:
    rows = await session.execute(select(PositionModel).filter_by(status=POSITION_OPEN_STATUS, book_id=book_id))
    return list(rows.scalars().all())


async def _pending_open_orders(session: AsyncSession, book_id: str) -> list[OrderModel]:
    rows = await session.execute(
        select(OrderModel).filter(
            OrderModel.book_id == book_id,
            OrderModel.action == "OPEN",
            OrderModel.status.in_(PENDING_ORDER_STATUSES),
        )
    )
    return list(rows.scalars().all())


async def evaluate_book_gates(session: AsyncSession, candidate: CandidateOrder) -> GateDecision:
    """Run every envelope gate for one candidate and log each outcome.

    Fail-closed: an unknown or non-ACTIVE book blocks outright. The decision
    commits its gate_events before returning, so the record exists even if
    the caller subsequently aborts.
    """
    outcomes: list[GateOutcome] = []
    book = await session.get(BookModel, candidate.book_id)
    if book is None or book.status != "ACTIVE":
        status = "missing" if book is None else book.status
        outcomes.append(GateOutcome("BOOK_ACTIVE", BLOCK, f"book {candidate.book_id} is {status}"))
        await _log_outcomes(session, candidate.book_id, outcomes)
        return GateDecision(allowed=False, outcomes=tuple(outcomes))

    envelope = resolve_book_config(book.config).envelope
    open_positions = await _book_open_positions(session, candidate.book_id)
    pending_orders = await _pending_open_orders(session, candidate.book_id)

    risk = candidate.risk_dollars
    max_loss_cap = envelope.basis * envelope.max_loss_pct_per_trade / 100.0
    outcomes.append(
        GateOutcome(
            "MAX_LOSS_PER_TRADE",
            PASS if risk <= max_loss_cap else BLOCK,
            f"risk ${risk:.0f} vs cap ${max_loss_cap:.0f} ({envelope.max_loss_pct_per_trade}% of ${envelope.basis:.0f})",
        )
    )

    deployed = sum(capital_at_risk(p.max_loss, p.contracts) for p in open_positions)
    encumbered = sum(o.encumbered_risk for o in pending_orders)
    deployed_cap = envelope.basis * envelope.max_deployed_pct / 100.0
    total_after = deployed + encumbered + risk
    outcomes.append(
        GateOutcome(
            "MAX_DEPLOYED",
            PASS if total_after <= deployed_cap else BLOCK,
            f"deployed ${deployed:.0f} + encumbered ${encumbered:.0f} + candidate ${risk:.0f}"
            f" = ${total_after:.0f} vs cap ${deployed_cap:.0f}",
        )
    )

    slots_used = len(open_positions) + len(pending_orders)
    outcomes.append(
        GateOutcome(
            "MAX_POSITIONS",
            PASS if slots_used + 1 <= envelope.max_positions else BLOCK,
            f"{slots_used} open/pending + 1 vs max {envelope.max_positions}",
        )
    )

    # #679: open positions alone are not the book's full same-bucket
    # exposure — a STAGED/SUBMITTED/PARTIAL OPEN order for the SAME
    # strategy/expiry, evaluated earlier in the same Layer C pass tonight
    # (or resting from a prior night, not yet filled/synced), is real
    # intended concentration the broker will hold once it fills. Exactly
    # the #665/#670 bug class for the cross-book netting gate, applied
    # here: without this, two same-bucket candidates evaluated back-to-back
    # both see only the (unchanged) open-positions count and both pass.
    # CLOSE orders are excluded by _pending_open_orders itself
    # (action == "OPEN" only) for the same reason #665 excluded them from
    # netting: a close reduces this bucket, it never adds to it.
    same_bucket_open = sum(
        1
        for p in open_positions
        if p.strategy_type == candidate.strategy_type and p.expiration_date == candidate.expiration_date
    )
    same_bucket_pending = sum(
        1
        for o in pending_orders
        if (o.combo_legs or {}).get("strategy_type") == candidate.strategy_type
        and (o.combo_legs or {}).get("expiration_date") == candidate.expiration_date
    )
    same_bucket = same_bucket_open + same_bucket_pending
    outcomes.append(
        GateOutcome(
            "STRATEGY_EXPIRY_CONCENTRATION",
            PASS if same_bucket + 1 <= envelope.max_same_strategy_expiry else BLOCK,
            f"{same_bucket_open} open + {same_bucket_pending} pending sharing "
            f"{candidate.strategy_type}@{candidate.expiration_date} vs max {envelope.max_same_strategy_expiry}",
        )
    )

    outcomes.append(await _cross_book_netting_outcome(session, candidate))

    await _log_outcomes(session, candidate.book_id, outcomes)
    return GateDecision(allowed=all(o.result == PASS for o in outcomes), outcomes=tuple(outcomes))


async def _cross_book_netting_outcome(session: AsyncSession, candidate: CandidateOrder) -> GateOutcome:
    """Hard-block opposite-direction exposure on the same contract anywhere in
    the account. The broker nets per conId, so opposite directions net to flat
    at the broker — making broker state ambiguous and exercise/expiry
    unattributable (design §4.3). Same-direction sharing is fine.

    #665: OPEN positions alone are not the account's full exposure — a
    STAGED/SUBMITTED OPEN order (same run, an earlier book tonight; or a
    resting order from a PRIOR night not yet filled/synced) is real intended
    exposure the broker will hold once it fills, and it must block an
    opposite-direction candidate exactly like an already-filled position
    would. PARTIAL is pending too (book_gates.PENDING_ORDER_STATUSES — the
    same set the encumbrance gate already trusts for "this order still
    reserves capital"). Global, not book-scoped: netting is an account-wide
    broker-conId fact, not a per-book one.

    A PARTIAL order's `combo_legs["legs"]` is read whole below — every leg
    staged with the order, not just the legs that have actually filled so
    far — and that is deliberate, not an oversight to later tighten to
    leg-level filled-only accounting. A resting combo order is one order at
    the broker: a PARTIAL fill leaves its UNFILLED legs still live and
    working, still capable of completing the position exactly as staged, so
    treating them as not-yet-real exposure would let an opposite-direction
    candidate race in against a leg the broker could fill moments later —
    reopening the exact netting gap this gate exists to close. Whole-order
    counting for a still-pending order is the conservative, correct read; a
    future "fix" to filled-legs-only accounting would silently reintroduce
    the gap.

    CLOSE orders are deliberately excluded, not merely omitted: an in-flight
    close's combo_legs mirror the POSITION being closed (SELL-the-bag
    reverses the ORDER's execution side, never the stored "direction" field
    — see executor.py's entry/close order construction, both of which copy
    the position-oriented LONG/SHORT straight from pos.legs / legs_meta).
    So a close's legs are exactly the already-open position's legs, already
    counted by the OPEN-positions query above — adding them again would be
    redundant at best, and if a future refactor ever inverted that
    convention, wrongly reads "closing a LONG" as new SHORT exposure. The
    position itself, not its in-flight close order, is the source of truth
    for held direction until the close actually fills.

    #690: a single order stuck RESTORE_GAP_UNKNOWN_HELD (#542/#653 — no
    reconciliation baseline, or restore gap > 1 trading day, so the sync
    holds it indefinitely rather than guessing) still counts here, same as
    any other pending order — that's the intended, safe failure direction
    (block, don't risk broker-netting ambiguity on stale trust), kept as
    the operator's explicit decision on this issue. But its blast radius is
    account-wide (this query has no book_id filter), not scoped to its own
    book, so a single unresolved held order can silently block a DIFFERENT,
    healthy book's candidate every night with nothing distinguishing that
    from an ordinary live-order netting conflict. When a blocking leg
    belongs to an order that has ever logged RESTORE_GAP_UNKNOWN_HELD (and
    is still non-terminal, or it wouldn't be in pending_open_orders at all —
    resolution.py always terminalizes before an order leaves this query),
    a NETTING_BLOCKED_BY_HELD_ORDER audit event fires with both the held
    order's ref and the blocked candidate's book, so the cost is visible
    and correlatable instead of reading as an unexplained generic block.
    """
    from backend.market_data import format_occ_symbol

    open_positions = (
        (await session.execute(select(PositionModel).filter_by(status=POSITION_OPEN_STATUS))).scalars().all()
    )
    pending_open_orders = (
        (
            await session.execute(
                select(OrderModel).filter(OrderModel.action == "OPEN", OrderModel.status.in_(PENDING_ORDER_STATUSES))
            )
        )
        .scalars()
        .all()
    )

    held: dict[str, set[str]] = {}
    # (occ, direction) -> order_refs of PENDING orders contributing that leg
    # — only orders (never positions) can ever be RESTORE_GAP_UNKNOWN_HELD.
    held_by_order_ref: dict[tuple[str, str], set[str]] = {}
    for pos in open_positions:
        for leg in pos.legs:
            occ = format_occ_symbol(pos.underlying, leg["expiration"], leg["option_type"], leg["strike"])
            held.setdefault(occ, set()).add(leg["direction"])
    for order in pending_open_orders:
        for leg in order.combo_legs.get("legs", []):
            # Entry-order legs_meta always precomputes "occ" (executor.py) —
            # falling back to a recompute only guards a hypothetical future
            # combo_legs shape that omits it.
            occ = leg.get("occ") or format_occ_symbol(
                order.combo_legs.get("underlying", ""), leg["expiration"], leg["option_type"], leg["strike"]
            )
            held.setdefault(occ, set()).add(leg["direction"])
            held_by_order_ref.setdefault((occ, leg["direction"]), set()).add(order.order_ref)

    for occ, direction in candidate.legs:
        opposite = "SHORT" if direction == "LONG" else "LONG"
        if opposite in held.get(occ, set()):
            blocking_refs = held_by_order_ref.get((occ, opposite), set())
            if blocking_refs:
                held_order_refs = (
                    (await session.execute(select(AuditEventModel).filter_by(event_type="RESTORE_GAP_UNKNOWN_HELD")))
                    .scalars()
                    .all()
                )
                stuck_refs = {e.payload.get("order_ref") for e in held_order_refs} & blocking_refs
                for ref in stuck_refs:
                    await _audit(
                        session,
                        "NETTING_BLOCKED_BY_HELD_ORDER",
                        candidate.book_id,
                        {
                            "held_order_ref": ref,
                            "candidate_book_id": candidate.book_id,
                            "candidate_strategy_type": candidate.strategy_type,
                            "occ": occ,
                            "direction": direction,
                            "detail": (
                                f"{candidate.book_id} candidate ({candidate.strategy_type}, {occ} {direction}) "
                                f"blocked by restore-gap-held order {ref} — resolve it via the resolution panel"
                            ),
                        },
                    )
            return GateOutcome("CROSS_BOOK_NETTING", BLOCK, f"{occ}: candidate {direction} vs held {opposite}")
    return GateOutcome("CROSS_BOOK_NETTING", PASS, "no opposite-direction contract sharing")


async def _log_outcomes(session: AsyncSession, book_id: str, outcomes: list[GateOutcome]) -> None:
    now = _now()
    for o in outcomes:
        session.add(
            GateEventModel(book_id=book_id, run_at=now, gate=o.gate, result=o.result, context={"detail": o.detail})
        )
    await session.commit()


async def stage_order(
    session: AsyncSession,
    candidate: CandidateOrder,
    *,
    order_id: str,
    order_ref: str,
    limit_price: float,
    decision_midpoint: float,
    combo_legs: dict,
    quote_snapshot: dict | None = None,
) -> OrderModel:
    """Write the intent row (STAGED) with its capital encumbrance — BEFORE
    placeOrder, per the idempotency contract (design §2.4). The encumbrance
    holds until release_order() moves the row to a terminal status."""
    # Decision-time config fingerprint (#534): the gates just evaluated THIS
    # config; a seed-sync landing before the fill must not re-attribute the
    # trade. The position copies this hash at fill time.
    book = await session.get(BookModel, candidate.book_id)
    order = OrderModel(
        id=order_id,
        book_id=candidate.book_id,
        config_hash=book.config_hash if book is not None else None,
        position_id=None,
        order_ref=order_ref,
        ib_order_id=None,
        ib_perm_id=None,
        action="OPEN",
        combo_legs=combo_legs,
        order_type="LIMIT",
        limit_price=limit_price,
        decision_midpoint=decision_midpoint,
        status="STAGED",
        submitted_at=None,
        completed_at=None,
        encumbered_risk=candidate.risk_dollars,
        quote_snapshot=quote_snapshot,
    )
    session.add(order)
    await session.commit()
    return order


async def release_order(session: AsyncSession, order_id: str, final_status: str) -> None:
    """Move a pending order to a terminal status, releasing its encumbrance
    (the deployed gate only counts PENDING_ORDER_STATUSES)."""
    # FILLED deliberately absent (#481 F9): fills settle exclusively through
    # the executor's _order_to_position (position, cash, audits) — a
    # release_order("FILLED") would terminalize the row with none of that,
    # and no caller has ever needed it.
    if final_status not in ("CANCELLED", "REJECTED"):
        raise ValueError(f"Not a terminal order status: {final_status!r}")
    order = await session.get(OrderModel, order_id)
    if order is None:
        raise ValueError(f"No order {order_id!r}")
    order.status = final_status
    order.completed_at = _now()
    await session.commit()

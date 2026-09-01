"""anomaly.py — deterministic auto-halt rules (spec/supervision.md §6.2–6.3, #71).

Machine-checkable rules with IDs used verbatim in audit_events, halt
reasons, digests, and tests. Automatic responses stop at HALT_ENTRIES
(ADR-0008) — nothing here ever liquidates. Escalation only, with one narrow
carve-out (#927): a HALT_ENTRIES this module itself set may self-clear back
to ACTIVE once every rule that contributed to it is in the self-clearable
set (_SELF_CLEARABLE_RULES) and this sweep re-evaluated it clean — see
_self_clear_expired_halts. FLATTEN_REQUESTED is never downgraded, and an
operator- or ntfy-set halt never auto-lifts.

Wired by the executor: DUPLICATE_ORDER at entry-staging time, the rest as a
post-session pass. RECONCILIATION_DRIFT / UNEXPECTED_INSTRUMENT live in
backend/reconciliation.py; STALE_DATA and UNFILLED_ENTRY are pipeline
behaviors in backend/executor.py — same rule vocabulary, one enforcement
point each.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.book_gates import resolve_book_config
from backend.calendars import is_trading_day
from backend.dates import market_date_of, market_today
from backend.models import (
    AnomalyAlertStateModel,
    AuditEventModel,
    BookModel,
    BookMtmHistoryModel,
    FillModel,
    OrderModel,
    PositionModel,
    TradingControlModel,
)
from backend.pricing import capital_at_risk
from backend.states import (
    BOOK_ACTIVE_STATUS,
    ORDER_CANCELLED_OR_REJECTED_STATUSES,
    ORDER_PENDING_STATUSES,
    POSITION_OPEN_STATUS,
)
from backend.trading_control import ACTIVE, GLOBAL_SCOPE, HALT_ENTRIES, set_control

logger = logging.getLogger(__name__)

REPEATED_REJECTION = "REPEATED_REJECTION"
DUPLICATE_ORDER = "DUPLICATE_ORDER"
PNL_SHOCK = "PNL_SHOCK"
ENVELOPE_BREACH_POSTHOC = "ENVELOPE_BREACH_POSTHOC"
ZOMBIE_FILL = "ZOMBIE_FILL"
PREVIEW_INFRA_FAILURE = "PREVIEW_INFRA_FAILURE"
PARTIAL_FILL = "PARTIAL_FILL"

# Rejection-shaped audit event types this counter pools together (#744, the
# third instance of the outgrown-enumeration class after #665/#686 — the
# fix here is naming this constant explicitly and commenting the reasoning
# so it reads as a deliberate, reviewable set rather than an easy-to-outgrow
# tuple literal; states.py's mechanical tripwire is scoped to ORM `.status`
# predicates specifically and isn't a natural fit for audit event_type
# strings, so this stays a plain named constant rather than growing a
# second enforcement mechanism for one counter).
#
# ENTRY_PREVIEW_REFUSED (#628) predates this tuple: broker.preview_spread
# rejecting a candidate (executor.py's whatIf check, before ANY order
# reaches the broker) is pooled with real ORDER_REJECTED/CLOSE_REJECTED at
# the SAME threshold, deliberately. A broken builder/pricing path that
# repeatedly fails preview is the same failure mode this rule exists to
# catch — it just gets caught one step earlier, with strictly less broker
# exposure than a real rejection. Pooling at the same threshold is
# therefore conservative, not aggressive: it halts on the cheaper signal
# instead of waiting for it to escalate into real (costlier) rejections.
#
# Deliberately EXCLUDED (#820): ENTRY_REFUSED_THIN_CREDIT and the
# CANDIDATE_UNPRICEABLE family — our own decision-time QUALITY gates
# declining a candidate, not a broker or pipeline failure. A book whose
# knob refuses thin credits every night is the knob working as designed;
# pooling it here would let a deliberately strict config trip
# REPEATED_REJECTION and halt a healthy system.
_REJECTION_EVENTS = ("ORDER_REJECTED", "CLOSE_REJECTED", "ENTRY_PREVIEW_REFUSED")

PERMISSIONS_REFUSED = "PERMISSIONS_REFUSED"
CROSS_BOOK_ORDER_COLLISION = "CROSS_BOOK_ORDER_COLLISION"

# #853: preview refusals are not one failure mode. On 2026-08-27, fifteen
# ENTRY_PREVIEW_REFUSED events latched the global halt, but ten were the
# account-structural "open orders on both sides of the same contract" wall
# (expected at 34-books-one-account scale, now pre-empted by
# check_order_leg_collision), four were IBKR's riskless-combination cap
# (a per-candidate refusal, not a model-is-wrong signal), and two were
# missing strategy permissions (needs a human immediately, but scoped to
# the playbook). Pooling all three at one threshold produced the wrong
# blast radius in both directions. Classification is by the broker's own
# reason text, which broker.preview_spread now captures (same #853).
_COLLISION_REASON = "both sides of the same US Option"
_RISKLESS_REASON = "Riskless combination"
_PERMISSIONS_REASON = "trading permissions"

# #927: gateway/infra failures, not evidence our model of the broker's rules
# is wrong. broker.preview_spread raises these two exact shapes (see its
# docstring) when IBKR's whatIf answer itself is unusable — an API error in
# place of an order state, or the call hanging past CALL_TIMEOUT — as
# opposed to the broker actually evaluating and refusing the candidate. The
# 2026-08-27 burst (15 API-error events in a 5-minute window) was this class
# pooled with real broker-rule rejections: it kept re-tripping
# REPEATED_REJECTION's trailing-sessions bucket on later nights with zero
# new broker-rule evidence.
_INFRA_API_ERROR_REASON = "API error instead of an order state"
_INFRA_TIMEOUT_REASON = "timed out - no usable order state"


def classify_preview_refusal(reason: str) -> str:
    """One of 'collision' | 'riskless' | 'permissions' | 'infra' | 'other'
    for an ENTRY_PREVIEW_REFUSED payload reason. 'other' (including reasons
    from before the broker-text capture existed) stays pooled in the
    REPEATED_REJECTION counter — unknown refusals keep the conservative
    halting behavior; only positively identified classes are diverted."""
    if _COLLISION_REASON in reason:
        return "collision"
    if _RISKLESS_REASON in reason:
        return "riskless"
    if _PERMISSIONS_REASON in reason:
        return "permissions"
    if _INFRA_API_ERROR_REASON in reason or _INFRA_TIMEOUT_REASON in reason:
        return "infra"
    return "other"


PNL_SHOCK_PCT = 15.0  # of book basis; envelope-derived, re-derive once real fills exist


@dataclass(frozen=True)
class AnomalyFinding:
    rule: str
    scope: str  # GLOBAL or a book id
    detail: str
    # #853: a non-latching finding surfaces in the digest and audit trail at
    # full urgency but does NOT set HALT_ENTRIES — for failure classes where
    # halting all 34 books is the wrong blast radius (a permissions-refused
    # playbook affects only candidates of that playbook; the other books'
    # entries are not evidence-linked to it).
    latches: bool = True
    # #922/#924: one (kind, ratio) pair per structurally distinct sub-check
    # that is currently breaching, set by the check that can recur as a
    # STANDING condition on an already-open position (currently only
    # check_envelope_breach). Empty means "this rule doesn't get ntfy-push
    # dedup" — see _DEDUPED_RULES.
    #
    # *kind* is the dedup identity of ONE breaching condition — "count",
    # "deployed", "per_trade:{pos_id}" (one per breaching position),
    # "bucket:{strategy_type}@{expiration_date}" (one per breaching
    # concentration bucket) — each tracked and re-alerted independently
    # (own row in anomaly_alert_state, own baseline). This is what makes a
    # BRAND-NEW breach kind (a different gate bypass, e.g. a fresh
    # MAX_POSITIONS violation appearing alongside a standing per-trade
    # breach) alert on its own merits instead of being folded into one
    # finding-wide worst_ratio that a standing breach of a different,
    # structurally larger kind can silently dominate (#924 HIGH-1, MED-3 —
    # a count ratio and a dollar ratio are both dimensionless but not
    # severity-comparable, so one max() across kinds is wrong).
    #
    # *ratio* is the dimensionless measured/cap ratio (>1.0 means
    # breaching) for that one kind — comparable night over night WITHIN a
    # kind, never across kinds.
    sub_breaches: tuple[tuple[str, float], ...] = ()


# #922: ntfy-push dedup applies only to rules whose finding is a STANDING
# condition — the same open position sitting over the envelope stays a
# breach every run until it closes, so re-alerting on it nightly at full
# priority is fatigue, not signal (an operator who has seen it 3 times has
# seen it). REPEATED_REJECTION/ZOMBIE_FILL are scoped to events since this
# run's start, and PNL_SHOCK is scoped to tonight's MTM move — each firing
# there is a fresh, independent incident (two rejection nights in a row are
# two real incidents, not a repeat), so they are deliberately excluded, same
# idiom as _REJECTION_EVENTS above pooling only positively-identified
# classes.
_DEDUPED_RULES = frozenset({ENVELOPE_BREACH_POSTHOC})

# A repeat must exceed the LAST ALERTED magnitude by more than this fraction
# to re-alert (#922) — "crossing to a higher band" in the issue's language.
ALERT_INCREASE_THRESHOLD = 1.10


async def _should_alert(session: AsyncSession, finding: AnomalyFinding) -> bool:
    """True if *finding* should reach the urgent ntfy push; False if it
    should fold into the regular digest line instead. Governs the push
    only — the caller writes the audit ledger row and applies the halt
    latch for every occurrence regardless of this result (#922: the ledger
    must record every occurrence; only the push dedupes).

    Persisted in anomaly_alert_state (DB, not memory): the executor is a
    fresh process every run, so an in-memory cache would forget every prior
    alert and never suppress anything.

    #924: each (kind, ratio) pair in finding.sub_breaches is tracked and
    re-alerted INDEPENDENTLY — its own row, keyed
    f"{rule}|{scope}|{kind}", its own baseline. The finding as a whole
    alerts if ANY sub-breach is new or has crossed its own +10% band; a
    standing per-trade breach never masks a brand-new count/deployed/bucket
    breach (or vice versa) because they no longer share one key or one
    max()-across-kinds magnitude (HIGH-1, MED-3). A sub-breach's baseline
    only updates when IT alerts — a suppressed kind keeps its old baseline,
    same invariant as before, just per kind instead of per finding.

    Known gap, accepted: this marks a sub-breach alerted regardless of
    whether send_ntfy_with_retry (executor.py, after this returns) actually
    reaches ntfy — the two are not transactional. A push failure the night
    of a first occurrence means a following unchanged repeat is suppressed
    even though the operator never saw the original. Not silent either way:
    the digest body still carries the finding via summary.anomalies, and
    _control_banner reprints the standing HALT_ENTRIES line every night."""
    if finding.rule not in _DEDUPED_RULES or not finding.sub_breaches:
        return True
    alert = False
    for kind, ratio in finding.sub_breaches:
        key = f"{finding.rule}|{finding.scope}|{kind}"
        row = await session.get(AnomalyAlertStateModel, key)
        if row is not None and ratio <= row.last_magnitude * ALERT_INCREASE_THRESHOLD:
            continue
        alert = True
        await session.merge(
            AnomalyAlertStateModel(key=key, last_magnitude=ratio, last_alerted_at=datetime.now(UTC).isoformat())
        )
    return alert


async def _clear_resolved_sub_breaches(
    session: AsyncSession, rule: str, scope: str, active_kinds: frozenset[str]
) -> None:
    """#924 HIGH-2: delete any anomaly_alert_state row for (*rule*, *scope*)
    whose kind is not among tonight's *active_kinds* — a resolved (or
    never-breaching) sub-check must not leave a baseline behind for a later,
    unrelated breach of the same kind to inherit. Called every night this
    check evaluates, whether or not tonight has ANY breach — the check
    returning no finding at all (active_kinds == frozenset()) previously
    left the row(s) in place forever, which is how a book-level breach's
    baseline ratcheted up permanently even after it fully resolved."""
    prefix = f"{rule}|{scope}|"
    rows = (
        (await session.execute(select(AnomalyAlertStateModel).filter(AnomalyAlertStateModel.key.startswith(prefix))))
        .scalars()
        .all()
    )
    for row in rows:
        if row.key[len(prefix) :] not in active_kinds:
            await session.delete(row)


async def _halt(session: AsyncSession, finding: AnomalyFinding) -> None:
    """Record the finding; latch HALT_ENTRIES for its scope only when the
    finding latches (escalation only either way — never auto-resumes)."""
    row = await session.get(TradingControlModel, finding.scope)
    current = row.state if row is not None else None
    alert = await _should_alert(session, finding)
    session.add(
        AuditEventModel(
            run_at=datetime.now(UTC).isoformat(),
            book_id=None if finding.scope == GLOBAL_SCOPE else finding.scope,
            event_type=finding.rule,
            actor="anomaly",
            payload={"detail": finding.detail, "state_before": current, "alert_suppressed": not alert},
        )
    )
    await session.commit()
    if finding.latches and (current == ACTIVE or current is None):
        await set_control(
            session, finding.scope, HALT_ENTRIES, reason=f"{finding.rule}: {finding.detail}", actor="anomaly"
        )
    if alert:
        logger.error("Anomaly %s (%s): %s", finding.rule, finding.scope, finding.detail)
    else:
        logger.info(
            "Anomaly %s (%s) repeat suppressed from ntfy push (unchanged/lower than last alert): %s",
            finding.rule,
            finding.scope,
            finding.detail,
        )


def entry_signature(book_id: str, legs: tuple[tuple[str, str, int], ...]) -> str:
    """(book, legs+directions+ratio) fingerprint — OCC symbols already encode
    underlying, expiry, strike, and option type. Ratio is part of the
    signature (#740): a BWB body's 2x ratio on one leg is a genuinely
    different combo from a hypothetical 1x structure sharing the same
    strikes, and dropping ratio from the fingerprint would let those
    false-positive as duplicates of each other."""
    return f"{book_id}|" + "|".join(f"{occ}:{direction}:{ratio}" for occ, direction, ratio in sorted(legs))


def _collapse_ratio_expanded_legs(legs_meta: list[dict]) -> tuple[tuple[str, str, int], ...]:
    """#740: PositionModel/order-meta legs store a BWB body's ratio
    EXPANDED into duplicate leg dicts (executor.py's `legs_meta.extend([...]
    * ratio)`, same convention #709's _distinct_leg_count precedent
    collapses on the fill-coverage side) — collapse duplicates back into
    one (occ, direction, ratio) entry per distinct leg so this side is
    comparable to the candidate's own aggregated-with-ratio form."""
    counts: dict[tuple[str, str], int] = {}
    for leg in legs_meta:
        key = (leg["occ"], leg["direction"])
        counts[key] = counts.get(key, 0) + 1
    return tuple((occ, direction, ratio) for (occ, direction), ratio in counts.items())


async def check_duplicate_order(
    session: AsyncSession, book_id: str, legs: tuple[tuple[str, str, int], ...], window_start: str
) -> bool:
    """True if a matching entry already exists this evening (logic bug, not
    market condition). The caller must block the order AND halt globally.

    *legs* is the candidate's AGGREGATED form — one (occ, direction, ratio)
    entry per distinct leg, ratio included (#740: a BWB body carries ratio
    2; comparing against a ratio-EXPANDED stored form of different length
    silently killed duplicate detection for every ratio structure — see
    _collapse_ratio_expanded_legs, which normalizes the stored side back to
    the same shape before comparing).

    *window_start* is a UTC ISO timestamp (market_evening_window_start);
    matching is >= against it — the caller has passed a timestamp since #259,
    and the old date-prefix startswith silently matched NOTHING against it,
    leaving duplicate detection dead (found by audit H5, #275). A STAGED row
    is always in-window: the sync expires stale STAGED intents at run start,
    so one alive during the entry phase was created this run."""
    signature = entry_signature(book_id, legs)
    orders = (
        (await session.execute(select(OrderModel).filter(OrderModel.book_id == book_id, OrderModel.action == "OPEN")))
        .scalars()
        .all()
    )
    for order in orders:
        stamp = order.submitted_at or order.completed_at or ""
        if order.status != "STAGED" and (not stamp or stamp < window_start):
            continue
        meta = order.combo_legs or {}
        existing = _collapse_ratio_expanded_legs(meta.get("legs", []))
        if existing and entry_signature(book_id, existing) == signature:
            return True
    return False


def _broker_side(direction: str, order_action: str) -> str:
    """The side IBKR sees for a resting order's leg: the stored direction for
    an OPEN order, its inverse for a CLOSE (a TP rider closing a LONG leg is
    a resting SELL on that contract). This is the quantity the both-sides
    rule reasons over — not our position-accounting direction."""
    if order_action == "CLOSE":
        return "SHORT" if direction == "LONG" else "LONG"
    return direction


async def check_order_leg_collision(session: AsyncSession, candidate_legs: tuple[tuple[str, str], ...]) -> str | None:
    """The #853 pre-preview gate: the order_ref of a resting (STAGED /
    SUBMITTED / PARTIAL) order — ANY book's, ANY action — holding the
    opposite broker side of any candidate leg, or None. IBKR forbids open
    orders on both sides of one US option contract ACCOUNT-wide; the 34
    books share one account, so a later book's candidate can contest an
    earlier book's resting leg (including within a single run, as tonight's
    submissions accumulate). Skipping here, before whatIfOrder, is the same
    refusal the broker would issue — minus fifteen preview round-trips and a
    false REPEATED_REJECTION halt (2026-08-27). *candidate_legs* is the
    entry's (occ, direction) pairs; entry legs are always effective-side ==
    direction (action OPEN by construction)."""
    wanted = {(occ, direction) for occ, direction in candidate_legs}
    orders = (
        (await session.execute(select(OrderModel).filter(OrderModel.status.in_(ORDER_PENDING_STATUSES))))
        .scalars()
        .all()
    )
    for order in orders:
        meta = order.combo_legs or {}
        for leg in meta.get("legs", []):
            occ = leg.get("occ")
            direction = leg.get("direction")
            if not occ or direction not in ("LONG", "SHORT"):
                continue
            resting_side = _broker_side(direction, order.action)
            opposite = "SHORT" if resting_side == "LONG" else "LONG"
            if (occ, opposite) in wanted:
                return order.order_ref
    return None


async def check_permissions_refusals(session: AsyncSession, since: str | None) -> AnomalyFinding | None:
    """#853: permissions-class preview refusals ("no trading permissions for
    this options strategy") get an immediate, NON-latching finding on first
    occurrence — no threshold, because retrying cannot fix a missing account
    permission, and no global halt, because the other playbooks' entries are
    not evidence-linked to it. The digest carries it as needs-human."""
    if since is None:
        return None
    events = (
        (
            await session.execute(
                select(AuditEventModel).filter(
                    AuditEventModel.event_type == "ENTRY_PREVIEW_REFUSED", AuditEventModel.run_at >= since
                )
            )
        )
        .scalars()
        .all()
    )
    playbooks = sorted(
        {
            str((e.payload or {}).get("playbook", "?"))
            for e in events
            if classify_preview_refusal(str((e.payload or {}).get("reason", ""))) == "permissions"
        }
    )
    if not playbooks:
        return None
    return AnomalyFinding(
        PERMISSIONS_REFUSED,
        GLOBAL_SCOPE,
        f"account lacks trading permissions for: {', '.join(playbooks)} - fix in account management",
        latches=False,
    )


def _trailing_market_sessions(today: str, count: int) -> frozenset[str]:
    """The *count* most recent trading-day dates on/before *today* (ISO,
    market calendar) — #927's age bound for check_repeated_rejection's
    trailing bucket.

    Walks the calendar backward from *today* rather than the events
    themselves: the old bucket picked the *count* most recent DATES WITH
    EVENTS (`sorted(by_date)[:count]`), so a single stale burst (the
    2026-08-27 whatIfOrder API-error storm) never rolled off — it stayed the
    most recent bucket, re-firing the halt with zero new evidence, until two
    MORE rejection-bearing sessions happened to occur. Bounding by the
    calendar instead means a session with no rejections still consumes its
    slot in the window, same as a session with some."""
    end = date.fromisoformat(today)
    sessions: list[str] = []
    cursor = end
    while len(sessions) < count:
        if is_trading_day(cursor):
            sessions.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return frozenset(sessions)


async def check_repeated_rejection(
    session: AsyncSession, today: str, since: str | None = None
) -> AnomalyFinding | None:
    """≥2 rejections tonight, or ≥3 across the trailing 3 market sessions with
    rejections — our model of the broker's rules is wrong; retrying digs holes.
    *since* (run-start timestamp, #259) defines "tonight" robustly: a UTC
    date-prefix undercounts every EST evening, where most of the run happens
    after midnight UTC.

    The trailing-sessions bucket is keyed by MARKET date (#419, #537), not a
    UTC date prefix: run_at is UTC, and in EST season the 18:45 ET run
    straddles 00:00 UTC, splitting one session's rejections across two UTC
    buckets — or, with a later task variant, pushing the whole run onto the
    next UTC date and merging adjacent sessions. It is also age-bounded to
    the 3 most recent trading sessions BY CALENDAR (#927,
    _trailing_market_sessions), not the 3 most recent dates that happen to
    have a rejection — see that helper's docstring."""
    events = (
        (await session.execute(select(AuditEventModel).filter(AuditEventModel.event_type.in_(_REJECTION_EVENTS))))
        .scalars()
        .all()
    )
    # #853/#927: positively identified collision/riskless/infra preview
    # refusals are handled failure classes (see classify_preview_refusal)
    # and no longer count toward this halt; permissions-class refusals are
    # diverted to their own immediate, playbook-scoped finding rather than
    # needing to accumulate to a threshold here, and infra-class refusals to
    # PREVIEW_INFRA_FAILURE's own same-night rule.
    events = [
        e
        for e in events
        if e.event_type != "ENTRY_PREVIEW_REFUSED"
        or classify_preview_refusal(str((e.payload or {}).get("reason", ""))) == "other"
    ]
    by_date: dict[str, int] = {}
    for e in events:
        key = market_date_of(e.run_at).isoformat()
        by_date[key] = by_date.get(key, 0) + 1
    tonight = sum(1 for e in events if e.run_at >= since) if since else by_date.get(today, 0)
    if tonight >= 2:
        return AnomalyFinding(REPEATED_REJECTION, GLOBAL_SCOPE, f"{tonight} rejections tonight")
    trailing_sessions = _trailing_market_sessions(today, 3)
    trailing = sum(count for session_date, count in by_date.items() if session_date in trailing_sessions)
    if trailing >= 3:
        return AnomalyFinding(REPEATED_REJECTION, GLOBAL_SCOPE, f"{trailing} rejections across trailing 3 sessions")
    return None


async def check_preview_infra_failure(session: AsyncSession, since: str | None) -> tuple[AnomalyFinding | None, bool]:
    """#927: >=3 infra-class preview refusals (whatIfOrder API error, or a
    timed-out whatIf with no usable order state — see classify_preview_refusal)
    in THIS run halt globally on their own rule. This is a gateway outage
    signal, not "our model of the broker's rules is wrong" — REPEATED_REJECTION
    exists for the latter, and pooling the two let one bad night poison three
    sessions of a counter it was never evidence for. Same-night only (no
    trailing bucket): a gateway outage is a point-in-time event, not a
    recurring pattern to track across sessions.

    Returns (finding, evaluated). evaluated is False only when since is
    None, mirroring the early return just below: a since=None sweep has no
    run-start boundary to scope "tonight" against, so this rule did not run
    at all — "not evaluated," never "evaluated and clean." Self-clear
    (#927, _self_clear_expired_halts) depends on that distinction: treating
    a not-evaluated rule as evaluated-clean would let a since=None sweep
    silently lift a live PREVIEW_INFRA_FAILURE halt.

    LOW-3, accepted gap: below the >=3 threshold, infra-class refusals are
    invisible to every anomaly rule — 1-2 whatIfOrder API errors/timeouts in
    one run count toward nothing (excluded from REPEATED_REJECTION by
    classify_preview_refusal, below this rule's own threshold). They still
    land in audit_events (ENTRY_PREVIEW_REFUSED, classify_preview_refusal ==
    'infra') and are visible there on inspection, but nothing surfaces them
    proactively — a gateway flaking at exactly 1-2 events a night, night
    after night, produces no digest line and no push."""
    if since is None:
        return None, False
    events = (
        (
            await session.execute(
                select(AuditEventModel).filter(
                    AuditEventModel.event_type == "ENTRY_PREVIEW_REFUSED", AuditEventModel.run_at >= since
                )
            )
        )
        .scalars()
        .all()
    )
    infra_count = sum(
        1 for e in events if classify_preview_refusal(str((e.payload or {}).get("reason", ""))) == "infra"
    )
    if infra_count < 3:
        return None, True
    return (
        AnomalyFinding(PREVIEW_INFRA_FAILURE, GLOBAL_SCOPE, f"{infra_count} infra-class preview failures tonight"),
        True,
    )


def book_mtm(book: BookModel, open_positions: list[PositionModel]) -> float:
    """Mark-to-market book equity: cash plus signed liquidation value of open
    positions (credit positions carry a buy-back liability)."""
    equity = book.cash_balance
    for pos in open_positions:
        value = pos.current_value_per_share * 100 * pos.contracts
        equity += value if pos.premium_direction == "DEBIT" else -value
    return round(equity, 2)


async def check_pnl_shock(
    session: AsyncSession, book: BookModel, open_positions: list[PositionModel], today: str | None = None
) -> AnomalyFinding | None:
    """Day MTM move beyond 15% of basis: a 4-position defined-risk book
    respecting the envelope cannot legitimately lose that much in a day —
    beyond it is a pricing-data or attribution bug. Updates the baseline.
    *today* is the run's market date (#259) — the equity-curve row must not
    land under tomorrow just because UTC rolled over mid-run."""
    basis = resolve_book_config(book.config).envelope.basis
    mtm = book_mtm(book, open_positions)
    previous = book.last_mtm
    previous_at = book.last_mtm_at
    book.last_mtm = mtm
    book.last_mtm_at = datetime.now(UTC).isoformat()
    # The equity curve (#239): last_mtm alone is overwritten nightly, so
    # every mark also lands in book_mtm_history. merge = same-day rerun
    # overwrites its row instead of duplicating.
    mark_date = today or market_today().isoformat()
    await session.merge(BookMtmHistoryModel(book_id=book.id, date=mark_date, mtm=mtm))
    if previous is None:
        return None
    move = abs(mtm - previous)
    if move > basis * PNL_SHOCK_PCT / 100.0:
        # A multi-session move must not trip a ONE-day threshold (#280, M4):
        # if the previous mark is older than the prior trading day (missed
        # night, holiday+failure), record the gap instead of halting.
        if previous_at and _market_days_between(previous_at, today) > 1:
            session.add(
                AuditEventModel(
                    run_at=datetime.now(UTC).isoformat(),
                    book_id=book.id,
                    event_type="PNL_SHOCK_SKIPPED_GAP",
                    actor="anomaly",
                    payload={"move": round(move, 2), "previous_mark_at": previous_at, "today": today},
                )
            )
            return None
        return AnomalyFinding(
            PNL_SHOCK, book.id, f"day MTM move ${move:.0f} exceeds {PNL_SHOCK_PCT}% of ${basis:.0f} basis"
        )
    return None


def _market_days_between(previous_iso: str, today: str | None) -> int:
    """Trading days from the previous mark's date to *today* (ISO market
    date). Unparseable inputs read as 1 — the shock check then applies.

    Timezone (#419): previous_iso is a UTC timestamp (run_at rows), and an
    evening run that commits after 00:00 UTC would read as the NEXT day —
    under-counting a gap by one and letting a genuinely missed night look
    covered. Aware timestamps convert to the market timezone first; naive
    inputs (plain dates) are taken as market dates already."""
    from datetime import date as _date

    from backend.calendars import is_trading_day

    try:
        start = market_date_of(previous_iso)
        end = _date.fromisoformat(today) if today else market_today()
    except ValueError:
        return 1
    days = 0
    cursor = start
    while cursor < end and days < 30:
        cursor = _date.fromordinal(cursor.toordinal() + 1)
        if is_trading_day(cursor):
            days += 1
    return days


async def check_envelope_breach(
    session: AsyncSession, book: BookModel, open_positions: list[PositionModel]
) -> tuple[AnomalyFinding | None, bool]:
    """Reconciled state violating the envelope proves a CODE defect — these
    are pre-blocked by gates, so post-hoc detection means a gate was bypassed.

    Era-scoped (#533, Audit II R4): positions are judged against the config
    era that DECIDED them (config_hash, #534), never against a later seed
    edit. A seeds.py envelope reduction is not a gate bypass — the old-era
    positions passed the gates they were entered under, and writing nightly
    breach rows for them would permanently poison the Live Gate's
    zero-breaches criterion (append-only table, no expunge path) with false
    positives indistinguishable from the real defects it exists to catch.
    NULL-hash rows (pre-#284) stay included — every executor-book position
    postdates hash stamping, so in practice None means a test fixture, and
    erring toward checking is the safe direction there.

    Returns (finding, era_clean). era_clean is True only when every open
    position for this book was judged this sweep — i.e. era_positions covers
    all of open_positions, prior_era == 0. #927 HIGH-3: a config_hash
    rotation (a seeds.py edit landing between two runs) moves open positions
    into a prior era, and the filter below silently excludes them from
    era_positions — "no breach" then means "nothing was judged," not "judged
    and clean." Self-clear (#927, _self_clear_expired_halts) reads era_clean
    alongside the finding, so a book with excluded positions never
    self-clears an ENVELOPE_BREACH_POSTHOC halt off a judgment that never
    actually happened."""
    envelope = resolve_book_config(book.config).envelope
    era_positions = [p for p in open_positions if p.config_hash == book.config_hash or p.config_hash is None]
    prior_era = len(open_positions) - len(era_positions)
    era_clean = prior_era == 0
    breaches: list[str] = []
    # #924 (HIGH-1/MED-3, superseding #922's single finding-wide worst_ratio
    # + per-trade-only dedup_key): one (kind, ratio) pair per structurally
    # distinct sub-check that is breaching. Each ratio is a dimensionless
    # measured/cap figure, comparable night over night WITHIN its own kind
    # (position count, dollars, bucket count) — never across kinds, which is
    # exactly what a single max() used to do. See AnomalyFinding.sub_breaches.
    sub_breaches: list[tuple[str, float]] = []
    if len(era_positions) > envelope.max_positions:
        breaches.append(f"{len(era_positions)} positions > {envelope.max_positions}")
        sub_breaches.append(("count", len(era_positions) / envelope.max_positions))
    deployed = sum(capital_at_risk(p.max_loss, p.contracts) for p in era_positions)
    deployed_cap = envelope.basis * envelope.max_deployed_pct / 100.0
    if deployed > deployed_cap:
        breaches.append(f"deployed ${deployed:.0f} > ${deployed_cap:.0f}")
        sub_breaches.append(("deployed", deployed / deployed_cap))
    per_trade_cap = envelope.basis * envelope.max_loss_pct_per_trade / 100.0
    for pos in era_positions:
        risk = capital_at_risk(pos.max_loss, pos.contracts)
        if risk > per_trade_cap:
            breaches.append(f"position {pos.id} risk ${risk:.0f} > ${per_trade_cap:.0f}")
            sub_breaches.append((f"per_trade:{pos.id}", risk / per_trade_cap))
    # #680: the fifth envelope limit, missing here until now — bucket the
    # same way STRATEGY_EXPIRY_CONCENTRATION does, so a gate bypass (a code
    # defect the gate should have caught, e.g. #679's pending-orders gap)
    # still shows up as a breach finding rather than running silently
    # indefinitely with zero evidence of it.
    bucket_counts: dict[tuple[str, str], int] = {}
    for pos in era_positions:
        key = (pos.strategy_type, pos.expiration_date)
        bucket_counts[key] = bucket_counts.get(key, 0) + 1
    for (strategy_type, expiration_date), count in sorted(bucket_counts.items()):
        if count > envelope.max_same_strategy_expiry:
            breaches.append(f"{count} {strategy_type}@{expiration_date} > {envelope.max_same_strategy_expiry}")
            sub_breaches.append(
                (f"bucket:{strategy_type}@{expiration_date}", count / envelope.max_same_strategy_expiry)
            )
    sub_breaches.sort()
    # #924 HIGH-2: reconcile alert-state rows against tonight's actual
    # breaching kinds — every night this check runs for this book, breach or
    # not, so a sub-check that fully resolves (including "the whole finding
    # resolves", i.e. sub_breaches == []) has its row deleted instead of
    # ratcheting a stale baseline forever.
    await _clear_resolved_sub_breaches(
        session, ENVELOPE_BREACH_POSTHOC, book.id, frozenset(kind for kind, _ratio in sub_breaches)
    )
    if breaches:
        if prior_era:
            breaches.append(f"{prior_era} prior-era position(s) excluded")
        return (
            AnomalyFinding(ENVELOPE_BREACH_POSTHOC, book.id, "; ".join(breaches), sub_breaches=tuple(sub_breaches)),
            era_clean,
        )
    return None, era_clean


async def check_zombie_fills(session: AsyncSession, since: str | None = None) -> tuple[AnomalyFinding | None, bool]:
    """Fills recorded tonight against an already-terminal order (#481 A-F5).

    Every legitimate fill lands on a pending row: the sync flips it FILLED,
    or a cancelled/vanished-with-fills verdict latches PARTIAL. A fresh fill
    attached to a CANCELLED/REJECTED row means an order the books stamped
    dead executed anyway — the double-exit signature #467's confirm step
    defends against, surfacing here if any path misses. Scoped to fills
    backfilled since run start: a resolve_partial_order latch release
    (CANCELLED row with OLD fills) is the designated workflow, not a zombie.

    #546 F5: if the sync latches PARTIAL tonight (fresh fill_time) and the
    operator external-closes + resolve_partial_order DURING this same run
    window (before the anomalies phase runs), the now-CANCELLED row carries
    exactly the fills that latch was reporting — a global halt for doing
    precisely what the latch asked. Refs terminalized THROUGH the resolution
    endpoint tonight (RESOLUTION_PARTIAL_TERMINALIZED, actor=resolution) are
    the designated workflow, not a zombie — excluded here.

    Returns (finding, evaluated). evaluated is False only when since is
    None, same shape as check_preview_infra_failure and for the same reason
    (#927). ZOMBIE_FILL is not in _SELF_CLEARABLE_RULES (see that
    constant's docstring) — self-clear does not consult this flag today,
    kept symmetrical with the other since-guarded check so this function's
    shape does not have to change if ZOMBIE_FILL's clearability is ever
    reconsidered.
    """
    if since is None:
        return None, False
    resolved_refs = set(
        (
            await session.execute(
                select(AuditEventModel).filter(
                    AuditEventModel.event_type == "RESOLUTION_PARTIAL_TERMINALIZED",
                    AuditEventModel.actor == "resolution",
                    AuditEventModel.run_at >= since,
                )
            )
        )
        .scalars()
        .all()
    )
    resolved_refs = {e.payload.get("order_ref") for e in resolved_refs}
    rows = (
        await session.execute(
            select(FillModel, OrderModel)
            .join(OrderModel, FillModel.order_id == OrderModel.id)
            .filter(OrderModel.status.in_(ORDER_CANCELLED_OR_REJECTED_STATUSES), FillModel.fill_time >= since)
        )
    ).all()
    rows = [(f, o) for f, o in rows if o.order_ref not in resolved_refs]
    if not rows:
        return None, True
    refs = sorted({o.order_ref for _f, o in rows})
    return (
        AnomalyFinding(ZOMBIE_FILL, GLOBAL_SCOPE, f"{len(rows)} fill(s) on terminal order(s): {', '.join(refs)}"),
        True,
    )


# #927: rules that RE-DERIVE their evidence from scratch every sweep, so "no
# live finding this run" can be trusted as "resolved" rather than merely "no
# NEW evidence arrived." REPEATED_REJECTION rebuilds its calendar window
# from the audit ledger every time; ENVELOPE_BREACH_POSTHOC recomputes a
# standing condition on currently-open positions — both start from zero and
# reconstruct the present, so a clean recompute genuinely means clean.
# PREVIEW_INFRA_FAILURE is different in kind: it is same-night-only (no
# trailing window at all — check_preview_infra_failure has nothing to
# re-derive ACROSS nights), but a gateway outage is itself a transient,
# point-in-time condition — last night's outage is not evidence about
# tonight's gateway, so "clean tonight" is still a meaningful resolution
# signal, just for a different reason than the other two. This set is not
# homogeneous; don't let its membership imply otherwise.
#
# ZOMBIE_FILL and PNL_SHOCK are OUT. Both are SINCE-bounded or
# self-overwriting, not re-derived: check_zombie_fills only ever looks at
# fills backfilled since THIS run's start, so a halt from a zombie fill last
# night reads as "no live evidence" again the very next run regardless of
# whether anyone investigated it — "no finding" there means "no NEW zombie
# tonight," never "the old one was explained." check_pnl_shock's baseline
# (book.last_mtm) is overwritten every run, so the move that tripped the
# halt measures ~0 the following night by construction; its own gap arm
# (_market_days_between) already declines to judge a multi-session move,
# the same admission from the other direction.
#
# DUPLICATE_ORDER is not a member either — latched at entry-staging time in
# executor.py, never re-run here, so this sweep has no way to re-derive its
# evidence. Provenance still recognizes its event type (_GLOBAL_HALTING_
# RULES below) so a DUPLICATE_ORDER halt is correctly identified as
# un-clearable rather than silently falling through as "no evidence found."
#
# PARTIAL_FILL (#927 round 2) is the same shape as DUPLICATE_ORDER: latched
# by executor.py at fill-sync time, not re-evaluated by this sweep, so
# there is no "clean recompute" to trust. It also has a designated in-band
# recovery (resolve_partial_order) that this sweep must not preempt —
# provenance recognizes the event type (_BOOK_HALTING_RULES below) so the
# halt blocks self-clear until that endpoint runs, rather than lifting on
# unrelated evidence aging out (the B03 regression this fixes).
_SELF_CLEARABLE_RULES = frozenset({REPEATED_REJECTION, ENVELOPE_BREACH_POSTHOC, PREVIEW_INFRA_FAILURE})

# Every rule whose finding can latch HALT_ENTRIES, grouped by the scope it
# always targets — _halting_rules_since uses this to attribute a ledger
# event to the scope it actually halted, not the scope its own book_id
# happens to carry. Every _halt-driven rule (everything here except
# DUPLICATE_ORDER) writes book_id=None for a GLOBAL finding and book_id=
# <book> for a book-scoped one (AnomalyFinding.scope, mirrored exactly by
# _halt) — so book_id IS the scope for those. DUPLICATE_ORDER is the one
# exception: executor.py audits it book_id=<offending book> (the
# entry-staging site is naturally book-scoped) but always halts GLOBAL
# (set_control(session, "GLOBAL", ...)) — so for provenance purposes it
# belongs to _GLOBAL_HALTING_RULES regardless of the book_id on its own
# event row. PERMISSIONS_REFUSED and CROSS_BOOK_ORDER_COLLISION (LOW-1) are
# absent on purpose: the former never latches (AnomalyFinding.latches=
# False) and the latter never reaches set_control at all — no set_control
# writer ever uses that reason, it only ever records a skip (executor.py's
# check_order_leg_collision path). Neither can ever be "the rule that
# caused a halt," so provenance has nothing to attribute to them.
#
# PARTIAL_FILL (#927 round 2) is the second exception, on the other side of
# the DUPLICATE_ORDER split: unlike everything else in this module it isn't
# an anomaly.py-authored AnomalyFinding at all — _latch_partial in
# executor.py writes its own PARTIAL_FILL audit event and set_control call
# directly, book-scoped (order.book_id) on both. It belongs here — not as a
# style match, but because _halting_rules_since's ledger scan is a query
# over event_type strings, blind to which module wrote them; leaving
# PARTIAL_FILL out made it invisible to that scan and thus to self-clear,
# which is exactly the bug this map now closes. Deliberately excluded from
# _SELF_CLEARABLE_RULES below: a partial fill needs the resolve_partial_
# order workflow, not evidence aging out.
_GLOBAL_HALTING_RULES = frozenset({REPEATED_REJECTION, PREVIEW_INFRA_FAILURE, ZOMBIE_FILL, DUPLICATE_ORDER})
_BOOK_HALTING_RULES = frozenset({PNL_SHOCK, ENVELOPE_BREACH_POSTHOC, PARTIAL_FILL})


async def _last_active_at(session: AsyncSession, scope: str) -> str | None:
    """run_at of the most recent CONTROL_STATE_CHANGED event that set
    *scope* to ACTIVE, regardless of actor — an operator RESUME counts
    exactly the same as anomaly's own prior self-clear; both mean "the slate
    was wiped here." None if *scope* has never been recorded ACTIVE (a
    control row seeded straight into a state, or halted since before any
    transition history exists) — _halting_rules_since then treats the
    window as unbounded back to the start of the ledger, the fail-closed
    direction: no anchor means no basis for believing anything has been
    superseded.

    #927 round 2 LOW-2: this scope's CONTROL_STATE_CHANGED history can be
    long-lived (every halt AND every clear), and the match (payload.state
    == ACTIVE) isn't SQL-filterable through the generic JSON column — so
    walk it newest-first in bounded pages (a projected run_at+payload
    select, not full ORM rows) instead of materializing the whole history
    to find one row near the top."""
    book_id = None if scope == GLOBAL_SCOPE else scope
    page_size = 200
    last_id: int | None = None
    while True:
        query = (
            select(AuditEventModel.id, AuditEventModel.run_at, AuditEventModel.payload)
            .filter(AuditEventModel.event_type == "CONTROL_STATE_CHANGED", AuditEventModel.book_id == book_id)
            .order_by(AuditEventModel.id.desc())
            .limit(page_size)
        )
        if last_id is not None:
            query = query.filter(AuditEventModel.id < last_id)
        page = (await session.execute(query)).all()
        if not page:
            return None
        for row_id, run_at, payload in page:
            if payload.get("scope") == scope and payload.get("state") == ACTIVE:
                return run_at
        if len(page) < page_size:
            return None
        last_id = page[-1][0]


async def _halting_rules_since(session: AsyncSession, scope: str) -> frozenset[str]:
    """Every halting-rule event_type recorded against *scope* since it was
    last ACTIVE (_last_active_at) — the provenance _self_clear_expired_halts
    checks against _SELF_CLEARABLE_RULES. Keyed by the SCOPE a rule targets
    (via _GLOBAL_HALTING_RULES / _BOOK_HALTING_RULES), not by matching
    event.book_id literally — see those constants' docstring for why
    DUPLICATE_ORDER needs that distinction. The lower bound is inclusive
    (>=): the anchor itself is a CONTROL_STATE_CHANGED row, never a halting-
    rule event, so >= can only pull in a finding that happens to share the
    anchor's exact timestamp — the direction fail-closed wants.

    #927 round 2 LOW-2: projected select (event_type + book_id only, no
    payload/actor/reason) — this sweep only ever needs those two columns to
    attribute a row to a scope."""
    window_start = await _last_active_at(session, scope)
    query = select(AuditEventModel.event_type, AuditEventModel.book_id).filter(
        AuditEventModel.event_type.in_(_GLOBAL_HALTING_RULES | _BOOK_HALTING_RULES)
    )
    if window_start is not None:
        query = query.filter(AuditEventModel.run_at >= window_start)
    rows = (await session.execute(query)).all()
    rules: set[str] = set()
    for event_type, book_id in rows:
        if event_type in _GLOBAL_HALTING_RULES:
            if scope == GLOBAL_SCOPE:
                rules.add(event_type)
        elif event_type in _BOOK_HALTING_RULES and book_id == scope:
            rules.add(event_type)
    return frozenset(rules)


async def _self_clear_expired_halts(
    session: AsyncSession, findings: list[AnomalyFinding], evaluated: frozenset[tuple[str, str]]
) -> None:
    """#927: lift an anomaly-actor HALT_ENTRIES back to ACTIVE once every
    rule that has contributed to it is (a) in _SELF_CLEARABLE_RULES and (b)
    cleanly re-evaluated this sweep with no live finding — with a
    CONTROL_STATE_CHANGED audit event (via set_control) naming what expired.
    A latched halt whose cause has evaporated is what trains the operator to
    ignore notifications — REPEATED_REJECTION's aged-out trailing window
    (the 2026-08-27 burst, 8/29, 8/31) is the motivating case.

    Provenance comes from the audit ledger (_halting_rules_since), not the
    control row's `reason` prose — the clear decision is now keyed by
    (rule, scope), the same thing the hazard is keyed by. A scope with
    MULTIPLE correlated rules in its provenance window only lifts when ALL
    of them clear together: one self-clearable rule aging out does not
    vacate another, still-standing rule's claim on the same scope (e.g. a
    REPEATED_REJECTION halt that a later ZOMBIE_FILL also latched onto —
    the rejection burst aging out must not lift a halt the zombie's own
    evidence still justifies).

    *evaluated* is the set of (rule, scope) pairs this sweep actually
    recomputed cleanly, populated by run_post_session_anomalies at each
    check's own call site (MEDIUM-1: derived from what ran, never a
    hardcoded scope literal). A rule missing from *evaluated* for a scope in
    its provenance — because since=None skipped it entirely
    (check_preview_infra_failure), or because check_envelope_breach's era
    filter excluded an open position from tonight's judgment (HIGH-3) —
    blocks the clear exactly like a live finding would.

    Never touches: FLATTEN_REQUESTED (escalation-only — this only ever moves
    HALT_ENTRIES toward ACTIVE, never downgrades a more severe state),
    operator/ntfy halts (actor != "anomaly" — only anomaly may resume its
    OWN prior action), or a scope with ANY live latching finding THIS sweep
    (checked before provenance at all — a scope re-halting tonight has
    nothing to clear, and skipping here avoids writing a spurious
    ACTIVE-then-HALT flap into the ledger).

    Two accepted gaps, LOW-2:
    (a) the live-finding check above narrows, but does not eliminate, a
    race between this read and set_control's commit below — a halt written
    by a concurrent process for the same scope in that window is not seen
    by this sweep and could be briefly overwritten back to ACTIVE.
    (b) the window anchor is the scope's own last ACTIVE transition, so an
    operator RESUME wipes the provenance slate clean — any unresolved
    evidence recorded before that RESUME no longer blocks a later
    self-clear. Correct for the ordinary case (a resume supersedes
    everything it resumed past), but a RESUME issued before investigating
    fully narrows a future self-clear's evidence window."""
    live_scopes = {f.scope for f in findings if f.latches}
    # #464/#546 F8 discipline: populate_existing forces a real SELECT and
    # overwrites any cached identity-map row — this run's own earlier
    # session.get(TradingControlModel, ...) calls (e.g. _halt, above) could
    # otherwise shadow a console RESUME or another process's write to the
    # same scope landed mid-run.
    rows = (
        (await session.execute(select(TradingControlModel).execution_options(populate_existing=True))).scalars().all()
    )
    for row in rows:
        if row.actor != "anomaly" or row.state != HALT_ENTRIES or row.scope in live_scopes:
            continue
        rules = await _halting_rules_since(session, row.scope)
        if not rules:
            continue  # no provenance recorded at all — fail closed, do not clear
        if not all(rule in _SELF_CLEARABLE_RULES and (rule, row.scope) in evaluated for rule in rules):
            continue
        await set_control(
            session,
            row.scope,
            ACTIVE,
            reason=f"{', '.join(sorted(rules))} evidence expired — auto-cleared by anomaly sweep",
            actor="anomaly",
            allow_resume=True,
        )


async def run_post_session_anomalies(
    session: AsyncSession, today: str, since: str | None = None
) -> list[AnomalyFinding]:
    """The end-of-run sweep: repeated rejections (global) plus per-book PNL
    shock and post-hoc envelope breaches. Applies latching halts, then lifts
    any anomaly-actor halt whose contributing rule(s) no longer trip (#927,
    self-clear). *today* is the run's market date; *since* its start
    timestamp (#259)."""
    findings: list[AnomalyFinding] = []
    # #927 MEDIUM-1: which (rule, scope) pairs this sweep actually
    # recomputed cleanly, derived from each check's own outcome — never a
    # hardcoded scope literal. Fed to _self_clear_expired_halts.
    evaluated: set[tuple[str, str]] = set()

    rejection = await check_repeated_rejection(session, today, since=since)
    if rejection:
        findings.append(rejection)
    evaluated.add((REPEATED_REJECTION, GLOBAL_SCOPE))  # no since=None guard — always runs

    infra, infra_evaluated = await check_preview_infra_failure(session, since=since)
    if infra:
        findings.append(infra)
    if infra_evaluated:
        evaluated.add((PREVIEW_INFRA_FAILURE, GLOBAL_SCOPE))

    permissions = await check_permissions_refusals(session, since=since)
    if permissions:
        findings.append(permissions)

    zombie, _zombie_evaluated = await check_zombie_fills(session, since=since)
    if zombie:
        findings.append(zombie)

    books = (
        (await session.execute(select(BookModel).filter(BookModel.status == BOOK_ACTIVE_STATUS, BookModel.id != "B00")))
        .scalars()
        .all()
    )
    for book in books:
        open_positions = list(
            (await session.execute(select(PositionModel).filter_by(status=POSITION_OPEN_STATUS, book_id=book.id)))
            .scalars()
            .all()
        )
        shock = await check_pnl_shock(session, book, open_positions, today=today)
        if shock:
            findings.append(shock)
        breach, era_clean = await check_envelope_breach(session, book, open_positions)
        if breach:
            findings.append(breach)
        if era_clean:  # HIGH-3: an era-excluded book was not cleanly judged this sweep
            evaluated.add((ENVELOPE_BREACH_POSTHOC, book.id))
    await session.commit()  # persists updated MTM baselines

    await _self_clear_expired_halts(session, findings, frozenset(evaluated))

    for finding in findings:
        await _halt(session, finding)
    return findings

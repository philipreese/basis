"""digest.py — the executor's evening digest and urgent-push tiering (#72, #982).

Implements spec/supervision.md §"Digest & push policy": the digest's section
order is fixed (control-state banner first — a halted system must say so
every night, or silence becomes indistinguishable from health),
"reconciliation clean" is stated explicitly (absence of the line must never
be interpretable as success), and events that need human action before the
next evening go out as a SEPARATE urgent push. Push fatigue is itself a
safety failure: normal fills, P&L, and gate hits batch into the digest; only
control-state changes and failures interrupt.

One data model, two renderers (#982):

- `build_digest_data` reads the night into a `DigestData` — every quantity
  the digest shows comes from here, so the two renderings can never
  disagree about the facts.
- `render_log_lines` / `render_log_line` is the dense, grep-friendly form —
  the pre-#982 body line for line (`test_digest.py`'s original assertions
  hold against it), with one deliberate difference: a night on which every
  variant reads INSUFFICIENT_DATA renders `Regime: INSUFFICIENT_DATA (…)`
  where the old code emitted `Regime split:  (…)` with an empty group. The
  executor logs it and persists it beside the push.
- `render_human` is the ntfy body: it leads with one sentence a person can
  act on, puts words beside every fraction, names a blocked position by
  what it is, collapses idle books to a count plus the dominant reason, and
  projects the Live Gate horizon. It is bounded to ntfy's message-size
  limit (`NTFY_BODY_LIMIT_BYTES`) — over that, ntfy silently turns the
  body into an attachment file the phone cannot read as a notification,
  so the fit is total: the control banner is bounded at the source
  (`_bounded_banner`) and the tail is cut behind a marker; nothing,
  the banner included, can push the body over the limit.

Honesty rule (#982): every word in the human body derives from data the
digest holds, or the body says it does not know. The leading sentence's
action slot reads the same urgent lines the urgent push sends, so the two
notifications can never contradict each other; an idle book's reason is
only ever a rung the ledger evidences, and the residual is "no entry
signal recorded", never a synthesized market explanation.
"""

import datetime
import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.anomaly import CLEAR_CONDITION_SEPARATOR, PARTIAL_FILL, REFIRE_MARKER_SEPARATOR
from backend.benchmark import spy_benchmark_line
from backend.book_gates import LIVE_GATE_TRADES, resolve_book_config
from backend.broker import first_needs_human_instruction
from backend.dates import market_evening_window_start, market_today
from backend.executor import ENTRY_NOT_TAKEN_EVENT, BlockedEntry, DayExpiredExit, ExecutorRunSummary
from backend.models import (
    AuditEventModel,
    BookModel,
    GateEventModel,
    OrderModel,
    PositionModel,
    RegimeReadingModel,
    TradingControlModel,
)
from backend.pricing import capital_at_risk
from backend.states import (
    BOOK_ACTIVE_STATUS,
    ORDER_FILLED_STATUS,
    ORDER_STAGED_OR_SUBMITTED_STATUSES,
    POSITION_CLOSED_STATUSES,
    POSITION_OPEN_STATUS,
)
from backend.trading_control import ACTIVE, GLOBAL_SCOPE, sentinel_halt_active

logger = logging.getLogger(__name__)

# ntfy's maximum message size (docs.ntfy.sh/publish → "Attach local file":
# "If a message is greater than the maximum message size (4,096 bytes) or
# consists of non UTF-8 characters, the ntfy server will automatically
# detect the mime type and size, and send the message as an attachment
# file."). An attachment is not a notification a person reads at 20:00 on a
# phone, so the human body is fitted under this — in BYTES, not characters:
# the body carries ⛔/⚠/×/— which are 2–3 bytes each.
NTFY_BODY_LIMIT_BYTES = 4096
_TRUNCATION_MARKER = "[… cut for ntfy's size limit — full digest in the executor log]"
# The control banner's share of the human body. Halts latch (set_control
# refuses ACTIVE without an explicit resume) and the banner re-emits a
# line per un-resumed row every night, so it grows without bound; past
# this many bytes the remaining rows collapse into one counted line, and
# the leading sentence and the rest of the body still have room. Every
# row stays in the log line, which is never cut.
_BANNER_BUDGET_BYTES = NTFY_BODY_LIMIT_BYTES // 2
# The most halted book ids the leading sentence spells out before counting.
_ACTION_SCOPES_NAMED = 5

# Audit event types that interrupt the human instead of waiting for the digest
URGENT_EVENT_TYPES = frozenset(
    {
        "REPEATED_REJECTION",
        # #927: a gateway/infra-shaped preview-refusal burst, diverted out of
        # REPEATED_REJECTION's broker-rule counter into its own same-night
        # rule — still needs to interrupt a human the night it halts.
        "PREVIEW_INFRA_FAILURE",
        "DUPLICATE_ORDER",
        "PNL_SHOCK",
        "ENVELOPE_BREACH_POSTHOC",
        "ORDER_REJECTED",
        "CLOSE_REJECTED",
        "EXECUTOR_BROKER_UNAVAILABLE",
        "ORDER_LOST_AT_BROKER",
        # Exit-side escalations (#280): a needed close that DIDN'T happen is
        # exactly what must interrupt a human.
        "STALE_MARK_CLOSE_SKIPPED",
        "CLOSE_LADDER_EXHAUSTED",
        PARTIAL_FILL,
        # #546 liveness: a TP cancel persistently unconfirmed skipped the
        # close nightly with no rung consumed and no escalation ever — this
        # is that escalation.
        "TP_CANCEL_STUCK",
        # A hard crash mid-run (#474): the executor stopped doing anything.
        "CRASH_ALERT",
        # A stolen run lock mid-run (#536): another run may hold the exact
        # same lock — the executor aborted itself, but a human needs to know
        # tonight, not find out from the digest tomorrow.
        "RUN_LOCK_LOST",
        # A restore-gap UNKNOWN held rather than terminalized (#542): the
        # sync could not tell dead from real-but-invisible, so it left the
        # row alone — that decision needs a human to actually resolve it via
        # the Flex audit / panel, not silently wait for the next run.
        "RESTORE_GAP_UNKNOWN_HELD",
        # #690: an unresolved held order's blast radius is account-wide, not
        # scoped to its own book — this is the OTHER book's candidate
        # silently blocked by it, which needs the same visibility as the
        # held order itself so an operator troubleshooting a healthy book's
        # non-trading can find the actual cause without correlating events
        # by hand.
        "NETTING_BLOCKED_BY_HELD_ORDER",
        # #960: the 12:30 exit pass refused to trade (drift, an unreachable
        # Gateway, a colliding tenant). The pass pushes this itself at
        # urgent priority — listing it here is for the console's `urgent`
        # flag, which renders the audit row the same way everywhere. It
        # cannot double-push through the evening urgent digest: that query
        # is bounded by the evening run's own start time, hours later.
        "MIDDAY_EXITS_HALTED",
        # #960 review A: the pass RAN and left a position worse than it found
        # it — a resting exit cancelled and not replaced. Urgent for the same
        # reason as the halt, and more so: here the exposure actually changed.
        # This is also the operator's only backstop when the pass's own push
        # exhausts its retries, since attention.py selects on this same set.
        "MIDDAY_EXITS_DEGRADED",
        # #960 review round 2, L2: a pass that never ran at all (wrong-hour
        # firing) is the same "operator hears nothing" failure as a halt —
        # the console attention feed is the backstop when its own urgent push
        # is lost, same reasoning as MIDDAY_EXITS_HALTED above.
        "MIDDAY_EXITS_OUT_OF_WINDOW",
    }
)
_URGENT_CONTROL_ACTORS = frozenset({"anomaly", "reconciliation", "ntfy"})
# Expiry-settlement blocks are namespaced (EXPIRY_SETTLEMENT_BLOCKED_PARTIAL,
# EXPIRY_SETTLEMENT_BLOCKED_STALE_MARK, …) rather than listed individually.
_URGENT_EVENT_PREFIXES = ("EXPIRY_SETTLEMENT_BLOCKED_",)

# What each regime means for entries, in words. The entries clause is read
# off eligibility.REGIME_ALLOWED_STRATEGIES (the enforced table), not
# guessed: EVENT_CATALYST permits only the long-vol structures, which ship
# disabled, so under every engine variant it means Do Nothing.
# test_digest.py asserts every regime in that table has a row here.
REGIME_WORDS: dict[str, tuple[str, str]] = {
    "EVENT_CATALYST": ("an event-driven market", "short-premium entries are held"),
    "CALM_BULL": ("a calm bull market", "income entries are open"),
    "HIGH_VOL_NEUTRAL": ("a high-volatility range", "condors and verticals are open"),
    "TRENDING_BEAR": ("a trending bear market", "only bearish verticals are open"),
}

# StrategyType (models.py) in words, for the blocked line. A position from
# the tail-hedge playbook (LONG_PUT under role HEDGE) reads as "tail put" —
# that is what the operator calls it, and it is the only long put the
# matrix trades.
STRATEGY_WORDS: dict[str, str] = {
    "BULL_CALL_SPREAD": "bull call spread",
    "BEAR_PUT_SPREAD": "bear put spread",
    "BULL_PUT_SPREAD": "bull put spread",
    "BEAR_CALL_SPREAD": "bear call spread",
    "IRON_CONDOR": "iron condor",
    "BROKEN_WING_BUTTERFLY": "broken-wing butterfly",
    "CALENDAR_SPREAD": "calendar spread",
    "LONG_STRADDLE": "long straddle",
    "LONG_STRANGLE": "long strangle",
    "LONG_PUT": "long put",
}

# Idle-reason vocabulary (#982 item 4). Every reason is a rung the ledger
# evidences for THAT book tonight — a run-wide block, a halt whose scope
# covers the book, broker state, the executor's own SCAN_BLOCKED /
# SPEC_HARD_BLOCKED audit rows — never guessed from what the book
# "probably" wanted. A book no rung names reads IDLE_NO_SIGNAL: the
# executor drops a book whose candidates were all ineligible with no audit
# row (the suppressed reason is discarded, executor.py's entry loop), so
# the digest cannot tell the regime gate from an IVR gate, an entry-filter
# window, or a missing price history — and says so rather than naming one.
# A gate BLOCK event and a missing variant reading are NOT rungs: the
# executor records a book-scoped BlockedEntry in exactly those cases, and
# a book the run BLOCKED (`entries_blocked`) is its own bucket, not idle
# (see _leading_sentence), so those books never reach this ladder.
# Ordered by how directly the evidence names the cause, which is also the
# tie-break when idle books split evenly across reasons.
IDLE_RUN_WIDE_BLOCK = "entries blocked run-wide"
IDLE_ENTRIES_HALTED = "entries halted"
IDLE_BROKER_UNREACHABLE = "broker unreachable"
IDLE_FILTERS_UNMET = "entry filters unmet"
IDLE_SPEC_BLOCKED = "trade spec hard-blocked"
IDLE_NO_SIGNAL = "no entry signal recorded"
_IDLE_REASON_PRIORITY = (
    IDLE_RUN_WIDE_BLOCK,
    IDLE_ENTRIES_HALTED,
    IDLE_BROKER_UNREACHABLE,
    IDLE_FILTERS_UNMET,
    IDLE_SPEC_BLOCKED,
    IDLE_NO_SIGNAL,
)
# Executor audit rows that drop a book out of the entry loop with no
# BlockedEntry (executor.py `_layer_c_entries`), and what each evidences.
_ENTRY_AUDIT_IDLE_REASONS: dict[str, str] = {
    "SCAN_BLOCKED": IDLE_FILTERS_UNMET,
    "SPEC_HARD_BLOCKED": IDLE_SPEC_BLOCKED,
}
# The entry phase stopped mid-fleet (executor.py: an order-path BrokerError
# in `_try_place_entry` audits this with book_id None and returns) — every
# book after the offending one in tonight's order was never scanned. The
# roll-error abort records a run-wide BlockedEntry beside the same row;
# this one records only the row, so the digest reads the row.
_ENTRY_PHASE_ABORTED = "ENTRY_PHASE_ABORTED"

# #984/#994: from 2026-08-28 the regime race measured nothing on 32 of 34
# books — V0's EVENT_CATALYST reading (which permits only the long-vol
# structures, shipped disabled, so it means Do Nothing outright) and
# V1-V3's CALM_BULL reading (which permits income entries, but the
# catalyst-window entry filter blocked every one) produced identical
# output: no entry, either way. A book-night is CONFOUNDED when its own
# candidate reached the catalyst entry filter (eligibility.py
# check_entry_filters, block_catalyst_14dte) — meaning the regime gate
# ahead of it already passed, so THIS variant's reading would have allowed
# entry — while some OTHER detector read EVENT_CATALYST the same night
# (do-nothing outright, under every variant). EntryOutcome.catalyst_blocked
# (executor.py) tracks the block regardless of the window's size in days
# (#990 shrinks it) and regardless of whatever deeper stage a sibling
# playbook on the same book reached the same night (#1000) — only the
# block firing, not its width or its rank among the night's refusals,
# decides the confound.


@dataclass(frozen=True)
class CatalystConfound:
    """Tonight's book-nights the regime race could not discriminate. `total`
    is every book-night that produced no entry (an `ENTRY_NOT_TAKEN` row);
    `confounded` is the subset where that book's own reading would have
    allowed entry but the catalyst window blocked it, while some other
    variant read EVENT_CATALYST outright the same night."""

    confounded: int
    total: int


# The Live Gate horizon is a projection from a cadence; past this many
# days out it is not a horizon a person can act on, and the arithmetic
# behind it is a corrupt-but-parseable entry_date rather than a cadence.
_HORIZON_MAX_DAYS = 365 * 100

_DEDUP_REASON = re.compile(r"^(?P<playbook>\S+) dedup \(open: (?P<position_id>[^)]+)\)$")


@dataclass(frozen=True)
class UrgentLine:
    """One line of the urgent push. `needs_action` marks the lines that
    are a call on the operator (an urgent event, an automated HALT) as
    opposed to a state the push restates every night (an acknowledgment
    held, a self-clear RESUME)."""

    text: str
    needs_action: bool


@dataclass(frozen=True)
class BookDigestRow:
    """One active book's roster facts. `is_idle`/`is_awaiting` decide
    whether the book gets its own line or folds into the collapsed counts."""

    book_id: str
    variant: str
    underlying: str
    pnl: float
    open_positions: int
    max_positions: int
    deployed_dollars: float
    basis_dollars: float
    deployed_pct: float
    closed_trades: int
    is_idle: bool
    is_awaiting: bool


@dataclass(frozen=True)
class BlockedDigestRow:
    """A blocked entry plus, for a dedup block, the open position it
    collided with — looked up so the human line can say what the book
    already holds instead of quoting a position id."""

    book_id: str | None
    reason: str
    position_id: str | None = None
    underlying: str | None = None
    strategy: str | None = None
    open_date: str | None = None


@dataclass(frozen=True)
class RegimeDigestData:
    """Tonight's regime readings per engine variant (book_id ALL)."""

    by_regime: dict[str, list[str]]
    missing: list[str]
    total_detectors: int
    raw_line: str


@dataclass(frozen=True)
class DigestData:
    """Everything the evening digest says, before either renderer says it.

    `halted_scopes` is the banner's data form: GLOBAL_SCOPE (a global row
    or the sentinel file) or a book id per non-ACTIVE control row — the
    per-book predicates read this, never the banner's prose.
    `blocked_book_ids` is the fleet bucket of books the run itself blocked
    (its own `entries_blocked`, book-scoped) and that hold nothing; those
    books are neither trading nor idle in the human body."""

    banner: list[str]
    halted_scopes: list[str]
    regime: RegimeDigestData | None
    broker_ok: bool
    broker_instruction: str | None
    broker_api_errors: list[tuple[int, str]]
    fills: list[str]
    positions_created_count: int
    closes_placed: list[str]
    entries_placed: list[str]
    blocked_entries: list[BlockedEntry]
    blocked_rows: list[BlockedDigestRow]
    intents_expired: list[str]
    day_expired: list[DayExpiredExit]
    book_rows: list[BookDigestRow]
    idle_book_ids: list[str]
    awaiting_book_ids: list[str]
    gate_hits: list[str]
    benchmark_line: str | None
    reconciliation: str
    anomalies: list[str]
    notes: list[str]
    gate_horizon: str
    urgent_lines: list[UrgentLine] = field(default_factory=list)
    blocked_book_ids: list[str] = field(default_factory=list)
    idle_reason_counts: dict[str, int] = field(default_factory=dict)
    catalyst_confound: CatalystConfound = field(default_factory=lambda: CatalystConfound(confounded=0, total=0))


@dataclass(frozen=True)
class DigestRenderings:
    """Both renderings of one night, sharing title and priority, plus the
    urgent push's lines from the same read — the executor sends those, so
    the digest's action slot and the urgent push are one dataset."""

    title: str
    human_body: str
    log_body: str
    priority: str
    urgent_lines: list[str]


def is_urgent_event_type(event_type: str) -> bool:
    """Single source of truth for 'this audit row needs a human now' (#474).
    Used both for the nightly urgent push (urgent_events, below) and for the
    server-computed AuditEventSchema.urgent flag every console list renders —
    so the two can never drift apart again."""
    return event_type in URGENT_EVENT_TYPES or event_type.startswith(_URGENT_EVENT_PREFIXES)


async def urgent_events(session: AsyncSession, since: str) -> list[str]:
    """Tonight's interrupt-worthy events, one line each (the urgent push)."""
    return [line.text for line in await urgent_event_lines(session, since)]


async def urgent_event_lines(session: AsyncSession, since: str) -> list[UrgentLine]:
    """Tonight's interrupt-worthy events, one line each. *since* is the run's
    start timestamp (#259): a date-prefix match broke whenever the pipeline
    crossed midnight UTC — every EST-season evening — silently emptying the
    urgent push of the very rejections and halts it exists to carry."""
    from backend.labels import book_label

    events = (await session.execute(select(AuditEventModel).filter(AuditEventModel.run_at >= since))).scalars().all()
    # #600 (2026-08-20 incident): "B04" on its own tells the operator
    # nothing about what's actually halted — one lookup per DISTINCT
    # book_id, not per event (an incident often produces several urgent
    # lines on the same book).
    label_cache: dict[str, str] = {}
    # #929 round-2 MEDIUM-3: (rule, book_id) pairs whose OWN finding line
    # actually rendered above — not merely fired this run. A finding
    # suppressed by _should_alert (the `continue` below, e.g. a deduped
    # ENVELOPE_BREACH_POSTHOC re-fire after an operator resume) never adds
    # itself here, so the CONTROL_STATE_CHANGED strip below must not assume
    # its clear condition is a duplicate of a line that was never printed.
    rendered_findings: set[tuple[str, str | None]] = set()
    lines: list[UrgentLine] = []
    for e in events:
        if is_urgent_event_type(e.event_type):
            # #922: a standing anomaly (e.g. ENVELOPE_BREACH_POSTHOC on an
            # already-open position) re-records every run, but anomaly.py's
            # _should_alert already decided an unchanged/lower repeat isn't
            # worth re-interrupting a human for — it still folds into the
            # regular digest body via summary.anomalies, just not here.
            if e.actor == "anomaly" and e.payload.get("alert_suppressed"):
                continue
            # #627: "reason" carries the broker's own rejection text (e.g.
            # 'Rejected by System: Guaranteed-to-Lose combination orders are
            # not allowed') recovered via the completedStatus capture shim —
            # the most specific detail available when present, checked first.
            detail = (
                e.payload.get("reason")
                or e.payload.get("detail")
                or e.payload.get("error")
                or e.payload.get("order_ref")
                or ""
            )
            # #928: the rule's own clear condition and re-fire marker ride
            # along on the finding's own event (payload keys set by
            # anomaly.py's _halt) — short, single-line, so the full evidence
            # breakdown stays audit-payload-only per the issue's "ntfy stays
            # short".
            if clear_condition := e.payload.get("clear_condition"):
                detail = f"{detail} — clears: {clear_condition}" if detail else f"clears: {clear_condition}"
            if refire_of := e.payload.get("refire_of"):
                detail = f"{detail} — {refire_of}" if detail else refire_of
            book_bit = ""
            if e.book_id:
                if e.book_id not in label_cache:
                    label_cache[e.book_id] = await book_label(session, e.book_id)
                book_bit = f" ({label_cache[e.book_id]})"
            rendered_findings.add((e.event_type, e.book_id))
            lines.append(UrgentLine(f"{e.event_type}{book_bit}: {detail}".rstrip(": "), needs_action=True))
        elif e.event_type == "CONTROL_STATE_CHANGED" and e.actor in _URGENT_CONTROL_ACTORS:
            # #927: self-clear (anomaly.py) writes this SAME event type to
            # move a scope back to ACTIVE — labeling it "HALT by anomaly"
            # would tell the operator the opposite of what just happened,
            # undermining the exact notification trust self-clear exists to
            # protect. payload["state"] (set_control's own audit payload)
            # is checked, not the reason text, so nothing hinges on wording.
            verb = "RESUMED" if e.payload.get("state") == ACTIVE else "HALT"
            reason = e.payload.get("reason", "")
            if verb == "HALT" and e.actor == "anomaly":
                # #929 MEDIUM-4b: a HALT_ENTRIES transition written by
                # anomaly's _halt is USUALLY committed in the same run as the
                # finding's own event above (that event is what triggered
                # this transition), whose clear condition / re-fire marker
                # already rendered there via _compose_reason. Stripping the
                # duplicate copy off this line keeps a latching night's ntfy
                # body from saying the same thing twice — but only when that
                # finding line actually rendered: a re-fire _should_alert
                # suppressed (the `continue` above) never added itself to
                # rendered_findings, and this CONTROL_STATE_CHANGED line
                # (refresh_reason, anomaly.py MEDIUM-3) is then the ONLY
                # place the clear condition appears — stripping it there too
                # would delete it from the push entirely.
                rule = reason.split(": ", 1)[0]
                if (rule, e.book_id) in rendered_findings:
                    reason = reason.split(CLEAR_CONDITION_SEPARATOR, 1)[0].split(REFIRE_MARKER_SEPARATOR, 1)[0]
            lines.append(UrgentLine(f"{verb} by {e.actor}: {reason}", needs_action=verb == "HALT"))
        elif e.event_type in ("ANOMALY_ACK_HELD", "ANOMALY_ACK_CLEARED"):
            # #931: neither is a CONTROL_STATE_CHANGED — the row's state
            # isn't moving (an ack holds an ACTIVE scope ACTIVE; clearing a
            # stale ack doesn't touch state either), so these are their own
            # event types rather than reusing that one (which would corrupt
            # anomaly.py's _last_active_at provenance anchor). Same tier as
            # the CONTROL_STATE_CHANGED lines above: unconditional, every
            # sweep it fires, not gated behind is_urgent_event_type/
            # alert_suppressed — an "acknowledged" state is exactly the kind
            # of thing that must say so every night or read as silence.
            book_bit = ""
            if e.book_id:
                if e.book_id not in label_cache:
                    label_cache[e.book_id] = await book_label(session, e.book_id)
                book_bit = f" ({label_cache[e.book_id]})"
            if e.event_type == "ANOMALY_ACK_HELD":
                identity = ", ".join(e.payload.get("identity", []))
                lines.append(
                    UrgentLine(
                        f"ACKNOWLEDGED{book_bit} {e.payload.get('rule')}: since {e.payload.get('ack_since')}: {identity}",
                        needs_action=False,
                    )
                )
            else:
                lines.append(
                    UrgentLine(f"ACK CLEARED{book_bit} {e.payload.get('rule')}: evidence resolved", needs_action=False)
                )
    return lines


async def _control_banner(session: AsyncSession) -> tuple[list[str], list[str]]:
    """The banner lines and, beside them, the halted scopes as data
    (GLOBAL_SCOPE or a book id) — a book is only halted by a row whose
    scope covers it, so every per-book predicate reads the scopes."""
    from backend.labels import book_label

    lines: list[str] = []
    scopes: list[str] = []
    if sentinel_halt_active():
        lines.append("⛔ SENTINEL HALT file present — all entries blocked")
        scopes.append(GLOBAL_SCOPE)
    rows = (await session.execute(select(TradingControlModel))).scalars().all()
    for row in sorted(rows, key=lambda r: r.scope):
        if row.state != ACTIVE:
            # #600: GLOBAL is already plain English; a book scope ("B04")
            # is exactly the halt-banner line the operator couldn't decode
            # during the 2026-08-20 incident without a separate lookup.
            scope = row.scope if row.scope == GLOBAL_SCOPE else await book_label(session, row.scope)
            lines.append(f"⛔ {scope} {row.state} since {row.changed_at[:16]} — {row.reason}")
            scopes.append(row.scope)
    return lines, scopes


def _halt_covers(halted_scopes: list[str], book_id: str) -> bool:
    return GLOBAL_SCOPE in halted_scopes or book_id in halted_scopes


async def _regime_data(session: AsyncSession, today: str) -> RegimeDigestData | None:
    """Tonight's regime per engine variant. Variant DISAGREEMENT is the
    informative early signal (regime_variants.py), long before per-book
    trade counts mean anything; a split must never require querying the
    database by hand to notice (#248). `raw_line` is the dense log form."""
    rows = (
        (
            await session.execute(
                select(RegimeReadingModel).filter(RegimeReadingModel.date == today, RegimeReadingModel.book_id == "ALL")
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    by_regime: dict[str, list[str]] = {}
    missing: list[str] = []
    for r in sorted(rows, key=lambda r: r.engine_variant):
        if r.regime == "INSUFFICIENT_DATA":
            missing.append(r.engine_variant)
        else:
            by_regime.setdefault(r.regime, []).append(r.engine_variant)
    suffix = f" ({' '.join(missing)} insufficient data)" if missing else ""
    if len(by_regime) == 1:
        regime = next(iter(by_regime))
        raw_line = f"Regime: {regime} (all variants){suffix}"
    elif by_regime:
        groups = sorted(by_regime.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        rendered = " / ".join(f"{regime} ({' '.join(variants)})" for regime, variants in groups)
        raw_line = f"Regime split: {rendered}{suffix}"
    else:
        raw_line = f"Regime: INSUFFICIENT_DATA{suffix}"
    return RegimeDigestData(by_regime=by_regime, missing=missing, total_detectors=len(rows), raw_line=raw_line)


async def _gate_hits(session: AsyncSession, since: str) -> list[str]:
    events = (
        (
            await session.execute(
                select(GateEventModel).filter(GateEventModel.result == "BLOCK", GateEventModel.run_at >= since)
            )
        )
        .scalars()
        .all()
    )
    by_gate: dict[str, int] = {}
    for e in events:
        key = f"{e.book_id}:{e.gate}"
        by_gate[key] = by_gate.get(key, 0) + 1
    return [f"Gate {key} blocked ×{n}" for key, n in sorted(by_gate.items())]


@dataclass(frozen=True)
class EntryAuditEvidence:
    """What tonight's entry-loop audit rows say about idle books:
    `book_reasons` per book (the rows in _ENTRY_AUDIT_IDLE_REASONS) and
    `phase_aborted` when an ENTRY_PHASE_ABORTED row with no book — the
    scan stopped before reaching the rest of the fleet — is on the ledger."""

    book_reasons: dict[str, str]
    phase_aborted: bool


async def _entry_audit_evidence(session: AsyncSession, since: str) -> EntryAuditEvidence:
    """Tonight's entry-loop audit rows as idle evidence. A book with both
    a SCAN_BLOCKED and a SPEC_HARD_BLOCKED row is impossible —
    SCAN_BLOCKED leaves the loop — so first wins."""
    events = (
        (
            await session.execute(
                select(AuditEventModel).filter(
                    AuditEventModel.event_type.in_([*_ENTRY_AUDIT_IDLE_REASONS, _ENTRY_PHASE_ABORTED]),
                    AuditEventModel.run_at >= since,
                )
            )
        )
        .scalars()
        .all()
    )
    reasons: dict[str, str] = {}
    phase_aborted = False
    for e in events:
        if e.event_type == _ENTRY_PHASE_ABORTED:
            phase_aborted = phase_aborted or e.book_id is None
        elif e.book_id is not None:
            reasons.setdefault(e.book_id, _ENTRY_AUDIT_IDLE_REASONS[e.event_type])
    return EntryAuditEvidence(book_reasons=reasons, phase_aborted=phase_aborted)


async def _catalyst_confound(session: AsyncSession, since: str, regime: RegimeDigestData | None) -> CatalystConfound:
    events = (
        (
            await session.execute(
                select(AuditEventModel).filter(
                    AuditEventModel.event_type == ENTRY_NOT_TAKEN_EVENT,
                    AuditEventModel.run_at >= since,
                    AuditEventModel.book_id.isnot(None),
                )
            )
        )
        .scalars()
        .all()
    )
    # No detector read EVENT_CATALYST tonight → there is no do-nothing-
    # outright reading for a catalyst-blocked book to be indistinguishable
    # from, so the count stays zero even if the block itself fired.
    catalyst_read_tonight = regime is not None and "EVENT_CATALYST" in regime.by_regime
    confounded = sum(1 for e in events if catalyst_read_tonight and e.payload.get("catalyst_blocked", False))
    return CatalystConfound(confounded=confounded, total=len(events))


async def _fills_section(session: AsyncSession, since: str) -> list[str]:
    orders = (
        (
            await session.execute(
                select(OrderModel).filter(OrderModel.status == ORDER_FILLED_STATUS, OrderModel.completed_at >= since)
            )
        )
        .scalars()
        .all()
    )
    lines: list[str] = []
    for o in orders:
        strategy = (o.combo_legs or {}).get("strategy_type", o.action)
        lines.append(
            f"Filled {o.book_id} {strategy} ({o.action}) @ limit {o.limit_price:+.2f}"
            f" (decision mid {o.decision_midpoint:+.2f})"
        )
    return lines


def _grouped_blocked(blocked: list[BlockedEntry]) -> list[str]:
    """Group identical block reasons across books: six copies of
    'variant V1 reading unavailable' become one line listing the books.
    Run-wide blocks (book_id None) render as ALL, ungrouped."""
    by_reason: dict[str, list[str]] = {}
    run_wide: list[str] = []
    for entry in blocked:
        if entry.book_id is None:
            run_wide.append(f"Blocked: ALL: {entry.reason}")
        else:
            by_reason.setdefault(entry.reason, []).append(entry.book_id)
    lines: list[str] = []
    for reason, books in sorted(by_reason.items()):
        if len(books) == 1:
            lines.append(f"Blocked: {books[0]}: {reason}")
        else:
            lines.append(f"Blocked ({reason}): {' '.join(sorted(books))}")
    return lines + run_wide


def _strategy_words(strategy_type: str, playbook_id: str | None) -> str:
    if playbook_id and "tail" in playbook_id.lower():
        return "tail put"
    return STRATEGY_WORDS.get(strategy_type, strategy_type.replace("_", " ").lower())


def _article(word: str) -> str:
    """'an XSP', 'an IWM', 'a SPY', 'a GLD': tickers are read letter by
    letter, and the letters whose NAMES start with a vowel sound take 'an'."""
    return "an" if word[:1].upper() in "AEFHILMNORSX" else "a"


def _blocked_reason_words(reason: str) -> str:
    """The executor's non-dedup block reasons, in words, with the playbook
    id kept in parentheses for the log-diver. Shapes not recognised here
    render verbatim — a guessed translation would be worse than the raw."""
    m = re.match(
        r"^(?P<playbook>\S+) (?P<rest>thin credit|unpriceable|gated|leg collision|preview refused|halted|rejected)(?P<detail>.*)$",
        reason,
    )
    if m is None:
        cm = re.match(r"^consensus (?P<votes>\d+)/(?P<need>\d+) on (?P<regime>\S+)$", reason)
        if cm is not None:
            return f"only {cm.group('votes')} of {cm.group('need')} engines agree on {cm.group('regime')}"
        return reason
    playbook, kind, detail = m.group("playbook"), m.group("rest"), m.group("detail").strip()
    words = {
        "thin credit": "credit too thin",
        "unpriceable": "could not price the spread",
        "gated": "stopped by the risk envelope",
        "leg collision": "a resting order already uses those legs",
        "preview refused": "broker refused the preview",
        "halted": "entries halted",
        "rejected": "broker rejected the order",
    }[kind]
    tail = f" {detail}" if detail else ""
    return f"{words}{tail} ({playbook})"


def _compute_gate_horizon(
    today: str,
    fleet_closed_trades: int,
    leading_book_closed: int,
    first_entry_date: str | None,
) -> str:
    """The Live Gate horizon line (#982 item 5).

    Formula — the fleet's cadence is measured by its LEADING book, since the
    question is when the FIRST book clears the gate:

        elapsed_days  = max(today − first_entry_date, 1)     # calendar days
        rate          = max(leading_book_closed, 1) / elapsed_days
        days_to_gate  = (LIVE_GATE_TRADES − leading_book_closed) / rate
        horizon       = today + round(days_to_gate)            → "Month Year"

    `first_entry_date` is the earliest position entry across the fleet (the
    cadence clock starts with the first trade, not the first book seed).
    Fewer than two closed trades fleet-wide is not a cadence, so the line
    says "not computable yet" rather than projecting from one point; so
    does an unparseable date or a projection past _HORIZON_MAX_DAYS (a
    corrupt-but-parseable entry_date such as 0001-01-01 makes the rate
    arbitrarily small — the line degrades, it never raises out of the
    nightly push).
    """
    prefix = f"At this cadence the earliest book reaches {LIVE_GATE_TRADES} closed trades"
    if fleet_closed_trades < 2 or not first_entry_date:
        return f"{prefix}: not computable yet"
    try:
        d_today = datetime.date.fromisoformat(today)
        d_first = datetime.date.fromisoformat(first_entry_date)
    except ValueError:
        return f"{prefix}: not computable yet"
    trades_needed = LIVE_GATE_TRADES - leading_book_closed
    if trades_needed <= 0:
        return f"{prefix}: already reached"
    elapsed_days = max((d_today - d_first).days, 1)
    rate = max(leading_book_closed, 1) / elapsed_days
    days_to_gate = trades_needed / rate
    if days_to_gate > _HORIZON_MAX_DAYS:
        return f"{prefix}: not computable yet"
    target = d_today + datetime.timedelta(days=round(days_to_gate))
    return f"{prefix} around {target.strftime('%B %Y')}"


def _market_words(regime: str) -> tuple[str, str]:
    return REGIME_WORDS.get(regime, (f"a {regime.lower().replace('_', ' ')} market", ""))


def _regime_words(regime: RegimeDigestData | None) -> str:
    """The regime clause. Entries are decided per book from that book's
    own variant reading (executor.py `_layer_c_entries`), so the largest
    group's entries clause is scoped to the variants that read it whenever
    it is not every detector's — on a split the minority variants, and
    the books on them, are named with their own reading."""
    if regime is None:
        return "no regime reading tonight"
    if not regime.by_regime:
        return f"all {regime.total_detectors} detectors report insufficient data"
    groups = sorted(regime.by_regime.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    top_regime, top_variants = groups[0]
    market, entries = _market_words(top_regime)
    lead = f"{len(top_variants)} of {regime.total_detectors} detectors see {market}"
    if len(top_variants) == regime.total_detectors:
        return f"{lead}; {entries}" if entries else lead
    clauses = [lead, f"{entries} for books on {' '.join(top_variants)}" if entries else ""]
    clauses.extend(
        f"{' '.join(variants)} {'read' if len(variants) > 1 else 'reads'} {_market_words(other)[0]}"
        for other, variants in groups[1:]
    )
    if regime.missing:
        verb = "report" if len(regime.missing) > 1 else "reports"
        clauses.append(f"{' '.join(regime.missing)} {verb} insufficient data")
    return "; ".join(clause for clause in clauses if clause)


def _operator_action(data: DigestData) -> str:
    """The one thing that needs the operator tonight, if anything. Blocked
    entries and resting orders are the system working, not a call to act.

    "nothing needs you tonight" is a claim, so it is only made when every
    surface that can call for a human is clear: the control banner (scoped
    — a single halted book names that book, not the fleet), broker state,
    the urgent push's own action lines (the same rows `urgent_event_lines`
    sends — an urgent push and a digest that says stand down can never
    arrive together), reconciliation, anomaly findings, run-wide blocks."""
    if data.halted_scopes:
        if GLOBAL_SCOPE in data.halted_scopes:
            return "action needed: entries are halted fleet-wide, resolve it in the console"
        scopes = sorted(data.halted_scopes)
        books = " ".join(scopes[:_ACTION_SCOPES_NAMED])
        if len(scopes) > _ACTION_SCOPES_NAMED:
            books += f" (+{len(scopes) - _ACTION_SCOPES_NAMED} more)"
        return f"action needed: entries are halted for {books}, resolve it in the console"
    if not data.broker_ok:
        return f"action needed: {data.broker_instruction or 'IB Gateway was unreachable, check it'}"
    actions = [line.text for line in data.urgent_lines if line.needs_action]
    if actions:
        more = f" (+{len(actions) - 1} more in the urgent push)" if len(actions) > 1 else ""
        return f"action needed: {actions[0]}{more}"
    if data.reconciliation == "DRIFT":
        return "action needed: resolve reconciliation drift"
    if data.anomalies:
        return f"action needed: review {data.anomalies[0].split(':', 1)[0]}"
    run_wide = [b.reason for b in data.blocked_rows if b.book_id is None]
    if run_wide:
        return f"action needed: {run_wide[0]}"
    return "nothing needs you tonight"


def _unblocked_idle_ids(data: DigestData) -> list[str]:
    """The human body's idle bucket: idle books the run did not itself
    block. (The log line's idle list keeps every idle id, blocked or not.)"""
    blocked = set(data.blocked_book_ids)
    return [book_id for book_id in data.idle_book_ids if book_id not in blocked]


@dataclass(frozen=True)
class FleetCounts:
    """One bucket per book, so the counts sum to the fleet. Precedence:
    trading (holds or has held a position, or carries P&L) → awaiting fill
    (an order resting at the broker) → blocked (the run's own
    `entries_blocked` names the book) → idle (the rest). A trading book
    with a block (a dedup block on the position it holds) counts as
    trading here; its block still renders on its own "Blocked:" line.
    test_digest.py asserts the sum on every DigestData it builds."""

    trading: int
    idle: int
    awaiting: int
    blocked: int


def fleet_counts(data: DigestData) -> FleetCounts:
    return FleetCounts(
        trading=sum(1 for b in data.book_rows if not b.is_idle and not b.is_awaiting),
        idle=len(_unblocked_idle_ids(data)),
        awaiting=len(data.awaiting_book_ids),
        blocked=len(data.blocked_book_ids),
    )


def _leading_sentence(data: DigestData) -> str:
    counts = fleet_counts(data)
    n_trading, n_idle, n_awaiting, n_blocked = counts.trading, counts.idle, counts.awaiting, counts.blocked
    fleet = [f"{n_trading} book{'' if n_trading == 1 else 's'} trading", f"{n_idle} idle"]
    if n_awaiting:
        fleet.append(f"{n_awaiting} awaiting fill")
    if n_blocked:
        fleet.append(f"{n_blocked} blocked")
    return f"{', '.join(fleet)}; {_regime_words(data.regime)}; {_operator_action(data)}."


def _idle_line(data: DigestData) -> str:
    n = len(_unblocked_idle_ids(data))
    noun = "book" if n == 1 else "books"
    if not data.idle_reason_counts:
        return f"{n} {noun} idle"
    dominant, count = max(data.idle_reason_counts.items(), key=lambda kv: (kv[1], -_IDLE_REASON_PRIORITY.index(kv[0])))
    qualifier = "" if count == n else "mostly "
    return f"{n} {noun} idle ({qualifier}{dominant})"


def _catalyst_confound_line(data: DigestData) -> str | None:
    """#994: zero renders nothing — a night with no confound is not worth a
    line, and a fleet-wide `0 of 34` reads as noise every quiet night."""
    if data.catalyst_confound.confounded == 0:
        return None
    return (
        f"{data.catalyst_confound.confounded} of {data.catalyst_confound.total} book-nights tonight were "
        "indistinguishable across variants (catalyst block)"
    )


def _fit_ntfy_length(lines: list[str]) -> str:
    """Last resort under NTFY_BODY_LIMIT_BYTES: keep whole lines from the
    top, stop at the first line that does not fit, end on a marker. The
    result is never over the limit — over it there is no notification at
    all, so nothing is exempt: the control banner comes first and is kept
    as far as it fits, and its own growth is bounded by _bounded_banner
    so that in practice it always does. render_human drops the per-book
    roster before this, so it only bites on an enormous tail."""
    body = "\n".join(lines)
    if len(body.encode("utf-8")) <= NTFY_BODY_LIMIT_BYTES:
        return body
    budget = NTFY_BODY_LIMIT_BYTES - len(("\n" + _TRUNCATION_MARKER).encode("utf-8"))
    kept: list[str] = []
    used = 0
    for line in lines:
        cost = len((line + "\n").encode("utf-8"))
        if used + cost > budget:
            break
        kept.append(line)
        used += cost
    return "\n".join(kept + [_TRUNCATION_MARKER])


def _bounded_banner(banner: list[str]) -> list[str]:
    """The control banner for the human body, bounded at the source: whole
    lines up to _BANNER_BUDGET_BYTES (the first line always — a halted
    system says so every night), the rest collapsed into one counted line.
    The log line carries every row; a pinned block that could grow past
    the ntfy limit would lose the whole notification, banner included."""
    if len("\n".join(banner).encode("utf-8")) <= _BANNER_BUDGET_BYTES:
        return banner
    elision_cost = len(_banner_elision(len(banner)).encode("utf-8"))  # the widest count the line can carry
    kept: list[str] = []
    used = 0
    for line in banner:
        cost = len((line + "\n").encode("utf-8"))
        if kept and used + cost + elision_cost > _BANNER_BUDGET_BYTES:
            break
        kept.append(line)
        used += cost
    return kept + [_banner_elision(len(banner) - len(kept))]


def _banner_elision(n_more: int) -> str:
    return f"⛔ +{n_more} more scopes halted — every row is in the executor log and the console"


def _broker_lines(data: DigestData) -> list[str]:
    # #823: a classified needs-a-human code (e.g. 10141, paper-trading
    # disclaimer) turns the generic unreachable line into the specific
    # instruction; unclassified failures keep the generic line but append
    # every captured API error so the cause is never swallowed again.
    # Body only — the ntfy TITLE must stay ASCII (#598).
    if data.broker_ok:
        return []
    if data.broker_instruction is not None:
        return [f"⛔ ACTION NEEDED: {data.broker_instruction}"]
    lines = ["⚠ IB Gateway unreachable — no orders were possible tonight"]
    lines.extend(f"  broker API error {code}: {message}" for code, message in data.broker_api_errors)
    return lines


def _order_lines(data: DigestData) -> list[str]:
    lines = list(data.fills)
    if data.positions_created_count:
        lines.append(f"{data.positions_created_count} position(s) opened from fills")
    lines.extend(f"Close submitted: {ref}" for ref in data.closes_placed)
    lines.extend(f"Entry submitted: {ref}" for ref in data.entries_placed)
    return lines


def _expiry_lines(data: DigestData) -> list[str]:
    lines = [f"Intent expired: {ref}" for ref in data.intents_expired]
    for exit_ in data.day_expired:
        # #959: informational only, never the headline — a DAY exit running
        # out its own session unfilled is expected, distinct from a genuine
        # ORDER_LOST_AT_BROKER (which keeps the urgent push/headline).
        line = f"Exit unfilled today: {exit_.order_ref}"
        if exit_.reissue_limit is not None:
            line += f" — re-issued at {exit_.reissue_limit:+.2f}"
        lines.append(line)
    return lines


def _reconciliation_line(data: DigestData) -> str:
    # Reconciliation is stated explicitly — silence must never read as success.
    if data.reconciliation == "CLEAN":
        return "Reconciliation clean"
    if data.reconciliation == "DRIFT":
        return "⛔ Reconciliation DRIFT — entries halted until resolved"
    return f"Reconciliation: {data.reconciliation}"


def render_log_lines(data: DigestData) -> list[str]:
    """The dense form: the pre-#982 digest body, line for line (the one
    deliberate difference is the all-INSUFFICIENT_DATA regime line, see the
    module docstring). Grep-friendly (`pos 0/8`, `gate 2/30`, idle ids
    listed) — the executor logs it and persists it beside the human push."""
    lines: list[str] = []
    lines.extend(data.banner)
    if data.regime is not None:
        lines.append(data.regime.raw_line)
    lines.extend(_broker_lines(data))
    lines.extend(_order_lines(data))
    lines.extend(_grouped_blocked(data.blocked_entries))
    lines.extend(_expiry_lines(data))
    for b in data.book_rows:
        if b.is_idle or b.is_awaiting:
            continue
        lines.append(
            f"{b.book_id} [{b.variant}/{b.underlying}] P&L {b.pnl:+.0f} | "
            f"pos {b.open_positions}/{b.max_positions} | "
            f"deployed {b.deployed_pct:.0f}% | gate {b.closed_trades}/{LIVE_GATE_TRADES}"
        )
    # Books whose orders are resting at the broker are NOT idle — orders only
    # become positions on the next fill sync, so on entry-heavy nights the
    # old positions-only heuristic listed every submitting book as idle (#225).
    if data.awaiting_book_ids:
        lines.append(
            f"{len(data.awaiting_book_ids)} book(s) awaiting fill (orders resting at broker): "
            f"{' '.join(data.awaiting_book_ids)}"
        )
    # 22 books of roster every night buries the signal (#160/ADR-0009), but
    # absence must never be silent (supervision.md) — the idle ids stay here.
    if data.idle_book_ids:
        lines.append(
            f"{len(data.idle_book_ids)} book(s) idle (no positions, gate 0/{LIVE_GATE_TRADES}): "
            f"{' '.join(data.idle_book_ids)}"
        )
    if (confound_line := _catalyst_confound_line(data)) is not None:
        lines.append(confound_line)
    if data.benchmark_line:
        lines.append(data.benchmark_line)
    lines.extend(data.gate_hits)
    lines.append(_reconciliation_line(data))
    lines.extend(f"⛔ {anomaly}" for anomaly in data.anomalies)
    lines.extend(data.notes)
    return lines


def render_log_line(data: DigestData) -> str:
    return "\n".join(render_log_lines(data))


def _human_book_lines(data: DigestData) -> list[str]:
    lines: list[str] = []
    for b in data.book_rows:
        if b.is_idle or b.is_awaiting:
            continue
        lines.append(
            f"{b.book_id} [{b.variant}/{b.underlying}] P&L {b.pnl:+.0f} | "
            f"{b.open_positions} of {b.max_positions} positions open | "
            f"${b.deployed_dollars:,.0f} of ${b.basis_dollars:,.0f} deployed | "
            f"{b.closed_trades} of {LIVE_GATE_TRADES} closed trades toward the live gate"
        )
    return lines


def _human_blocked_lines(data: DigestData) -> list[str]:
    lines: list[str] = []
    by_reason: dict[str, list[str]] = {}
    run_wide: list[str] = []
    for b in data.blocked_rows:
        if b.position_id is not None and b.book_id is not None:
            # A dedup block: name what the book already holds, id in parens
            # for the log-diver (#982 item 3). Position not found (closed
            # between the block and the digest) → the raw reason, not a guess.
            if b.underlying is None or b.strategy is None:
                lines.append(f"Blocked: {b.book_id}: {b.reason}")
                continue
            opened = f" opened {b.open_date}" if b.open_date else ""
            lines.append(
                f"Blocked: {b.book_id}: already holds {_article(b.underlying)} {b.underlying} {b.strategy}"
                f"{opened} ({b.position_id})"
            )
        elif b.book_id is None:
            run_wide.append(f"Blocked: ALL: {b.reason}")
        else:
            by_reason.setdefault(_blocked_reason_words(b.reason), []).append(b.book_id)
    for reason, books in sorted(by_reason.items()):
        if len(books) == 1:
            lines.append(f"Blocked: {books[0]}: {reason}")
        else:
            lines.append(f"Blocked ({reason}): {', '.join(sorted(books))}")
    return lines + run_wide


def _render_human_lines(data: DigestData, with_book_rows: bool) -> list[str]:
    lines: list[str] = []
    lines.extend(_bounded_banner(data.banner))
    lines.extend(_broker_lines(data))
    lines.append(_leading_sentence(data))
    lines.extend(_order_lines(data))
    if with_book_rows:
        lines.extend(_human_book_lines(data))
    else:
        n = sum(1 for b in data.book_rows if not b.is_idle and not b.is_awaiting)
        lines.append(f"{n} trading book rows omitted for length — per-book detail is in the executor log")
    lines.extend(_human_blocked_lines(data))
    if _unblocked_idle_ids(data):
        lines.append(_idle_line(data))
    if (confound_line := _catalyst_confound_line(data)) is not None:
        lines.append(confound_line)
    if data.awaiting_book_ids:
        n = len(data.awaiting_book_ids)
        lines.append(f"{n} book{'' if n == 1 else 's'} awaiting fill (orders resting at broker)")
    lines.append(data.gate_horizon)
    recon = _reconciliation_line(data)
    lines.append(f"{data.benchmark_line}; {recon}." if data.benchmark_line else f"{recon}.")
    lines.extend(data.gate_hits)
    lines.extend(f"⛔ {anomaly}" for anomaly in data.anomalies)
    lines.extend(_expiry_lines(data))
    lines.extend(data.notes)
    return lines


def render_human(data: DigestData) -> str:
    """The ntfy body (#982). Order:

    1. Control banner / broker failure (a halted system says so first).
    2. Leading sentence: fleet counts (trading / idle / awaiting / blocked),
       the regime consensus in words, the one operator action if any.
    3. Fills and orders submitted tonight.
    4. Per-book rows, words beside every fraction.
    5. Blocked entries in words (dedup blocks name the held position).
    6. Idle count + dominant reason; awaiting-fill count. No ids.
    7. Live Gate horizon.
    8. Benchmark and reconciliation, one sentence.
    9. Gate hits, anomalies, expiries, notes.

    Fitted under NTFY_BODY_LIMIT_BYTES, totally: the control banner is
    bounded at the source (_bounded_banner); the per-book roster (the bulk
    at full matrix) goes first, whole; only then are trailing lines cut
    behind a marker. The banner leads, so it is the last thing the cut
    can reach, and its bound keeps it out of reach in practice.
    """
    lines = _render_human_lines(data, with_book_rows=True)
    if len("\n".join(lines).encode("utf-8")) > NTFY_BODY_LIMIT_BYTES:
        lines = _render_human_lines(data, with_book_rows=False)
    return _fit_ntfy_length(lines)


async def _blocked_rows(session: AsyncSession, blocked: list[BlockedEntry]) -> list[BlockedDigestRow]:
    rows: list[BlockedDigestRow] = []
    for entry in blocked:
        m = _DEDUP_REASON.match(entry.reason)
        if m is None:
            rows.append(BlockedDigestRow(book_id=entry.book_id, reason=entry.reason))
            continue
        pos = await session.get(PositionModel, m.group("position_id"))
        rows.append(
            BlockedDigestRow(
                book_id=entry.book_id,
                reason=entry.reason,
                position_id=m.group("position_id"),
                underlying=pos.underlying if pos is not None else None,
                strategy=_strategy_words(pos.strategy_type, pos.playbook_id) if pos is not None else None,
                open_date=pos.entry_date if pos is not None else None,
            )
        )
    return rows


def _idle_reasons(
    idle_ids: list[str],
    run_wide_blocked: bool,
    halted_scopes: list[str],
    broker_ok: bool,
    entry_audit: EntryAuditEvidence,
) -> dict[str, int]:
    """Why each idle book sat out, from evidence the digest holds (#982 item
    4). Rungs, in the order the code runs them, first match wins:

    1. a run-wide block — `entries_blocked` with book_id None (STALE_DATA,
       the roll-error abort) — the scan never started for the book;
    2. a control halt whose scope covers THIS book (GLOBAL/sentinel or the
       book's own id — one halted book never explains thirty);
    3. the broker unreachable;
    4. the book's own SCAN_BLOCKED / SPEC_HARD_BLOCKED audit row tonight —
       evidence a book carries because the run DID reach it, so it always
       outranks the mid-run abort rung below even though that rung is
       checked first in the run-wide case;
    5. a mid-run ENTRY_PHASE_ABORTED row with no book (the order-path abort
       mid-fleet) — only for a book the run never got to, which is exactly
       the book with none of rungs 2-4's evidence; a book scanned before the
       abort keeps its own reason instead of being relabelled run-wide;
    6. otherwise IDLE_NO_SIGNAL — the ledger records nothing for the book,
       and the digest says that rather than naming a cause.

    A gate BLOCK event and a missing variant reading are not rungs: the
    executor records a book-scoped BlockedEntry in both cases, so those
    books are in the blocked bucket and never in *idle_ids*, which is the
    unblocked idle bucket (see _leading_sentence)."""
    counts: dict[str, int] = {}
    for book_id in idle_ids:
        if run_wide_blocked:
            reason = IDLE_RUN_WIDE_BLOCK
        elif _halt_covers(halted_scopes, book_id):
            reason = IDLE_ENTRIES_HALTED
        elif not broker_ok:
            reason = IDLE_BROKER_UNREACHABLE
        elif book_id in entry_audit.book_reasons:
            reason = entry_audit.book_reasons[book_id]
        elif entry_audit.phase_aborted:
            reason = IDLE_RUN_WIDE_BLOCK
        else:
            reason = IDLE_NO_SIGNAL
        counts[reason] = counts.get(reason, 0) + 1
    return counts


async def build_digest_data(
    session: AsyncSession, summary: ExecutorRunSummary, today: str | None = None, since: str | None = None
) -> DigestData:
    """Read the night into one DigestData for both renderers.

    *today* is the run's MARKET date (America/New_York, #259) — used for the
    regime-reading lookup. *since* is the run's start timestamp — every event
    filter uses it, because a date-prefix match silently dropped everything
    written after a mid-run UTC midnight (every EST-season evening)."""
    today = today or market_today().isoformat()
    # #545 L2: f"{today}T00:00:00" mixes a MARKET date with UTC run_at rows —
    # yesterday's post-19:00-ET events (already past 00:00 UTC) re-enter
    # tonight's sections in EST season. The real fallback (manual/test paths
    # only; the executor always passes since=summary.run_started_at) is the
    # same evening-window start the duplicate-order check uses.
    since = since or market_evening_window_start(datetime.date.fromisoformat(today))

    banner, halted_scopes = await _control_banner(session)
    regime = await _regime_data(session, today)
    fills = await _fills_section(session, since)
    gate_hits = await _gate_hits(session, since)
    entry_audit = await _entry_audit_evidence(session, since)
    catalyst_confound = await _catalyst_confound(session, since, regime)
    urgent_lines = await urgent_event_lines(session, since)
    benchmark = await spy_benchmark_line(session)
    broker_instruction = (
        None if summary.broker_ok else first_needs_human_instruction(code for code, _ in summary.broker_api_errors)
    )

    books = (
        (await session.execute(select(BookModel).filter(BookModel.status == BOOK_ACTIVE_STATUS, BookModel.id != "B00")))
        .scalars()
        .all()
    )
    pending_books = set(
        (
            await session.execute(
                select(OrderModel.book_id).filter(OrderModel.status.in_(ORDER_STAGED_OR_SUBMITTED_STATUSES))
            )
        )
        .scalars()
        .all()
    )
    book_rows: list[BookDigestRow] = []
    idle_ids: list[str] = []
    awaiting_ids: list[str] = []
    entry_dates: list[str] = []
    for book in sorted(books, key=lambda b: b.id):
        config = resolve_book_config(book.config)
        positions = (await session.execute(select(PositionModel).filter_by(book_id=book.id))).scalars().all()
        open_positions = [p for p in positions if p.status == POSITION_OPEN_STATUS]
        closed = sum(1 for p in positions if p.status in POSITION_CLOSED_STATUSES)
        entry_dates.extend(p.entry_date for p in positions if p.entry_date)
        deployed = sum(capital_at_risk(p.max_loss, p.contracts) for p in open_positions)
        basis = config.envelope.basis
        deployed_pct = deployed / basis * 100.0 if basis > 0 else 0.0
        pnl = (book.last_mtm - book.starting_capital) if book.last_mtm is not None else 0.0
        is_idle = is_awaiting = False
        if not open_positions and closed == 0 and pnl == 0.0:
            if book.id in pending_books:
                is_awaiting = True
                awaiting_ids.append(book.id)
            else:
                is_idle = True
                idle_ids.append(book.id)
        book_rows.append(
            BookDigestRow(
                book_id=book.id,
                variant=config.variant or "?",
                underlying=config.underlying or "?",
                pnl=pnl,
                open_positions=len(open_positions),
                max_positions=config.envelope.max_positions,
                deployed_dollars=deployed,
                basis_dollars=basis,
                deployed_pct=deployed_pct,
                closed_trades=closed,
                is_idle=is_idle,
                is_awaiting=is_awaiting,
            )
        )

    gate_horizon = _compute_gate_horizon(
        today,
        fleet_closed_trades=sum(b.closed_trades for b in book_rows),
        leading_book_closed=max((b.closed_trades for b in book_rows), default=0),
        first_entry_date=min(entry_dates) if entry_dates else None,
    )
    blocked_books = {entry.book_id for entry in summary.entries_blocked if entry.book_id is not None}
    blocked_ids = [book_id for book_id in idle_ids if book_id in blocked_books]
    idle_reason_counts = _idle_reasons(
        [book_id for book_id in idle_ids if book_id not in blocked_books],
        run_wide_blocked=any(entry.book_id is None for entry in summary.entries_blocked),
        halted_scopes=halted_scopes,
        broker_ok=summary.broker_ok,
        entry_audit=entry_audit,
    )

    return DigestData(
        banner=banner,
        halted_scopes=halted_scopes,
        regime=regime,
        broker_ok=summary.broker_ok,
        broker_instruction=broker_instruction,
        broker_api_errors=summary.broker_api_errors,
        fills=fills,
        positions_created_count=len(summary.positions_created),
        closes_placed=summary.closes_placed,
        entries_placed=summary.entries_placed,
        blocked_entries=summary.entries_blocked,
        blocked_rows=await _blocked_rows(session, summary.entries_blocked),
        intents_expired=summary.intents_expired,
        day_expired=summary.day_expired,
        book_rows=book_rows,
        idle_book_ids=idle_ids,
        awaiting_book_ids=awaiting_ids,
        gate_hits=gate_hits,
        benchmark_line=benchmark,
        reconciliation=summary.reconciliation,
        anomalies=summary.anomalies,
        notes=summary.notes,
        gate_horizon=gate_horizon,
        urgent_lines=urgent_lines,
        blocked_book_ids=blocked_ids,
        idle_reason_counts=idle_reason_counts,
        catalyst_confound=catalyst_confound,
    )


def _title_and_priority(data: DigestData) -> tuple[str, str]:
    # The title carries the blocked count so a fully-blocked night never
    # reads "all quiet" (#942); a blocked count alone never escalates.
    title_bits: list[str] = []
    if data.banner or data.anomalies or data.reconciliation == "DRIFT":
        title_bits.append("HALTED" if data.banner else "alerts")
    if data.entries_placed:
        title_bits.append(f"{len(data.entries_placed)} entered")
    if data.closes_placed:
        title_bits.append(f"{len(data.closes_placed)} closing")
    if data.blocked_entries:
        title_bits.append(f"{len(data.blocked_entries)} blocked")
    if not title_bits:
        title_bits.append("all quiet")
    title = "basis executor: " + ", ".join(title_bits)
    priority = (
        "high" if data.banner or data.anomalies or not data.broker_ok or data.reconciliation == "DRIFT" else "default"
    )
    return title, priority


async def compose_executor_digest_renderings(
    session: AsyncSession, summary: ExecutorRunSummary, today: str | None = None, since: str | None = None
) -> DigestRenderings:
    """Both bodies from one read of the night: the human one is pushed, the
    dense one is logged and persisted beside it (spec/supervision.md). The
    urgent push's lines ride along from the same read."""
    data = await build_digest_data(session, summary, today=today, since=since)
    title, priority = _title_and_priority(data)
    return DigestRenderings(
        title=title,
        human_body=render_human(data),
        log_body=render_log_line(data),
        priority=priority,
        urgent_lines=[line.text for line in data.urgent_lines],
    )


async def compose_executor_digest(
    session: AsyncSession,
    summary: ExecutorRunSummary,
    today: str | None = None,
    since: str | None = None,
    format: Literal["human", "log"] = "human",
) -> tuple[str, str, str]:
    """Build (title, body, ntfy_priority). *format* picks the body: "human"
    (the ntfy push, default) or "log" (the dense line)."""
    renderings = await compose_executor_digest_renderings(session, summary, today=today, since=since)
    body = renderings.log_body if format == "log" else renderings.human_body
    return renderings.title, body, renderings.priority

"""states.py — centralized state vocabularies for order/position/book
lifecycle query predicates (#674).

AGENTS.md's state-enumeration review rule (#671) asks a change that touches
lifecycle states to "list every existing predicate that enumerates states of
that kind... and re-verify each still covers the world" — the cross-book
netting gate reasoned over OPEN positions from an era when open positions
WERE the account's whole broker-visible exposure, and silently missed
STAGED/SUBMITTED/PARTIAL orders as real pre-position exposure once those
existed (#665). That rule is diligence-shaped: it relies on a reader finding
every predicate by hand. Before this module, "pending order statuses" was
independently spelled out FOUR times (`book_gates.PENDING_ORDER_STATUSES`,
`restore_drill.PENDING_ORDER_STATUSES`, `main._LIVE_ORDER_STATUSES`,
`observation._LIVE_ORDER_STATUSES` — all `("STAGED", "SUBMITTED", "PARTIAL")`,
none importing another), and "closed position statuses" twice
(`console._CLOSED_STATUSES`, `empirical_null_drill._CLOSED_STATUSES`) — a
new state added to one copy silently does not reach the others.

This module makes the review mechanical instead of diligent: every query
predicate enumerating an order/position/book status imports a named
constant from here (or names a deliberately narrower subset with a comment
citing the set it's a subset of, e.g. ORDER_STAGED_OR_SUBMITTED_STATUSES
below). `backend/tests/test_state_vocabularies.py` greps backend/*.py for
raw status-literal predicates outside this module and fails naming the
offender — a deliberate narrow literal predicate stays expressible via a
`# state-literal-ok: <reason>` comment the tripwire honors, so "exactly
OPEN, on purpose" doesn't have to hide behind a manufactured one-member set.

Adding a new state now forces exactly one edit here; every predicate that
imports the relevant set updates automatically, and the tripwire's failure
message on a call site importing nothing shows the reviewer precisely which
predicates exist to reconsider.
"""

# ---------------------------------------------------------------------------
# OrderModel.status: STAGED -> SUBMITTED -> (PARTIAL | FILLED | CANCELLED | REJECTED)
# PARTIAL is a human-resolved latch (#283/#348), not an ordinary terminal
# state — it stays pending, encumbered, until resolved through the
# resolution panel; it belongs in ORDER_PENDING_STATUSES, never in
# ORDER_TERMINAL_STATUSES.
# ---------------------------------------------------------------------------

ORDER_PENDING_STATUSES: frozenset[str] = frozenset({"STAGED", "SUBMITTED", "PARTIAL"})
ORDER_TERMINAL_STATUSES: frozenset[str] = frozenset({"FILLED", "CANCELLED", "REJECTED"})
ORDER_FILLED_STATUS = "FILLED"
# Exactly PARTIAL: a human-resolved latch, distinct from "still awaiting any
# broker verdict" (ORDER_STAGED_OR_SUBMITTED_STATUSES) — attention.py's
# resolution-needed query means precisely this one state.
ORDER_PARTIAL_STATUS = "PARTIAL"

# Subset of ORDER_PENDING_STATUSES, excluding PARTIAL deliberately: these
# predicates ask "still awaiting a broker verdict, not yet even partially
# executed" — PARTIAL means the broker already returned SOMETHING, a
# different question (digest.py's live-order count, resolution.py's
# cancel-eligibility check).
ORDER_STAGED_OR_SUBMITTED_STATUSES: frozenset[str] = frozenset({"STAGED", "SUBMITTED"})

# Exactly SUBMITTED, the narrowest member of ORDER_PENDING_STATUSES: "the
# broker is working this order right now." STAGED never reached the broker
# and PARTIAL is a human-resolved latch — neither can be cancelled and
# re-marked. midday_exits.py's resting-exit reprice (#960) means precisely
# this one state.
ORDER_SUBMITTED_STATUS = "SUBMITTED"

# Not a subset of ORDER_PENDING_STATUSES: FILLED is terminal, PARTIAL is
# pending — this predicate (analysis.py's fill-quality report) means "has at
# least some real fill evidence to measure," which both satisfy for
# different reasons.
ORDER_FILLED_OR_PARTIAL_STATUSES: frozenset[str] = frozenset({"FILLED", "PARTIAL"})

# Subset of ORDER_TERMINAL_STATUSES, excluding FILLED: "died without
# executing" (anomaly.py's rejection-streak detector).
ORDER_CANCELLED_OR_REJECTED_STATUSES: frozenset[str] = frozenset({"CANCELLED", "REJECTED"})

# ---------------------------------------------------------------------------
# ShareOrderModel.status (#1054): STAGED -> SUBMITTED -> (FILLED | CANCELLED |
# REJECTED). A separate vocabulary from OrderModel's on purpose, even where
# the words match: there is NO PARTIAL latch — shares are fungible, so a
# partial fill is booked exactly (filled_quantity) and the row terminalizes
# CANCELLED. Every reader of "a share order still working at the broker"
# (the ghost-order scan, the sync-pending carve-out, the one-rebalance-at-a-
# time guard) imports SHARE_ORDER_PENDING_STATUSES.
# ---------------------------------------------------------------------------

SHARE_ORDER_PENDING_STATUSES: frozenset[str] = frozenset({"STAGED", "SUBMITTED"})
SHARE_ORDER_TERMINAL_STATUSES: frozenset[str] = frozenset({"FILLED", "CANCELLED", "REJECTED"})

# ShareOrderModel.purpose (#1074): why the order exists. REBALANCE is the
# month-end rotation (the only kind before #1074, hence the column default);
# FLATTEN is a FLATTEN_REQUESTED sell (ADR-0011). Readers that judge the
# month-end rebalance — the missed/unfilled-rebalance digest lines, the
# expired-order note — must filter on REBALANCE so a flatten's non-fill is
# never reported as a missed rotation, and vice versa.
SHARE_ORDER_PURPOSE_REBALANCE = "REBALANCE"
SHARE_ORDER_PURPOSE_FLATTEN = "FLATTEN"
SHARE_ORDER_PURPOSES: frozenset[str] = frozenset({SHARE_ORDER_PURPOSE_REBALANCE, SHARE_ORDER_PURPOSE_FLATTEN})

# ShareDistributionModel.status (#1074): a broker cash distribution is either
# CREDITED to exactly one designated book, or UNATTRIBUTED (surfaced, never
# guessed). Both are terminal — a row is written once, keyed on IBKR's
# transactionID, which is what makes the nightly credit idempotent.
SHARE_DISTRIBUTION_CREDITED_STATUS = "CREDITED"
SHARE_DISTRIBUTION_UNATTRIBUTED_STATUS = "UNATTRIBUTED"
# #1083: the SAME economic distribution arrived from both sources (Flex and
# the public fallback). The row that arrived second moved no cash — its
# `matched_transaction_id` names the row that did. A reader summing what a
# book was actually paid must sum CREDITED only, never SUPERSEDED too.
SHARE_DISTRIBUTION_SUPERSEDED_STATUS = "SUPERSEDED"

# ShareDistributionModel.source (#1083): which path produced the row. FLEX is
# the source of truth whenever the query carries Cash Transactions; PUBLIC is
# the per-share dividend-history fallback, used only when Flex affirmatively
# has no Cash Transactions section configured (never on a Flex outage, which
# would race the two sources against each other).
SHARE_DISTRIBUTION_SOURCE_FLEX = "flex"
SHARE_DISTRIBUTION_SOURCE_PUBLIC = "public"

# ---------------------------------------------------------------------------
# PositionModel.status: OPEN -> (CLOSED | EXPIRED)
# ---------------------------------------------------------------------------

POSITION_OPEN_STATUS = "OPEN"
POSITION_CLOSED_STATUSES: frozenset[str] = frozenset({"CLOSED", "EXPIRED"})

# ---------------------------------------------------------------------------
# BookModel.status: ACTIVE -> RETIRED (one-way, #1088) | LEGACY | OPS (#1093)
# ---------------------------------------------------------------------------

BOOK_ACTIVE_STATUS = "ACTIVE"
# Exactly RETIRED: the control-plane retirement ADR-0015 §2 names as the
# only way a backtest RETIRE verdict acts on a book. The stage-1 entry bar's
# "not retired" row (#1059, backend/stage1.py) means precisely this one state.
BOOK_RETIRED_STATUS = "RETIRED"
# #1093: an operations book (R01, the share-path paper rehearsal), not a lab
# arm. It can hold real broker shares, so reconciliation counts its
# designated holdings and a flatten sells them, but it is never evidence.
# Every ACTIVE-only reader (Layer C, the share rebalance and its missed-month
# watch) and every BOOK_MANAGED_STATUSES reader (the marks, the digest's book
# rows, fleet NAV) skips it by status: it is deliberately in neither set.
# The readers that take every book exclude it: console.book_summaries and
# distribution attribution (share_distributions._owners) by this constant,
# the empirical null drill by id (its loader must carry no status filter).
BOOK_OPS_STATUS = "OPS"
# B00, the pre-executor manual book. Never traded by the executor.
BOOK_LEGACY_STATUS = "LEGACY"
# Books whose OPEN positions, marks and cash the system still manages (#1088).
# A RETIRED book opens no new risk (Layer C, rolls and rebalances read
# BOOK_ACTIVE_STATUS alone), but the positions it already holds run off
# exactly as before, so every sweep that marks, monitors or settles what a
# book holds reads this set. Opening risk reads ACTIVE; holding it reads this.
BOOK_MANAGED_STATUSES: frozenset[str] = frozenset({BOOK_ACTIVE_STATUS, BOOK_RETIRED_STATUS})
# The audit event init_db writes, once per book, when it syncs a seeds.py
# retirement (#1088). Never BOOK_CONFIG_SYNCED: that event starts a new
# evidence era, and retiring a book must not restart or rewrite its era.
BOOK_RETIRED_EVENT = "BOOK_RETIRED"

# ---------------------------------------------------------------------------
# BookModel.live_authority (#713 reserved, #1059 first writer): None | PAPER |
# LIVE | REVOKED. A different axis from BookModel.status (paper-vs-live
# authority, not lifecycle). None means never granted and reads exactly like
# PAPER. REVOKED is written by ADR-0014's automated demotion (the stage-1
# stake drawdown halt, anomaly.check_stake_drawdown) and stays until an
# operator grants again; nothing automatic ever moves a book out of it.
# ---------------------------------------------------------------------------

LIVE_AUTHORITY_PAPER = "PAPER"
LIVE_AUTHORITY_LIVE = "LIVE"
LIVE_AUTHORITY_REVOKED = "REVOKED"

# LiveGrantModel.kind (#1065): STAGE1 grants live authority at the stage-1
# stake; STEP_UP re-records it at a larger stake (ADR-0006's #1084 amendment).
LIVE_GRANT_STAGE1 = "STAGE1"
LIVE_GRANT_STEP_UP = "STEP_UP"
LIVE_GRANT_KINDS: frozenset[str] = frozenset({LIVE_GRANT_STAGE1, LIVE_GRANT_STEP_UP})

# EntryOutcome.stage vocabulary (#985) — ranked by the entry funnel's actual
# depth for a single candidate's path through _layer_c_entries/_try_place_
# entry, shallowest to deepest, NOT by call-site line order: before #987 H1
# a single "gated" label covered sites from before the quote fetch (playbook
# dedup) through after the final placement attempt (book gates, a control
# halt) — one rank spanning the whole funnel silently dropped whichever
# refusal was actually deeper for a book with two candidates at different
# depths. Splitting it into gated/refused/book_gated/submission_blocked below
# fixes that. scan_blocked (book-wide, before the candidate loop) < ineligible
# (candidate.eligible) < gated (spec unavailable / playbook dedup, before the
# quote fetch) < unpriceable (the quote/pricing checks) < refused (thin-
# credit floor / leg collision, after pricing but before the broker preview)
# < preview_refused (the broker's whatIf preview) < book_gated (duplicate-
# order / evaluate_book_gates, after preview passes) < submission_blocked
# (the final placement attempt itself: a control halt latched between preview
# and submission, or the broker rejecting the actual order) — the deepest a
# candidate can get without being placed.
ENTRY_STAGE_ORDER = (
    "no_candidate",
    "scan_blocked",
    "ineligible",
    "gated",
    "unpriceable",
    "refused",
    "preview_refused",
    "book_gated",
    "submission_blocked",
)

# ---------------------------------------------------------------------------
# PlaybookDefinitionSchema.role: HEDGE | DIRECTIONAL (#967) — a playbook with
# no role key (every playbook seeded before #967) means DIRECTIONAL, never a
# third silent state. HEDGE marks an always-on insurance playbook (e.g. the
# XSP tail-hedge put) whose "wrong-direction" positioning relative to the
# regime is the entire point, not a signal the regime-conflict scan should
# ever flag — a bearish put in a bull market is doing its job, not drifting.
# ---------------------------------------------------------------------------

PLAYBOOK_ROLE_HEDGE = "HEDGE"
PLAYBOOK_ROLE_DIRECTIONAL = "DIRECTIONAL"

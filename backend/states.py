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

# Not a subset of ORDER_PENDING_STATUSES: FILLED is terminal, PARTIAL is
# pending — this predicate (analysis.py's fill-quality report) means "has at
# least some real fill evidence to measure," which both satisfy for
# different reasons.
ORDER_FILLED_OR_PARTIAL_STATUSES: frozenset[str] = frozenset({"FILLED", "PARTIAL"})

# Subset of ORDER_TERMINAL_STATUSES, excluding FILLED: "died without
# executing" (anomaly.py's rejection-streak detector).
ORDER_CANCELLED_OR_REJECTED_STATUSES: frozenset[str] = frozenset({"CANCELLED", "REJECTED"})

# ---------------------------------------------------------------------------
# PositionModel.status: OPEN -> (CLOSED | EXPIRED)
# ---------------------------------------------------------------------------

POSITION_OPEN_STATUS = "OPEN"
POSITION_CLOSED_STATUSES: frozenset[str] = frozenset({"CLOSED", "EXPIRED"})

# ---------------------------------------------------------------------------
# BookModel.status: ACTIVE | RETIRED | LEGACY
# ---------------------------------------------------------------------------

BOOK_ACTIVE_STATUS = "ACTIVE"

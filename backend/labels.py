"""labels.py — plain-English book/instrument labels for operator surfaces (#600).

Refs are `basis:{book_id}:{order_id}:{action}[:tp]` with no built-in mapping
to what the position/order actually IS — an operator reading "B04" or
"basis:B04:o_8bf1070c:open:tp" in a halt/drift message has to go look up the
book definition separately (2026-08-20 evening incident walkthrough, #562
item 1). These helpers build "B04 — SPY 745/742 bull put (Oct 2 '26)" from
whatever position/order data is actually available, degrading gracefully
when there isn't enough to work with — a halt/drift can fire on a flat book
with no open position, or on a ghost order with no DB row at all — so the
raw book_id is always the floor, never omitted outright.

BookModel/BookConfig carry only `underlying` + engine variant, no
strikes/expiration/spread-type (those live on PositionModel.legs /
.expiration_date / .strategy_type) — a label can't be built from the book
row alone, which is why book_label and ref_label are async and DB-joined.
"""

import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.book_gates import resolve_book_config
from backend.models import BookModel, OrderModel, PositionModel

# Short, operator-facing names — not the raw enum. Anything not listed here
# (a new strategy type added later) falls back to a lowercased, space-joined
# version of the enum rather than silently omitting the strategy.
_FRIENDLY_STRATEGY_NAMES = {
    "IRON_CONDOR": "iron condor",
    "BULL_CALL_SPREAD": "bull call",
    "BEAR_PUT_SPREAD": "bear put",
    "BULL_PUT_SPREAD": "bull put",
    "BEAR_CALL_SPREAD": "bear call",
    "BROKEN_WING_BUTTERFLY": "broken wing butterfly",
    "CALENDAR_SPREAD": "calendar",
    "LONG_STRADDLE": "straddle",
    "LONG_STRANGLE": "strangle",
    "LONG_PUT": "long put",
}


def _friendly_strategy_name(strategy_type: str) -> str:
    return _FRIENDLY_STRATEGY_NAMES.get(strategy_type, strategy_type.replace("_", " ").lower())


def _friendly_expiration(expiration_date: str) -> str | None:
    """'2026-10-02' -> "Oct 2 '26". None for anything unparseable — a label
    with no expiration is still useful; a label that crashes is not."""
    try:
        d = datetime.date.fromisoformat(expiration_date)
    except ValueError:
        return None
    return f"{d:%b} {d.day} '{d:%y}"


def format_spread_label(strategy_type: str, legs: list[dict], expiration_date: str | None = None) -> str:
    """ "745/742 bull put (Oct 2 '26)" — no underlying prefix (callers that
    have one, e.g. book_label, prepend it themselves; ref_label callers
    without a resolved underlying still get a useful strikes+strategy
    label). Strikes are unique, sorted highest-first (matches the operator's
    own "745/742" phrasing) — a single-leg strategy (LONG_PUT) shows just
    the one strike."""
    strikes = sorted({leg["strike"] for leg in legs}, reverse=True)
    strikes_str = "/".join(f"{s:g}" for s in strikes)
    label = f"{strikes_str} {_friendly_strategy_name(strategy_type)}"
    exp = _friendly_expiration(expiration_date) if expiration_date else None
    return f"{label} ({exp})" if exp else label


async def book_label(session: AsyncSession, book_id: str, book: BookModel | None = None) -> str:
    """ "B04 — SPY 745/742 bull put (Oct 2 '26)", degrading through "B04 —
    SPY bull put" (position exists but not the caller's business to look up
    further), "B04 — SPY" (book has a resolved underlying but no open
    position — a halt/drift fires on flat books too), down to the bare
    book_id when nothing else is known. Never raises — every caller of this
    is on an alert/audit path, where a labeling bug must not become the
    reason the alert itself is lost.

    *book* lets a caller that already has the row (e.g. iterating books)
    skip the extra lookup; otherwise it's fetched here."""
    try:
        if book is None:
            book = await session.get(BookModel, book_id)
        if book is None:
            return book_id
        underlying = resolve_book_config(book.config).underlying
        pos = (
            await session.execute(
                select(PositionModel)
                .filter_by(book_id=book_id, status="OPEN")
                .order_by(PositionModel.entry_date.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if pos is not None:
            spread = format_spread_label(pos.strategy_type, pos.legs, pos.expiration_date)
            return f"{book_id} — {pos.underlying} {spread}"
        if underlying:
            return f"{book_id} — {underlying}"
        return book_id
    except Exception:
        return book_id


def _parse_basis_ref(ref: str) -> tuple[str, str] | None:
    """(book_id, order_id) from 'basis:{book_id}:{order_id}:{action}[:tp]',
    or None for anything that doesn't parse — a human's own manual IBKR
    order, or malformed input."""
    parts = ref.split(":")
    if len(parts) < 4 or parts[0] != "basis":
        return None
    return parts[1], parts[2]


async def order_label(session: AsyncSession, book_id: str, combo_legs: dict) -> str:
    """Label for a live (not-yet-filled) order straight from its own
    combo_legs snapshot (#601) — a STAGED/SUBMITTED entry order has no
    position row yet, so ref_label's DB join would just fall back to
    book_label's "most recent OPEN position" guess, which can name the
    WRONG spread on a book that already has one open. combo_legs carries
    exactly what this specific order actually is."""
    try:
        legs = combo_legs.get("legs")
        strategy_type = combo_legs.get("strategy_type")
        if legs and strategy_type:
            underlying = combo_legs.get("underlying")
            spread = format_spread_label(strategy_type, legs, combo_legs.get("expiration_date"))
            return f"{book_id} — {underlying} {spread}" if underlying else f"{book_id} — {spread}"
        return await book_label(session, book_id)
    except Exception:
        return book_id


async def ref_label(session: AsyncSession, ref: str) -> str:
    """Label for a specific order ref — more precise than book_label when a
    DB row (or a ghost-order's parseable ref) identifies exactly which
    position the ref belongs to, rather than guessing at "the book's most
    recent open position". Falls back to book_label, then to the raw ref
    when nothing parses at all (a non-basis ref, or a book_id lookup that
    also comes up empty)."""
    try:
        parsed = _parse_basis_ref(ref)
        if parsed is None:
            return ref
        book_id, order_id = parsed
        order = (await session.execute(select(OrderModel).filter_by(order_ref=ref))).scalar_one_or_none()
        position_id = order.position_id if order is not None else None
        if position_id is None and ":open" in ref:
            # Ghost TP/entry with no DB row (#559): _order_to_position keys
            # the position an entry fill creates as f"pos_{order_id}" off
            # this exact order_id, so a ghost TP maps straight to it even
            # with no row of its own to join through.
            position_id = f"pos_{order_id}"
        if position_id is not None:
            pos = await session.get(PositionModel, position_id)
            if pos is not None:
                spread = format_spread_label(pos.strategy_type, pos.legs, pos.expiration_date)
                return f"{book_id} — {pos.underlying} {spread}"
        return await book_label(session, book_id)
    except Exception:
        return ref

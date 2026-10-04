"""share_distributions.py — share books are credited the cash distributions
their holdings earn (#1074, operator ruling 2026-10-03: "dividends count").

The TWS API the executor talks to shows no per-symbol dividend history (only
account-level cash), so the source is the Activity Flex statement's Cash
Transactions section: IBKR's own record of each dividend, payment in lieu and
the withholding tax against it, per symbol, with a stable transactionID. The
evening run fetches it, and this module credits each distribution once.

Disciplines:
- Idempotent per distribution: one share_distributions row per transactionID,
  written in the same transaction as the cash it moves. A row with no
  transactionID cannot be deduplicated, so it is surfaced, never credited.
- Attribution fails closed. A distribution is credited only when exactly ONE
  designated book (its config lists the symbol in `share_symbols`) holds the
  symbol or has ever filled an order on it. None, or more than one, is
  UNATTRIBUTED: recorded, named in the digest, and left for a human — the
  cash adjustment is the audited way to credit it by hand.
- Only USD rows at execution-level detail (a SUMMARY row would double the
  DETAIL ones); anything else on a share symbol is surfaced, not credited.
- Never blocks trading. A Flex outage, a missing token or a query without the
  Cash Transactions section is a digest line ("distributions not checked
  tonight"), and the next night catches up as long as the query's period
  still covers the payment.

#1083: the operator cannot add the Cash Transactions section to the Flex
query (the paper account's Client Portal has essentially no settings or
reporting menus to add it from), and confirmed empirically it is absent
today. On a night the Flex query affirmatively returns no Cash Transactions
section (never on an outage — that would race the two sources), this module
falls back to `backend/dividend_history.py`'s public per-share dividend
history instead of only logging "not checked". Flex remains the source of
truth whenever its Cash Transactions section IS present; the reconciliation
check in `_find_cross_source_match` keeps the two from ever both crediting
the same economic distribution. See `run_public_dividend_fallback` and
`credit_public_dividends` below.
"""

import asyncio
import logging
import math
import os
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.book_gates import credit_book_cash, resolve_book_config
from backend.dividend_history import PublicDividend, PublicDividendError, fetch_public_dividends
from backend.flex_audit import FlexError, fetch_flex_statement
from backend.models import AuditEventModel, BookModel, ShareDistributionModel, ShareHoldingModel, ShareOrderModel
from backend.states import (
    BOOK_MANAGED_STATUSES,
    BOOK_OPS_STATUS,
    SHARE_DISTRIBUTION_CREDITED_STATUS,
    SHARE_DISTRIBUTION_SOURCE_FLEX,
    SHARE_DISTRIBUTION_SOURCE_PUBLIC,
    SHARE_DISTRIBUTION_SUPERSEDED_STATUS,
    SHARE_DISTRIBUTION_UNATTRIBUTED_STATUS,
)

logger = logging.getLogger(__name__)

# The Flex Cash Transactions `type` values that are a distribution on a held
# symbol (or the tax withheld from one). Everything else in the section —
# interest, deposits, fees — is not a share book's to claim.
DISTRIBUTION_TYPES: frozenset[str] = frozenset({"Dividends", "Payment In Lieu Of Dividends", "Withholding Tax"})
_QTY_TOLERANCE = 1e-6

SHARE_DISTRIBUTION_CREDITED = "SHARE_DISTRIBUTION_CREDITED"
SHARE_DISTRIBUTION_UNATTRIBUTED = "SHARE_DISTRIBUTION_UNATTRIBUTED"

# #1083: when the SAME economic distribution turns up from both sources (the
# operator later manages to add Cash Transactions, or a one-off manual query
# change), the two sources' dates won't line up exactly — Flex reports the
# PAY date, the fallback credits on the EX-date, and that gap is commonly
# 2-6 weeks. A window wide enough to span that gap but well short of a
# quarterly dividend's ~91-day cadence (so two REAL distinct distributions on
# the same book+symbol are never mistaken for one) catches the cross-source
# duplicate without needing the two sources' dates to agree.
_RECONCILE_WINDOW_DAYS = 45
_ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class CashDistribution:
    transaction_id: str
    symbol: str
    kind: str
    amount: float
    currency: str
    paid_on: str
    level: str


def _iso_date(raw: str) -> str:
    """Flex dates come as YYYYMMDD, YYYYMMDD;HHMMSS or YYYY-MM-DD."""
    head = (raw or "").split(";")[0].split(" ")[0].strip()
    if len(head) == 8 and head.isdigit():
        return f"{head[:4]}-{head[4:6]}-{head[6:]}"
    return head


def parse_cash_distributions(statement: ET.Element) -> list[CashDistribution] | None:
    """The candidate rows of the statement's Cash Transactions section, or
    None when the section is absent (the query was not built with it — a
    configuration gap the caller reports, never read as "no dividends").

    Kept: every distribution-type row, AND every row of any other type that
    names a symbol. The second set is what keeps a spelling this code does
    not know from silently dropping a dividend: credit_distributions surfaces
    an unrecognized type on a share symbol as UNATTRIBUTED (never credits
    it) and ignores it on any other symbol."""
    if statement.find(".//CashTransactions") is None:
        return None
    rows: list[CashDistribution] = []
    for el in statement.iter("CashTransaction"):
        kind = (el.get("type") or "").strip()
        if kind not in DISTRIBUTION_TYPES and not (el.get("symbol") or "").strip():
            continue
        try:
            amount = float(el.get("amount") or "nan")
        except ValueError:
            amount = float("nan")
        rows.append(
            CashDistribution(
                transaction_id=(el.get("transactionID") or "").strip(),
                symbol=(el.get("symbol") or "").strip().upper(),
                kind=kind,
                amount=amount,
                currency=(el.get("currency") or "").strip().upper(),
                paid_on=_iso_date(el.get("dateTime") or el.get("reportDate") or ""),
                level=(el.get("levelOfDetail") or "DETAIL").strip().upper(),
            )
        )
    return rows


def fetch_cash_distributions() -> list[CashDistribution] | None:
    """Fetch and parse the configured Activity Flex statement. Raises
    FlexError when it is not configured or the service refuses; None when
    the query has no Cash Transactions section."""
    token = os.getenv("IBKR_FLEX_TOKEN")
    query_id = os.getenv("IBKR_FLEX_QUERY_ID")
    if not token or not query_id:
        raise FlexError("IBKR_FLEX_TOKEN / IBKR_FLEX_QUERY_ID not set")
    return parse_cash_distributions(fetch_flex_statement(token, query_id))


async def _owners(session: AsyncSession) -> dict[str, list[str]]:
    """Per symbol, every designated book that holds it or has ever filled an
    order on it — the books a distribution on that symbol could belong to.

    An ops book (the share rehearsal's R01, #1093) never owns one. Counting
    its "ever filled" would make every later distribution on a rehearsed
    symbol ambiguous between R01 and B36, so B36's dividends would go
    UNATTRIBUTED for good. The rehearsal holds a share for minutes to days,
    so a distribution it really earned is rare and cents. Such a row then
    has no owner (or wrongly reads as B36's once B36 holds the symbol);
    the rehearsal's default symbols were picked to keep that out of reach
    (share_rehearsal.DEFAULT_SYMBOLS)."""
    designated = {
        book.id: frozenset(resolve_book_config(book.config).share_symbols)
        for book in (await session.execute(select(BookModel).filter(BookModel.status != BOOK_OPS_STATUS)))
        .scalars()
        .all()
    }
    touched: set[tuple[str, str]] = {
        (h.book_id, h.symbol)
        for h in (await session.execute(select(ShareHoldingModel))).scalars().all()
        if abs(h.quantity) > _QTY_TOLERANCE
    }
    touched |= {
        (o.book_id, o.symbol)
        for o in (await session.execute(select(ShareOrderModel).filter(ShareOrderModel.filled_quantity > 0)))
        .scalars()
        .all()
    }
    owners: dict[str, list[str]] = {}
    for book_id, symbol in sorted(touched):
        if symbol in designated.get(book_id, frozenset()):
            owners.setdefault(symbol, []).append(book_id)
    return owners


async def _designated_symbols(session: AsyncSession) -> frozenset[str]:
    """Every symbol any book is designated to hold (`share_symbols`)."""
    return frozenset(
        symbol
        for book in (await session.execute(select(BookModel))).scalars().all()
        for symbol in resolve_book_config(book.config).share_symbols
    )


def _unattributable_reason(row: CashDistribution, owners: list[str]) -> str | None:
    if row.kind not in DISTRIBUTION_TYPES:
        return f"unrecognized cash transaction type {row.kind!r} on a share symbol"
    if not math.isfinite(row.amount):
        return "amount is not a number"
    if row.level != "DETAIL":
        return f"{row.level} row, not execution detail"
    if row.currency != "USD":
        return f"currency {row.currency or 'missing'}, not USD"
    if not row.symbol:
        return "no symbol"
    if not owners:
        return "no designated book holds or has held this symbol"
    if len(owners) > 1:
        return f"{len(owners)} designated books could own it ({', '.join(owners)})"
    return None


_AMOUNT_TOLERANCE_FRACTION = 0.05  # 5%: same book, same holding, same event — any real gap means NOT a match
_AMOUNT_TOLERANCE_FLOOR = 0.02  # for tiny distributions, a fixed cent-level floor instead of a vanishing 5%


def _amounts_match(a: float, b: float) -> bool:
    return abs(a - b) <= max(_AMOUNT_TOLERANCE_FLOOR, _AMOUNT_TOLERANCE_FRACTION * max(abs(a), abs(b)))


async def _unconsumed_credited(
    session: AsyncSession, book_id: str, symbol: str, source: str
) -> list[ShareDistributionModel]:
    """CREDITED rows for *book_id*/*symbol* from *source*, not already matched
    to something from the other source (`matched_transaction_id is None`) —
    the pool a NEW arrival from the OTHER source may match against. Excludes
    Withholding Tax: it has no counterpart in the other source, so it can
    never participate in cross-source matching (on either side)."""
    rows = (
        (
            await session.execute(
                select(ShareDistributionModel).filter_by(
                    book_id=book_id,
                    symbol=symbol,
                    status=SHARE_DISTRIBUTION_CREDITED_STATUS,
                    source=source,
                    matched_transaction_id=None,
                )
            )
        )
        .scalars()
        .all()
    )
    return [r for r in rows if r.kind != "Withholding Tax"]


@dataclass(frozen=True)
class _CrossSourceLookup:
    match: ShareDistributionModel | None  # same window AND same amount: the real duplicate
    mismatch: ShareDistributionModel | None  # same window, amount disagrees: inconsistent, not a free pass


async def _find_cross_source_match(
    session: AsyncSession, book_id: str, symbol: str, new_source: str, new_date: str, amount: float
) -> _CrossSourceLookup:
    """Every not-yet-consumed CREDITED row from the OTHER source dated so the
    real pay date falls between the ex-date and ex-date+45 days (the typical
    ex-to-pay gap) is a candidate for being the SAME economic distribution as
    this new one. `.match` is the nearest candidate whose amount also agrees
    — the real duplicate, safe to supersede. A candidate in the window whose
    amount does NOT agree is `.mismatch`: evidence of something wrong (e.g. a
    distribution Flex reports as more than one row — ordinary income plus a
    capital-gain component — or one source simply being out of date), not
    proof of "no duplicate". Returning None from `.match` in that case must
    NEVER fall through to a normal credit: an unexplained amount gap next to
    a plausible duplicate is exactly the "source data... inconsistent" case
    #1083's discipline surfaces, not guesses past.

    *new_date* is the new row's OWN date: a pay date when *new_source* is
    FLEX, an ex-date when it is PUBLIC (the fallback credits on the ex-date —
    see dividend_history.py). The candidate (the OTHER source) supplies
    whichever of the two *new_date* is not: a Flex candidate's `paid_on` is
    the real pay date; a public candidate's `paid_on` is the ex-date it
    credited on.

    Matching is strictly one-to-one (`matched_transaction_id`): a candidate
    already consumed by an earlier match is never offered again — without
    that, a monthly payer's Flex pay-date can drift into the NEXT month's
    still-unconsumed fallback credit and silently swallow it (confirmed
    empirically: TBIL and UTEN both pay monthly, #1083 step-1 check). A
    45-day window comfortably spans one ex-to-pay gap but also reaches past a
    ~30-day monthly cadence into the next event, so one-to-one consumption
    (not the window alone) is what keeps the match from landing on the wrong
    month."""
    other_source = (
        SHARE_DISTRIBUTION_SOURCE_PUBLIC
        if new_source == SHARE_DISTRIBUTION_SOURCE_FLEX
        else SHARE_DISTRIBUTION_SOURCE_FLEX
    )
    try:
        new = date.fromisoformat(new_date)
    except ValueError:
        return _CrossSourceLookup(None, None)
    candidates = await _unconsumed_credited(session, book_id, symbol, other_source)
    in_window: list[tuple[int, ShareDistributionModel]] = []
    for row in candidates:
        try:
            other = date.fromisoformat(row.paid_on)
        except ValueError:
            continue
        pay, ex = (new, other) if new_source == SHARE_DISTRIBUTION_SOURCE_FLEX else (other, new)
        diff = (pay - ex).days
        if 0 <= diff <= _RECONCILE_WINDOW_DAYS:
            in_window.append((diff, row))
    if not in_window:
        return _CrossSourceLookup(None, None)
    in_window.sort(key=lambda pair: pair[0])
    matching = [row for _diff, row in in_window if _amounts_match(row.amount, amount)]
    if matching:
        return _CrossSourceLookup(matching[0], None)
    return _CrossSourceLookup(None, in_window[0][1])


async def credit_distributions(session: AsyncSession, rows: list[CashDistribution]) -> list[str]:
    """Credit each not-yet-seen distribution to its one owning book, or record
    it UNATTRIBUTED. Returns digest lines for what changed tonight (a row
    already recorded on an earlier night says nothing — it is settled).

    #1083: processed in PAID-ON order, oldest first, regardless of the order
    the Flex statement lists them in. `_find_cross_source_match` always picks
    the NEAREST unconsumed fallback candidate — with two monthly rows and a
    window wider than the monthly gap, processing a later pay date first can
    claim an earlier month's candidate (it's still the nearest one available
    at that moment), leaving the earlier Flex row to match nothing, credit
    fresh, and double-pay that month once the earlier row is processed
    second. Oldest-first exhausts each month's candidate in the same order
    the fallback created them, so this can't happen."""
    notes: list[str] = []
    owners = await _owners(session)
    share_symbols = await _designated_symbols(session)
    now = datetime.now(UTC).isoformat()
    seen: set[str] = set()
    for row in sorted(rows, key=lambda r: r.paid_on):
        if row.kind not in DISTRIBUTION_TYPES and row.symbol not in share_symbols:
            continue  # interest, fees and the like on a symbol no share book holds: not ours
        if not row.transaction_id:
            notes.append(
                f"⚠ distribution NOT credited — Flex row has no transactionID, so it cannot be credited exactly once: "
                f"{row.symbol or '?'} {row.kind} {row.amount:+.2f} {row.currency} paid {row.paid_on}"
            )
            continue
        if row.transaction_id in seen or await session.get(ShareDistributionModel, row.transaction_id) is not None:
            continue
        seen.add(row.transaction_id)
        book_owners = owners.get(row.symbol, [])
        reason = _unattributable_reason(row, book_owners)
        if reason is not None:
            session.add(
                ShareDistributionModel(
                    transaction_id=row.transaction_id,
                    book_id=None,
                    symbol=row.symbol,
                    kind=row.kind,
                    amount=row.amount if math.isfinite(row.amount) else 0.0,
                    paid_on=row.paid_on,
                    status=SHARE_DISTRIBUTION_UNATTRIBUTED_STATUS,
                    recorded_at=now,
                    note=reason,
                    source=SHARE_DISTRIBUTION_SOURCE_FLEX,
                )
            )
            session.add(
                AuditEventModel(
                    run_at=now,
                    book_id=None,
                    event_type=SHARE_DISTRIBUTION_UNATTRIBUTED,
                    actor="executor",
                    payload={
                        "transaction_id": row.transaction_id,
                        "symbol": row.symbol,
                        "kind": row.kind,
                        "amount": row.amount if math.isfinite(row.amount) else None,
                        "paid_on": row.paid_on,
                        "reason": reason,
                    },
                )
            )
            notes.append(
                f"⚠ distribution NOT credited ({reason}): {row.symbol or '?'} {row.kind} {row.amount:+.2f} "
                f"paid {row.paid_on} (txn {row.transaction_id}) — if it belongs to a book, credit it with the "
                "resolution cash adjustment"
            )
            continue
        book_id = book_owners[0]
        # #1083: Withholding Tax has no fallback counterpart (the fallback
        # only ever produces a gross per-share dividend), so it can never
        # cross-match and always credits normally. Dividends / Payment In
        # Lieu may already have been credited by the fallback (under a
        # synthetic id, dated by ex-date) while Cash Transactions was
        # unavailable — match it, mark it consumed, and record the real
        # transactionID for future idempotency without moving cash twice.
        lookup = (
            _CrossSourceLookup(None, None)
            if row.kind == "Withholding Tax"
            else await _find_cross_source_match(
                session, book_id, row.symbol, SHARE_DISTRIBUTION_SOURCE_FLEX, row.paid_on, row.amount
            )
        )
        if lookup.mismatch is not None:
            # A same-window fallback credit exists but its amount disagrees —
            # evidence of something wrong (a split distribution, a stale
            # source), not proof there's no duplicate. Never credit past it.
            conflict = lookup.mismatch
            reason = (
                f"a fallback credit in the same window disagrees on amount ({conflict.amount:+.2f} vs "
                f"{row.amount:+.2f}, txn {conflict.transaction_id})"
            )
            session.add(
                ShareDistributionModel(
                    transaction_id=row.transaction_id,
                    book_id=None,
                    symbol=row.symbol,
                    kind=row.kind,
                    amount=row.amount,
                    paid_on=row.paid_on,
                    status=SHARE_DISTRIBUTION_UNATTRIBUTED_STATUS,
                    recorded_at=now,
                    source=SHARE_DISTRIBUTION_SOURCE_FLEX,
                    note=reason,
                )
            )
            session.add(
                AuditEventModel(
                    run_at=now,
                    book_id=None,
                    event_type=SHARE_DISTRIBUTION_UNATTRIBUTED,
                    actor="executor",
                    payload={
                        "transaction_id": row.transaction_id,
                        "symbol": row.symbol,
                        "kind": row.kind,
                        "amount": row.amount,
                        "paid_on": row.paid_on,
                        "reason": reason,
                    },
                )
            )
            notes.append(
                f"⚠ distribution NOT credited ({reason}): {row.symbol} {row.kind} {row.amount:+.2f} paid "
                f"{row.paid_on} (txn {row.transaction_id}) — if it belongs to a book, credit it with the "
                "resolution cash adjustment"
            )
            continue
        dup = lookup.match
        if dup is not None:
            dup.matched_transaction_id = row.transaction_id
            session.add(
                ShareDistributionModel(
                    transaction_id=row.transaction_id,
                    book_id=book_id,
                    symbol=row.symbol,
                    kind=row.kind,
                    amount=row.amount,
                    paid_on=row.paid_on,
                    status=SHARE_DISTRIBUTION_SUPERSEDED_STATUS,
                    recorded_at=now,
                    source=SHARE_DISTRIBUTION_SOURCE_FLEX,
                    matched_transaction_id=dup.transaction_id,
                    note=f"already credited via the public-dividend fallback (txn {dup.transaction_id}) — not re-credited",
                )
            )
            notes.append(
                f"{book_id} {row.symbol} {row.kind} {row.amount:+.2f} paid {row.paid_on} matches a fallback credit "
                f"already booked (txn {dup.transaction_id}) — not re-credited"
            )
            continue
        balance = await credit_book_cash(session, book_id, row.amount)
        session.add(
            ShareDistributionModel(
                transaction_id=row.transaction_id,
                book_id=book_id,
                symbol=row.symbol,
                kind=row.kind,
                amount=row.amount,
                paid_on=row.paid_on,
                status=SHARE_DISTRIBUTION_CREDITED_STATUS,
                recorded_at=now,
                source=SHARE_DISTRIBUTION_SOURCE_FLEX,
            )
        )
        session.add(
            AuditEventModel(
                run_at=now,
                book_id=book_id,
                event_type=SHARE_DISTRIBUTION_CREDITED,
                actor="executor",
                payload={
                    "transaction_id": row.transaction_id,
                    "symbol": row.symbol,
                    "kind": row.kind,
                    "amount": row.amount,
                    "paid_on": row.paid_on,
                    "cash_after": round(balance, 2) if balance is not None else None,
                },
            )
        )
        notes.append(f"{book_id} {row.symbol} {row.kind} {row.amount:+.2f} paid {row.paid_on} credited to book cash")
    await session.commit()
    return notes


async def share_books_hold_or_held(session: AsyncSession) -> bool:
    """True when any managed share book holds shares or has ever filled a
    share order — the only time the nightly distribution check runs. A
    RETIRED share book (#1088) still counts: shares it holds keep paying."""
    books = (
        (await session.execute(select(BookModel).filter(BookModel.status.in_(BOOK_MANAGED_STATUSES)))).scalars().all()
    )
    share_books = {b.id for b in books if resolve_book_config(b.config).share_symbols}
    if not share_books:
        return False
    owners = await _owners(session)
    return any(book_id in share_books for ids in owners.values() for book_id in ids)


# ---------------------------------------------------------------------------
# #1083: the public-dividend fallback, used only when Flex affirmatively has
# no Cash Transactions section (never on an outage — see run_distribution_credit)
# ---------------------------------------------------------------------------


def _market_date(exec_time: str) -> str:
    """The ET calendar date of a broker execution timestamp (ISO, any tz —
    naive is treated as UTC, matching how the broker adapter writes it)."""
    dt = datetime.fromisoformat(exec_time)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_ET).date().isoformat()


async def _designated_books_by_symbol(session: AsyncSession) -> dict[str, list[str]]:
    """Every symbol's designated book ids (`share_symbols`), independent of
    whether they have ever held or filled it — the fallback determines
    holding itself (`_shares_as_of`), unlike the Flex path's `_owners`."""
    out: dict[str, list[str]] = {}
    for book in (await session.execute(select(BookModel))).scalars().all():
        for symbol in resolve_book_config(book.config).share_symbols:
            out.setdefault(symbol, []).append(book.id)
    return out


async def _fill_timeline_or_none(session: AsyncSession, book_id: str, symbol: str) -> list[tuple[str, float]] | None:
    """Every (market_date, signed_quantity) fill on this book+symbol, oldest
    first, replayed from every share order regardless of status (a partial
    fill on a since-CANCELLED order is real executed quantity) — OR None
    when this holdings timeline cannot be trusted:
    - a resolution-settled execution (`resolution:<ref>`,
      backend/resolution.py settle_share_order): its exec_time is the
      SETTLEMENT time, not the real trade time, so keying a replay on it
      could misplace the fill relative to an ex-date that falls in between;
    - an unparseable or missing exec_time (defensive: a crash here would
      reach the executor's outer crash handler mid-run, the one thing this
      fallback must never do);
    - a hand `RESOLUTION_SHARE_HOLDING_CORRECTED` correction: a correction
      the replay does not and cannot know the quantity timeline around.

    A book with NO orders at all on this symbol returns an empty (not None)
    timeline — a trustworthy "held nothing, ever," not a failure. That
    matters: once a book's FIRST order on a symbol exists, any later
    correction makes the WHOLE timeline untrustworthy (we cannot know where
    in the sequence the correction's effect belongs), which would otherwise
    flag every one of a symbol's years of PRE-HISTORY ex-dates — before the
    book ever traded it — as "unreliable" for no reason. Callers bound that
    by `_earliest_order_date`, checked BEFORE this function runs."""
    orders = (await session.execute(select(ShareOrderModel).filter_by(book_id=book_id, symbol=symbol))).scalars().all()
    timeline: list[tuple[str, float]] = []
    for order in orders:
        sign = 1.0 if order.side == "BUY" else -1.0
        for fill in order.fills or []:
            if str(fill.get("exec_id", "")).startswith("resolution:"):
                return None
            try:
                market_date = _market_date(fill["exec_time"])
                quantity = float(fill["quantity"])
            except (KeyError, ValueError, TypeError):
                return None
            timeline.append((market_date, sign * quantity))
    corrections = (
        (
            await session.execute(
                select(AuditEventModel).filter_by(
                    book_id=book_id,
                    event_type="RESOLUTION_SHARE_HOLDING_CORRECTED",  # resolution.py's own event name, not a status
                )
            )
        )
        .scalars()
        .all()
    )
    if any((event.payload or {}).get("symbol") == symbol for event in corrections):
        return None
    timeline.sort(key=lambda t: t[0])
    return timeline


async def _earliest_order_date(session: AsyncSession, book_id: str, symbol: str) -> str | None:
    """The earliest market-date fill on this book+symbol (an activity
    FLOOR — computed independent of `_fill_timeline_or_none`'s reliability
    check, best-effort over whatever exec_times parse), or None when there is
    NO fill at all. An ex-date before this floor is one no designated book
    could plausibly have been entitled to, regardless of whether the REST of
    the timeline is trustworthy — this is what keeps a symbol's years of
    pre-history ex-dates silent even after a later correction makes the
    timeline itself unreliable (see `_fill_timeline_or_none`).

    A fill that EXISTS but whose exec_time fails to parse must NOT silently
    push the floor past it (that would wrongly treat the book as having
    "no candidate" for an ex-date the bad fill might really have covered,
    hiding the exact problem `_fill_timeline_or_none` exists to surface) —
    the empty string sorts before every real ISO date, so such a book is
    always a candidate and reaches the reliability check instead."""
    orders = (await session.execute(select(ShareOrderModel).filter_by(book_id=book_id, symbol=symbol))).scalars().all()
    any_fill = False
    dates: list[str] = []
    for order in orders:
        for fill in order.fills or []:
            any_fill = True
            try:
                dates.append(_market_date(fill["exec_time"]))
            except (KeyError, ValueError, TypeError):
                continue
    if not any_fill:
        return None
    return min(dates) if dates else ""


def _shares_as_of_timeline(timeline: list[tuple[str, float]], ex_date: str) -> float:
    """The entitled quantity for an ex-dividend event on *ex_date*: every
    signed fill strictly BEFORE it. A buy executed ON the ex-date is not yet
    entitled (excluded); a holding sold ON the ex-date still is (the sell's
    own date is not < ex_date, so it never reduces the sum)."""
    return sum(qty for market_date, qty in timeline if market_date < ex_date)


def _public_distribution_ids(symbol: str, ex_date: str, owner_books: list[str]) -> list[str]:
    """Every transaction_id this ex-date could already be recorded under —
    the ambiguous/unreliable symbol-level key, plus one per candidate book."""
    return [f"pubdiv:{symbol}:{ex_date}"] + [f"pubdiv:{book_id}:{symbol}:{ex_date}" for book_id in owner_books]


async def credit_public_dividends(session: AsyncSession, by_symbol: dict[str, list[PublicDividend]]) -> list[str]:
    """Credit each not-yet-seen public-source ex-dividend event to its one
    designated, holding book, or record it UNATTRIBUTED. Mirrors
    `credit_distributions`'s fail-closed shape: no designated book is not
    ours to report; more than one designated book HOLDING on the ex-date, or
    a book+symbol whose holdings history can't be trusted, is surfaced and
    never guessed; a designated book that simply held nothing on the ex-date
    is not owed anything and is left silent (unlike Flex, no cash arrived to
    account for)."""
    notes: list[str] = []
    now = datetime.now(UTC).isoformat()
    designated = await _designated_books_by_symbol(session)

    for symbol, divs in by_symbol.items():
        owner_books = designated.get(symbol, [])
        if not owner_books:
            continue  # no book is designated for this symbol: not ours
        for div in divs:
            ex_date = div.ex_date
            ids = _public_distribution_ids(symbol, ex_date, owner_books)
            already = False
            for txn_id in ids:
                if await session.get(ShareDistributionModel, txn_id) is not None:
                    already = True
                    break
            if already:
                continue

            # A book with no fill on this symbol on or before ex_date could
            # not plausibly have been entitled — excluded from both the
            # reliability check and the holders count, so a symbol's years
            # of pre-history ex-dates stay silent even once a LATER
            # correction makes the book's post-history timeline unreliable.
            candidate_books = [
                b
                for b in owner_books
                if (floor := await _earliest_order_date(session, b, symbol)) is not None and floor <= ex_date
            ]
            if not candidate_books:
                continue  # nobody could plausibly have held it yet: silent, nothing owed

            timelines = {b: await _fill_timeline_or_none(session, b, symbol) for b in candidate_books}
            unreliable = [b for b in candidate_books if timelines[b] is None]
            if unreliable:
                reason = (
                    f"holdings reconstruction unreliable for {', '.join(sorted(unreliable))} (a resolution-settled "
                    "order or a manual share-holding correction is on record for this symbol)"
                )
                session.add(
                    ShareDistributionModel(
                        transaction_id=f"pubdiv:{symbol}:{ex_date}",
                        book_id=None,
                        symbol=symbol,
                        kind="Dividend (public)",
                        amount=div.amount_per_share,
                        paid_on=ex_date,
                        status=SHARE_DISTRIBUTION_UNATTRIBUTED_STATUS,
                        recorded_at=now,
                        note=reason,
                        source=SHARE_DISTRIBUTION_SOURCE_PUBLIC,
                    )
                )
                session.add(
                    AuditEventModel(
                        run_at=now,
                        book_id=None,
                        event_type=SHARE_DISTRIBUTION_UNATTRIBUTED,
                        actor="executor",
                        payload={
                            "symbol": symbol,
                            "ex_date": ex_date,
                            "amount_per_share": div.amount_per_share,
                            "reason": reason,
                            "source": SHARE_DISTRIBUTION_SOURCE_PUBLIC,
                        },
                    )
                )
                notes.append(
                    f"⚠ distribution NOT credited ({symbol} ex {ex_date}, {div.amount_per_share:.4f}/share): "
                    f"{reason} — credit the book's actual holding x this rate by hand if owed"
                )
                continue

            holders = [
                (book_id, qty)
                for book_id in candidate_books
                for qty in [_shares_as_of_timeline(timelines[book_id], ex_date)]
                if qty > _QTY_TOLERANCE
            ]
            if not holders:
                continue  # nobody held it on the ex-date: nothing owed, nothing to surface
            if len(holders) > 1:
                breakdown = ", ".join(
                    f"{b} {q:g}sh x {div.amount_per_share:.4f} = {q * div.amount_per_share:+.2f}" for b, q in holders
                )
                reason = f"{len(holders)} designated books held shares on the ex-date ({breakdown})"
                session.add(
                    ShareDistributionModel(
                        transaction_id=f"pubdiv:{symbol}:{ex_date}",
                        book_id=None,
                        symbol=symbol,
                        kind="Dividend (public)",
                        amount=div.amount_per_share,
                        paid_on=ex_date,
                        status=SHARE_DISTRIBUTION_UNATTRIBUTED_STATUS,
                        recorded_at=now,
                        note=reason,
                        source=SHARE_DISTRIBUTION_SOURCE_PUBLIC,
                    )
                )
                session.add(
                    AuditEventModel(
                        run_at=now,
                        book_id=None,
                        event_type=SHARE_DISTRIBUTION_UNATTRIBUTED,
                        actor="executor",
                        payload={
                            "symbol": symbol,
                            "ex_date": ex_date,
                            "amount_per_share": div.amount_per_share,
                            "reason": reason,
                            "source": SHARE_DISTRIBUTION_SOURCE_PUBLIC,
                        },
                    )
                )
                notes.append(f"⚠ distribution NOT credited ({symbol} ex {ex_date}): {reason}")
                continue

            book_id, quantity = holders[0]
            amount = round(quantity * div.amount_per_share, 2)
            txn_id = f"pubdiv:{book_id}:{symbol}:{ex_date}"
            lookup = await _find_cross_source_match(
                session, book_id, symbol, SHARE_DISTRIBUTION_SOURCE_PUBLIC, ex_date, amount
            )
            if lookup.mismatch is not None:
                # A same-window Flex credit exists but its amount disagrees
                # with the fallback's own quantity x per-share calculation —
                # inconsistent, never a free pass to credit past it. Keyed
                # separately from txn_id (idempotent across nights on its
                # own) so a later real match under txn_id is never blocked
                # by this row, and this row is never re-notified once seen.
                conflict_id = f"{txn_id}:conflict"
                if await session.get(ShareDistributionModel, conflict_id) is not None:
                    continue
                conflict = lookup.mismatch
                reason = (
                    f"a Flex credit in the same window disagrees on amount ({conflict.amount:+.2f} vs {amount:+.2f}, "
                    f"txn {conflict.transaction_id})"
                )
                session.add(
                    ShareDistributionModel(
                        transaction_id=f"{txn_id}:conflict",
                        book_id=None,
                        symbol=symbol,
                        kind="Dividend (public)",
                        amount=amount,
                        paid_on=ex_date,
                        status=SHARE_DISTRIBUTION_UNATTRIBUTED_STATUS,
                        recorded_at=now,
                        source=SHARE_DISTRIBUTION_SOURCE_PUBLIC,
                        note=reason,
                    )
                )
                session.add(
                    AuditEventModel(
                        run_at=now,
                        book_id=None,
                        event_type=SHARE_DISTRIBUTION_UNATTRIBUTED,
                        actor="executor",
                        payload={
                            "symbol": symbol,
                            "ex_date": ex_date,
                            "amount_per_share": div.amount_per_share,
                            "amount": amount,
                            "reason": reason,
                            "source": SHARE_DISTRIBUTION_SOURCE_PUBLIC,
                        },
                    )
                )
                notes.append(f"⚠ distribution NOT credited ({symbol} ex {ex_date}): {reason}")
                continue
            dup = lookup.match
            if dup is not None:
                dup.matched_transaction_id = txn_id
                session.add(
                    ShareDistributionModel(
                        transaction_id=txn_id,
                        book_id=book_id,
                        symbol=symbol,
                        kind="Dividend (public)",
                        amount=amount,
                        paid_on=ex_date,
                        status=SHARE_DISTRIBUTION_SUPERSEDED_STATUS,
                        recorded_at=now,
                        source=SHARE_DISTRIBUTION_SOURCE_PUBLIC,
                        matched_transaction_id=dup.transaction_id,
                        note=f"already credited via Flex (txn {dup.transaction_id}) — not re-credited",
                    )
                )
                notes.append(
                    f"{book_id} {symbol} public dividend {amount:+.2f} ex {ex_date} matches a Flex credit already "
                    f"booked (txn {dup.transaction_id}) — not re-credited"
                )
                continue

            balance = await credit_book_cash(session, book_id, amount)
            session.add(
                ShareDistributionModel(
                    transaction_id=txn_id,
                    book_id=book_id,
                    symbol=symbol,
                    kind="Dividend (public)",
                    amount=amount,
                    paid_on=ex_date,
                    status=SHARE_DISTRIBUTION_CREDITED_STATUS,
                    recorded_at=now,
                    source=SHARE_DISTRIBUTION_SOURCE_PUBLIC,
                )
            )
            session.add(
                AuditEventModel(
                    run_at=now,
                    book_id=book_id,
                    event_type=SHARE_DISTRIBUTION_CREDITED,
                    actor="executor",
                    payload={
                        "symbol": symbol,
                        "ex_date": ex_date,
                        "quantity": quantity,
                        "amount_per_share": div.amount_per_share,
                        "amount": amount,
                        "cash_after": round(balance, 2) if balance is not None else None,
                        "source": SHARE_DISTRIBUTION_SOURCE_PUBLIC,
                    },
                )
            )
            notes.append(
                f"{book_id} {symbol} dividend {quantity:g}sh x {div.amount_per_share:.4f} = {amount:+.2f} "
                f"ex {ex_date} credited to book cash (public source)"
            )
    await session.commit()
    return notes


async def run_public_dividend_fallback(
    session: AsyncSession, fetch: Callable[[str], list[PublicDividend] | None] | None = None
) -> list[str]:
    """Fetch every designated share symbol's public dividend history and
    credit what's new. One symbol's fetch failure is a digest line for that
    symbol only — it never blocks the others or the run.

    *fetch* defaults to None, resolved to this module's OWN
    `fetch_public_dividends` name at call time rather than as a bound default
    argument — the same reason conftest.py's `_isolated_database` comment
    gives for the db_backup.DATABASE_URL bug: a default bound at def time is
    frozen to that object forever, so a test fixture that patches the name
    afterward (`_no_real_public_dividends`) would silently miss it."""
    fetch = fetch or fetch_public_dividends
    designated = await _designated_books_by_symbol(session)
    if not designated:
        return []
    notes: list[str] = []
    by_symbol: dict[str, list[PublicDividend]] = {}
    for symbol in sorted(designated):
        try:
            divs = await asyncio.to_thread(fetch, symbol)
        except PublicDividendError as exc:
            logger.warning("Public dividend history skipped for %s: %s", symbol, exc)
            notes.append(f"⚠ {symbol} public dividend history NOT checked tonight ({exc})")
            continue
        except Exception as exc:  # httpx and friends: this step never fails the run
            logger.warning("Public dividend history failed for %s: %s", symbol, exc)
            notes.append(f"⚠ {symbol} public dividend history NOT checked tonight ({type(exc).__name__}: {exc})")
            continue
        if divs is None:
            notes.append(f"⚠ {symbol} public dividend history NOT checked — the source could not resolve the symbol")
            continue
        # A declared-but-not-yet-happened ex-date (unverified whether the
        # source ever returns one) must not be credited against TODAY's
        # holdings — entitlement isn't fixed until the ex-date itself
        # arrives, and a sell between now and then would make today's
        # holding count wrong. Simply wait for a later night, after the
        # ex-date has passed, same as every other credited ex-date.
        today = _market_date(datetime.now(UTC).isoformat())
        by_symbol[symbol] = [d for d in divs if d.ex_date <= today]
    notes.extend(await credit_public_dividends(session, by_symbol))
    return notes


async def run_distribution_credit(
    session: AsyncSession,
    fetch: Callable[[], list[CashDistribution] | None] = fetch_cash_distributions,
    fetch_public: Callable[[str], list[PublicDividend] | None] | None = None,
) -> list[str]:
    """The evening run's distribution step. Fail-soft for trading, fail-loud
    for the books: every reason the check could not run is a digest line.

    #1083: on a night Flex affirmatively has no Cash Transactions section,
    this falls back to the public dividend-history source instead of only
    logging "not checked" — the operator cannot add that section, so the
    fallback is the long-term path, not a stopgap. It does NOT run on a Flex
    OUTAGE (a missing token, a service error, unparseable XML): that would
    let the two sources race each other for the same night's distributions,
    exactly the double-credit risk `_find_cross_source_match` exists to
    prevent for the case where they genuinely overlap across nights."""
    if not await share_books_hold_or_held(session):
        return []
    try:
        rows = await asyncio.to_thread(fetch)
    except (FlexError, OSError, ET.ParseError) as exc:
        logger.warning("Distribution check skipped: %s", exc)
        return [f"⚠ share-book distributions NOT checked tonight ({exc}) — dividends paid since are not yet credited"]
    except Exception as exc:  # httpx and friends: this step never fails the run
        logger.warning("Distribution check failed: %s", exc)
        return [f"⚠ share-book distributions NOT checked tonight ({type(exc).__name__}: {exc})"]
    if rows is None:
        return await run_public_dividend_fallback(session, fetch=fetch_public)
    return await credit_distributions(session, rows)

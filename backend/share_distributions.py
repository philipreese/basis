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
"""

import asyncio
import logging
import math
import os
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.book_gates import credit_book_cash, resolve_for_book
from backend.flex_audit import FlexError, fetch_flex_statement
from backend.models import AuditEventModel, BookModel, ShareDistributionModel, ShareHoldingModel, ShareOrderModel
from backend.states import (
    BOOK_MANAGED_STATUSES,
    SHARE_DISTRIBUTION_CREDITED_STATUS,
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
    order on it — the books a distribution on that symbol could belong to."""
    designated = {
        book.id: frozenset(resolve_for_book(book).share_symbols)
        for book in (await session.execute(select(BookModel))).scalars().all()
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
        for symbol in resolve_for_book(book).share_symbols
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


async def credit_distributions(session: AsyncSession, rows: list[CashDistribution]) -> list[str]:
    """Credit each not-yet-seen distribution to its one owning book, or record
    it UNATTRIBUTED. Returns digest lines for what changed tonight (a row
    already recorded on an earlier night says nothing — it is settled)."""
    notes: list[str] = []
    owners = await _owners(session)
    share_symbols = await _designated_symbols(session)
    now = datetime.now(UTC).isoformat()
    seen: set[str] = set()
    for row in rows:
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
    share_books = {b.id for b in books if resolve_for_book(b).share_symbols}
    if not share_books:
        return False
    owners = await _owners(session)
    return any(book_id in share_books for ids in owners.values() for book_id in ids)


async def run_distribution_credit(
    session: AsyncSession, fetch: Callable[[], list[CashDistribution] | None] = fetch_cash_distributions
) -> list[str]:
    """The evening run's distribution step. Fail-soft for trading, fail-loud
    for the books: every reason the check could not run is a digest line."""
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
        return [
            (
                "⚠ share-book distributions NOT checked: the Flex query has no Cash Transactions section — "
                "add it (Dividends, Payment In Lieu, Withholding Tax) so dividends can be credited"
            )
        ]
    return await credit_distributions(session, rows)

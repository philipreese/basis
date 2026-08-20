"""flex_audit.py — weekly Activity Flex Query audit of the fills ledger (#74).

Design §4.5: `reqExecutions` is current-day-only, so the nightly ledger is
captured incrementally; this job pulls a broker-side Activity Flex statement
(which includes orderRef and commissions over arbitrary date ranges) and
audits the incremental ledger end-to-end. It also answers the standing
empirical question: do the paper account's Flex exports carry orderRef?

Read-only against the broker and the ledger — discrepancies are reported
(audit event + ntfy push), never auto-corrected, same as reconciliation.

Requires in .env (both from Client Portal → Performance & Reports → Flex
Queries; the token is a secret):
  IBKR_FLEX_TOKEN     Flex Web Service token
  IBKR_FLEX_QUERY_ID  Activity Flex Query id (Trades section, orderRef on)
"""

import asyncio
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import AuditEventModel, FillModel, OrderModel

logger = logging.getLogger(__name__)

FLEX_BASE = os.getenv("IBKR_FLEX_BASE", "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService")
_SEND = "/SendRequest"
_GET = "/GetStatement"
_VERSION = 3
_POLL_ATTEMPTS = 10
_POLL_DELAY_S = 5.0
# Flex error 1019: statement generation in progress — the one retryable code
_IN_PROGRESS_CODE = "1019"

ORDER_REF_PREFIX = "basis:"


class FlexError(RuntimeError):
    """Flex Web Service refused or failed — the audit cannot run."""


@dataclass(frozen=True)
class FlexTrade:
    exec_id: str
    order_ref: str
    quantity: float
    price: float
    commission: float


@dataclass
class FlexAuditResult:
    trades_total: int = 0
    trades_ours: int = 0  # orderRef starts with "basis:"
    missing_order_ref: int = 0  # trades with NO orderRef at all — the §4.5 question
    discrepancies: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.discrepancies


def _flex_get(client: httpx.Client, url: str, params: dict) -> ET.Element:
    resp = client.get(url, params=params, timeout=60.0)
    resp.raise_for_status()
    try:
        return ET.fromstring(resp.text)
    except ET.ParseError as exc:
        raise FlexError(f"Flex service returned unparseable XML: {resp.text[:200]!r}") from exc


def fetch_flex_statement(token: str, query_id: str) -> ET.Element:
    """Two-step Flex Web Service protocol: request a reference code, then poll."""
    with httpx.Client() as client:
        root = _flex_get(client, FLEX_BASE + _SEND, {"t": token, "q": query_id, "v": _VERSION})
        status = root.findtext("Status")
        if status != "Success":
            raise FlexError(f"Flex SendRequest failed: {root.findtext('ErrorCode')} {root.findtext('ErrorMessage')}")
        ref_code = root.findtext("ReferenceCode")

        for _attempt in range(_POLL_ATTEMPTS):
            stmt = _flex_get(client, FLEX_BASE + _GET, {"t": token, "q": ref_code, "v": _VERSION})
            if stmt.tag == "FlexQueryResponse":
                return stmt
            if stmt.findtext("ErrorCode") == _IN_PROGRESS_CODE:
                time.sleep(_POLL_DELAY_S)
                continue
            raise FlexError(f"Flex GetStatement failed: {stmt.findtext('ErrorCode')} {stmt.findtext('ErrorMessage')}")
    raise FlexError(f"Flex statement not ready after {_POLL_ATTEMPTS} polls")


def parse_trades(statement: ET.Element) -> list[FlexTrade]:
    """Extract execution-level rows from the statement's Trades section."""
    trades: list[FlexTrade] = []
    for el in statement.iter("Trade"):
        exec_id = el.get("ibExecID") or el.get("execId") or ""
        if not exec_id:
            continue  # order-level summary rows have no execution id
        # IBKR reports combo fills as per-leg executions PLUS a BAG row at
        # the net price, carrying the same orderRef and a real execId. The
        # nightly capture filters these at the broker seam (#331); if the
        # Activity Flex includes them too, every combo fill would raise one
        # false MISSING_FROM_LEDGER per weekly audit (#352). Only skip what
        # is positively a BAG — an absent attribute keeps the row.
        category = (el.get("assetCategory") or el.get("secType") or "").strip().upper()
        if category == "BAG":
            continue
        trades.append(
            FlexTrade(
                exec_id=exec_id,
                order_ref=(el.get("orderReference") or "").strip(),
                quantity=abs(float(el.get("quantity") or 0.0)),
                price=float(el.get("tradePrice") or 0.0),
                commission=abs(float(el.get("ibCommission") or 0.0)),
            )
        )
    return trades


async def audit_fills(session: AsyncSession, trades: list[FlexTrade]) -> FlexAuditResult:
    """Compare broker-side Flex trades against the local fills ledger.

    Report-only: MISSING_FROM_LEDGER means the nightly incremental capture
    dropped an execution (the failure §4.5 exists to catch); UNKNOWN_ORDER_REF
    means the broker echoes a basis ref this database has never staged.
    """
    result = FlexAuditResult(trades_total=len(trades))
    fills = {f.exec_id: f for f in (await session.execute(select(FillModel))).scalars().all()}
    known_refs = {o.order_ref for o in (await session.execute(select(OrderModel))).scalars().all()}

    for trade in trades:
        if not trade.order_ref:
            result.missing_order_ref += 1
        if not trade.order_ref.startswith(ORDER_REF_PREFIX):
            continue  # manual/other activity in the account is not ours to audit
        result.trades_ours += 1

        if trade.order_ref not in known_refs:
            result.discrepancies.append(f"UNKNOWN_ORDER_REF {trade.order_ref} (exec {trade.exec_id})")
        fill = fills.get(trade.exec_id)
        if fill is None:
            result.discrepancies.append(f"MISSING_FROM_LEDGER exec {trade.exec_id} ref {trade.order_ref}")
            continue
        if abs(abs(fill.quantity) - trade.quantity) > 1e-9 or abs(fill.price - trade.price) > 1e-6:
            result.discrepancies.append(
                f"FILL_MISMATCH exec {trade.exec_id}: ledger {fill.quantity}@{fill.price}"
                f" vs flex {trade.quantity}@{trade.price}"
            )

    # The empirical §4.5 answer: paper exports SHOULD carry orderRef; a
    # statement where every one of our sessions' trades lacks it means the
    # audit chain is blind and the assumption in the design doc is wrong.
    if result.trades_total and result.missing_order_ref == result.trades_total:
        result.discrepancies.append(f"NO_ORDER_REFS_IN_EXPORT: all {result.trades_total} trades lack orderRef")

    session.add(
        AuditEventModel(
            run_at=datetime.now(UTC).isoformat(),
            book_id=None,
            event_type="FLEX_AUDIT",
            actor="flex_audit",
            payload={
                "trades_total": result.trades_total,
                "trades_ours": result.trades_ours,
                "missing_order_ref": result.missing_order_ref,
                "discrepancies": result.discrepancies,
            },
        )
    )
    await session.commit()
    return result


async def run_flex_audit() -> FlexAuditResult:
    token = os.getenv("IBKR_FLEX_TOKEN")
    query_id = os.getenv("IBKR_FLEX_QUERY_ID")
    if not token or not query_id:
        raise FlexError("IBKR_FLEX_TOKEN / IBKR_FLEX_QUERY_ID not set — configure a Flex query first (.env)")

    statement = await asyncio.to_thread(fetch_flex_statement, token, query_id)
    trades = parse_trades(statement)

    from backend.database import async_session_maker

    async with async_session_maker() as session:
        return await audit_fills(session, trades)


async def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    from backend.run_logging import setup_run_logging

    setup_run_logging("flex_audit")
    from backend.database import init_db
    from backend.operator import send_ntfy

    await init_db()
    try:
        result = await run_flex_audit()
    except FlexError as exc:
        logger.error("Flex audit could not run: %s", exc)
        send_ntfy("basis flex audit: FAILED", str(exc), priority="high")
        raise SystemExit(1) from exc
    except Exception as exc:
        # Beyond the known FlexError modes: never exit silently (#271).
        logger.exception("Flex audit crashed")
        send_ntfy("basis flex audit CRASHED", f"{type(exc).__name__}: {exc}", priority="high")
        raise SystemExit(1) from exc

    if result.clean:
        summary = (
            f"{result.trades_ours}/{result.trades_total} trades ours, ledger consistent, "
            f"{result.missing_order_ref} without orderRef"
        )
        logger.info("Flex audit clean: %s", summary)
        send_ntfy("basis flex audit: clean", summary)
    else:
        # Detection only (#410): nothing backfills these — say so, or the
        # operator files the alert away assuming the system self-heals.
        body = "\n".join(result.discrepancies) + (
            "\n— NOT auto-corrected: fix the books via the console resolution panel (external close / cash adjust)"
        )
        logger.error("Flex audit found %d discrepancies:\n%s", len(result.discrepancies), body)
        send_ntfy(f"basis flex audit: {len(result.discrepancies)} discrepancies", body, priority="urgent")


if __name__ == "__main__":
    asyncio.run(main())

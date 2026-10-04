"""#1074: dividends count — distributions credited to the share book once,
from the Flex Cash Transactions section; unattributable ones surfaced, never
guessed; the 60/40 benchmark's total-return series refreshed wholesale.

No network: the Flex fetch is injected and the Gateway call is faked."""

import asyncio
import math
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import market_data
from backend import operator as operator_mod
from backend.flex_audit import FlexError
from backend.models import (
    AuditEventModel,
    Base,
    BookModel,
    ShareDistributionModel,
    ShareHoldingModel,
    ShareOrderModel,
    TotalReturnHistoryModel,
)
from backend.seeds import LAB_BOOKS
from backend.share_distributions import (
    CashDistribution,
    credit_distributions,
    parse_cash_distributions,
    run_distribution_credit,
)
from backend.states import SHARE_DISTRIBUTION_CREDITED_STATUS, SHARE_DISTRIBUTION_UNATTRIBUTED_STATUS

B36_CONFIG = next(b for b in LAB_BOOKS if b["id"] == "B36")["config"]

STATEMENT = """<FlexQueryResponse><FlexStatements><FlexStatement>
<CashTransactions>
  <CashTransaction type="Dividends" symbol="SGOV" amount="31.42" currency="USD" dateTime="20261105;202000"
    transactionID="t1" levelOfDetail="DETAIL"/>
  <CashTransaction type="Withholding Tax" symbol="VEA" amount="-0.80" currency="USD" dateTime="2026-12-20"
    transactionID="t2" levelOfDetail="DETAIL"/>
  <CashTransaction type="Broker Interest Received" symbol="" amount="4.00" currency="USD" dateTime="20261105"
    transactionID="t3"/>
  <CashTransaction type="Payment In Lieu Of Dividends" symbol="vti" amount="bad" currency="USD"
    dateTime="20261224" transactionID="t4"/>
  <CashTransaction type="Other Fees" symbol="" amount="-10.00" currency="USD" dateTime="20261105"
    transactionID="t5"/>
  <CashTransaction type="Dividend Distribution" symbol="GLD" amount="2.00" currency="USD" dateTime="20261105"
    transactionID="t6"/>
</CashTransactions>
</FlexStatement></FlexStatements></FlexQueryResponse>"""


def _row(txn: str = "t1", symbol: str = "TBIL", amount: float = 31.42, **kw) -> CashDistribution:
    args = {
        "transaction_id": txn,
        "symbol": symbol,
        "kind": "Dividends",
        "amount": amount,
        "currency": "USD",
        "paid_on": "2026-11-05",
        "level": "DETAIL",
    }
    args.update(kw)
    return CashDistribution(**args)


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with m() as session:
        for book_id, config in (("B36", B36_CONFIG), ("B01", {"engine_variant": "V0", "underlying": "XSP"})):
            session.add(
                BookModel(
                    id=book_id,
                    name=book_id,
                    config=config,
                    config_version=1,
                    config_hash=f"hash-{book_id}",
                    starting_capital=10000.0,
                    cash_balance=10000.0,
                    status="ACTIVE",
                    created_at="2026-10-01T00:00:00+00:00",
                )
            )
        session.add(ShareHoldingModel(book_id="B36", symbol="TBIL", quantity=80.0, updated_at="t0"))
        await session.commit()
    yield m
    await engine.dispose()


async def _cash(m, book_id: str = "B36") -> float:
    async with m() as session:
        return (await session.get(BookModel, book_id)).cash_balance


async def _credit(m, rows) -> list[str]:
    async with m() as session:
        return await credit_distributions(session, rows)


async def _rows(m) -> list[ShareDistributionModel]:
    async with m() as session:
        return list((await session.execute(select(ShareDistributionModel))).scalars().all())


class TestParse:
    def test_reads_distribution_rows_and_any_row_naming_a_symbol(self):
        rows = parse_cash_distributions(ET.fromstring(STATEMENT))
        assert [(r.transaction_id, r.symbol, r.kind, r.paid_on) for r in rows] == [
            ("t1", "SGOV", "Dividends", "2026-11-05"),
            ("t2", "VEA", "Withholding Tax", "2026-12-20"),
            ("t4", "VTI", "Payment In Lieu Of Dividends", "2026-12-24"),
            ("t6", "GLD", "Dividend Distribution", "2026-11-05"),
        ]
        assert rows[0].amount == 31.42 and rows[1].amount == -0.80
        assert math.isnan(rows[2].amount)  # unparseable amount: kept, so it is surfaced
        assert rows[2].level == "DETAIL"  # absent levelOfDetail reads as detail

    def test_a_query_without_the_section_is_not_no_dividends(self):
        assert parse_cash_distributions(ET.fromstring("<FlexQueryResponse><Trades/></FlexQueryResponse>")) is None


class TestCredit:
    @pytest.mark.asyncio
    async def test_a_distribution_is_credited_once(self, maker):
        notes = await _credit(maker, [_row()])
        assert await _cash(maker) == pytest.approx(10_031.42)
        assert notes == ["B36 TBIL Dividends +31.42 paid 2026-11-05 credited to book cash"]
        # The same transaction on the next night (and twice in one statement) moves nothing.
        assert await _credit(maker, [_row(), _row()]) == []
        assert await _cash(maker) == pytest.approx(10_031.42)
        (row,) = await _rows(maker)
        assert (row.book_id, row.status) == ("B36", SHARE_DISTRIBUTION_CREDITED_STATUS)
        async with maker() as session:
            events = (
                (await session.execute(select(AuditEventModel).filter_by(event_type="SHARE_DISTRIBUTION_CREDITED")))
                .scalars()
                .all()
            )
        assert len(events) == 1 and events[0].payload["amount"] == 31.42

    @pytest.mark.asyncio
    async def test_withholding_tax_is_debited(self, maker):
        await _credit(maker, [_row("t9", kind="Withholding Tax", amount=-3.0)])
        assert await _cash(maker) == pytest.approx(9_997.0)

    @pytest.mark.asyncio
    async def test_a_book_that_sold_out_still_earns_a_dividend_paid_later(self, maker):
        # Ex-date while held, paid after the sale: the filled order is the evidence.
        async with maker() as session:
            session.add(
                ShareOrderModel(
                    id="o1",
                    book_id="B36",
                    order_ref="basis:B36:o1:share",
                    symbol="SCHB",
                    side="SELL",
                    quantity=5,
                    limit_price=294.0,
                    decision_close=300.0,
                    signal_date="2026-12-31",
                    status="FILLED",
                    config_hash="hash-B36",
                    created_at="t0",
                    fills=[],
                    filled_quantity=5.0,
                )
            )
            await session.commit()
        await _credit(maker, [_row("t5", symbol="SCHB", amount=4.1)])
        assert await _cash(maker) == pytest.approx(10_004.1)


class TestUnattributableFailsClosed:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("row", "reason"),
        [
            (_row("u1", symbol="SPY", amount=120.0), "no designated book holds or has held this symbol"),
            # SCHF is designated (B36's menu) but neither held nor ever filled
            # in this fixture — distinct from u1 (SPY), never designated at all.
            (_row("u2", symbol="SCHF"), "no designated book holds or has held this symbol"),
            (_row("u3", currency="CAD"), "currency CAD, not USD"),
            (_row("u4", level="SUMMARY"), "SUMMARY row, not execution detail"),
            (_row("u5", amount=math.nan), "amount is not a number"),
            (_row("u6", symbol=""), "no symbol"),
        ],
    )
    async def test_is_recorded_surfaced_and_never_credited(self, maker, row, reason):
        notes = await _credit(maker, [row])
        assert await _cash(maker) == 10_000.0
        assert len(notes) == 1 and notes[0].startswith(f"⚠ distribution NOT credited ({reason})")
        (stored,) = await _rows(maker)
        assert stored.status == SHARE_DISTRIBUTION_UNATTRIBUTED_STATUS and stored.book_id is None
        # Settled once: it is not re-announced on the next night.
        assert await _credit(maker, [row]) == []

    @pytest.mark.asyncio
    async def test_an_unrecognized_type_on_a_share_symbol_is_surfaced_never_dropped(self, maker):
        # A dividend spelled in a way this code does not know must not vanish.
        row = _row("u7", kind="Dividend Distribution")
        notes = await _credit(maker, [row])
        assert notes[0].startswith(
            "⚠ distribution NOT credited (unrecognized cash transaction type 'Dividend Distribution' on a share symbol)"
        )
        assert await _cash(maker) == 10_000.0
        (stored,) = await _rows(maker)
        assert stored.status == SHARE_DISTRIBUTION_UNATTRIBUTED_STATUS
        # Surfaced once, so it rides the urgent push, not only the digest tail.
        from backend.digest import urgent_event_lines

        async with maker() as session:
            urgent = await urgent_event_lines(session, since="")
        assert [line.text for line in urgent] == [
            "SHARE_DISTRIBUTION_UNATTRIBUTED: unrecognized cash transaction type 'Dividend Distribution' on a share symbol"
        ]

    @pytest.mark.asyncio
    async def test_an_unrecognized_type_on_another_symbol_is_not_ours(self, maker):
        assert await _credit(maker, [_row("u8", symbol="SPY", kind="Other Fees", amount=-1.0)]) == []
        assert await _rows(maker) == []

    @pytest.mark.asyncio
    async def test_two_designated_owners_is_ambiguous(self, maker):
        async with maker() as session:
            session.add(
                BookModel(
                    id="B37",
                    name="B37",
                    config=B36_CONFIG,
                    config_version=1,
                    config_hash="hash-B37",
                    starting_capital=10000.0,
                    cash_balance=10000.0,
                    status="ACTIVE",
                    created_at="2026-10-01T00:00:00+00:00",
                )
            )
            session.add(ShareHoldingModel(book_id="B37", symbol="TBIL", quantity=10.0, updated_at="t0"))
            await session.commit()
        notes = await _credit(maker, [_row()])
        assert "2 designated books could own it (B36, B37)" in notes[0]
        assert await _cash(maker) == 10_000.0 and await _cash(maker, "B37") == 10_000.0

    @pytest.mark.asyncio
    async def test_a_row_without_a_transaction_id_cannot_be_credited_exactly_once(self, maker):
        notes = await _credit(maker, [_row("")])
        assert "no transactionID" in notes[0]
        assert await _cash(maker) == 10_000.0 and await _rows(maker) == []


class TestNightlyStep:
    @pytest.mark.asyncio
    async def test_skips_without_a_share_book_holding(self, maker):
        async with maker() as session:
            (await session.get(ShareHoldingModel, ("B36", "TBIL"))).quantity = 0.0
            await session.commit()

        def _boom():
            raise AssertionError("must not fetch")

        async with maker() as session:
            assert await run_distribution_credit(session, fetch=_boom) == []

    @pytest.mark.asyncio
    async def test_a_flex_failure_is_a_digest_line_not_a_crash(self, maker):
        def _down():
            raise FlexError("IBKR_FLEX_TOKEN / IBKR_FLEX_QUERY_ID not set")

        async with maker() as session:
            notes = await run_distribution_credit(session, fetch=_down)
        assert notes == [
            (
                "⚠ share-book distributions NOT checked tonight (IBKR_FLEX_TOKEN / IBKR_FLEX_QUERY_ID not set) — "
                "dividends paid since are not yet credited"
            )
        ]

    @pytest.mark.asyncio
    async def test_an_unexpected_error_is_also_contained(self, maker):
        def _odd():
            raise RuntimeError("socket closed")

        async with maker() as session:
            notes = await run_distribution_credit(session, fetch=_odd)
        assert "NOT checked tonight (RuntimeError: socket closed)" in notes[0]

    @pytest.mark.asyncio
    async def test_a_query_without_cash_transactions_says_so(self, maker):
        async with maker() as session:
            notes = await run_distribution_credit(session, fetch=lambda: None)
        assert "no Cash Transactions section" in notes[0]

    @pytest.mark.asyncio
    async def test_credits_from_the_fetched_statement(self, maker):
        async with maker() as session:
            notes = await run_distribution_credit(session, fetch=lambda: [_row()])
        assert notes == ["B36 TBIL Dividends +31.42 paid 2026-11-05 credited to book cash"]
        assert await _cash(maker) == pytest.approx(10_031.42)

    @pytest.mark.asyncio
    async def test_a_retired_share_book_still_gets_its_dividends(self, maker):
        # #1088: retirement stops new risk, not what the book already holds.
        from backend.states import BOOK_RETIRED_STATUS

        async with maker() as session:
            (await session.get(BookModel, "B36")).status = BOOK_RETIRED_STATUS
            await session.commit()
        async with maker() as session:
            notes = await run_distribution_credit(session, fetch=lambda: [_row()])
        assert notes == ["B36 SGOV Dividends +31.42 paid 2026-11-05 credited to book cash"]

    def test_default_fetch_refuses_without_configuration(self):
        from backend.share_distributions import fetch_cash_distributions

        with pytest.raises(FlexError, match="not set"):
            fetch_cash_distributions()

    def test_default_fetch_parses_the_configured_statement(self, monkeypatch):
        from backend import share_distributions

        monkeypatch.setenv("IBKR_FLEX_TOKEN", "tok")
        monkeypatch.setenv("IBKR_FLEX_QUERY_ID", "q1")
        monkeypatch.setattr(share_distributions, "fetch_flex_statement", lambda t, q: ET.fromstring(STATEMENT))
        assert [r.transaction_id for r in share_distributions.fetch_cash_distributions()] == ["t1", "t2", "t4", "t6"]


# ---------------------------------------------------------------------------
# The benchmark's total-return series
# ---------------------------------------------------------------------------


class TestTotalReturnSeries:
    @pytest.mark.asyncio
    async def test_each_fetch_replaces_the_series_and_a_miss_keeps_it(self, maker, monkeypatch):
        fetched = {"VTI": [("2026-11-02", 300.0), ("2026-11-03", 301.0)], "IEF": [("2026-11-02", 95.0)]}
        monkeypatch.setattr(operator_mod, "fetch_adjusted_daily_closes", lambda s, years: fetched.get(s))
        async with maker() as session:
            assert await operator_mod.persist_benchmark_total_return(session) == 3
        # A distribution rescales history: the next fetch rewrites VTI whole;
        # IEF's fetch misses and its stored series is left exactly as it was.
        fetched = {"VTI": [("2026-11-02", 299.1), ("2026-11-03", 300.1), ("2026-11-04", 302.0)]}
        async with maker() as session:
            assert await operator_mod.persist_benchmark_total_return(session) == 3
            rows = (await session.execute(select(TotalReturnHistoryModel))).scalars().all()
        series = {(r.symbol, r.date): r.close for r in rows}
        assert series == {
            ("VTI", "2026-11-02"): 299.1,
            ("VTI", "2026-11-03"): 300.1,
            ("VTI", "2026-11-04"): 302.0,
            ("IEF", "2026-11-02"): 95.0,
        }

    def test_the_gateway_fetch_asks_for_adjusted_closes(self, monkeypatch):
        calls: list[dict] = []

        class _IB:
            async def reqHistoricalDataAsync(self, contract, **kwargs):
                calls.append({"symbol": contract.symbol, **kwargs})
                return [SimpleNamespace(date="2026-11-02", close=300.5)]

        monkeypatch.setattr(market_data, "_run_ib", lambda op, retry=False: asyncio.run(op(_IB())))
        assert market_data.fetch_adjusted_daily_closes("VTI", 3) == [("2026-11-02", 300.5)]
        assert calls[0]["whatToShow"] == "ADJUSTED_LAST"
        assert calls[0]["endDateTime"] == "" and calls[0]["durationStr"] == "3 Y"

    @pytest.mark.parametrize("outcome", ["empty", "error"])
    def test_the_gateway_fetch_fails_soft(self, monkeypatch, outcome):
        def _run(op, retry=False):
            if outcome == "error":
                raise ConnectionRefusedError("no gateway")
            return []

        monkeypatch.setattr(market_data, "_run_ib", _run)
        assert market_data.fetch_adjusted_daily_closes("VTI", 3) is None

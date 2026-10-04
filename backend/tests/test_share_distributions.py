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
from backend.dividend_history import PublicDividend
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
    credit_public_dividends,
    parse_cash_distributions,
    run_distribution_credit,
    run_public_dividend_fallback,
)
from backend.states import (
    SHARE_DISTRIBUTION_CREDITED_STATUS,
    SHARE_DISTRIBUTION_SUPERSEDED_STATUS,
    SHARE_DISTRIBUTION_UNATTRIBUTED_STATUS,
)

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
    async def test_a_query_without_cash_transactions_falls_back_to_public_history(self, maker):
        # #1083: no Cash Transactions section no longer just logs "not
        # checked" — it hands off to the public dividend-history fallback.
        # The autouse _no_real_public_dividends fixture stubs every symbol's
        # fetch to "unresolved" (None), which is itself a digest line.
        async with maker() as session:
            notes = await run_distribution_credit(session, fetch=lambda: None)
        assert any("public dividend history NOT checked" in n for n in notes)
        assert all("Cash Transactions" not in n for n in notes)

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
        assert notes == ["B36 TBIL Dividends +31.42 paid 2026-11-05 credited to book cash"]

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


# ---------------------------------------------------------------------------
# #1083: the public dividend-history fallback
# ---------------------------------------------------------------------------


def _fill(exec_id: str, quantity: float, exec_time: str, price: float = 10.0) -> dict:
    return {"exec_id": exec_id, "quantity": quantity, "price": price, "commission": 0.0, "exec_time": exec_time}


async def _add_order(
    session: AsyncSession,
    *,
    order_id: str,
    book_id: str,
    symbol: str,
    side: str,
    quantity: float,
    fills: list[dict],
    status: str = "FILLED",
) -> None:
    session.add(
        ShareOrderModel(
            id=order_id,
            book_id=book_id,
            order_ref=f"basis:{book_id}:{order_id}:share",
            symbol=symbol,
            side=side,
            quantity=quantity,
            limit_price=10.0,
            decision_close=10.0,
            signal_date="2026-09-30",
            status=status,
            config_hash=f"hash-{book_id}",
            created_at="2026-09-30T00:00:00+00:00",
            fills=fills,
            filled_quantity=sum(f["quantity"] for f in fills),
        )
    )


class TestPublicDividendFallback:
    @pytest.mark.asyncio
    async def test_credits_the_sole_holder_based_on_reconstructed_holdings(self, maker):
        async with maker() as session:
            await _add_order(
                session,
                order_id="o1",
                book_id="B36",
                symbol="SCHH",
                side="BUY",
                quantity=10,
                fills=[_fill("e1", 10, "2026-10-01T15:00:00+00:00")],
            )
            await session.commit()
        async with maker() as session:
            notes = await credit_public_dividends(session, {"SCHH": [PublicDividend("SCHH", "2026-11-05", 0.50)]})
        assert await _cash(maker) == pytest.approx(10_005.0)
        assert notes == ["B36 SCHH dividend 10sh x 0.5000 = +5.00 ex 2026-11-05 credited to book cash (public source)"]
        (row,) = await _rows(maker)
        assert (row.book_id, row.status, row.source) == ("B36", "CREDITED", "public")
        # Idempotent: a second pass over the same ex-date credits nothing more.
        async with maker() as session:
            assert await credit_public_dividends(session, {"SCHH": [PublicDividend("SCHH", "2026-11-05", 0.50)]}) == []
        assert await _cash(maker) == pytest.approx(10_005.0)

    @pytest.mark.asyncio
    async def test_a_buy_on_the_ex_date_is_not_yet_entitled(self, maker):
        async with maker() as session:
            await _add_order(
                session,
                order_id="o1",
                book_id="B36",
                symbol="SCHH",
                side="BUY",
                quantity=10,
                fills=[_fill("e1", 10, "2026-11-05T15:00:00+00:00")],  # same day as the ex-date
            )
            await session.commit()
        async with maker() as session:
            notes = await credit_public_dividends(session, {"SCHH": [PublicDividend("SCHH", "2026-11-05", 0.50)]})
        assert notes == []  # nobody held it as of the ex-date: silent, nothing owed
        assert await _cash(maker) == 10_000.0
        assert await _rows(maker) == []

    @pytest.mark.asyncio
    async def test_a_sell_on_the_ex_date_is_still_entitled(self, maker):
        async with maker() as session:
            await _add_order(
                session,
                order_id="o1",
                book_id="B36",
                symbol="SCHH",
                side="BUY",
                quantity=10,
                fills=[_fill("e1", 10, "2026-10-01T15:00:00+00:00")],
            )
            await _add_order(
                session,
                order_id="o2",
                book_id="B36",
                symbol="SCHH",
                side="SELL",
                quantity=10,
                fills=[_fill("e2", 10, "2026-11-05T15:00:00+00:00")],  # sold ON the ex-date
            )
            await session.commit()
        async with maker() as session:
            notes = await credit_public_dividends(session, {"SCHH": [PublicDividend("SCHH", "2026-11-05", 0.50)]})
        assert notes == ["B36 SCHH dividend 10sh x 0.5000 = +5.00 ex 2026-11-05 credited to book cash (public source)"]

    @pytest.mark.asyncio
    async def test_nobody_designated_is_not_ours(self, maker):
        async with maker() as session:
            notes = await credit_public_dividends(session, {"SPY": [PublicDividend("SPY", "2026-11-05", 1.0)]})
        assert notes == []
        assert await _rows(maker) == []

    @pytest.mark.asyncio
    async def test_no_holder_on_the_ex_date_is_silent(self, maker):
        async with maker() as session:
            notes = await credit_public_dividends(session, {"SCHH": [PublicDividend("SCHH", "2026-11-05", 0.50)]})
        assert notes == []
        assert await _rows(maker) == []

    @pytest.mark.asyncio
    async def test_two_designated_books_both_holding_is_ambiguous(self, maker):
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
            await _add_order(
                session,
                order_id="o1",
                book_id="B36",
                symbol="SCHH",
                side="BUY",
                quantity=10,
                fills=[_fill("e1", 10, "2026-10-01T15:00:00+00:00")],
            )
            await _add_order(
                session,
                order_id="o2",
                book_id="B37",
                symbol="SCHH",
                side="BUY",
                quantity=4,
                fills=[_fill("e2", 4, "2026-10-01T15:00:00+00:00")],
            )
            await session.commit()
        async with maker() as session:
            notes = await credit_public_dividends(session, {"SCHH": [PublicDividend("SCHH", "2026-11-05", 0.50)]})
        assert len(notes) == 1 and "2 designated books held shares" in notes[0]
        assert await _cash(maker) == 10_000.0 and await _cash(maker, "B37") == 10_000.0
        (row,) = await _rows(maker)
        assert row.status == SHARE_DISTRIBUTION_UNATTRIBUTED_STATUS and row.book_id is None

    @pytest.mark.asyncio
    async def test_a_resolution_settled_fill_makes_the_reconstruction_unreliable(self, maker):
        async with maker() as session:
            await _add_order(
                session,
                order_id="o1",
                book_id="B36",
                symbol="SCHH",
                side="BUY",
                quantity=10,
                fills=[_fill("resolution:basis:B36:o1:share", 10, "2026-10-01T15:00:00+00:00")],
            )
            await session.commit()
        async with maker() as session:
            notes = await credit_public_dividends(session, {"SCHH": [PublicDividend("SCHH", "2026-11-05", 0.50)]})
        assert len(notes) == 1 and "reconstruction unreliable" in notes[0]
        assert await _cash(maker) == 10_000.0
        (row,) = await _rows(maker)
        assert row.status == SHARE_DISTRIBUTION_UNATTRIBUTED_STATUS and row.source == "public"

    @pytest.mark.asyncio
    async def test_a_manual_holding_correction_makes_the_reconstruction_unreliable(self, maker):
        async with maker() as session:
            await _add_order(
                session,
                order_id="o1",
                book_id="B36",
                symbol="SCHH",
                side="BUY",
                quantity=10,
                fills=[_fill("e1", 10, "2026-10-01T15:00:00+00:00")],
            )
            session.add(
                AuditEventModel(
                    run_at="2026-10-15T00:00:00+00:00",
                    book_id="B36",
                    event_type="RESOLUTION_SHARE_HOLDING_CORRECTED",
                    actor="resolution",
                    payload={"symbol": "SCHH", "quantity_before": 10.0, "quantity_after": 9.0},
                )
            )
            await session.commit()
        async with maker() as session:
            notes = await credit_public_dividends(session, {"SCHH": [PublicDividend("SCHH", "2026-11-05", 0.50)]})
        assert len(notes) == 1 and "reconstruction unreliable" in notes[0]
        assert await _cash(maker) == 10_000.0

    @pytest.mark.asyncio
    async def test_fallback_skips_when_flex_already_credited_the_same_distribution(self, maker):
        async with maker() as session:
            await _add_order(
                session,
                order_id="o1",
                book_id="B36",
                symbol="SCHH",
                side="BUY",
                quantity=10,
                fills=[_fill("e1", 10, "2026-10-01T15:00:00+00:00")],
            )
            await session.commit()
        # Flex, the source of truth, already credited this dividend (its own
        # transactionID, dated by PAY date rather than the fallback's ex-date).
        await _credit(maker, [_row("flex-t1", symbol="SCHH", amount=5.0, paid_on="2026-11-20")])
        assert await _cash(maker) == pytest.approx(10_005.0)
        async with maker() as session:
            notes = await credit_public_dividends(session, {"SCHH": [PublicDividend("SCHH", "2026-11-05", 0.50)]})
        assert len(notes) == 1 and "matches a Flex credit already booked" in notes[0]
        assert await _cash(maker) == pytest.approx(10_005.0)  # not credited twice
        rows = await _rows(maker)
        assert len(rows) == 2  # the Flex row, plus a reconciliation marker row (no second cash move)

    @pytest.mark.asyncio
    async def test_flex_skips_when_the_fallback_already_credited_the_same_distribution(self, maker):
        async with maker() as session:
            await _add_order(
                session,
                order_id="o1",
                book_id="B36",
                symbol="SCHH",
                side="BUY",
                quantity=10,
                fills=[_fill("e1", 10, "2026-10-01T15:00:00+00:00")],
            )
            await session.commit()
        # The fallback credited it first (Cash Transactions was unavailable).
        async with maker() as session:
            await credit_public_dividends(session, {"SCHH": [PublicDividend("SCHH", "2026-11-05", 0.50)]})
        assert await _cash(maker) == pytest.approx(10_005.0)
        # The operator later manages to add Cash Transactions, and Flex now
        # reports the same economic distribution under its own transactionID.
        notes = await _credit(maker, [_row("flex-t2", symbol="SCHH", amount=5.0, paid_on="2026-11-20")])
        assert len(notes) == 1 and "matches a fallback credit already booked" in notes[0]
        assert await _cash(maker) == pytest.approx(10_005.0)  # not credited twice
        rows = await _rows(maker)
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_the_nightly_step_runs_the_fallback_only_when_flex_has_no_section(self, maker):
        async with maker() as session:
            await _add_order(
                session,
                order_id="o1",
                book_id="B36",
                symbol="SCHH",
                side="BUY",
                quantity=10,
                fills=[_fill("e1", 10, "2026-08-01T15:00:00+00:00")],
            )
            await session.commit()

        def _fetch_public(symbol):
            if symbol == "SCHH":
                return [PublicDividend("SCHH", "2026-09-05", 0.50)]  # safely in the past
            return []

        async with maker() as session:
            notes = await run_distribution_credit(session, fetch=lambda: None, fetch_public=_fetch_public)
        assert any("credited to book cash (public source)" in n for n in notes)
        assert await _cash(maker) == pytest.approx(10_005.0)

    @pytest.mark.asyncio
    async def test_the_nightly_step_never_runs_the_fallback_on_a_flex_outage(self, maker):
        async with maker() as session:
            await _add_order(
                session,
                order_id="o1",
                book_id="B36",
                symbol="SCHH",
                side="BUY",
                quantity=10,
                fills=[_fill("e1", 10, "2026-10-01T15:00:00+00:00")],
            )
            await session.commit()

        def _boom_public(symbol):
            raise AssertionError("the fallback must not run on a Flex outage")

        def _down():
            raise FlexError("down")

        async with maker() as session:
            notes = await run_distribution_credit(session, fetch=_down, fetch_public=_boom_public)
        assert "NOT checked tonight" in notes[0]
        assert await _cash(maker) == 10_000.0

    @pytest.mark.asyncio
    async def test_run_public_dividend_fallback_contains_one_symbols_fetch_failure(self, maker):
        async with maker() as session:
            await _add_order(
                session,
                order_id="o1",
                book_id="B36",
                symbol="SCHH",
                side="BUY",
                quantity=10,
                fills=[_fill("e1", 10, "2026-10-01T15:00:00+00:00")],
            )
            await session.commit()

        def _fetch(symbol):
            if symbol == "SCHH":
                raise RuntimeError("socket closed")
            return []

        async with maker() as session:
            notes = await run_public_dividend_fallback(session, fetch=_fetch)
        assert any("SCHH public dividend history NOT checked tonight" in n for n in notes)

    @pytest.mark.asyncio
    async def test_a_malformed_exec_time_is_unreliable_not_a_crash(self, maker):
        async with maker() as session:
            await _add_order(
                session,
                order_id="o1",
                book_id="B36",
                symbol="SCHH",
                side="BUY",
                quantity=10,
                fills=[{"exec_id": "e1", "quantity": 10, "price": 10.0, "commission": 0.0, "exec_time": "not-a-date"}],
            )
            await session.commit()
        async with maker() as session:
            notes = await credit_public_dividends(session, {"SCHH": [PublicDividend("SCHH", "2026-11-05", 0.50)]})
        assert len(notes) == 1 and "reconstruction unreliable" in notes[0]
        assert await _cash(maker) == 10_000.0

    @pytest.mark.asyncio
    async def test_pre_history_ex_dates_stay_silent_even_after_a_later_correction(self, maker):
        # A correction on the books (from a trade that started AFTER this
        # ex-date) must not flag every one of the symbol's years of
        # pre-history dividends as "unreliable" — only ex-dates on or after
        # the book's first-ever fill on the symbol are even candidates.
        async with maker() as session:
            await _add_order(
                session,
                order_id="o1",
                book_id="B36",
                symbol="SCHH",
                side="BUY",
                quantity=10,
                fills=[_fill("e1", 10, "2026-10-01T15:00:00+00:00")],
            )
            session.add(
                AuditEventModel(
                    run_at="2026-10-15T00:00:00+00:00",
                    book_id="B36",
                    event_type="RESOLUTION_SHARE_HOLDING_CORRECTED",
                    actor="resolution",
                    payload={"symbol": "SCHH", "quantity_before": 10.0, "quantity_after": 9.0},
                )
            )
            await session.commit()
        async with maker() as session:
            notes = await credit_public_dividends(
                session,
                {
                    "SCHH": [
                        PublicDividend("SCHH", "2021-12-08", 0.168),  # years before the book's first fill
                        PublicDividend("SCHH", "2026-11-05", 0.50),  # after the first fill: genuinely unreliable
                    ]
                },
            )
        assert len(notes) == 1 and "2026-11-05" in notes[0] and "reconstruction unreliable" in notes[0]
        assert await _cash(maker) == 10_000.0

    @pytest.mark.asyncio
    async def test_withholding_tax_never_cross_matches_and_credits_of_zero_are_clean(self, maker):
        # Withholding Tax has no fallback counterpart, and this account/IRA
        # setup expects none in practice — a zero-amount row must still
        # record and move exactly nothing, never crash the match lookup.
        notes = await _credit(maker, [_row("w1", kind="Withholding Tax", amount=0.0)])
        assert await _cash(maker) == 10_000.0
        assert notes == ["B36 TBIL Withholding Tax +0.00 paid 2026-11-05 credited to book cash"]
        (row,) = await _rows(maker)
        assert row.status == SHARE_DISTRIBUTION_CREDITED_STATUS and row.matched_transaction_id is None

    @pytest.mark.asyncio
    async def test_monthly_cadence_does_not_cross_match_the_wrong_month(self, maker):
        # #1083: TBIL and UTEN both pay monthly (confirmed empirically in the
        # step-1 check) — a window wide enough to span one ex-to-pay gap also
        # overlaps two consecutive monthly events. One-to-one consumption
        # must keep a later Flex pay-date from swallowing the WRONG month's
        # already-credited fallback distribution.
        async with maker() as session:
            await _add_order(
                session,
                order_id="o1",
                book_id="B36",
                symbol="SCHH",
                side="BUY",
                quantity=10,
                fills=[_fill("e1", 10, "2026-10-01T15:00:00+00:00")],
            )
            await session.commit()
        async with maker() as session:
            await credit_public_dividends(
                session,
                {
                    "SCHH": [
                        PublicDividend("SCHH", "2026-11-03", 0.50),
                        PublicDividend("SCHH", "2026-12-03", 0.50),
                    ]
                },
            )
        assert await _cash(maker) == pytest.approx(10_010.0)  # two months, 10sh x 0.50 each
        # Flex now reports BOTH pay dates. Processed in order, the November
        # pay date must match November's fallback credit, not get "claimed"
        # by whichever candidate happens to be nearest at the time — and
        # December's Flex row must still match December, not be silently
        # swallowed because November's candidate looked close enough.
        notes = await _credit(
            maker,
            [
                _row("flex-nov", symbol="SCHH", amount=5.0, paid_on="2026-11-07"),
                _row("flex-dec", symbol="SCHH", amount=5.0, paid_on="2026-12-07"),
            ],
        )
        assert len(notes) == 2
        assert all("matches a fallback credit already booked" in n for n in notes)
        assert await _cash(maker) == pytest.approx(10_010.0)  # still just the two fallback credits — no month lost
        rows = {r.transaction_id: r for r in await _rows(maker)}
        nov_fallback = rows["pubdiv:B36:SCHH:2026-11-03"]
        dec_fallback = rows["pubdiv:B36:SCHH:2026-12-03"]
        assert rows["flex-nov"].matched_transaction_id == nov_fallback.transaction_id
        assert rows["flex-dec"].matched_transaction_id == dec_fallback.transaction_id
        assert nov_fallback.matched_transaction_id == "flex-nov"
        assert dec_fallback.matched_transaction_id == "flex-dec"

    @pytest.mark.parametrize("order", [["nov", "dec"], ["dec", "nov"]], ids=["nov-then-dec", "dec-then-nov"])
    @pytest.mark.asyncio
    async def test_a_lost_month_is_never_swallowed_by_one_to_one_consumption(self, maker, order):
        # The fallback only ever managed to credit ONE month (Cash
        # Transactions wasn't available yet for the second). Once it
        # arrives, Flex reports BOTH months' pay dates. credit_distributions
        # processes them oldest-pay-date-first regardless of the statement's
        # own row order — without that, a later pay date (processed first)
        # could claim November's fallback credit as "nearest available",
        # leaving the earlier Flex row (processed second) to match nothing,
        # credit fresh, and double-pay that month once a LATER fallback run
        # for a later ex-date finds no unconsumed candidate left to match.
        async with maker() as session:
            await _add_order(
                session,
                order_id="o1",
                book_id="B36",
                symbol="SCHH",
                side="BUY",
                quantity=10,
                fills=[_fill("e1", 10, "2026-10-01T15:00:00+00:00")],
            )
            await session.commit()
        async with maker() as session:
            await credit_public_dividends(session, {"SCHH": [PublicDividend("SCHH", "2026-11-03", 0.50)]})
        assert await _cash(maker) == pytest.approx(10_005.0)  # only November was ever fallback-credited

        rows_by_key = {
            "nov": _row("flex-nov", symbol="SCHH", amount=5.0, paid_on="2026-11-07"),
            "dec": _row("flex-dec", symbol="SCHH", amount=5.0, paid_on="2026-12-07"),
        }
        notes = await _credit(maker, [rows_by_key[k] for k in order])
        assert await _cash(maker) == pytest.approx(10_010.0)
        rows = {r.transaction_id: r for r in await _rows(maker)}
        nov_fallback = rows["pubdiv:B36:SCHH:2026-11-03"]
        assert rows["flex-nov"].status == SHARE_DISTRIBUTION_SUPERSEDED_STATUS
        assert rows["flex-nov"].matched_transaction_id == nov_fallback.transaction_id
        assert rows["flex-dec"].status == SHARE_DISTRIBUTION_CREDITED_STATUS
        assert any("matches a fallback credit already booked" in n for n in notes)
        assert any("credited to book cash" in n for n in notes)

        # A later fallback re-check for BOTH ex-dates must not double-credit
        # either: November is already recorded (silent), and December
        # correctly recognizes flex-dec's already-booked credit as the same
        # event (superseded, no second cash move) — confirming the
        # oldest-pay-date-first fix left no unconsumed Flex row able to be
        # claimed a second time by a later-arriving fallback computation.
        async with maker() as session:
            more_notes = await credit_public_dividends(
                session,
                {"SCHH": [PublicDividend("SCHH", "2026-11-03", 0.50), PublicDividend("SCHH", "2026-12-03", 0.50)]},
            )
        assert len(more_notes) == 1 and "matches a Flex credit already booked" in more_notes[0]
        assert await _cash(maker) == pytest.approx(10_010.0)

    @pytest.mark.asyncio
    async def test_flex_amount_mismatch_against_an_in_window_fallback_credit_is_surfaced(self, maker):
        async with maker() as session:
            await _add_order(
                session,
                order_id="o1",
                book_id="B36",
                symbol="SCHH",
                side="BUY",
                quantity=10,
                fills=[_fill("e1", 10, "2026-10-01T15:00:00+00:00")],
            )
            await session.commit()
        async with maker() as session:
            await credit_public_dividends(session, {"SCHH": [PublicDividend("SCHH", "2026-11-03", 0.50)]})
        assert await _cash(maker) == pytest.approx(10_005.0)
        # Flex reports a DIFFERENT amount in the same window — e.g. the real
        # distribution had a capital-gain component the public source
        # doesn't carry. This must be surfaced, never credited on top.
        notes = await _credit(maker, [_row("flex-nov", symbol="SCHH", amount=9.0, paid_on="2026-11-07")])
        assert len(notes) == 1 and "disagrees on amount" in notes[0]
        assert await _cash(maker) == pytest.approx(10_005.0)  # not credited at all — surfaced instead
        rows = {r.transaction_id: r for r in await _rows(maker)}
        assert rows["flex-nov"].status == SHARE_DISTRIBUTION_UNATTRIBUTED_STATUS
        # Settled once: it does not repeat on a later night.
        assert await _credit(maker, [_row("flex-nov", symbol="SCHH", amount=9.0, paid_on="2026-11-07")]) == []

    @pytest.mark.asyncio
    async def test_fallback_amount_mismatch_against_an_in_window_flex_credit_is_surfaced(self, maker):
        async with maker() as session:
            await _add_order(
                session,
                order_id="o1",
                book_id="B36",
                symbol="SCHH",
                side="BUY",
                quantity=10,
                fills=[_fill("e1", 10, "2026-10-01T15:00:00+00:00")],
            )
            await session.commit()
        await _credit(maker, [_row("flex-nov", symbol="SCHH", amount=9.0, paid_on="2026-11-07")])
        assert await _cash(maker) == pytest.approx(10_009.0)
        # The fallback's own quantity x per-share figure disagrees with the
        # amount Flex already credited in the same window — surfaced, not
        # credited on top.
        async with maker() as session:
            notes = await credit_public_dividends(session, {"SCHH": [PublicDividend("SCHH", "2026-11-03", 0.50)]})
        assert len(notes) == 1 and "disagrees on amount" in notes[0]
        assert await _cash(maker) == pytest.approx(10_009.0)  # not credited at all
        # Settled once: a second pass over the same ex-date is silent.
        async with maker() as session:
            assert await credit_public_dividends(session, {"SCHH": [PublicDividend("SCHH", "2026-11-03", 0.50)]}) == []

    @pytest.mark.asyncio
    async def test_a_declared_future_ex_date_is_not_credited_against_todays_holdings(self, maker):
        # Unverified whether the public source ever returns a declared,
        # not-yet-happened ex-date — guarded anyway: entitlement isn't fixed
        # until the ex-date arrives, and crediting it against TODAY's
        # holdings would be wrong if a sell happens before then.
        async with maker() as session:
            await _add_order(
                session,
                order_id="o1",
                book_id="B36",
                symbol="SCHH",
                side="BUY",
                quantity=10,
                fills=[_fill("e1", 10, "2026-08-01T15:00:00+00:00")],
            )
            await session.commit()

        def _fetch_public(symbol):
            if symbol == "SCHH":
                return [PublicDividend("SCHH", "2099-01-01", 0.50)]
            return []

        async with maker() as session:
            notes = await run_public_dividend_fallback(session, fetch=_fetch_public)
        assert notes == []
        assert await _cash(maker) == 10_000.0
        assert await _rows(maker) == []

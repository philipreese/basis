"""Tests for the anomaly auto-halt rules (backend/anomaly.py, #71).

One test per rule trigger from spec/supervision.md §6.2–6.3, plus the
escalation-only guarantee (FLATTEN_REQUESTED is never downgraded) and the
audit trail every firing must leave.
"""

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.anomaly import (
    ENVELOPE_BREACH_POSTHOC,
    PNL_SHOCK,
    REPEATED_REJECTION,
    book_mtm,
    check_duplicate_order,
    entry_signature,
    run_post_session_anomalies,
)
from backend.models import (
    AuditEventModel,
    Base,
    BookModel,
    OrderModel,
    PositionModel,
    TradingControlModel,
)

TODAY = "2026-08-18"


def _book(book_id: str = "B01", cash: float = 10000.0) -> BookModel:
    return BookModel(
        id=book_id,
        name=book_id,
        config={"engine_variant": "V0", "underlying": "XSP", "envelope": {}},
        config_version=1,
        config_hash="h",
        starting_capital=10000.0,
        cash_balance=cash,
        status="ACTIVE",
        created_at="t0",
    )


def _position(pos_id: str, book_id: str = "B01", max_loss: float = 2.0, current: float = 1.0) -> PositionModel:
    return PositionModel(
        id=pos_id,
        underlying="XSP",
        strategy_type="BULL_PUT_SPREAD",
        execution_mode="PAPER",
        legs=[
            {
                "option_type": "PUT",
                "direction": "SHORT",
                "strike": 610.0,
                "expiration": "2026-12-18",
                "delta": -0.3,
                "theta": 0.02,
                "vega": 0.1,
                "gamma": 0.01,
            }
        ],
        entry_date="2026-08-10",
        expiration_date="2026-12-18",
        entry_premium=1.0,
        premium_direction="CREDIT",
        current_value_per_share=current,
        contracts=1,
        max_profit=1.0,
        max_loss=max_loss,
        notes="",
        rolls=0,
        status="OPEN",
        journal={},
        book_id=book_id,
    )


def _rejection(day: str) -> AuditEventModel:
    return AuditEventModel(
        run_at=f"{day}T22:00:00+00:00", book_id="B01", event_type="ORDER_REJECTED", actor="executor", payload={}
    )


@pytest_asyncio.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        session.add(_book("B01"))
        session.add(TradingControlModel(scope="GLOBAL", state="ACTIVE", reason="", actor="t", changed_at="t0"))
        session.add(TradingControlModel(scope="B01", state="ACTIVE", reason="", actor="t", changed_at="t0"))
        await session.commit()
    yield maker
    await engine.dispose()


async def _sweep(maker):
    async with maker() as session:
        return await run_post_session_anomalies(session, TODAY)


async def _state(maker, scope: str) -> str:
    async with maker() as session:
        return (await session.get(TradingControlModel, scope)).state


class TestRepeatedRejection:
    @pytest.mark.asyncio
    async def test_two_rejections_tonight_halt_globally(self, session_maker):
        async with session_maker() as session:
            session.add_all([_rejection(TODAY), _rejection(TODAY)])
            await session.commit()
        findings = await _sweep(session_maker)
        assert [f.rule for f in findings] == [REPEATED_REJECTION]
        assert await _state(session_maker, "GLOBAL") == "HALT_ENTRIES"

    @pytest.mark.asyncio
    async def test_one_rejection_tonight_is_tolerated(self, session_maker):
        async with session_maker() as session:
            session.add(_rejection(TODAY))
            await session.commit()
        findings = await _sweep(session_maker)
        assert findings == []
        assert await _state(session_maker, "GLOBAL") == "ACTIVE"

    @pytest.mark.asyncio
    async def test_three_across_trailing_sessions_halt(self, session_maker):
        async with session_maker() as session:
            session.add_all([_rejection("2026-08-15"), _rejection("2026-08-17"), _rejection(TODAY)])
            await session.commit()
        findings = await _sweep(session_maker)
        assert [f.rule for f in findings] == [REPEATED_REJECTION]


class TestPnlShock:
    @pytest.mark.asyncio
    async def test_first_run_sets_baseline_without_halting(self, session_maker):
        findings = await _sweep(session_maker)
        assert findings == []
        async with session_maker() as session:
            book = await session.get(BookModel, "B01")
        assert book.last_mtm == 10000.0
        assert book.last_mtm_at is not None

    @pytest.mark.asyncio
    async def test_every_mark_lands_in_the_equity_curve(self, session_maker):
        # last_mtm alone is overwritten nightly — the curve must persist
        # (#239), and a same-day rerun overwrites its row, not duplicates.
        from sqlalchemy import select

        from backend.models import BookMtmHistoryModel

        await _sweep(session_maker)
        await _sweep(session_maker)  # same-day rerun
        async with session_maker() as session:
            rows = (await session.execute(select(BookMtmHistoryModel).filter_by(book_id="B01"))).scalars().all()
        assert len(rows) == 1
        assert rows[0].mtm == 10000.0
        assert rows[0].date == rows[0].date[:10]  # bare ISO date, not a timestamp

    @pytest.mark.asyncio
    async def test_shock_move_halts_the_book(self, session_maker):
        await _sweep(session_maker)  # baseline 10000
        async with session_maker() as session:
            # Credit position whose buy-back cost exploded: equity drops $2,000
            session.add(_position("p1", current=20.0))
            await session.commit()
        findings = await _sweep(session_maker)
        assert [f.rule for f in findings] == [PNL_SHOCK]
        assert findings[0].scope == "B01"
        assert await _state(session_maker, "B01") == "HALT_ENTRIES"
        assert await _state(session_maker, "GLOBAL") == "ACTIVE"  # scoped, not global

    @pytest.mark.asyncio
    async def test_normal_drift_updates_baseline_quietly(self, session_maker):
        await _sweep(session_maker)
        async with session_maker() as session:
            session.add(_position("p1", current=2.0))  # -$200 move: inside 15% of $10K
            await session.commit()
        findings = await _sweep(session_maker)
        assert findings == []
        async with session_maker() as session:
            book = await session.get(BookModel, "B01")
        assert book.last_mtm == 9800.0

    def test_book_mtm_signs(self):
        book = _book(cash=10120.0)
        credit_pos = _position("p1", current=0.6)  # buy-back liability $60
        assert book_mtm(book, [credit_pos]) == 10060.0
        debit_pos = _position("p2", current=3.0)
        debit_pos.premium_direction = "DEBIT"
        assert book_mtm(book, [debit_pos]) == 10420.0


class TestEnvelopeBreach:
    @pytest.mark.asyncio
    async def test_too_many_positions(self, session_maker):
        async with session_maker() as session:
            for i in range(9):
                session.add(_position(f"p{i}"))
            await session.commit()
        findings = await _sweep(session_maker)
        assert ENVELOPE_BREACH_POSTHOC in [f.rule for f in findings]
        assert await _state(session_maker, "B01") == "HALT_ENTRIES"

    @pytest.mark.asyncio
    async def test_oversize_position(self, session_maker):
        async with session_maker() as session:
            session.add(_position("p1", max_loss=3.0))  # $300 > $250 cap
            await session.commit()
        findings = await _sweep(session_maker)
        (finding,) = [f for f in findings if f.rule == ENVELOPE_BREACH_POSTHOC]
        assert "p1" in finding.detail

    @pytest.mark.asyncio
    async def test_over_deployed(self, session_maker):
        async with session_maker() as session:
            session.add(_position("p1", max_loss=26.0))  # $2600
            session.add(_position("p2", max_loss=25.0))  # $2500 → $5100 > $5000
            await session.commit()
        findings = await _sweep(session_maker)
        details = " ".join(f.detail for f in findings if f.rule == ENVELOPE_BREACH_POSTHOC)
        assert "deployed" in details

    @pytest.mark.asyncio
    async def test_clean_book_is_quiet(self, session_maker):
        async with session_maker() as session:
            session.add(_position("p1"))
            await session.commit()
        assert await _sweep(session_maker) == []


class TestEscalationOnly:
    @pytest.mark.asyncio
    async def test_flatten_requested_is_never_downgraded(self, session_maker):
        async with session_maker() as session:
            row = await session.get(TradingControlModel, "B01")
            row.state = "FLATTEN_REQUESTED"
            await session.commit()
            for i in range(9):
                session.add(_position(f"p{i}"))
            await session.commit()
        findings = await _sweep(session_maker)
        assert findings  # the breach is still found and audited...
        assert await _state(session_maker, "B01") == "FLATTEN_REQUESTED"  # ...but never downgraded

    @pytest.mark.asyncio
    async def test_every_firing_leaves_an_audit_event(self, session_maker):
        async with session_maker() as session:
            session.add_all([_rejection(TODAY), _rejection(TODAY)])
            await session.commit()
        await _sweep(session_maker)
        async with session_maker() as session:
            events = (
                (await session.execute(select(AuditEventModel).filter_by(event_type=REPEATED_REJECTION)))
                .scalars()
                .all()
            )
        assert len(events) == 1
        assert events[0].actor == "anomaly"


class TestDuplicateOrder:
    @pytest.mark.asyncio
    async def test_same_legs_same_book_same_day_is_duplicate(self, session_maker):
        legs = (("XSP261218P00610000", "SHORT"), ("XSP261218P00605000", "LONG"))
        async with session_maker() as session:
            session.add(
                OrderModel(
                    id="o1",
                    book_id="B01",
                    position_id=None,
                    order_ref="basis:B01:o1:open",
                    ib_order_id=1,
                    ib_perm_id=1,
                    action="OPEN",
                    combo_legs={
                        "legs": [
                            {
                                "occ": "XSP261218P00610000",
                                "direction": "SHORT",
                                "option_type": "PUT",
                                "strike": 610.0,
                                "expiration": "2026-12-18",
                            },
                            {
                                "occ": "XSP261218P00605000",
                                "direction": "LONG",
                                "option_type": "PUT",
                                "strike": 605.0,
                                "expiration": "2026-12-18",
                            },
                        ],
                        "quantity": 1,
                    },
                    order_type="LIMIT",
                    limit_price=-1.0,
                    decision_midpoint=-1.0,
                    status="SUBMITTED",
                    submitted_at=f"{TODAY}T22:00:00+00:00",
                    completed_at=None,
                    encumbered_risk=200.0,
                )
            )
            await session.commit()
            assert await check_duplicate_order(session, "B01", legs, TODAY) is True
            # Different book, different day, different legs → not duplicates
            assert await check_duplicate_order(session, "B02", legs, TODAY) is False
            assert await check_duplicate_order(session, "B01", legs, "2026-08-19") is False
            other = (("XSP261218P00600000", "SHORT"), ("XSP261218P00595000", "LONG"))
            assert await check_duplicate_order(session, "B01", other, TODAY) is False

    def test_signature_is_order_insensitive(self):
        a = (("X1", "SHORT"), ("X2", "LONG"))
        b = (("X2", "LONG"), ("X1", "SHORT"))
        assert entry_signature("B01", a) == entry_signature("B01", b)

"""Tests for per-book gates and capital encumbrance (backend/book_gates.py, #67)."""

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.book_gates import (
    BLOCK,
    PASS,
    CandidateOrder,
    evaluate_book_gates,
    release_order,
    stage_order,
)
from backend.models import Base, BookModel, GateEventModel, OrderModel, PositionModel

SHORT_OCC = "XSP261218P00610000"
LONG_OCC = "XSP261218P00605000"


def _candidate(
    book_id: str = "B01",
    max_loss_per_share: float = 2.0,  # $200 risk at 1 contract — inside the $250 cap
    contracts: int = 1,
    strategy_type: str = "BULL_PUT_SPREAD",
    expiration: str = "2026-12-18",
    legs: tuple = ((SHORT_OCC, "SHORT"), (LONG_OCC, "LONG")),
) -> CandidateOrder:
    return CandidateOrder(
        book_id=book_id,
        strategy_type=strategy_type,
        expiration_date=expiration,
        legs=legs,
        max_loss_per_share=max_loss_per_share,
        contracts=contracts,
    )


def _position(pos_id: str, book_id: str = "B01", max_loss: float = 2.0, strategy: str = "IRON_CONDOR") -> PositionModel:
    return PositionModel(
        id=pos_id,
        underlying="XSP",
        strategy_type=strategy,
        execution_mode="PAPER",
        legs=[
            {
                "option_type": "PUT",
                "direction": "SHORT",
                "strike": 590.0,
                "expiration": "2026-11-20",
                "delta": -0.2,
                "theta": 0.02,
                "vega": 0.1,
                "gamma": 0.01,
            }
        ],
        entry_date="2026-08-10",
        expiration_date="2026-11-20",
        entry_premium=1.0,
        premium_direction="CREDIT",
        current_value_per_share=1.0,
        contracts=1,
        max_profit=1.0,
        max_loss=max_loss,
        notes="",
        rolls=0,
        status="OPEN",
        journal={},
        book_id=book_id,
    )


@pytest_asyncio.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        for book_id in ("B01", "B02"):
            session.add(
                BookModel(
                    id=book_id,
                    name=book_id,
                    config={},
                    config_version=1,
                    config_hash="",
                    starting_capital=10000.0,
                    cash_balance=10000.0,
                    status="ACTIVE",
                    created_at="t0",
                )
            )
        session.add(
            BookModel(
                id="B09",
                name="retired",
                config={},
                config_version=1,
                config_hash="",
                starting_capital=10000.0,
                cash_balance=10000.0,
                status="RETIRED",
                created_at="t0",
            )
        )
        await session.commit()
    yield maker
    await engine.dispose()


async def _decide(maker, candidate: CandidateOrder):
    async with maker() as session:
        return await evaluate_book_gates(session, candidate)


class TestEnvelopeGates:
    @pytest.mark.asyncio
    async def test_clean_candidate_passes_all_gates(self, session_maker):
        decision = await _decide(session_maker, _candidate())
        assert decision.allowed
        assert {o.result for o in decision.outcomes} == {PASS}

    @pytest.mark.asyncio
    async def test_max_loss_per_trade(self, session_maker):
        # 2.5% of $10K = $250; $3.00/share × 100 = $300 risk
        decision = await _decide(session_maker, _candidate(max_loss_per_share=3.0))
        assert not decision.allowed
        assert "MAX_LOSS_PER_TRADE" in decision.blocked_by()

    @pytest.mark.asyncio
    async def test_max_deployed(self, session_maker):
        async with session_maker() as session:
            # $2,400 × 2 already deployed; cap is $5,000; candidate $250 tips it
            session.add(_position("p1", max_loss=24.0))
            session.add(_position("p2", max_loss=24.0))
            await session.commit()
        decision = await _decide(session_maker, _candidate(max_loss_per_share=2.5))
        assert "MAX_DEPLOYED" in decision.blocked_by()

    @pytest.mark.asyncio
    async def test_max_positions(self, session_maker):
        async with session_maker() as session:
            for i in range(4):
                session.add(_position(f"p{i}"))
            await session.commit()
        decision = await _decide(session_maker, _candidate())
        assert "MAX_POSITIONS" in decision.blocked_by()

    @pytest.mark.asyncio
    async def test_strategy_expiry_concentration(self, session_maker):
        async with session_maker() as session:
            for i in range(2):
                pos = _position(f"p{i}", strategy="BULL_PUT_SPREAD")
                pos.expiration_date = "2026-12-18"
                session.add(pos)
            await session.commit()
        decision = await _decide(session_maker, _candidate())
        assert "STRATEGY_EXPIRY_CONCENTRATION" in decision.blocked_by()

    @pytest.mark.asyncio
    async def test_books_are_isolated(self, session_maker):
        """B02 stuffed full must not block a B01 candidate — virtual ledgers."""
        async with session_maker() as session:
            for i in range(4):
                session.add(_position(f"p{i}", book_id="B02"))
            await session.commit()
        decision = await _decide(session_maker, _candidate(book_id="B01"))
        assert decision.allowed

    @pytest.mark.asyncio
    async def test_envelope_overridable_per_book(self, session_maker):
        async with session_maker() as session:
            book = await session.get(BookModel, "B01")
            book.config = {"envelope": {"max_loss_pct_per_trade": 1.0}}  # $100 cap
            await session.commit()
        decision = await _decide(session_maker, _candidate(max_loss_per_share=2.0))  # $200
        assert "MAX_LOSS_PER_TRADE" in decision.blocked_by()


class TestFailClosed:
    @pytest.mark.asyncio
    async def test_unknown_book_blocks(self, session_maker):
        decision = await _decide(session_maker, _candidate(book_id="B77"))
        assert not decision.allowed
        assert "BOOK_ACTIVE" in decision.blocked_by()

    @pytest.mark.asyncio
    async def test_retired_book_blocks(self, session_maker):
        decision = await _decide(session_maker, _candidate(book_id="B09"))
        assert "BOOK_ACTIVE" in decision.blocked_by()


class TestCrossBookNetting:
    @pytest.mark.asyncio
    async def test_opposite_direction_same_contract_blocks(self, session_maker):
        async with session_maker() as session:
            pos = _position("p1", book_id="B02")
            pos.legs = [
                {
                    "option_type": "PUT",
                    "direction": "LONG",
                    "strike": 610.0,
                    "expiration": "2026-12-18",
                    "delta": -0.3,
                    "theta": 0.01,
                    "vega": 0.1,
                    "gamma": 0.01,
                }
            ]
            pos.underlying = "XSP"
            session.add(pos)
            await session.commit()
        # Candidate SHORTs the same 610 put that B02 holds LONG → nets at broker
        decision = await _decide(session_maker, _candidate(book_id="B01"))
        assert "CROSS_BOOK_NETTING" in decision.blocked_by()

    @pytest.mark.asyncio
    async def test_same_direction_sharing_is_fine(self, session_maker):
        async with session_maker() as session:
            pos = _position("p1", book_id="B02")
            pos.legs = [
                {
                    "option_type": "PUT",
                    "direction": "SHORT",
                    "strike": 610.0,
                    "expiration": "2026-12-18",
                    "delta": -0.3,
                    "theta": 0.01,
                    "vega": 0.1,
                    "gamma": 0.01,
                }
            ]
            pos.underlying = "XSP"
            session.add(pos)
            await session.commit()
        decision = await _decide(session_maker, _candidate(book_id="B01"))
        assert decision.allowed


class TestGateEvents:
    @pytest.mark.asyncio
    async def test_every_evaluation_logged_pass_and_block(self, session_maker):
        await _decide(session_maker, _candidate())  # all pass
        await _decide(session_maker, _candidate(max_loss_per_share=3.0))  # one block
        async with session_maker() as session:
            events = (await session.execute(select(GateEventModel))).scalars().all()
        assert len(events) == 10  # 5 gates × 2 evaluations
        results = {(e.gate, e.result) for e in events}
        assert ("MAX_LOSS_PER_TRADE", PASS) in results
        assert ("MAX_LOSS_PER_TRADE", BLOCK) in results
        assert all(e.book_id == "B01" for e in events)

    @pytest.mark.asyncio
    async def test_fail_closed_block_is_logged_too(self, session_maker):
        await _decide(session_maker, _candidate(book_id="B77"))
        async with session_maker() as session:
            events = (await session.execute(select(GateEventModel))).scalars().all()
        assert [(e.gate, e.result) for e in events] == [("BOOK_ACTIVE", BLOCK)]


class TestEncumbrance:
    @pytest.mark.asyncio
    async def test_staged_order_reserves_capital(self, session_maker):
        """The design's motivating case: two same-evening candidates must not
        both pass the deployed gate once the first is staged."""
        big = _candidate(max_loss_per_share=2.5)  # $250 each
        async with session_maker() as session:
            book = await session.get(BookModel, "B01")
            book.config = {"envelope": {"max_deployed_pct": 4.0}}  # $400 cap for the test
            await session.commit()

        async with session_maker() as session:
            first = await evaluate_book_gates(session, big)
            assert first.allowed
            await stage_order(
                session,
                big,
                order_id="o1",
                order_ref="basis:B01:o1:open",
                limit_price=-1.25,
                decision_midpoint=-1.30,
                combo_legs=[],
            )
            second = await evaluate_book_gates(session, big)
        assert "MAX_DEPLOYED" in second.blocked_by()  # $250 encumbered + $250 candidate > $400

    @pytest.mark.asyncio
    async def test_pending_order_counts_toward_position_slots(self, session_maker):
        async with session_maker() as session:
            for i in range(3):
                session.add(_position(f"p{i}"))
            await session.commit()
        async with session_maker() as session:
            await stage_order(
                session,
                _candidate(),
                order_id="o1",
                order_ref="basis:B01:o1:open",
                limit_price=-1.25,
                decision_midpoint=-1.30,
                combo_legs=[],
            )
            decision = await evaluate_book_gates(session, _candidate())
        assert "MAX_POSITIONS" in decision.blocked_by()  # 3 open + 1 pending = full

    @pytest.mark.asyncio
    async def test_release_frees_the_reservation(self, session_maker):
        big = _candidate(max_loss_per_share=2.5)
        async with session_maker() as session:
            book = await session.get(BookModel, "B01")
            book.config = {"envelope": {"max_deployed_pct": 4.0}}
            await session.commit()
        async with session_maker() as session:
            await stage_order(
                session,
                big,
                order_id="o1",
                order_ref="basis:B01:o1:open",
                limit_price=-1.25,
                decision_midpoint=-1.30,
                combo_legs=[],
            )
            await release_order(session, "o1", "CANCELLED")
            decision = await evaluate_book_gates(session, big)
        assert decision.allowed  # encumbrance released

    @pytest.mark.asyncio
    async def test_release_validates_terminal_status(self, session_maker):
        async with session_maker() as session:
            with pytest.raises(ValueError, match="Not a terminal order status"):
                await release_order(session, "o1", "SUBMITTED")
            with pytest.raises(ValueError, match="No order"):
                await release_order(session, "ghost", "CANCELLED")

    @pytest.mark.asyncio
    async def test_stage_order_records_intent_before_submission(self, session_maker):
        async with session_maker() as session:
            await stage_order(
                session,
                _candidate(),
                order_id="o1",
                order_ref="basis:B01:o1:open",
                limit_price=-1.25,
                decision_midpoint=-1.30,
                combo_legs=[{"occ": SHORT_OCC, "action": "SELL"}],
            )
        async with session_maker() as session:
            order = (await session.execute(select(OrderModel))).scalar_one()
        assert order.status == "STAGED"
        assert order.encumbered_risk == 200.0
        assert order.decision_midpoint == -1.30  # slippage evidence captured at stage time

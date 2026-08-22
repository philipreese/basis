"""Tests for per-book gates and capital encumbrance (backend/book_gates.py, #67)."""

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.book_gates import (
    BLOCK,
    PASS,
    CandidateOrder,
    credit_book_cash,
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


_ORDER_SEQ = iter(range(10_000))


def _open_order(
    book_id: str,
    occ_direction_legs: tuple[tuple[str, str, float, str], ...],  # (occ, direction, strike, expiration)
    status: str = "STAGED",
    action: str = "OPEN",
    underlying: str = "XSP",
) -> OrderModel:
    n = next(_ORDER_SEQ)
    return OrderModel(
        id=f"o{n}",
        book_id=book_id,
        position_id=None,
        order_ref=f"basis:{book_id}:o{n}:open",
        ib_order_id=None,
        ib_perm_id=None,
        action=action,
        combo_legs={
            "legs": [
                {"occ": occ, "option_type": "PUT", "direction": direction, "strike": strike, "expiration": expiration}
                for occ, direction, strike, expiration in occ_direction_legs
            ],
            "quantity": 1,
            "underlying": underlying,
        },
        order_type="LIMIT",
        limit_price=1.0,
        decision_midpoint=1.0,
        status=status,
        submitted_at=None if status == "STAGED" else "t0",
        completed_at="t1" if status in ("CANCELLED", "REJECTED", "FILLED") else None,
        encumbered_risk=200.0,
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
            for i in range(8):
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
            for i in range(8):
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

    @pytest.mark.asyncio
    async def test_same_run_second_book_blocked_by_a_staged_order(self, session_maker):
        # #665: a candidate staged for B02 earlier in the SAME nightly run has
        # no position yet (it fills tomorrow morning at the earliest) — the
        # old OPEN-positions-only query was blind to it, letting B01 take the
        # opposite side of a contract B02 already committed to tonight.
        async with session_maker() as session:
            session.add(_open_order("B02", (("XSP261218P00610000", "LONG", 610.0, "2026-12-18"),)))
            await session.commit()
        # Candidate SHORTs the same 610 put B02 just staged LONG.
        decision = await _decide(session_maker, _candidate(book_id="B01"))
        assert "CROSS_BOOK_NETTING" in decision.blocked_by()

    @pytest.mark.asyncio
    async def test_prior_night_resting_order_blocks_tonight_candidate(self, session_maker):
        # A SUBMITTED order resting at the broker from a prior night (not yet
        # filled/synced into a position) is exactly as real as a STAGED one.
        async with session_maker() as session:
            session.add(_open_order("B02", (("XSP261218P00610000", "LONG", 610.0, "2026-12-18"),), status="SUBMITTED"))
            await session.commit()
        decision = await _decide(session_maker, _candidate(book_id="B01"))
        assert "CROSS_BOOK_NETTING" in decision.blocked_by()

    @pytest.mark.asyncio
    async def test_partial_order_blocks_too(self, session_maker):
        # PARTIAL is "pending" for encumbrance (book_gates.PENDING_ORDER_STATUSES)
        # — netting must trust the same set, not a narrower STAGED/SUBMITTED one.
        async with session_maker() as session:
            session.add(_open_order("B02", (("XSP261218P00610000", "LONG", 610.0, "2026-12-18"),), status="PARTIAL"))
            await session.commit()
        decision = await _decide(session_maker, _candidate(book_id="B01"))
        assert "CROSS_BOOK_NETTING" in decision.blocked_by()

    @pytest.mark.asyncio
    async def test_same_direction_pending_order_sharing_still_passes(self, session_maker):
        async with session_maker() as session:
            session.add(_open_order("B02", (("XSP261218P00610000", "SHORT", 610.0, "2026-12-18"),)))
            await session.commit()
        decision = await _decide(session_maker, _candidate(book_id="B01"))
        assert decision.allowed

    @pytest.mark.asyncio
    async def test_terminal_orders_do_not_block(self, session_maker):
        # CANCELLED/REJECTED never materialize exposure; FILLED-into-position
        # is already covered by the position itself (or, if the position
        # somehow doesn't exist, must not phantom-block on the order alone).
        async with session_maker() as session:
            for status in ("CANCELLED", "REJECTED", "FILLED"):
                session.add(_open_order("B02", (("XSP261218P00610000", "LONG", 610.0, "2026-12-18"),), status=status))
            await session.commit()
        decision = await _decide(session_maker, _candidate(book_id="B01"))
        assert decision.allowed

    @pytest.mark.asyncio
    async def test_close_orders_are_not_double_counted_as_new_exposure(self, session_maker):
        # #665: a close order's combo_legs mirror the POSITION's own
        # direction (SELL-the-bag reverses the order's execution side, never
        # the stored LONG/SHORT field) — an in-flight close for an OPEN LONG
        # position must not make a same-direction LONG candidate look
        # blocked, and must not introduce a phantom SHORT the position
        # itself never held.
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
            session.add(
                _open_order(
                    "B02",
                    (("XSP261218P00610000", "LONG", 610.0, "2026-12-18"),),
                    status="STAGED",
                    action="CLOSE",
                )
            )
            await session.commit()
        # Same-direction LONG candidate on the same contract must still pass.
        decision = await _decide(session_maker, _candidate(book_id="B01", legs=(("XSP261218P00610000", "LONG"),)))
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
                combo_legs={},
            )
            second = await evaluate_book_gates(session, big)
        assert "MAX_DEPLOYED" in second.blocked_by()  # $250 encumbered + $250 candidate > $400

    @pytest.mark.asyncio
    async def test_pending_order_counts_toward_position_slots(self, session_maker):
        async with session_maker() as session:
            for i in range(7):
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
                combo_legs={},
            )
            decision = await evaluate_book_gates(session, _candidate())
        assert "MAX_POSITIONS" in decision.blocked_by()  # 7 open + 1 pending = full

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
                combo_legs={},
            )
            await release_order(session, "o1", "CANCELLED")
            decision = await evaluate_book_gates(session, big)
        assert decision.allowed  # encumbrance released

    @pytest.mark.asyncio
    async def test_release_validates_terminal_status(self, session_maker):
        async with session_maker() as session:
            with pytest.raises(ValueError, match="Not a terminal order status"):
                await release_order(session, "o1", "SUBMITTED")
            # #481 F9: FILLED settles only through _order_to_position — a
            # release_order("FILLED") would terminalize the row with no
            # position, no cash, no audits, and no caller ever used it.
            with pytest.raises(ValueError, match="Not a terminal order status"):
                await release_order(session, "o1", "FILLED")
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
                combo_legs={"legs": [{"occ": SHORT_OCC, "action": "SELL"}], "quantity": 1},
            )
        async with session_maker() as session:
            order = (await session.execute(select(OrderModel))).scalar_one()
        assert order.status == "STAGED"
        assert order.encumbered_risk == 200.0
        assert order.decision_midpoint == -1.30  # slippage evidence captured at stage time

    @pytest.mark.asyncio
    async def test_stage_order_stamps_the_decision_time_config_hash(self, session_maker):
        # #534 (Audit II R4): the gates just evaluated THIS config — a
        # seed-sync landing before the fill must not re-attribute the trade.
        async with session_maker() as session:
            book = await session.get(BookModel, "B01")
            book.config_hash = "decided123"
            await session.commit()
        async with session_maker() as session:
            await stage_order(
                session,
                _candidate(),
                order_id="o_hash",
                order_ref="basis:B01:o_hash:open",
                limit_price=-1.25,
                decision_midpoint=-1.30,
                combo_legs={},
            )
        async with session_maker() as session:
            order = (await session.execute(select(OrderModel).filter_by(id="o_hash"))).scalar_one()
        assert order.config_hash == "decided123"


class TestCreditBookCash:
    """SQL-side cash increments (#462): the executor's night-long session and
    the console's request sessions both move book cash; a read-modify-write on
    a stale ORM instance lets the last flush silently erase the other side's
    movement while both audit rows claim they landed."""

    @pytest.mark.asyncio
    async def test_returns_the_new_balance(self, session_maker):
        async with session_maker() as session:
            assert await credit_book_cash(session, "B01", 125.0) == 10125.0
            await session.commit()

    @pytest.mark.asyncio
    async def test_unknown_book_is_a_none_not_a_crash(self, session_maker):
        async with session_maker() as session:
            assert await credit_book_cash(session, "B99", 125.0) is None

    @pytest.mark.asyncio
    async def test_interleaved_sessions_both_land(self, session_maker):
        # The lost-update shape: session A reads the book (10000), session B
        # reads-and-writes (+200 → 10200 committed), then A writes its own
        # movement. With `book.cash_balance += x` A's flush stamps 10000+x,
        # silently erasing B's +200. SQL-side increments make both land.
        async with session_maker() as session_a, session_maker() as session_b:
            # A loads the instance FIRST so its identity map holds the stale 10000.
            book_a = await session_a.get(BookModel, "B01")
            assert book_a.cash_balance == 10000.0
            await credit_book_cash(session_b, "B01", 200.0)
            await session_b.commit()
            await credit_book_cash(session_a, "B01", -50.0)
            await session_a.commit()
        async with session_maker() as session:
            book = await session.get(BookModel, "B01")
        assert book.cash_balance == 10150.0  # both movements survived

    @pytest.mark.asyncio
    async def test_same_session_instance_sees_the_increment(self, session_maker):
        # The executor holds the BookModel instance all night; later reads
        # (MTM sweep, digest) must see the post-increment value, not the
        # identity-map's stale attribute.
        async with session_maker() as session:
            book = await session.get(BookModel, "B01")
            await credit_book_cash(session, "B01", 300.0)
            assert book.cash_balance == 10300.0

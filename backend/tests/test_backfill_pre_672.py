"""Tests for the #766 one-off startup migration (database._backfill_pre_672_close_fills).

Reproduces the B25 shape found by the #766 data audit: a CLOSE order that
completed before #672 (95d063f) landed was booked at the order's
limit_price instead of the fill-derived exit value, even though the fills
ledger has complete leg coverage. The migration recomputes the correct
value from the SAME executor._fill_derived_net path #672 itself introduced
— never a hand-computed constant — and corrects current_value_per_share,
the post-mortem's realized_pnl/outcome, and books.cash_balance, leaving a
FILL_PRICE_BACKFILL_CORRECTED audit row with the old/new values.
"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import _PRE_672_FIX_CUTOFF, _backfill_pre_672_close_fills
from backend.models import (
    AuditEventModel,
    Base,
    BookModel,
    ClosurePostMortemModel,
    FillModel,
    OrderModel,
    PositionModel,
)

PRE_FIX = "2026-08-21T22:45:26+00:00"  # B25's real CLOSE_FILLED timestamp — predates the cutoff
POST_FIX = "2026-08-23T10:00:00+00:00"  # after the cutoff


def _book(book_id: str = "B25", cash: float = 10000.0) -> BookModel:
    return BookModel(
        id=book_id,
        name="test book",
        config={},
        config_version=1,
        config_hash="h",
        starting_capital=10000.0,
        cash_balance=cash,
        status="ACTIVE",
        created_at="2026-01-01T00:00:00+00:00",
    )


_LEGS = [
    {"occ": "occA", "expiration": "2026-09-18", "option_type": "PUT", "strike": 610.0, "direction": "SHORT"},
    {"occ": "occB", "expiration": "2026-09-18", "option_type": "PUT", "strike": 605.0, "direction": "LONG"},
]


def _position(pos_id: str = "pos_o_8da86ccd", book_id: str = "B25", current_value: float = 1.02) -> PositionModel:
    return PositionModel(
        id=pos_id,
        underlying="XSP",
        strategy_type="BULL_PUT_SPREAD",
        execution_mode="PAPER",
        legs=[
            {
                "option_type": leg["option_type"],
                "direction": leg["direction"],
                "strike": leg["strike"],
                "expiration": leg["expiration"],
                "delta": -0.2,
                "theta": 0.02,
                "vega": 0.1,
                "gamma": 0.01,
            }
            for leg in _LEGS
        ],
        entry_date="2026-08-01",
        expiration_date="2026-09-18",
        entry_premium=2.90,
        premium_direction="DEBIT",
        current_value_per_share=current_value,
        contracts=1,
        max_profit=1.0,
        max_loss=2.90,
        notes="",
        rolls=0,
        status="CLOSED",
        journal={},
        book_id=book_id,
    )


def _post_mortem(pos_id: str, realized_pnl: float = -188.00, outcome: str = "LOSS") -> ClosurePostMortemModel:
    return ClosurePostMortemModel(
        id=f"pm_{pos_id}",
        position_id=pos_id,
        outcome=outcome,
        realized_pnl=realized_pnl,
        actual_underlying_move_pct=0.0,
        exit_date="2026-08-21",
        exit_trigger="MANUAL",
        lesson_tags=[],
        user_override_logged=False,
        playbook_id=None,
        playbook_version=None,
    )


def _close_order(
    order_id: str = "o_b395626e",
    book_id: str = "B25",
    position_id: str = "pos_o_8da86ccd",
    completed_at: str = PRE_FIX,
    limit_price: float = 1.02,
    status: str = "FILLED",
) -> OrderModel:
    return OrderModel(
        id=order_id,
        book_id=book_id,
        position_id=position_id,
        order_ref=f"basis:{book_id}:{order_id}:close",
        ib_order_id=None,
        ib_perm_id=None,
        action="CLOSE",
        combo_legs={"legs": _LEGS, "quantity": 1, "underlying": "XSP"},
        order_type="LIMIT",
        limit_price=limit_price,
        decision_midpoint=limit_price,
        status=status,
        submitted_at="2026-08-21T22:40:00+00:00",
        completed_at=completed_at,
        encumbered_risk=0.0,
    )


def _close_fills(order_id: str = "o_b395626e", book_id: str = "B25") -> list[FillModel]:
    # BOT 13.14 buying back the short leg (con_id A), SLD 15.40 selling the
    # long leg (con_id B) — raw net = 13.14 - 15.40 = -2.26; CLOSE-side
    # negation (executor._fill_derived_net) makes exit_value = 2.26.
    return [
        FillModel(
            exec_id=f"{order_id}_1",
            order_id=order_id,
            book_id=book_id,
            con_id=1,
            side="BOT",
            quantity=1,
            price=13.14,
            commission=0.65,
            fill_time=PRE_FIX,
        ),
        FillModel(
            exec_id=f"{order_id}_2",
            order_id=order_id,
            book_id=book_id,
            con_id=2,
            side="SLD",
            quantity=1,
            price=15.40,
            commission=0.65,
            fill_time=PRE_FIX,
        ),
    ]


@pytest_asyncio.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield maker
    await engine.dispose()


async def _seed_b25_shape(maker) -> None:
    async with maker() as session:
        session.add(_book())
        session.add(_position())
        session.add(_post_mortem("pos_o_8da86ccd"))
        session.add(_close_order())
        session.add_all(_close_fills())
        await session.commit()


class TestBackfillCorrectsTheB25Shape:
    @pytest.mark.asyncio
    async def test_corrects_current_value_realized_pnl_and_cash(self, session_maker, monkeypatch):
        import backend.database as db_mod

        backup_calls = []
        monkeypatch.setattr(db_mod, "_backup_before_migration", lambda url: backup_calls.append(url))
        await _seed_b25_shape(session_maker)

        async with session_maker() as session:
            await _backfill_pre_672_close_fills(session)

        async with session_maker() as session:
            pos = await session.get(PositionModel, "pos_o_8da86ccd")
            pm = await session.get(ClosurePostMortemModel, "pm_pos_o_8da86ccd")
            book = await session.get(BookModel, "B25")

        assert pos.current_value_per_share == pytest.approx(2.26)
        assert pm.realized_pnl == pytest.approx(-64.00)
        assert pm.outcome == "LOSS"
        # cash delta = (2.26 - 1.02) * 100 * 1 = 124.00
        assert book.cash_balance == pytest.approx(10000.0 + 124.00)
        assert backup_calls  # a .bak snapshot was taken exactly because a correction happened

    @pytest.mark.asyncio
    async def test_writes_an_audit_row_with_old_and_new_values(self, session_maker, monkeypatch):
        import backend.database as db_mod

        monkeypatch.setattr(db_mod, "_backup_before_migration", lambda url: None)
        await _seed_b25_shape(session_maker)

        async with session_maker() as session:
            await _backfill_pre_672_close_fills(session)

        async with session_maker() as session:
            from sqlalchemy import select

            rows = (
                (await session.execute(select(AuditEventModel).filter_by(event_type="FILL_PRICE_BACKFILL_CORRECTED")))
                .scalars()
                .all()
            )
        assert len(rows) == 1
        payload = rows[0].payload
        assert payload["old_current_value_per_share"] == pytest.approx(1.02)
        assert payload["new_current_value_per_share"] == pytest.approx(2.26)
        assert payload["old_realized_pnl"] == pytest.approx(-188.00)
        assert payload["new_realized_pnl"] == pytest.approx(-64.00)
        assert payload["cash_delta"] == pytest.approx(124.00)
        assert payload["order_ref"] == "basis:B25:o_b395626e:close"
        assert rows[0].actor == "migration"


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_rerun_finds_nothing_to_correct(self, session_maker, monkeypatch):
        import backend.database as db_mod

        monkeypatch.setattr(db_mod, "_backup_before_migration", lambda url: None)
        await _seed_b25_shape(session_maker)

        async with session_maker() as session:
            await _backfill_pre_672_close_fills(session)
        async with session_maker() as session:
            await _backfill_pre_672_close_fills(session)  # second run

        async with session_maker() as session:
            from sqlalchemy import select

            rows = (
                (await session.execute(select(AuditEventModel).filter_by(event_type="FILL_PRICE_BACKFILL_CORRECTED")))
                .scalars()
                .all()
            )
            pos = await session.get(PositionModel, "pos_o_8da86ccd")
            book = await session.get(BookModel, "B25")
        assert len(rows) == 1  # only the FIRST run corrected anything
        assert pos.current_value_per_share == pytest.approx(2.26)
        assert book.cash_balance == pytest.approx(10000.0 + 124.00)  # not double-applied

    @pytest.mark.asyncio
    async def test_already_correct_row_is_a_silent_noop(self, session_maker, monkeypatch):
        import backend.database as db_mod

        monkeypatch.setattr(db_mod, "_backup_before_migration", lambda url: None)
        async with session_maker() as session:
            session.add(_book())
            # Already booked at the fill-derived value, coincidentally, pre-cutoff.
            session.add(_position(current_value=2.26))
            session.add(_post_mortem("pos_o_8da86ccd", realized_pnl=-64.00, outcome="LOSS"))
            session.add(_close_order(limit_price=2.26))
            session.add_all(_close_fills())
            await session.commit()

        async with session_maker() as session:
            await _backfill_pre_672_close_fills(session)

        async with session_maker() as session:
            from sqlalchemy import select

            corrected = (
                (await session.execute(select(AuditEventModel).filter_by(event_type="FILL_PRICE_BACKFILL_CORRECTED")))
                .scalars()
                .all()
            )
            book = await session.get(BookModel, "B25")
        assert corrected == []
        assert book.cash_balance == pytest.approx(10000.0)  # untouched


class TestGuards:
    @pytest.mark.asyncio
    async def test_post_672_close_is_untouched_even_if_booked_value_looks_wrong(self, session_maker, monkeypatch):
        import backend.database as db_mod

        monkeypatch.setattr(db_mod, "_backup_before_migration", lambda url: None)
        async with session_maker() as session:
            session.add(_book())
            session.add(_position(current_value=1.02))  # "wrong" vs fills, but the order postdates the fix
            session.add(_post_mortem("pos_o_8da86ccd"))
            session.add(_close_order(completed_at=POST_FIX, limit_price=1.02))
            session.add_all(_close_fills())
            await session.commit()

        async with session_maker() as session:
            await _backfill_pre_672_close_fills(session)

        async with session_maker() as session:
            pos = await session.get(PositionModel, "pos_o_8da86ccd")
            book = await session.get(BookModel, "B25")
        assert pos.current_value_per_share == pytest.approx(1.02)  # untouched
        assert book.cash_balance == pytest.approx(10000.0)

    @pytest.mark.asyncio
    async def test_incomplete_fill_coverage_is_skipped_not_force_corrected(self, session_maker, monkeypatch):
        import backend.database as db_mod

        monkeypatch.setattr(db_mod, "_backup_before_migration", lambda url: None)
        async with session_maker() as session:
            session.add(_book())
            session.add(_position(current_value=1.02))
            session.add(_post_mortem("pos_o_8da86ccd"))
            session.add(_close_order())
            # Only ONE of the two legs' fills present — genuinely incomplete.
            session.add(_close_fills()[0])
            await session.commit()

        async with session_maker() as session:
            await _backfill_pre_672_close_fills(session)

        async with session_maker() as session:
            from sqlalchemy import select

            pos = await session.get(PositionModel, "pos_o_8da86ccd")
            pm = await session.get(ClosurePostMortemModel, "pm_pos_o_8da86ccd")
            book = await session.get(BookModel, "B25")
            skipped = (
                (await session.execute(select(AuditEventModel).filter_by(event_type="FILL_PRICE_BACKFILL_SKIPPED")))
                .scalars()
                .all()
            )
            corrected = (
                (await session.execute(select(AuditEventModel).filter_by(event_type="FILL_PRICE_BACKFILL_CORRECTED")))
                .scalars()
                .all()
            )
        assert pos.current_value_per_share == pytest.approx(1.02)  # untouched, not force-corrected
        assert pm.realized_pnl == pytest.approx(-188.00)
        assert book.cash_balance == pytest.approx(10000.0)
        assert len(skipped) == 1
        assert skipped[0].payload["reason"] == "incomplete_or_absent_fills"
        assert corrected == []

    @pytest.mark.asyncio
    async def test_non_close_and_non_filled_orders_are_ignored(self, session_maker, monkeypatch):
        import backend.database as db_mod

        monkeypatch.setattr(db_mod, "_backup_before_migration", lambda url: None)
        async with session_maker() as session:
            session.add(_book())
            session.add(_position(current_value=1.02))
            session.add(_post_mortem("pos_o_8da86ccd"))
            # STAGED, not FILLED — must be ignored.
            session.add(_close_order(status="STAGED"))
            session.add_all(_close_fills())
            await session.commit()

        async with session_maker() as session:
            await _backfill_pre_672_close_fills(session)  # must not raise or touch anything

        async with session_maker() as session:
            pos = await session.get(PositionModel, "pos_o_8da86ccd")
        assert pos.current_value_per_share == pytest.approx(1.02)

    @pytest.mark.asyncio
    async def test_scans_every_book_not_just_one(self, session_maker, monkeypatch):
        import backend.database as db_mod

        monkeypatch.setattr(db_mod, "_backup_before_migration", lambda url: None)
        async with session_maker() as session:
            session.add(_book("B25"))
            session.add(_book("B26"))
            session.add(_position("pos_1", book_id="B25"))
            session.add(_post_mortem("pos_1"))
            session.add(_close_order("o_1", book_id="B25", position_id="pos_1"))
            session.add_all(_close_fills("o_1", book_id="B25"))
            session.add(_position("pos_2", book_id="B26"))
            session.add(_post_mortem("pos_2"))
            session.add(_close_order("o_2", book_id="B26", position_id="pos_2"))
            session.add_all(_close_fills("o_2", book_id="B26"))
            await session.commit()

        async with session_maker() as session:
            await _backfill_pre_672_close_fills(session)

        async with session_maker() as session:
            from sqlalchemy import select

            corrected = (
                (await session.execute(select(AuditEventModel).filter_by(event_type="FILL_PRICE_BACKFILL_CORRECTED")))
                .scalars()
                .all()
            )
            pos1 = await session.get(PositionModel, "pos_1")
            pos2 = await session.get(PositionModel, "pos_2")
        assert len(corrected) == 2
        assert pos1.current_value_per_share == pytest.approx(2.26)
        assert pos2.current_value_per_share == pytest.approx(2.26)

    @pytest.mark.asyncio
    async def test_cutoff_matches_the_672_merge_commit(self):
        # Sanity pin: the cutoff is the exact 95d063f commit time, not an
        # approximation — see the #766 issue's evidence timeline.
        assert _PRE_672_FIX_CUTOFF == "2026-08-22T20:13:03+00:00"

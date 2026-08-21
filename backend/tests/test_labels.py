"""Tests for backend/labels.py (#600): plain-English book/instrument labels
for operator-facing surfaces (digest, console API, drift/audit rows)."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.labels import book_label, format_spread_label, ref_label
from backend.models import Base, BookModel, OrderModel, PositionModel


@pytest_asyncio.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield maker
    await engine.dispose()


def _book(book_id: str = "B04", config: dict | None = None) -> BookModel:
    return BookModel(
        id=book_id,
        name=f"lab {book_id}",
        config=config if config is not None else {"underlying": "SPY", "envelope": {}},
        config_version=1,
        config_hash="cafe1234",
        starting_capital=10000.0,
        cash_balance=10000.0,
        status="ACTIVE",
        created_at="2026-08-01T00:00:00+00:00",
    )


_LEGS = [
    {"option_type": "PUT", "direction": "SHORT", "strike": 745.0, "expiration": "2026-10-02", "delta": -0.3},
    {"option_type": "PUT", "direction": "LONG", "strike": 742.0, "expiration": "2026-10-02", "delta": -0.2},
]


def _position(book_id: str = "B04", pos_id: str = "pos_o_8bf1070c", **overrides) -> PositionModel:
    defaults: dict = {
        "id": pos_id,
        "underlying": "SPY",
        "strategy_type": "BULL_PUT_SPREAD",
        "execution_mode": "PAPER",
        "legs": _LEGS,
        "entry_date": "2026-08-01",
        "expiration_date": "2026-10-02",
        "entry_premium": 1.2,
        "premium_direction": "CREDIT",
        "current_value_per_share": 1.0,
        "contracts": 1,
        "max_profit": 1.2,
        "max_loss": 1.8,
        "notes": "",
        "rolls": 0,
        "status": "OPEN",
        "journal": {},
        "book_id": book_id,
    }
    defaults.update(overrides)
    return PositionModel(**defaults)


class TestFormatSpreadLabel:
    def test_bull_put_spread_with_expiration(self):
        assert format_spread_label("BULL_PUT_SPREAD", _LEGS, "2026-10-02") == "745/742 bull put (Oct 2 '26)"

    def test_no_expiration_omits_the_parenthetical(self):
        assert format_spread_label("BULL_PUT_SPREAD", _LEGS, None) == "745/742 bull put"

    def test_unparseable_expiration_degrades_gracefully(self):
        assert format_spread_label("BULL_PUT_SPREAD", _LEGS, "not-a-date") == "745/742 bull put"

    def test_single_leg_strategy_shows_one_strike(self):
        legs = [{"option_type": "PUT", "direction": "LONG", "strike": 610.0, "expiration": "2026-09-18"}]
        assert format_spread_label("LONG_PUT", legs, "2026-09-18") == "610 long put (Sep 18 '26)"

    def test_unknown_strategy_type_falls_back_to_a_readable_name(self):
        # A future strategy this helper doesn't know about must still
        # produce something, not raise or silently omit the strategy.
        legs = [{"option_type": "CALL", "direction": "SHORT", "strike": 500.0, "expiration": "2026-09-18"}]
        assert "future spread" in format_spread_label("FUTURE_SPREAD", legs, "2026-09-18")

    def test_whole_number_strikes_have_no_trailing_decimal(self):
        assert "745/742" in format_spread_label("BULL_PUT_SPREAD", _LEGS, "2026-10-02")


class TestBookLabel:
    @pytest.mark.asyncio
    async def test_book_with_open_position_shows_the_full_spread(self, session_maker):
        async with session_maker() as session:
            session.add(_book("B04"))
            session.add(_position("B04"))
            await session.commit()
            label = await book_label(session, "B04")
        assert label == "B04 — SPY 745/742 bull put (Oct 2 '26)"

    @pytest.mark.asyncio
    async def test_flat_book_degrades_to_underlying_only(self, session_maker):
        # A halt/drift fires on flat books too (#562) — no open position to
        # describe, but the book's own config still names an underlying.
        async with session_maker() as session:
            session.add(_book("B04"))
            await session.commit()
            label = await book_label(session, "B04")
        assert label == "B04 — SPY"

    @pytest.mark.asyncio
    async def test_book_with_no_resolved_underlying_is_just_the_id(self, session_maker):
        async with session_maker() as session:
            session.add(_book("B00", config={}))
            await session.commit()
            label = await book_label(session, "B00")
        assert label == "B00"

    @pytest.mark.asyncio
    async def test_unknown_book_id_is_just_the_id(self, session_maker):
        async with session_maker() as session:
            label = await book_label(session, "B99")
        assert label == "B99"

    @pytest.mark.asyncio
    async def test_closed_positions_are_not_used_for_the_label(self, session_maker):
        async with session_maker() as session:
            session.add(_book("B04"))
            session.add(_position("B04", status="CLOSED"))
            await session.commit()
            label = await book_label(session, "B04")
        assert label == "B04 — SPY"  # degrades same as the flat-book case

    @pytest.mark.asyncio
    async def test_passing_an_already_fetched_book_skips_the_lookup(self, session_maker):
        book = _book("B04")
        async with session_maker() as session:
            label = await book_label(session, "B04", book=book)
        assert label == "B04 — SPY"


class TestRefLabel:
    @pytest.mark.asyncio
    async def test_order_ref_with_a_linked_position_resolves_the_full_spread(self, session_maker):
        async with session_maker() as session:
            session.add(_book("B04"))
            session.add(_position("B04", pos_id="pos_1"))
            session.add(
                OrderModel(
                    id="o_close1",
                    book_id="B04",
                    position_id="pos_1",
                    order_ref="basis:B04:o_close1:close",
                    action="CLOSE",
                    combo_legs={},
                    order_type="LIMIT",
                    limit_price=-1.0,
                    decision_midpoint=-1.0,
                    status="SUBMITTED",
                    encumbered_risk=180.0,
                )
            )
            await session.commit()
            label = await ref_label(session, "basis:B04:o_close1:close")
        assert label == "B04 — SPY 745/742 bull put (Oct 2 '26)"

    @pytest.mark.asyncio
    async def test_ghost_tp_ref_with_no_db_row_still_resolves_via_the_naming_convention(self, session_maker):
        # #559: a ghost TP shares its parent entry's order_id — the exact
        # incident this issue is about (basis:B04:o_8bf1070c:open:tp with
        # no DB row anywhere for it).
        async with session_maker() as session:
            session.add(_book("B04"))
            session.add(_position("B04", pos_id="pos_o_8bf1070c"))
            await session.commit()
            label = await ref_label(session, "basis:B04:o_8bf1070c:open:tp")
        assert label == "B04 — SPY 745/742 bull put (Oct 2 '26)"

    @pytest.mark.asyncio
    async def test_unresolvable_ref_degrades_to_the_book_label(self, session_maker):
        async with session_maker() as session:
            session.add(_book("B04"))
            await session.commit()
            label = await ref_label(session, "basis:B04:o_ghost_close:close")
        assert label == "B04 — SPY"  # close-side ghosts can't map to a position (#559)

    @pytest.mark.asyncio
    async def test_non_basis_ref_is_returned_unchanged(self, session_maker):
        async with session_maker() as session:
            label = await ref_label(session, "some-other-broker-ref")
        assert label == "some-other-broker-ref"

"""Tests for the roll workflow (#7): derivation in Layer A + the roll endpoint.

The roll rules are non-negotiable domain rules (spec/domain-rules.md): net
credit only, max 2 rolls then forced exit, down-and-out for puts / up-and-out
for calls. Each block is pinned here, plus the ledger mutation semantics
(cumulative credit, width-derived max loss, rolls counter).
"""

import datetime

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import get_db
from backend.models import (
    Base,
    OperationalJournalEntrySchema,
    OptionLegSchema,
    PositionModel,
    PositionSchema,
    TradingControlModel,
)
from backend.observation import derive_roll_candidate

TODAY = datetime.date(2026, 8, 18)
FAR = "2026-12-18"  # ~120 DTE from TODAY
NEAR = "2026-09-04"  # ≤21 DTE from TODAY


def _leg(strike: float, direction: str, option_type: str = "PUT", expiration: str = FAR) -> OptionLegSchema:
    return OptionLegSchema(
        option_type=option_type,
        direction=direction,
        strike=strike,
        expiration=expiration,
        delta=-0.3,
        theta=0.05,
        vega=0.1,
        gamma=0.01,
    )


def _position(**overrides) -> PositionSchema:
    defaults: dict = {
        "id": "p1",
        "underlying": "XSP",
        "strategy_type": "BULL_PUT_SPREAD",
        "execution_mode": "PAPER",
        "legs": [_leg(700, "SHORT"), _leg(695, "LONG")],
        "entry_date": "2026-08-01",
        "expiration_date": FAR,
        "entry_premium": 1.0,
        "premium_direction": "CREDIT",
        "current_value_per_share": 1.0,
        "contracts": 1,
        "max_profit": 1.0,
        "max_loss": 4.0,
        "notes": "",
        "rolls": 0,
        "status": "OPEN",
        "journal": OperationalJournalEntrySchema(
            core_thesis_rationale="t",
            structural_invalidation="t",
            expected_underlying_move_pct=1.0,
            pre_trade_emotional_state="Calm",
            pre_trade_confidence_rating=3,
        ),
    }
    defaults.update(overrides)
    return PositionSchema(**defaults)


class TestDerivation:
    def test_healthy_position_gets_no_roll(self):
        assert derive_roll_candidate(_position(), TODAY) is None

    def test_loss_pressure_suggests_put_roll_down_and_out(self):
        # buyback at 160% of credit — halfway past the pressure threshold
        cand = derive_roll_candidate(_position(current_value_per_share=1.6), TODAY)
        assert cand is not None and cand.eligible
        assert cand.suggested_expiration == "2027-01-15"  # +28 days from FAR
        assert sorted(leg.strike for leg in cand.suggested_legs) == [690.0, 695.0]  # shifted DOWN by the $5 width
        assert all(leg.option_type == "PUT" for leg in cand.suggested_legs)
        assert "Net credit required" in cand.reason

    def test_time_pressure_suggests_call_roll_up_and_out(self):
        pos = _position(
            strategy_type="BEAR_CALL_SPREAD",
            legs=[_leg(710, "SHORT", "CALL", NEAR), _leg(715, "LONG", "CALL", NEAR)],
            expiration_date=NEAR,
        )
        cand = derive_roll_candidate(pos, TODAY)
        assert cand is not None and cand.eligible
        assert sorted(leg.strike for leg in cand.suggested_legs) == [715.0, 720.0]  # shifted UP

    def test_roll_cap_blocks_with_forced_exit_reason(self):
        cand = derive_roll_candidate(_position(current_value_per_share=1.6, rolls=2), TODAY)
        assert cand is not None and not cand.eligible
        assert "ROLL_CAP_REACHED" in cand.reason
        assert cand.suggested_legs is None

    def test_debit_and_condor_positions_are_not_roll_instruments(self):
        debit = _position(premium_direction="DEBIT", strategy_type="BULL_CALL_SPREAD", current_value_per_share=1.6)
        assert derive_roll_candidate(debit, TODAY) is None
        condor = _position(strategy_type="IRON_CONDOR", current_value_per_share=1.6)
        assert derive_roll_candidate(condor, TODAY) is None

    def test_closed_position_gets_no_roll(self):
        assert derive_roll_candidate(_position(status="CLOSED", current_value_per_share=1.6), TODAY) is None

    def test_suggested_expiration_snaps_off_good_friday(self):
        # #541: expiration + 28 days from 2027-02-26 (Friday) lands on
        # 2027-03-26, Good Friday 2027 (a market holiday) — the naive +28d
        # suggests a contract that doesn't exist. Must walk back to the
        # prior trading day, 2027-03-25 (Thursday).
        pos = _position(expiration_date="2027-02-26", current_value_per_share=1.6)
        cand = derive_roll_candidate(pos, datetime.date(2027, 2, 1))
        assert cand is not None and cand.eligible
        assert cand.suggested_expiration == "2027-03-25"


@pytest_asyncio.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        # #741: roll_position now goes through assert_entries_allowed, which
        # fails closed if GLOBAL/the book have no control row.
        session.add(TradingControlModel(scope="GLOBAL", state="ACTIVE", reason="", actor="t", changed_at="t0"))
        session.add(TradingControlModel(scope="B00", state="ACTIVE", reason="", actor="t", changed_at="t0"))
        session.add(TradingControlModel(scope="B01", state="ACTIVE", reason="", actor="t", changed_at="t0"))
        await session.commit()
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture
async def client(session_maker):
    from backend.main import app

    async def override_get_db():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _seed_position(maker, book_id: str = "B00", **overrides) -> None:
    schema = _position(**overrides)
    async with maker() as session:
        session.add(
            PositionModel(
                id=schema.id,
                underlying=schema.underlying,
                strategy_type=schema.strategy_type,
                legs=[leg.model_dump() for leg in schema.legs],
                entry_date=schema.entry_date,
                expiration_date=schema.expiration_date,
                entry_premium=schema.entry_premium,
                premium_direction=schema.premium_direction,
                current_value_per_share=schema.current_value_per_share,
                contracts=schema.contracts,
                max_profit=schema.max_profit,
                max_loss=schema.max_loss,
                notes=schema.notes,
                rolls=schema.rolls,
                status=schema.status,
                journal=schema.journal.model_dump(),
                book_id=book_id,
            )
        )
        await session.commit()


ROLL_REQUEST = {
    "close_cost_per_share": 1.6,
    "new_credit_per_share": 1.75,
    "new_expiration": "2027-01-15",
    "new_legs": [
        {"option_type": "PUT", "direction": "SHORT", "strike": 695, "expiration": "2027-01-15"},
        {"option_type": "PUT", "direction": "LONG", "strike": 690, "expiration": "2027-01-15"},
    ],
}


class TestRollEndpoint:
    @pytest.mark.asyncio
    async def test_credit_roll_mutates_the_position(self, client, session_maker):
        await _seed_position(session_maker, current_value_per_share=1.6)
        resp = await client.post("/api/positions/p1/roll", json=ROLL_REQUEST)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["rolls"] == 1
        assert data["expiration_date"] == "2027-01-15"
        assert data["entry_premium"] == pytest.approx(1.15)  # 1.0 + (1.75 − 1.60) cumulative credit
        assert data["max_loss"] == pytest.approx(5.0 - 1.15)  # width − cumulative credit
        assert sorted(leg["strike"] for leg in data["legs"]) == [690.0, 695.0]
        assert "Rolled" in data["notes"]

    @pytest.mark.asyncio
    async def test_debit_roll_is_blocked(self, client, session_maker):
        await _seed_position(session_maker, current_value_per_share=1.6)
        resp = await client.post("/api/positions/p1/roll", json={**ROLL_REQUEST, "new_credit_per_share": 1.5})
        assert resp.status_code == 400
        assert "DEBIT_ROLL_BLOCKED" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_third_roll_is_blocked(self, client, session_maker):
        await _seed_position(session_maker, current_value_per_share=1.6, rolls=2)
        resp = await client.post("/api/positions/p1/roll", json=ROLL_REQUEST)
        assert resp.status_code == 400
        assert "ROLL_CAP_REACHED" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_put_roll_up_is_blocked(self, client, session_maker):
        await _seed_position(session_maker, current_value_per_share=1.6)
        wrong_way = {
            **ROLL_REQUEST,
            "new_legs": [
                {"option_type": "PUT", "direction": "SHORT", "strike": 705, "expiration": "2027-01-15"},
                {"option_type": "PUT", "direction": "LONG", "strike": 700, "expiration": "2027-01-15"},
            ],
        }
        resp = await client.post("/api/positions/p1/roll", json=wrong_way)
        assert resp.status_code == 400
        assert "puts roll DOWN" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_earlier_expiration_is_blocked(self, client, session_maker):
        await _seed_position(session_maker, current_value_per_share=1.6)
        resp = await client.post(
            "/api/positions/p1/roll",
            json={
                **ROLL_REQUEST,
                "new_expiration": "2026-11-20",
                "new_legs": [
                    {"option_type": "PUT", "direction": "SHORT", "strike": 695, "expiration": "2026-11-20"},
                    {"option_type": "PUT", "direction": "LONG", "strike": 690, "expiration": "2026-11-20"},
                ],
            },
        )
        assert resp.status_code == 400
        assert "later" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_non_rollable_strategy_is_blocked(self, client, session_maker):
        await _seed_position(
            session_maker,
            strategy_type="IRON_CONDOR",
            current_value_per_share=1.6,
            legs=[_leg(700, "SHORT"), _leg(695, "LONG"), _leg(760, "SHORT", "CALL"), _leg(765, "LONG", "CALL")],
        )
        resp = await client.post("/api/positions/p1/roll", json=ROLL_REQUEST)
        assert resp.status_code == 400
        assert "NOT_ROLLABLE" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_second_roll_accumulates_credit(self, client, session_maker):
        await _seed_position(session_maker, current_value_per_share=1.6, rolls=1, entry_premium=1.15)
        resp = await client.post("/api/positions/p1/roll", json=ROLL_REQUEST)
        assert resp.status_code == 200
        data = resp.json()
        assert data["rolls"] == 2
        assert data["entry_premium"] == pytest.approx(1.30)
        async with session_maker() as session:
            row = (await session.execute(select(PositionModel).filter_by(id="p1"))).scalar_one()
        assert row.rolls == 2

    @pytest.mark.asyncio
    async def test_roll_is_blocked_under_a_book_halt(self, client, session_maker):
        # #741: a roll counts as an entry under ADR-0008 §5/§7 and must go
        # through the same choke point as every other order-shaped mutation —
        # this pins that no API mutation path skips trading control.
        await _seed_position(session_maker, book_id="B01", current_value_per_share=1.6)
        async with session_maker() as session:
            row = await session.get(TradingControlModel, "B01")
            row.state = "HALT_ENTRIES"
            await session.commit()
        resp = await client.post("/api/positions/p1/roll", json={**ROLL_REQUEST, "acknowledge_broker_divergence": True})
        assert resp.status_code == 409
        assert "Entries blocked" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_roll_is_blocked_under_a_global_halt(self, client, session_maker):
        await _seed_position(session_maker, current_value_per_share=1.6)
        async with session_maker() as session:
            row = await session.get(TradingControlModel, "GLOBAL")
            row.state = "HALT_ENTRIES"
            await session.commit()
        resp = await client.post("/api/positions/p1/roll", json=ROLL_REQUEST)
        assert resp.status_code == 409
        assert "Entries blocked" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_executor_book_roll_is_blocked_without_acknowledgement(self, client, session_maker):
        # #741: this endpoint places no broker order — rolling an
        # executor-book position (real legs at the broker) here would
        # manufacture instant DB-vs-broker drift, same class of bug as #279.
        await _seed_position(session_maker, book_id="B01", current_value_per_share=1.6)
        resp = await client.post("/api/positions/p1/roll", json=ROLL_REQUEST)
        assert resp.status_code == 409
        assert "acknowledge_broker_divergence" in resp.json()["detail"]
        async with session_maker() as session:
            row = (await session.execute(select(PositionModel).filter_by(id="p1"))).scalar_one()
        assert row.rolls == 0  # unmutated

    @pytest.mark.asyncio
    async def test_executor_book_roll_proceeds_when_acknowledged(self, client, session_maker):
        await _seed_position(session_maker, book_id="B01", current_value_per_share=1.6)
        resp = await client.post("/api/positions/p1/roll", json={**ROLL_REQUEST, "acknowledge_broker_divergence": True})
        assert resp.status_code == 200
        assert resp.json()["rolls"] == 1


class TestConcurrentRoll:
    """#763: apply_roll mutates the ORM-tracked position in place from
    values read at the initial SELECT — two concurrent requests can both
    pass its rolls-cap/direction checks against the SAME stale rolls count
    before either writes. The atomic conditional UPDATE (WHERE rolls =
    original_rolls AND status = OPEN) is the real guard, mirroring
    close_position's precedent (#463) for the identical risk."""

    @pytest.mark.asyncio
    async def test_a_second_concurrent_roll_gets_409_not_silent_data_loss(self, client, session_maker):
        from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

        await _seed_position(session_maker, current_value_per_share=1.6)

        original_execute = _AsyncSession.execute
        triggered = False
        winner_resp = None

        async def racing_execute(self_session, statement, *a, **kw):
            nonlocal triggered, winner_resp
            sql = str(statement)
            if not triggered and "UPDATE positions" in sql and "rolls" in sql:
                triggered = True
                _AsyncSession.execute = original_execute
                # A concurrent second request completes its FULL roll
                # (guard included) before this request's own guarded
                # UPDATE below gets a chance to run.
                winner_resp = await client.post("/api/positions/p1/roll", json=ROLL_REQUEST)
            return await original_execute(self_session, statement, *a, **kw)

        _AsyncSession.execute = racing_execute
        try:
            loser_resp = await client.post("/api/positions/p1/roll", json=ROLL_REQUEST)
        finally:
            _AsyncSession.execute = original_execute
        assert triggered, "the guarded UPDATE never ran — test setup invalid"

        assert winner_resp.status_code == 200
        assert winner_resp.json()["rolls"] == 1
        # The loser's guarded UPDATE matched zero rows (rolls already moved
        # from 0 to 1 by the winner) — 409, not a silently-discarded roll.
        assert loser_resp.status_code == 409
        assert "concurrently" in loser_resp.json()["detail"]

        async with session_maker() as session:
            row = (await session.execute(select(PositionModel).filter_by(id="p1"))).scalar_one()
        # Exactly ONE roll's economics survive — not the loser's silently
        # overwriting or being overwritten by a mismatched partial state.
        assert row.rolls == 1
        assert row.entry_premium == pytest.approx(1.15)  # 1.0 + (1.75 − 1.60)

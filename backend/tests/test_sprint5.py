"""
test_sprint5.py — Sprint 5: Intent Journal, Post-Mortem & Ledger

Tests cover:
- POST /api/positions without journal → 422
- POST /api/positions with partial journal → 422
- POST /api/positions with full journal → 201
- POST /api/positions/{id}/close: happy path WIN/LOSS/BREAKEVEN
- Close already-closed position → 400
- Close unknown position → 404
- lesson_tags stored and returned correctly
- user_override_logged = True when warnings_acknowledged non-empty
- GET /api/positions/post-mortems empty initially, populated after close
- GET /api/positions/{id}/post-mortem happy path and 404
- GET /api/opportunity/ledger empty initially
- POST /api/opportunity/ledger creates accepted and bypassed records
- PATCH /api/opportunity/ledger/{id} updates outcome_if_taken
- PATCH /api/opportunity/ledger/{id} unknown → 404
- GET /api/performance/diagnostics empty with no closed positions
- GET /api/performance/diagnostics win_rate and profit_factor computed correctly
- Diagnostics benchmarks stub note is non-empty
- Diagnostics groups by playbook_id/version correctly
"""

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import SEED_PLAYBOOKS, SEED_PORTFOLIO_CONFIG, get_db
from backend.main import app
from backend.models import (
    Base,
    MarketStateModel,
    PlaybookDefinitionModel,
    PortfolioConfigModel,
)

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    session_maker = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    async with session_maker() as session:
        yield session


@pytest_asyncio.fixture
async def seeded_db(db_session):
    """Seed config, market state, and one playbook for API tests."""
    db_session.add(
        PortfolioConfigModel(
            id=1,
            account=SEED_PORTFOLIO_CONFIG["account"],
            risk_profile=SEED_PORTFOLIO_CONFIG["risk_profile"],
            portfolio_greek_limits=SEED_PORTFOLIO_CONFIG["portfolio_greek_limits"],
        )
    )
    db_session.add(
        MarketStateModel(
            id=1,
            current_regime="CALM_BULL",
            spy_price=758.0,
            spy_sma20=750.0,
            vix_close=14.5,
            underlying_ivrs={"SPY": 25.0},
            spy_daily_return=0.005,
            catalyst_dates=[],
            regime_scores={},
        )
    )
    for pb in SEED_PLAYBOOKS:
        db_session.add(
            PlaybookDefinitionModel(
                id=pb["id"],
                version=pb["version"],
                name=pb["name"],
                underlying_ticker=pb["underlying_ticker"],
                strategy_type=pb["strategy_type"],
                entry_filters=pb["entry_filters"],
                execution_specs=pb["execution_specs"],
                exit_rules=pb["exit_rules"],
            )
        )
    await db_session.commit()
    return db_session


@pytest_asyncio.fixture
async def api_client(db_engine):
    session_maker = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)

    async def override_get_db():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def api_client_seeded(seeded_db, api_client):
    return api_client


VALID_JOURNAL = {
    "core_thesis_rationale": "Strong trend with elevated vol",
    "structural_invalidation": "SPY breaks below SMA20",
    "expected_underlying_move_pct": 2.5,
    "pre_trade_emotional_state": "Calm",
    "pre_trade_confidence_rating": 4,
}

VALID_POSITION = {
    "id": "test_pos_001",
    "underlying": "SPY",
    "strategy_type": "LONG_STRADDLE",
    "execution_mode": "PAPER",
    "legs": [
        {
            "option_type": "CALL",
            "direction": "LONG",
            "strike": 759.0,
            "expiration": "2026-07-18",
            "delta": 0.5,
            "theta": -0.1,
            "vega": 0.2,
            "gamma": 0.05,
        },
        {
            "option_type": "PUT",
            "direction": "LONG",
            "strike": 759.0,
            "expiration": "2026-07-18",
            "delta": -0.5,
            "theta": -0.1,
            "vega": 0.2,
            "gamma": 0.05,
        },
    ],
    "entry_date": "2026-06-09",
    "expiration_date": "2026-07-18",
    "entry_premium": 20.0,
    "premium_direction": "DEBIT",
    "current_value_per_share": 20.0,
    "contracts": 1,
    "max_profit": 999999.0,
    "max_loss": 20.0,
    "profit_target_per_share": 40.0,
    "loss_limit_per_share": 10.0,
    "notes": "Test position",
    "rolls": 0,
    "status": "OPEN",
    "journal": VALID_JOURNAL,
    "warnings_acknowledged": [],
}


# ---------------------------------------------------------------------------
# Journal enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_position_without_journal_returns_422(api_client_seeded):
    pos = {k: v for k, v in VALID_POSITION.items() if k != "journal"}
    resp = await api_client_seeded.post("/api/positions", json=pos)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_position_partial_journal_returns_422(api_client_seeded):
    pos = dict(VALID_POSITION)
    pos["journal"] = {
        "core_thesis_rationale": "Some rationale",
        # missing: structural_invalidation, expected_underlying_move_pct, pre_trade_emotional_state, pre_trade_confidence_rating
    }
    resp = await api_client_seeded.post("/api/positions", json=pos)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_position_with_valid_journal_succeeds(api_client_seeded):
    resp = await api_client_seeded.post("/api/positions", json=VALID_POSITION)
    assert resp.status_code == 200
    data = resp.json()
    assert data["journal"]["core_thesis_rationale"] == VALID_JOURNAL["core_thesis_rationale"]
    assert data["warnings_acknowledged"] == []


@pytest.mark.asyncio
async def test_create_position_stores_warnings_acknowledged(api_client_seeded):
    pos = dict(VALID_POSITION)
    pos["id"] = "test_pos_override"
    pos["warnings_acknowledged"] = ["REGIME_CONSISTENCY", "BREAK_EVEN_REALISM"]
    resp = await api_client_seeded.post("/api/positions", json=pos)
    assert resp.status_code == 200
    assert set(resp.json()["warnings_acknowledged"]) == {
        "REGIME_CONSISTENCY",
        "BREAK_EVEN_REALISM",
    }


# ---------------------------------------------------------------------------
# Close position — P&L computation and outcome
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_position_debit_win(api_client_seeded):
    await api_client_seeded.post("/api/positions", json=VALID_POSITION)
    resp = await api_client_seeded.post(
        "/api/positions/test_pos_001/close",
        json={
            "current_value_per_share": 30.0,  # profit: (30-20)*100*1 = +$1000
            "exit_trigger": "PROFIT_TARGET",
            "actual_underlying_move_pct": 3.0,
            "lesson_tags": ["clean-exit"],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["outcome"] == "WIN"
    assert abs(data["realized_pnl"] - 1000.0) < 0.01
    assert data["exit_trigger"] == "PROFIT_TARGET"
    assert data["lesson_tags"] == ["clean-exit"]
    assert data["user_override_logged"] is False


@pytest.mark.asyncio
async def test_close_position_debit_loss(api_client_seeded):
    pos = dict(VALID_POSITION)
    pos["id"] = "test_pos_loss"
    await api_client_seeded.post("/api/positions", json=pos)
    resp = await api_client_seeded.post(
        "/api/positions/test_pos_loss/close",
        json={
            "current_value_per_share": 10.0,  # loss: (10-20)*100*1 = -$1000
            "exit_trigger": "LOSS_LIMIT",
            "actual_underlying_move_pct": -1.2,
            "lesson_tags": [],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["outcome"] == "LOSS"
    assert abs(data["realized_pnl"] - (-1000.0)) < 0.01


@pytest.mark.asyncio
async def test_close_on_executor_book_requires_divergence_acknowledgement(api_client_seeded):
    # H6 (#279): this endpoint is bookkeeping-only; an executor-book position
    # has real broker legs, so closing it here guarantees drift. Refuse
    # without an explicit acknowledgement; allow with it.
    pos = dict(VALID_POSITION)
    pos["id"] = "test_pos_executor"
    pos["book_id"] = "B01"
    await api_client_seeded.post("/api/positions", json=pos)
    close_req = {
        "current_value_per_share": 30.0,
        "exit_trigger": "MANUAL",
        "actual_underlying_move_pct": 0.0,
        "lesson_tags": [],
    }
    resp = await api_client_seeded.post("/api/positions/test_pos_executor/close", json=close_req)
    assert resp.status_code == 409
    assert "reconciliation drift" in resp.json()["detail"]
    resp = await api_client_seeded.post(
        "/api/positions/test_pos_executor/close", json={**close_req, "acknowledge_broker_divergence": True}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_close_position_credit_win(api_client_seeded):
    credit_pos = dict(VALID_POSITION)
    credit_pos["id"] = "test_pos_credit"
    credit_pos["strategy_type"] = "IRON_CONDOR"
    credit_pos["premium_direction"] = "CREDIT"
    credit_pos["legs"] = [
        {
            "option_type": "PUT",
            "direction": "SHORT",
            "strike": 740.0,
            "expiration": "2026-07-18",
            "delta": -0.16,
            "theta": 0.05,
            "vega": -0.1,
            "gamma": -0.01,
        },
        {
            "option_type": "PUT",
            "direction": "LONG",
            "strike": 735.0,
            "expiration": "2026-07-18",
            "delta": -0.05,
            "theta": 0.02,
            "vega": -0.05,
            "gamma": -0.005,
        },
        {
            "option_type": "CALL",
            "direction": "SHORT",
            "strike": 775.0,
            "expiration": "2026-07-18",
            "delta": 0.16,
            "theta": 0.05,
            "vega": -0.1,
            "gamma": -0.01,
        },
        {
            "option_type": "CALL",
            "direction": "LONG",
            "strike": 780.0,
            "expiration": "2026-07-18",
            "delta": 0.05,
            "theta": 0.02,
            "vega": -0.05,
            "gamma": -0.005,
        },
    ]
    await api_client_seeded.post("/api/positions", json=credit_pos)
    # Credit win: entry_premium=20 (credit received), current_value=8 (cheaper to close)
    # pnl = (20 - 8) * 100 * 1 = +$1200
    resp = await api_client_seeded.post(
        "/api/positions/test_pos_credit/close",
        json={
            "current_value_per_share": 8.0,
            "exit_trigger": "PROFIT_TARGET",
            "actual_underlying_move_pct": 0.0,
            "lesson_tags": [],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["outcome"] == "WIN"
    assert data["realized_pnl"] > 0


@pytest.mark.asyncio
async def test_close_position_breakeven(api_client_seeded):
    pos = dict(VALID_POSITION)
    pos["id"] = "test_pos_be"
    await api_client_seeded.post("/api/positions", json=pos)
    # Exactly breakeven: current_value == entry_premium
    resp = await api_client_seeded.post(
        "/api/positions/test_pos_be/close",
        json={
            "current_value_per_share": 20.0,
            "exit_trigger": "MANUAL",
            "actual_underlying_move_pct": 0.0,
            "lesson_tags": [],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "BREAKEVEN"
    assert abs(resp.json()["realized_pnl"]) < 0.01


@pytest.mark.asyncio
async def test_close_position_sets_status_closed(api_client_seeded):
    pos = dict(VALID_POSITION)
    pos["id"] = "test_pos_statuscheck"
    await api_client_seeded.post("/api/positions", json=pos)
    await api_client_seeded.post(
        "/api/positions/test_pos_statuscheck/close",
        json={
            "current_value_per_share": 25.0,
            "exit_trigger": "MANUAL",
            "actual_underlying_move_pct": 1.0,
            "lesson_tags": [],
        },
    )
    resp = await api_client_seeded.get("/api/positions/test_pos_statuscheck")
    assert resp.json()["status"] == "CLOSED"


@pytest.mark.asyncio
async def test_close_already_closed_returns_400(api_client_seeded):
    pos = dict(VALID_POSITION)
    pos["id"] = "test_pos_double_close"
    await api_client_seeded.post("/api/positions", json=pos)
    close_req = {
        "current_value_per_share": 25.0,
        "exit_trigger": "MANUAL",
        "actual_underlying_move_pct": 1.0,
        "lesson_tags": [],
    }
    await api_client_seeded.post("/api/positions/test_pos_double_close/close", json=close_req)
    resp = await api_client_seeded.post("/api/positions/test_pos_double_close/close", json=close_req)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_close_unknown_position_returns_404(api_client_seeded):
    resp = await api_client_seeded.post(
        "/api/positions/nonexistent_id/close",
        json={
            "current_value_per_share": 10.0,
            "exit_trigger": "MANUAL",
            "actual_underlying_move_pct": 0.0,
            "lesson_tags": [],
        },
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_user_override_logged_when_warnings_acknowledged(api_client_seeded):
    pos = dict(VALID_POSITION)
    pos["id"] = "test_pos_override_flag"
    pos["warnings_acknowledged"] = ["REGIME_CONSISTENCY"]
    await api_client_seeded.post("/api/positions", json=pos)
    resp = await api_client_seeded.post(
        "/api/positions/test_pos_override_flag/close",
        json={
            "current_value_per_share": 25.0,
            "exit_trigger": "MANUAL",
            "actual_underlying_move_pct": 1.0,
            "lesson_tags": [],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["user_override_logged"] is True


# ---------------------------------------------------------------------------
# Post-mortem retrieval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_post_mortems_empty_initially(api_client_seeded):
    resp = await api_client_seeded.get("/api/positions/post-mortems")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_get_post_mortems_populated_after_close(api_client_seeded):
    pos = dict(VALID_POSITION)
    pos["id"] = "test_pos_pm_list"
    await api_client_seeded.post("/api/positions", json=pos)
    await api_client_seeded.post(
        "/api/positions/test_pos_pm_list/close",
        json={
            "current_value_per_share": 30.0,
            "exit_trigger": "PROFIT_TARGET",
            "actual_underlying_move_pct": 2.0,
            "lesson_tags": ["test"],
        },
    )
    resp = await api_client_seeded.get("/api/positions/post-mortems")
    assert resp.status_code == 200
    pms = resp.json()
    assert len(pms) == 1
    assert pms[0]["position_id"] == "test_pos_pm_list"


@pytest.mark.asyncio
async def test_get_post_mortem_by_position_id(api_client_seeded):
    pos = dict(VALID_POSITION)
    pos["id"] = "test_pos_pm_by_id"
    await api_client_seeded.post("/api/positions", json=pos)
    await api_client_seeded.post(
        "/api/positions/test_pos_pm_by_id/close",
        json={
            "current_value_per_share": 15.0,
            "exit_trigger": "LOSS_LIMIT",
            "actual_underlying_move_pct": -2.0,
            "lesson_tags": [],
        },
    )
    resp = await api_client_seeded.get("/api/positions/test_pos_pm_by_id/post-mortem")
    assert resp.status_code == 200
    assert resp.json()["position_id"] == "test_pos_pm_by_id"
    assert resp.json()["outcome"] == "LOSS"


@pytest.mark.asyncio
async def test_get_post_mortem_unknown_position_returns_404(api_client_seeded):
    resp = await api_client_seeded.get("/api/positions/nonexistent/post-mortem")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Opportunity ledger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_opportunity_ledger_empty_initially(api_client_seeded):
    resp = await api_client_seeded.get("/api/opportunity/ledger")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_create_accepted_opportunity_record(api_client_seeded):
    resp = await api_client_seeded.post(
        "/api/opportunity/ledger",
        json={
            "id": "",
            "playbook_id": "spy_iron_condor_v1",
            "playbook_version": "1.0",
            "generated_at": "2026-06-09T10:00:00",
            "accepted": True,
            "outcome_if_taken": None,
            "bypass_reason": None,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["playbook_id"] == "spy_iron_condor_v1"
    assert data["accepted"] is True
    assert data["id"] != ""


@pytest.mark.asyncio
async def test_create_bypassed_opportunity_record(api_client_seeded):
    resp = await api_client_seeded.post(
        "/api/opportunity/ledger",
        json={
            "id": "",
            "playbook_id": "spy_bull_call_spread_v1",
            "playbook_version": "1.0",
            "generated_at": "2026-06-09T10:00:00",
            "accepted": False,
            "outcome_if_taken": None,
            "bypass_reason": "IVR below minimum threshold",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["accepted"] is False
    assert data["bypass_reason"] == "IVR below minimum threshold"


@pytest.mark.asyncio
async def test_list_opportunity_records(api_client_seeded):
    for i in range(3):
        await api_client_seeded.post(
            "/api/opportunity/ledger",
            json={
                "id": "",
                "playbook_id": f"playbook_{i}",
                "playbook_version": "1.0",
                "generated_at": "2026-06-09T10:00:00",
                "accepted": i % 2 == 0,
                "outcome_if_taken": None,
                "bypass_reason": None,
            },
        )
    resp = await api_client_seeded.get("/api/opportunity/ledger")
    assert resp.status_code == 200
    assert len(resp.json()) == 3


@pytest.mark.asyncio
async def test_patch_opportunity_outcome(api_client_seeded):
    create_resp = await api_client_seeded.post(
        "/api/opportunity/ledger",
        json={
            "id": "",
            "playbook_id": "spy_iron_condor_v1",
            "playbook_version": "1.0",
            "generated_at": "2026-06-09T10:00:00",
            "accepted": False,
            "outcome_if_taken": None,
            "bypass_reason": "Filter not met",
        },
    )
    record_id = create_resp.json()["id"]
    patch_resp = await api_client_seeded.patch(
        f"/api/opportunity/ledger/{record_id}",
        json={"outcome_if_taken": 350.0},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["outcome_if_taken"] == 350.0


@pytest.mark.asyncio
async def test_patch_opportunity_unknown_returns_404(api_client_seeded):
    resp = await api_client_seeded.patch(
        "/api/opportunity/ledger/nonexistent-id",
        json={"outcome_if_taken": 100.0},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Performance diagnostics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diagnostics_empty_with_no_closed_positions(api_client_seeded):
    resp = await api_client_seeded.get("/api/performance/diagnostics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["playbook_metrics"] == []
    assert data["benchmarks"]["note"] != ""


@pytest.mark.asyncio
async def test_diagnostics_benchmarks_stub_note_non_empty(api_client_seeded):
    resp = await api_client_seeded.get("/api/performance/diagnostics")
    note = resp.json()["benchmarks"]["note"]
    assert isinstance(note, str) and len(note) > 0


@pytest.mark.asyncio
async def test_diagnostics_win_rate_computed_correctly(api_client_seeded):
    # Close 2 winning and 1 losing position under the same playbook
    for i, (close_val, exp_outcome) in enumerate([(30.0, "WIN"), (30.0, "WIN"), (10.0, "LOSS")]):
        pos = dict(VALID_POSITION)
        pos["id"] = f"test_diag_pos_{i}"
        pos["playbook_id"] = "spy_long_straddle_v1"
        pos["playbook_version"] = "1.0"
        await api_client_seeded.post("/api/positions", json=pos)
        await api_client_seeded.post(
            f"/api/positions/test_diag_pos_{i}/close",
            json={
                "current_value_per_share": close_val,
                "exit_trigger": "MANUAL",
                "actual_underlying_move_pct": 1.0,
                "lesson_tags": [],
            },
        )

    resp = await api_client_seeded.get("/api/performance/diagnostics")
    assert resp.status_code == 200
    metrics = resp.json()["playbook_metrics"]
    assert len(metrics) == 1
    m = metrics[0]
    assert m["total_trades"] == 3
    assert abs(m["win_rate"] - (2 / 3)) < 0.001


@pytest.mark.asyncio
async def test_diagnostics_profit_factor_computed_correctly(api_client_seeded):
    # 1 win of $1000, 1 loss of $500
    for i, close_val in enumerate([30.0, 15.0]):
        pos = dict(VALID_POSITION)
        pos["id"] = f"test_pf_pos_{i}"
        pos["playbook_id"] = "spy_long_straddle_v1"
        pos["playbook_version"] = "1.0"
        await api_client_seeded.post("/api/positions", json=pos)
        await api_client_seeded.post(
            f"/api/positions/test_pf_pos_{i}/close",
            json={
                "current_value_per_share": close_val,
                "exit_trigger": "MANUAL",
                "actual_underlying_move_pct": 0.5,
                "lesson_tags": [],
            },
        )

    resp = await api_client_seeded.get("/api/performance/diagnostics")
    metrics = resp.json()["playbook_metrics"]
    m = metrics[0]
    # win pnl = (30-20)*100 = 1000, loss pnl = (15-20)*100 = -500
    # profit_factor = 1000 / 500 = 2.0
    assert abs(m["profit_factor"] - 2.0) < 0.01


@pytest.mark.asyncio
async def test_diagnostics_groups_by_playbook_version(api_client_seeded):
    # Close positions under two different playbooks
    for pb_id, pos_id in [
        ("spy_long_straddle_v1", "test_group_pos_a"),
        ("spy_iron_condor_v1", "test_group_pos_b"),
    ]:
        pos = dict(VALID_POSITION)
        pos["id"] = pos_id
        pos["playbook_id"] = pb_id
        pos["playbook_version"] = "1.0"
        if pb_id == "spy_iron_condor_v1":
            pos["strategy_type"] = "IRON_CONDOR"
            pos["premium_direction"] = "CREDIT"
            pos["legs"] = [
                {
                    "option_type": "PUT",
                    "direction": "SHORT",
                    "strike": 740.0,
                    "expiration": "2026-07-18",
                    "delta": -0.16,
                    "theta": 0.05,
                    "vega": -0.1,
                    "gamma": -0.01,
                },
                {
                    "option_type": "PUT",
                    "direction": "LONG",
                    "strike": 735.0,
                    "expiration": "2026-07-18",
                    "delta": -0.05,
                    "theta": 0.02,
                    "vega": -0.05,
                    "gamma": -0.005,
                },
                {
                    "option_type": "CALL",
                    "direction": "SHORT",
                    "strike": 775.0,
                    "expiration": "2026-07-18",
                    "delta": 0.16,
                    "theta": 0.05,
                    "vega": -0.1,
                    "gamma": -0.01,
                },
                {
                    "option_type": "CALL",
                    "direction": "LONG",
                    "strike": 780.0,
                    "expiration": "2026-07-18",
                    "delta": 0.05,
                    "theta": 0.02,
                    "vega": -0.05,
                    "gamma": -0.005,
                },
            ]
        await api_client_seeded.post("/api/positions", json=pos)
        await api_client_seeded.post(
            f"/api/positions/{pos_id}/close",
            json={
                "current_value_per_share": 25.0,
                "exit_trigger": "MANUAL",
                "actual_underlying_move_pct": 0.5,
                "lesson_tags": [],
            },
        )

    resp = await api_client_seeded.get("/api/performance/diagnostics")
    assert resp.status_code == 200
    metrics = resp.json()["playbook_metrics"]
    playbook_ids = {m["playbook_id"] for m in metrics}
    assert "spy_long_straddle_v1" in playbook_ids
    assert "spy_iron_condor_v1" in playbook_ids
    assert len(metrics) == 2


@pytest.mark.asyncio
async def test_diagnostics_gates_annualized_metrics_below_sample_size(api_client_seeded):
    pos = dict(VALID_POSITION)
    pos["id"] = "test_na_pos"
    pos["playbook_id"] = "spy_long_straddle_v1"
    pos["playbook_version"] = "1.0"
    await api_client_seeded.post("/api/positions", json=pos)
    await api_client_seeded.post(
        "/api/positions/test_na_pos/close",
        json={
            "current_value_per_share": 25.0,
            "exit_trigger": "MANUAL",
            "actual_underlying_move_pct": 1.0,
            "lesson_tags": [],
        },
    )
    resp = await api_client_seeded.get("/api/performance/diagnostics")
    m = resp.json()["playbook_metrics"][0]
    # One closed trade is far below the sample gate (#9): annualized figures
    # must be null, never fabricated; drawdown is an honest dollar figure.
    assert m["cagr"] is None
    assert m["sharpe"] is None
    assert m["max_drawdown"] is not None


# ---------------------------------------------------------------------------
# Options Pricing Live Refresh & OCC symbol helper tests
# ---------------------------------------------------------------------------


def test_format_occ_symbol():
    from backend.market_data import format_occ_symbol

    # 3-char ticker
    sym = format_occ_symbol("SPY", "2026-06-18", "CALL", 759.0)
    assert sym == "SPY260618C00759000"

    # 4-char ticker
    sym2 = format_occ_symbol("AAPL", "2026-07-20", "PUT", 182.5)
    assert sym2 == "AAPL260720P00182500"


@pytest.mark.asyncio
async def test_fetch_options_latest_quotes_gateway_unreachable():
    from unittest.mock import patch

    from backend.market_data import fetch_options_latest_quotes

    with patch("backend.market_data._run_ib", side_effect=ConnectionRefusedError("gateway down")):
        res = fetch_options_latest_quotes(["SPY260618C00759000"])
    assert res == {}


@pytest.mark.asyncio
async def test_fetch_options_latest_quotes_mocked():
    from unittest.mock import patch

    from backend.market_data import fetch_options_latest_quotes

    with patch("backend.market_data._run_ib", return_value={"SPY260618C00759000": 10.25}):
        res = fetch_options_latest_quotes(["SPY260618C00759000"])
    assert res == {"SPY260618C00759000": 10.25}


def test_fetch_options_latest_quotes_skips_invalid_symbols():
    from backend.market_data import fetch_options_latest_quotes

    # No valid OCC symbols → no gateway call at all, empty result
    assert fetch_options_latest_quotes(["NOT_AN_OCC_SYMBOL"]) == {}
    assert fetch_options_latest_quotes([]) == {}


def test_parse_occ_symbol_round_trip():
    from backend.market_data import format_occ_symbol, parse_occ_symbol

    sym = format_occ_symbol("SPY", "2026-06-18", "CALL", 759.0)
    parsed = parse_occ_symbol(sym)
    assert parsed == {"underlying": "SPY", "expiration": "20260618", "right": "C", "strike": 759.0}

    sym2 = format_occ_symbol("AAPL", "2026-07-20", "PUT", 182.5)
    parsed2 = parse_occ_symbol(sym2)
    assert parsed2 == {"underlying": "AAPL", "expiration": "20260720", "right": "P", "strike": 182.5}

    assert parse_occ_symbol("garbage") is None


@pytest.mark.asyncio
async def test_refresh_positions_endpoint_no_open(api_client_seeded):
    # No positions open initially
    resp = await api_client_seeded.post("/api/positions/refresh")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_refresh_positions_endpoint_succeeds(api_client_seeded):
    from unittest.mock import patch

    # 1. Create a position
    await api_client_seeded.post("/api/positions", json=VALID_POSITION)

    # 2. Mock fetch_options_latest_quotes
    mock_quotes = {"SPY260718C00759000": 12.00, "SPY260718P00759000": 8.00}

    # The route delegates to operator.refresh_position_values, so the quote
    # fetch is patched at the operator seam.
    with patch("backend.operator.fetch_options_latest_quotes", return_value=mock_quotes):
        resp = await api_client_seeded.post("/api/positions/refresh")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        pos = data[0]
        assert pos["id"] == "test_pos_001"
        # Debit straddle: long call (12.00) + long put (8.00) = 20.00
        assert pos["current_value_per_share"] == 20.00


@pytest.mark.asyncio
async def test_observation_without_market_state_is_404_not_fabricated(db_session, api_client):
    """#180: the observation route used to fabricate a CALM_BULL state with
    stale magic numbers when the market_state row was missing — an
    observation that looked real but wasn't. Absence is now an explicit 404
    (init_db seeds the row, so absence means an uninitialized database)."""
    db_session.add(
        PortfolioConfigModel(
            id=1,
            account=SEED_PORTFOLIO_CONFIG["account"],
            risk_profile=SEED_PORTFOLIO_CONFIG["risk_profile"],
            portfolio_greek_limits=SEED_PORTFOLIO_CONFIG["portfolio_greek_limits"],
        )
    )
    await db_session.commit()
    resp = await api_client.get("/api/portfolio/observation")
    assert resp.status_code == 404
    assert "Market state" in resp.json()["detail"]

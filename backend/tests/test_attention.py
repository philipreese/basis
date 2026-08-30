"""Tests for GET /api/attention's composition (backend/attention.py, #890).

Per-item-type coverage per DESIGN-890.md §5 step 2's verify list: one halt,
one PARTIAL order, an unresolved DRIFT run (halt + drift both present), and a
resolved DRIFT run (drift shows resolved=true/no action, halt still present
until GLOBAL is separately resumed — resolving a drift run never auto-resumes,
ADR-0008).
"""

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.attention import compose_attention
from backend.models import AttentionActionKind, Base, OrderModel, ReconciliationRunModel, TradingControlModel
from backend.tests.test_opportunity import _make_market_state, _make_portfolio_config
from backend.trading_control import ACTIVE, GLOBAL_SCOPE, HALT_ENTRIES

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def _no_sentinel(monkeypatch, tmp_path):
    # Never let a stray project-root HALT file leak a SENTINEL HaltItem into
    # these tests — same isolation test_trading_control.py already relies on.
    monkeypatch.setenv("HALT_FILE", str(tmp_path / "HALT"))


async def _compose(session: AsyncSession):
    return await compose_attention(session, _make_portfolio_config(), [], _make_market_state(), now=NOW)


def _partial_order(order_id: str = "o1", book_id: str = "B01") -> OrderModel:
    return OrderModel(
        id=order_id,
        book_id=book_id,
        order_ref=f"basis:{book_id}:{order_id}:close",
        action="CLOSE",
        combo_legs={},
        order_type="LIMIT",
        limit_price=-1.0,
        decision_midpoint=-1.0,
        status="PARTIAL",
    )


def _drift_run(
    run_id: int = 1, resolved_at: str | None = None, resolution: str | None = None
) -> ReconciliationRunModel:
    return ReconciliationRunModel(
        id=run_id,
        run_at="2026-08-29T01:00:00+00:00",
        broker_snapshot={"positions": [], "open_orders": [], "unknown_ref_exec_ids": []},
        books_expected={},
        result="DRIFT",
        drift_details=[{"kind": "GHOST_ORDER", "key": "basis:B04:o1:open", "sec_type": "ORDER"}],
        resolved_at=resolved_at,
        resolution=resolution,
    )


class TestHalts:
    @pytest.mark.asyncio
    async def test_one_halt_row_produces_one_halt_item_with_ack_action(self, session_maker):
        async with session_maker() as session:
            session.add(
                TradingControlModel(
                    scope=GLOBAL_SCOPE, state=HALT_ENTRIES, reason="manual test halt", actor="test", changed_at="t0"
                )
            )
            await session.commit()
            result = await _compose(session)
        (halt,) = result.halts
        assert halt.scope == GLOBAL_SCOPE
        assert halt.state == HALT_ENTRIES
        assert halt.action.kind == AttentionActionKind.ACK_HALT
        assert halt.action.requires_reason is True
        assert result.problem_count >= 1

    @pytest.mark.asyncio
    async def test_active_scope_produces_no_halt_item(self, session_maker):
        async with session_maker() as session:
            session.add(TradingControlModel(scope=GLOBAL_SCOPE, state=ACTIVE, reason="", actor="test", changed_at="t0"))
            await session.commit()
            result = await _compose(session)
        assert result.halts == []


class TestPartialOrders:
    @pytest.mark.asyncio
    async def test_partial_order_produces_one_partial_order_item(self, session_maker):
        async with session_maker() as session:
            session.add(_partial_order())
            await session.commit()
            result = await _compose(session)
        (item,) = result.partial_orders
        assert item.order_ref == "basis:B01:o1:close"
        assert item.book_id == "B01"
        assert item.action.kind == AttentionActionKind.RESOLVE_PARTIAL_ORDER
        assert item.action.requires_reason is True


class TestReconciliationDrift:
    @pytest.mark.asyncio
    async def test_unresolved_drift_yields_both_a_halt_and_a_drift_item(self, session_maker):
        async with session_maker() as session:
            # An unresolved DRIFT run latches a global halt in the real
            # pipeline (reconciliation.run_reconciliation) — seeded directly
            # here since this is a pure composition test, not an integration
            # test of run_reconciliation itself.
            session.add(
                TradingControlModel(
                    scope=GLOBAL_SCOPE,
                    state=HALT_ENTRIES,
                    reason="RECONCILIATION_DRIFT: 1 discrepancies (run 1)",
                    actor="reconciliation",
                    changed_at="2026-08-29T01:00:00+00:00",
                )
            )
            session.add(_drift_run())
            await session.commit()
            result = await _compose(session)
        assert len(result.halts) == 1
        assert result.reconciliation_drift is not None
        assert result.reconciliation_drift.resolved is False
        assert result.reconciliation_drift.action is not None
        assert result.reconciliation_drift.action.kind == AttentionActionKind.RESOLVE_RECONCILIATION
        assert result.reconciliation_drift.drift_summary == ["GHOST_ORDER: basis:B04:o1:open"]

    @pytest.mark.asyncio
    async def test_resolved_drift_shows_no_action_but_halt_persists_until_global_resume(self, session_maker):
        async with session_maker() as session:
            # Resolving a drift run never auto-resumes entries (ADR-0008,
            # reconciliation.resolve_reconciliation's own docstring) — the
            # GLOBAL halt row is untouched until a separate console RESUME.
            session.add(
                TradingControlModel(
                    scope=GLOBAL_SCOPE,
                    state=HALT_ENTRIES,
                    reason="RECONCILIATION_DRIFT: 1 discrepancies (run 1)",
                    actor="reconciliation",
                    changed_at="2026-08-29T01:00:00+00:00",
                )
            )
            session.add(_drift_run(resolved_at="2026-08-29T09:00:00+00:00", resolution="Explained at broker"))
            await session.commit()
            result = await _compose(session)
        assert result.reconciliation_drift is not None
        assert result.reconciliation_drift.resolved is True
        assert result.reconciliation_drift.action is None
        (halt,) = result.halts
        assert halt.scope == GLOBAL_SCOPE
        assert halt.state == HALT_ENTRIES


class TestProblemCountAndHeadline:
    @pytest.mark.asyncio
    async def test_all_clear_when_nothing_is_seeded(self, session_maker):
        async with session_maker() as session:
            result = await _compose(session)
        assert result.status == "ok"
        assert result.headline == "All clear"
        assert result.problem_count == 0

    @pytest.mark.asyncio
    async def test_acknowledge_only_items_are_not_counted(self, session_maker):
        async with session_maker() as session:
            # A resolved drift run with its halt already separately resumed:
            # nothing actionable should remain.
            session.add(TradingControlModel(scope=GLOBAL_SCOPE, state=ACTIVE, reason="", actor="test", changed_at="t0"))
            session.add(_drift_run(resolved_at="2026-08-29T09:00:00+00:00", resolution="Explained at broker"))
            await session.commit()
            result = await _compose(session)
        assert result.reconciliation_drift.resolved is True
        assert result.reconciliation_drift.action is None
        assert result.problem_count == 0
        assert result.status == "ok"

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
from backend.models import (
    AttentionActionKind,
    AuditEventModel,
    Base,
    OrderModel,
    ReconciliationRunModel,
    TradingControlModel,
)
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


def _audit_event(
    event_type: str, payload: dict, *, run_at: str = "2026-08-29T01:00:00+00:00", book_id: str | None = None
) -> AuditEventModel:
    return AuditEventModel(run_at=run_at, book_id=book_id, event_type=event_type, actor="test", payload=payload)


class TestFlexDiscrepancies:
    @pytest.mark.asyncio
    async def test_each_discrepancy_shape_extracts_exec_id_per_shape(self, session_maker):
        # #890: every discrepancy line flex_audit.py emits (flex_audit.py:207,
        # 213, 248, 257) spells an execution as "exec <id>" except
        # NO_ORDER_REFS_IN_EXPORT, which names no single execution at all.
        discrepancies = [
            "UNKNOWN_ORDER_REF basis:B01:o1:open (exec 0001.1)",
            "MISSING_FROM_LEDGER exec 0001.2 ref basis:B02:o2:close",
            "COMMISSION_MISMATCH exec 0001.3: ledger [0001.4=1.20; 0001.5=1.25] vs flex 1.30",
            "NO_ORDER_REFS_IN_EXPORT: all 3 trades lack orderRef",
        ]
        async with session_maker() as session:
            session.add(_audit_event("FLEX_AUDIT", {"discrepancies": discrepancies}))
            await session.commit()
            result = await _compose(session)
        items = result.flex_discrepancies
        assert len(items) == 4
        assert items[0].exec_id == "0001.1"
        assert items[0].action.kind == AttentionActionKind.FLEX_ACK
        assert items[1].exec_id == "0001.2"
        assert items[1].action.kind == AttentionActionKind.FLEX_ACK
        assert items[2].exec_id == "0001.3"  # the leading exec id, not a bracketed candidate
        assert items[2].action.kind == AttentionActionKind.FLEX_ACK
        assert items[3].exec_id is None  # not exec-scoped — un-ackable by design
        assert items[3].action.kind == AttentionActionKind.ACKNOWLEDGE_ONLY


class TestDeliveryGaps:
    @pytest.mark.asyncio
    async def test_digest_not_pushed_produces_one_item(self, session_maker):
        async with session_maker() as session:
            session.add(_audit_event("DIGEST_COMPOSED", {"pushed": False}))
            await session.commit()
            result = await _compose(session)
        (item,) = result.delivery_gaps
        assert item.kind == "digest"
        assert item.action.kind == AttentionActionKind.ACKNOWLEDGE_ONLY

    @pytest.mark.asyncio
    async def test_no_digest_composed_event_leaves_the_none_tristate_unflagged(self, session_maker):
        # last_digest_pushed is None (no DIGEST_COMPOSED event ever ran) —
        # distinct from a real push failure and must not surface a gap.
        async with session_maker() as session:
            result = await _compose(session)
        assert result.delivery_gaps == []


class TestBrokerErrors:
    @pytest.mark.asyncio
    async def test_known_error_code_resolves_the_needs_human_instruction(self, session_maker):
        async with session_maker() as session:
            session.add(
                _audit_event(
                    "EXECUTOR_BROKER_UNAVAILABLE",
                    {"error": "TimeoutError", "api_errors": [{"code": 10141, "message": "disclaimer"}]},
                    book_id="B01",
                )
            )
            await session.commit()
            result = await _compose(session)
        (item,) = result.broker_errors
        assert item.book_id == "B01"
        assert "paper-trading disclaimer" in item.instruction

    @pytest.mark.asyncio
    async def test_unknown_error_code_falls_back_to_the_generic_line(self, session_maker):
        async with session_maker() as session:
            session.add(
                _audit_event(
                    "EXECUTOR_BROKER_UNAVAILABLE",
                    {"error": "boom", "api_errors": [{"code": 99999, "message": "unrecognized"}]},
                    book_id="B01",
                )
            )
            await session.commit()
            result = await _compose(session)
        (item,) = result.broker_errors
        assert item.instruction == "boom"  # falls back to payload.error, not the NEEDS_HUMAN table


class TestUnresolvedUrgentEvents:
    @pytest.mark.asyncio
    async def test_urgent_event_in_the_lookback_window_appears(self, session_maker):
        async with session_maker() as session:
            session.add(_audit_event("PNL_SHOCK", {"detail": "drawdown"}, run_at="2026-08-29T01:00:00+00:00"))
            await session.commit()
            result = await _compose(session)
        (item,) = result.unresolved_urgent_events
        assert item.event_type == "PNL_SHOCK"
        assert item.action.kind == AttentionActionKind.ACKNOWLEDGE_ONLY

    @pytest.mark.asyncio
    async def test_broker_error_event_is_deduped_out_of_the_urgent_catch_all(self, session_maker):
        # EXECUTOR_BROKER_UNAVAILABLE is itself urgent-typed (digest.py's
        # URGENT_EVENT_TYPES) — it must show once as a BrokerErrorItem, not
        # a second time in the catch-all bucket.
        async with session_maker() as session:
            session.add(
                _audit_event(
                    "EXECUTOR_BROKER_UNAVAILABLE",
                    {"error": "boom", "api_errors": []},
                    run_at="2026-08-29T01:00:00+00:00",
                )
            )
            await session.commit()
            result = await _compose(session)
        assert len(result.broker_errors) == 1
        assert result.unresolved_urgent_events == []

    @pytest.mark.asyncio
    async def test_event_older_than_the_lookback_window_is_excluded(self, session_maker):
        # No resolved reconciliation run seeded -> the lookback falls back to
        # NOW - 24h (attention._urgent_lookback_since); this event is outside it.
        async with session_maker() as session:
            session.add(_audit_event("PNL_SHOCK", {"detail": "drawdown"}, run_at="2026-08-28T01:00:00+00:00"))
            await session.commit()
            result = await _compose(session)
        assert result.unresolved_urgent_events == []


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

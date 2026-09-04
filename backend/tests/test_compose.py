"""Tests for the compose functions extracted from the fat routes (#179).

These previously lived inside main.py route bodies, testable only through
HTTP. The compose interface is the test surface now — no app, no DB.
"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.models import Base, OrderModel
from backend.observation import compose_observation, in_flight_close_orders, resolve_in_flight_records
from backend.performance import compose_diagnostics
from backend.tests.test_experiment_matrix import _position
from backend.tests.test_opportunity import _make_market_state, _make_portfolio_config


def _p1_position(pos_id: str = "p1"):
    # CREDIT position, premium 1.65: profit_per_share = 1.65 - 0.10 = 1.55,
    # which clears the 50% default profit-take threshold (0.825) — P1.
    return _position(pos_id=pos_id, current=0.10)


class TestComposeObservation:
    def test_quiet_portfolio_composes_all_sections(self):
        pos = _position()
        result = compose_observation(_make_portfolio_config(), [pos], _make_market_state())
        (scanned,) = result["scanned_positions"]
        assert scanned["position_id"] == pos.id
        assert scanned["priority"]  # lifecycle scan ran
        # #479: server truth, not a frontend guess from legs[0].direction.
        assert scanned["premium_direction"] == pos.premium_direction == "CREDIT"
        assert "net_delta" in result["greeks"]
        assert result["market_state"].current_regime == "CALM_BULL"
        assert not any(w["type"].startswith("GREEK_LIMIT") for w in result["safeguards"])

    def test_greek_limit_breach_appends_critical_warning(self):
        config = _make_portfolio_config()
        config.portfolio_greek_limits.max_net_delta = 0.01
        result = compose_observation(config, [_position()], _make_market_state())
        (warning,) = [w for w in result["safeguards"] if w["type"] == "GREEK_LIMIT_DELTA"]
        assert warning["severity"] == "CRITICAL"

    def test_closed_positions_are_not_scanned_but_count_toward_greeks(self):
        closed = _position(pos_id="closed")
        closed = closed.model_copy(update={"status": "CLOSED", "book_id": "B00"})
        result = compose_observation(_make_portfolio_config(), [closed], _make_market_state())
        assert result["scanned_positions"] == []

    def test_large_executor_book_position_does_not_breach_b00s_nav(self):
        # #889: an executor book's capital-at-risk must never be judged
        # against B00's (the manual book's) small NAV.
        config = _make_portfolio_config(nav=10000.0, max_deployed_pct=85.0)
        executor_pos = _position(pos_id="executor", current=1.65).model_copy(
            update={"book_id": "B01", "max_loss": 50000.0}
        )
        result = compose_observation(config, [executor_pos], _make_market_state())
        assert not any(w["type"] == "CAPITAL_DEPLOYED" for w in result["safeguards"])
        assert result["greeks"]["net_delta"] == 0.0

    def test_actual_b00_breach_still_trips_the_safeguard(self):
        config = _make_portfolio_config(nav=10000.0, max_deployed_pct=85.0)
        b00_pos = _position(pos_id="manual", current=1.65).model_copy(update={"book_id": "B00", "max_loss": 9000.0})
        result = compose_observation(config, [b00_pos], _make_market_state())
        assert any(w["type"] == "CAPITAL_DEPLOYED" for w in result["safeguards"])


class TestComposeObservationCloseInFlight:
    """#602: a position whose close is already staged/submitted must not
    re-demand a manual close — a duplicate exit risk."""

    def test_no_close_in_flight_param_behaves_as_before(self):
        pos = _p1_position()
        result = compose_observation(_make_portfolio_config(), [pos], _make_market_state())
        (scanned,) = result["scanned_positions"]
        assert scanned["priority"] == "P1 — CLOSE NOW"
        assert scanned["close_in_flight"] is False
        assert scanned["close_in_flight_since"] is None
        assert scanned["action"] != "Close already in flight"

    def test_submitted_close_replaces_the_action_text_and_sets_the_timestamp(self):
        pos = _p1_position()
        result = compose_observation(
            _make_portfolio_config(),
            [pos],
            _make_market_state(),
            close_in_flight={
                pos.id: [{"kind": "CLOSE", "submitted_at": "2026-08-21T21:45:00+00:00", "limit_price": 1.0}]
            },
        )
        (scanned,) = result["scanned_positions"]
        assert scanned["priority"] == "P1 — CLOSE NOW"  # the math verdict is untouched
        assert scanned["close_in_flight"] is True
        assert scanned["close_in_flight_since"] == "2026-08-21T21:45:00+00:00"
        assert "already in flight" in scanned["action"]
        assert "2026-08-21T21:45:00+00:00" in scanned["action"]

    def test_staged_close_with_no_submitted_at_yet_still_marks_in_flight(self):
        # A "pending restage" (#602): STAGED, no submitted_at yet.
        pos = _p1_position()
        result = compose_observation(
            _make_portfolio_config(),
            [pos],
            _make_market_state(),
            close_in_flight={pos.id: [{"kind": "CLOSE", "submitted_at": None, "limit_price": 1.0}]},
        )
        (scanned,) = result["scanned_positions"]
        assert scanned["close_in_flight"] is True
        assert scanned["close_in_flight_since"] is None
        assert "staged" in scanned["action"].lower()

    def test_close_in_flight_for_a_different_position_does_not_affect_this_one(self):
        pos = _p1_position()
        result = compose_observation(
            _make_portfolio_config(),
            [pos],
            _make_market_state(),
            close_in_flight={"some-other-position": [{"kind": "CLOSE", "submitted_at": "t0", "limit_price": 1.0}]},
        )
        (scanned,) = result["scanned_positions"]
        assert scanned["close_in_flight"] is False

    def test_close_in_flight_does_not_alter_a_non_p1_p2_positions_action(self):
        # An OK/P3 position isn't offered a close button anyway — the action
        # override is scoped to positions the operator might actually act on.
        pos = _position(pos_id="calm", current=1.65)  # no trigger -> not P1/P2
        result = compose_observation(
            _make_portfolio_config(),
            [pos],
            _make_market_state(),
            close_in_flight={pos.id: [{"kind": "CLOSE", "submitted_at": "t0", "limit_price": 1.0}]},
        )
        (scanned,) = result["scanned_positions"]
        assert scanned["close_in_flight"] is True  # still reported truthfully
        assert "already in flight" not in scanned["action"]  # but action text is untouched


class TestComposeObservationRestingTakeProfit:
    """#967: a resting take-profit order (order_ref ending ':tp') is a
    non-terminal CLOSE-action order too, but it is NOT a lifecycle close in
    flight — it must never suppress a P1/P2 alert the way a genuine close
    does."""

    def test_resting_tp_labels_a_quiet_positions_action(self):
        pos = _position(pos_id="calm", current=1.65)  # not P1/P2
        result = compose_observation(
            _make_portfolio_config(),
            [pos],
            _make_market_state(),
            close_in_flight={pos.id: [{"kind": "TP", "submitted_at": "t0", "limit_price": 0.42}]},
        )
        (scanned,) = result["scanned_positions"]
        assert scanned["close_in_flight"] is False
        assert scanned["action"] == "Take-profit resting @ 0.42"

    def test_resting_tp_does_not_downgrade_a_p1_action(self):
        pos = _p1_position()
        result = compose_observation(
            _make_portfolio_config(),
            [pos],
            _make_market_state(),
            close_in_flight={pos.id: [{"kind": "TP", "submitted_at": "t0", "limit_price": 0.42}]},
        )
        (scanned,) = result["scanned_positions"]
        assert scanned["priority"] == "P1 — CLOSE NOW"
        assert scanned["close_in_flight"] is False
        assert scanned["action"] == "CLOSE NOW"
        assert "already in flight" not in scanned["action"]
        assert "Take-profit" not in scanned["action"]

    def test_a_genuine_close_alongside_a_resting_tp_still_suppresses_p1(self):
        pos = _p1_position()
        result = compose_observation(
            _make_portfolio_config(),
            [pos],
            _make_market_state(),
            close_in_flight={
                pos.id: [
                    {"kind": "TP", "submitted_at": "t0", "limit_price": 0.42},
                    {"kind": "CLOSE", "submitted_at": "2026-08-21T21:45:00+00:00", "limit_price": 1.0},
                ]
            },
        )
        (scanned,) = result["scanned_positions"]
        assert scanned["close_in_flight"] is True
        assert "already in flight" in scanned["action"]


class TestResolveInFlightRecords:
    def test_empty_list_returns_none_none(self):
        assert resolve_in_flight_records([]) == (None, None)

    def test_tp_only_returns_no_real_close(self):
        records = [{"kind": "TP", "submitted_at": "t0", "limit_price": 0.42}]
        real_close, tp = resolve_in_flight_records(records)
        assert real_close is None
        assert tp == records[0]

    def test_retried_close_keeps_the_earliest_submission(self):
        records = [
            {"kind": "CLOSE", "submitted_at": "2026-08-21T21:45:00+00:00", "limit_price": 1.0},
            {"kind": "CLOSE", "submitted_at": "2026-08-21T21:00:00+00:00", "limit_price": 1.1},
        ]
        real_close, tp = resolve_in_flight_records(records)
        assert real_close["submitted_at"] == "2026-08-21T21:00:00+00:00"
        assert tp is None


class TestComposeDiagnostics:
    def test_empty_inputs_yield_honest_note(self):
        result = compose_diagnostics([], {}, [], generated_at="2026-08-18T22:00:00+00:00")
        assert result.playbook_metrics == []
        assert result.benchmarks.spy_cagr is None
        assert "No benchmark data yet" in result.benchmarks.note


@pytest_asyncio.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield maker
    await engine.dispose()


def _close_order(
    order_id: str, position_id: str, status: str, submitted_at: str | None = None, order_ref: str | None = None
) -> OrderModel:
    return OrderModel(
        id=order_id,
        book_id="B00",
        position_id=position_id,
        order_ref=order_ref or f"basis:B00:{order_id}:close",
        action="CLOSE",
        combo_legs={},
        order_type="LIMIT",
        limit_price=-1.0,
        decision_midpoint=-1.0,
        status=status,
        submitted_at=submitted_at,
    )


class TestInFlightCloseOrders:
    """in_flight_close_orders (#602) — the DB half of the close-in-flight
    check; compose_observation/compose_digest just consume its output."""

    @pytest.mark.asyncio
    async def test_no_position_ids_short_circuits_without_a_query(self, session_maker):
        async with session_maker() as session:
            assert await in_flight_close_orders(session, []) == {}

    @pytest.mark.asyncio
    async def test_submitted_close_is_reported_with_its_timestamp(self, session_maker):
        async with session_maker() as session:
            session.add(_close_order("o1", "pos_1", "SUBMITTED", "2026-08-21T21:45:00+00:00"))
            await session.commit()
            result = await in_flight_close_orders(session, ["pos_1"])
        assert result == {
            "pos_1": [{"kind": "CLOSE", "submitted_at": "2026-08-21T21:45:00+00:00", "limit_price": -1.0}]
        }

    @pytest.mark.asyncio
    async def test_staged_close_with_no_submitted_at_is_still_reported(self, session_maker):
        async with session_maker() as session:
            session.add(_close_order("o1", "pos_1", "STAGED", None))
            await session.commit()
            result = await in_flight_close_orders(session, ["pos_1"])
        assert result == {"pos_1": [{"kind": "CLOSE", "submitted_at": None, "limit_price": -1.0}]}

    @pytest.mark.asyncio
    async def test_a_pending_tp_order_ref_is_reported_as_kind_tp(self, session_maker):
        # #967: the executor stages a resting take-profit with every open —
        # order_ref ending ':tp' — which must be distinguishable from a
        # genuine lifecycle close at the same action/status shape.
        async with session_maker() as session:
            session.add(_close_order("o1", "pos_1", "SUBMITTED", "t0", order_ref="basis:B00:o1:open:tp"))
            await session.commit()
            result = await in_flight_close_orders(session, ["pos_1"])
        assert result == {"pos_1": [{"kind": "TP", "submitted_at": "t0", "limit_price": -1.0}]}

    @pytest.mark.asyncio
    async def test_terminal_status_orders_are_not_in_flight(self, session_maker):
        async with session_maker() as session:
            for status in ("FILLED", "CANCELLED", "REJECTED"):
                session.add(_close_order(f"o_{status}", "pos_1", status, "t0"))
            await session.commit()
            result = await in_flight_close_orders(session, ["pos_1"])
        assert result == {}

    @pytest.mark.asyncio
    async def test_open_action_orders_are_not_treated_as_a_close(self, session_maker):
        async with session_maker() as session:
            order = _close_order("o1", "pos_1", "SUBMITTED", "t0")
            order.action = "OPEN"
            order.order_ref = "basis:B00:o1:open"
            session.add(order)
            await session.commit()
            result = await in_flight_close_orders(session, ["pos_1"])
        assert result == {}

    @pytest.mark.asyncio
    async def test_positions_not_asked_about_are_excluded_even_if_in_flight(self, session_maker):
        async with session_maker() as session:
            session.add(_close_order("o1", "pos_1", "SUBMITTED", "t0"))
            await session.commit()
            result = await in_flight_close_orders(session, ["pos_2"])
        assert result == {}

    @pytest.mark.asyncio
    async def test_a_retried_close_keeps_the_earliest_submission(self, session_maker):
        async with session_maker() as session:
            session.add(_close_order("o1", "pos_1", "REJECTED", "2026-08-21T20:00:00+00:00"))
            session.add(_close_order("o2", "pos_1", "SUBMITTED", "2026-08-21T21:45:00+00:00"))
            session.add(_close_order("o3", "pos_1", "SUBMITTED", "2026-08-21T21:00:00+00:00"))
            await session.commit()
            result = await in_flight_close_orders(session, ["pos_1"])
        # o1 is terminal (REJECTED) and excluded by the query; between the
        # two live rows in_flight_close_orders reports, resolve_in_flight_records
        # picks the earliest submission as "the" in-flight close.
        assert {r["submitted_at"] for r in result["pos_1"]} == {
            "2026-08-21T21:45:00+00:00",
            "2026-08-21T21:00:00+00:00",
        }
        real_close, tp = resolve_in_flight_records(result["pos_1"])
        assert real_close["submitted_at"] == "2026-08-21T21:00:00+00:00"
        assert tp is None

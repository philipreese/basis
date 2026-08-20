"""Tests for the Analysis read models (backend/analysis.py).

Fill quality (#242): slippage math is pinned against hand-computed
examples in both premium directions, decomposition (ladder concession +
market movement = total) holds, orders without backfilled fills are
counted as awaiting rather than silently measured at limit economics.
"""

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.analysis import fill_quality_report
from backend.database import get_db
from backend.models import Base, BookModel, FillModel, OrderModel


@pytest_asyncio.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
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


def _book(book_id: str = "B01") -> BookModel:
    return BookModel(
        id=book_id,
        name=f"lab {book_id}",
        config={},
        config_version=1,
        config_hash="cafe1234",
        starting_capital=10000.0,
        cash_balance=10000.0,
        status="ACTIVE",
        created_at="2026-08-01T00:00:00+00:00",
    )


def _order(order_id: str, book_id: str = "B01", *, action: str = "OPEN", ref_suffix: str = "open", **overrides):
    defaults: dict = {
        "id": order_id,
        "book_id": book_id,
        "position_id": None,
        "order_ref": f"basis:{book_id}:{order_id}:{ref_suffix}",
        "ib_order_id": 1,
        "ib_perm_id": 1,
        "action": action,
        "combo_legs": {"quantity": 2, "underlying": "XSP"},
        "order_type": "LIMIT",
        "limit_price": -1.20,
        "decision_midpoint": -1.30,
        "status": "FILLED",
        "submitted_at": "2026-08-19T22:45:00+00:00",
        "completed_at": "2026-08-20T13:31:00+00:00",
    }
    defaults.update(overrides)
    return OrderModel(**defaults)


def _fill(exec_id: str, order_id: str, *, side: str, price: float, qty: float = 2.0, commission: float = 1.1):
    return FillModel(
        exec_id=exec_id,
        order_id=order_id,
        book_id="B01",
        con_id=1,
        side=side,
        quantity=qty,
        price=price,
        commission=commission,
        fill_time="2026-08-20T13:31:00+00:00",
    )


class TestFillQuality:
    @pytest.mark.asyncio
    async def test_credit_spread_slippage_decomposition(self, session_maker):
        # Decided mid -1.30 (credit 1.30), posted limit -1.20 (one rung in),
        # market filled short leg 2.00 / long leg 0.85 → net -1.15 credit.
        async with session_maker() as session:
            session.add(_book())
            session.add(_order("o1"))
            session.add(_fill("e1", "o1", side="SLD", price=2.00))
            session.add(_fill("e2", "o1", side="BOT", price=0.85))
            await session.commit()
        async with session_maker() as session:
            report = await fill_quality_report(session)
        (row,) = report.rows
        assert row.net_fill_per_share == pytest.approx(-1.15)
        assert row.ladder_concession_per_share == pytest.approx(0.10)  # chose to give up 10c
        assert row.market_slippage_per_share == pytest.approx(0.05)  # market took 5c more
        assert row.total_slippage_per_share == pytest.approx(0.15)
        assert row.commissions == pytest.approx(2.2)
        # 15c/share × $100 = $15/contract — worse than the $5 haircut assumption.
        assert report.avg_slippage_per_contract == pytest.approx(15.0)
        assert report.haircut_per_contract == 5.0
        assert report.orders_analyzed == 1 and report.orders_awaiting_fills == 0

    @pytest.mark.asyncio
    async def test_close_fills_measure_against_the_sell_the_bag_convention(self, session_maker):
        # Audit II (#347): the executor stages credit buy-backs at NEGATIVE
        # limits (signed cash flow — pay 0.60), and the close SELLS the bag,
        # so IBKR reverses every leg's side: BOT the short back at 1.55, SLD
        # the long at 0.92 → raw leg net +0.63, true cash flow −0.63. The old
        # math reported +0.63 vs −0.62 → 1.25/share of phantom slippage.
        async with session_maker() as session:
            session.add(_book())
            session.add(_order("o2", action="CLOSE", ref_suffix="o2:tp", limit_price=-0.62, decision_midpoint=-0.60))
            session.add(_fill("e3", "o2", side="BOT", price=1.55))
            session.add(_fill("e4", "o2", side="SLD", price=0.92))
            await session.commit()
        async with session_maker() as session:
            report = await fill_quality_report(session)
        (row,) = report.rows
        assert row.action == "TP"
        assert row.net_fill_per_share == pytest.approx(-0.63)  # paid 0.63 — comparable to the limit
        # Worse-is-positive holds for closes: paid 3c more than decided.
        assert row.total_slippage_per_share == pytest.approx(0.03)
        assert row.market_slippage_per_share == pytest.approx(0.01)  # 1c beyond the posted limit
        assert row.ladder_concession_per_share == pytest.approx(0.02)  # chose to pay 2c over mid
        (agg,) = report.by_action
        assert agg.label == "TP" and agg.orders == 1
        assert agg.avg_slippage_per_contract == pytest.approx(3.0)

    @pytest.mark.asyncio
    async def test_debit_close_receiving_less_than_decided_is_worse(self, session_maker):
        # Selling out of a debit spread: positive limit (cash in). Legs
        # reversed: SLD the long at 1.20, BOT the short at 0.63 → received
        # 0.57 against a 0.60 decision — 3c worse, reported as +0.03.
        async with session_maker() as session:
            session.add(_book())
            session.add(_order("o2b", action="CLOSE", ref_suffix="close", limit_price=0.58, decision_midpoint=0.60))
            session.add(_fill("e3b", "o2b", side="SLD", price=1.20))
            session.add(_fill("e4b", "o2b", side="BOT", price=0.63))
            await session.commit()
        async with session_maker() as session:
            report = await fill_quality_report(session)
        (row,) = report.rows
        assert row.net_fill_per_share == pytest.approx(0.57)
        assert row.total_slippage_per_share == pytest.approx(0.03)

    @pytest.mark.asyncio
    async def test_partial_orders_are_listed_but_never_measured(self, session_maker):
        # Audit II (#347): a PARTIAL's fills cover fewer contracts than the
        # intended quantity — per-share math against full size fabricates
        # price improvement. Partials stay visible but unmeasured.
        async with session_maker() as session:
            session.add(_book())
            session.add(_order("o_part", status="PARTIAL"))  # intended 2 contracts
            session.add(_fill("e_p1", "o_part", side="SLD", price=1.30, qty=1.0))  # only 1 filled
            await session.commit()
        async with session_maker() as session:
            report = await fill_quality_report(session)
        (row,) = report.rows
        assert row.net_fill_per_share is None and row.total_slippage_per_share is None
        assert report.orders_analyzed == 0 and report.orders_awaiting_fills == 1
        assert report.avg_slippage_per_contract is None
        assert row.commissions == pytest.approx(1.1)  # what DID fill still shows its costs

    @pytest.mark.asyncio
    async def test_filled_order_without_fills_counts_as_awaiting(self, session_maker):
        async with session_maker() as session:
            session.add(_book())
            session.add(_order("o3"))
            await session.commit()
        async with session_maker() as session:
            report = await fill_quality_report(session)
        assert report.orders_analyzed == 0
        assert report.orders_awaiting_fills == 1
        assert report.avg_slippage_per_contract is None
        (row,) = report.rows
        assert row.net_fill_per_share is None and row.market_slippage_per_share is None
        # The concession we chose is known even before fills arrive.
        assert row.ladder_concession_per_share == pytest.approx(0.10)

    @pytest.mark.asyncio
    async def test_aggregates_weight_by_contracts_and_split_by_book(self, session_maker):
        async with session_maker() as session:
            session.add(_book("B01"))
            session.add(_book("B02"))
            # B01: 2 contracts at +0.10 slip; B02: 1 contract at +0.40 slip.
            session.add(_order("o4", "B01"))
            session.add(_fill("e5", "o4", side="SLD", price=1.20))  # net -1.20 vs mid -1.30
            session.add(
                _order("o5", "B02", combo_legs={"quantity": 1, "underlying": "XSP"}, order_ref="basis:B02:o5:open")
            )
            session.add(_fill("e6", "o5", side="SLD", price=0.90, qty=1.0))  # net -0.90 vs -1.30
            await session.commit()
        async with session_maker() as session:
            report = await fill_quality_report(session)
        # Weighted: (0.10×2 + 0.40×1)/3 = 0.20/share → $20/contract
        assert report.avg_slippage_per_contract == pytest.approx(20.0)
        by_book = {a.label: a for a in report.by_book}
        assert by_book["B01"].avg_slippage_per_contract == pytest.approx(10.0)
        assert by_book["B02"].avg_slippage_per_contract == pytest.approx(40.0)
        # Rows sorted worst-first.
        assert report.rows[0].book_id == "B02"

    @pytest.mark.asyncio
    async def test_zero_slippage_sorts_among_measured_rows(self, session_maker):
        # Audit II (#355): 0.0 is falsy — a perfect fill used to sort among
        # the awaiting (unmeasured) rows at the bottom.
        async with session_maker() as session:
            session.add(_book())
            session.add(_order("o_zero", decision_midpoint=-1.20))  # filled exactly at decision
            session.add(_fill("e_z", "o_zero", side="SLD", price=1.20))
            session.add(_order("o_wait", order_ref="basis:B01:o_wait:open"))  # no fills yet
            await session.commit()
        async with session_maker() as session:
            report = await fill_quality_report(session)
        assert [r.order_ref.split(":")[2] for r in report.rows] == ["o_zero", "o_wait"]
        assert report.rows[0].total_slippage_per_share == 0.0

    @pytest.mark.asyncio
    async def test_endpoint_serves_the_report(self, session_maker, client):
        resp = await client.get("/api/analysis/fill-quality")
        assert resp.status_code == 200
        body = resp.json()
        assert body["orders_analyzed"] == 0
        assert body["haircut_per_contract"] == 5.0


class TestRegimeHitRate:
    @staticmethod
    def _pos(pos_id: str, book_id: str, regime: str):
        from backend.models import PositionModel

        return PositionModel(
            id=pos_id,
            underlying="XSP",
            strategy_type="BULL_PUT_SPREAD",
            execution_mode="PAPER",
            legs=[],
            entry_date="2026-08-01",
            expiration_date="2026-09-18",
            entry_premium=1.0,
            premium_direction="CREDIT",
            current_value_per_share=0.4,
            contracts=1,
            max_profit=1.0,
            max_loss=2.0,
            notes="",
            rolls=0,
            status="CLOSED",
            journal={"entry_regime": regime} if regime else {},
            book_id=book_id,
        )

    @staticmethod
    def _pm(pm_id: str, pos_id: str, pnl: float):
        from backend.models import ClosurePostMortemModel

        return ClosurePostMortemModel(
            id=pm_id,
            position_id=pos_id,
            outcome="WIN" if pnl > 0 else "LOSS",
            realized_pnl=pnl,
            actual_underlying_move_pct=0.0,
            exit_date="2026-08-20",
            exit_trigger="PROFIT_TARGET",
            lesson_tags=[],
            user_override_logged=False,
        )

    @pytest.mark.asyncio
    async def test_groups_by_regime_and_engine_with_unknown_bucket(self, session_maker):
        from backend.analysis import regime_hit_rate_report
        from backend.models import BookModel

        async with session_maker() as session:
            session.add(
                BookModel(
                    id="B01",
                    name="V0",
                    config={"engine_variant": "V0"},
                    config_version=1,
                    config_hash="x",
                    starting_capital=10000.0,
                    cash_balance=10000.0,
                    status="ACTIVE",
                    created_at="2026-08-01T00:00:00+00:00",
                )
            )
            session.add(
                BookModel(
                    id="B02",
                    name="V1",
                    config={"engine_variant": "V1"},
                    config_version=1,
                    config_hash="x",
                    starting_capital=10000.0,
                    cash_balance=10000.0,
                    status="ACTIVE",
                    created_at="2026-08-01T00:00:00+00:00",
                )
            )
            # CALM_BULL: +50 (V0) and -20 (V1); no-regime legacy: +10.
            session.add(self._pos("p1", "B01", "CALM_BULL"))
            session.add(self._pos("p2", "B02", "CALM_BULL"))
            session.add(self._pos("p3", "B01", ""))
            session.add(self._pm("m1", "p1", 50.0))
            session.add(self._pm("m2", "p2", -20.0))
            session.add(self._pm("m3", "p3", 10.0))
            await session.commit()
        async with session_maker() as session:
            report = await regime_hit_rate_report(session)
        assert report.closed_trades == 3
        by_regime = {r.regime: r for r in report.by_regime}
        calm = by_regime["CALM_BULL"]
        assert calm.closed_trades == 2 and calm.wins == 1
        assert calm.win_rate == pytest.approx(0.5)
        assert calm.avg_pnl == pytest.approx(15.0)
        assert calm.total_pnl == pytest.approx(30.0)
        assert by_regime["UNKNOWN"].closed_trades == 1
        engine_rows = {(r.engine_variant, r.regime): r for r in report.by_engine_regime}
        assert engine_rows[("V0", "CALM_BULL")].total_pnl == pytest.approx(50.0)
        assert engine_rows[("V1", "CALM_BULL")].total_pnl == pytest.approx(-20.0)
        # Empty (engine, regime) combinations are omitted, not zero-filled.
        assert ("V1", "UNKNOWN") not in engine_rows

    @pytest.mark.asyncio
    async def test_endpoint_serves_empty_report(self, session_maker, client):
        resp = await client.get("/api/analysis/regime-hit-rate")
        assert resp.status_code == 200
        assert resp.json()["closed_trades"] == 0


class TestLeaderboard:
    def test_every_sweep_book_exists_in_the_seed_matrix(self):
        # The sweep table mirrors seeds.py (#219) — this is the enforcement
        # that a matrix change can't silently orphan a sweep point.
        from backend.analysis import KNOB_SWEEPS
        from backend.seeds import LAB_BOOKS

        seeded = {b["id"] for b in LAB_BOOKS}
        for dimension, spec in KNOB_SWEEPS:
            for book_id, _ in spec:
                assert book_id in seeded, f"{dimension}: {book_id} not in LAB_BOOKS"

    def test_sweep_hygiene(self):
        # Audit II (#355): every pairwise one-knob arm reads against B01;
        # the spreads-only delta sweep must NOT include mix-wide B01 (the
        # population confound B23/B24 exist to avoid); B30/B32 stay out
        # deliberately (different underlying / ADR-0012 insurance sleeve).
        from backend.analysis import KNOB_SWEEPS

        sweeps = dict(KNOB_SWEEPS)
        swept_books = {b for spec in sweeps.values() for b, _ in spec}
        assert {"B28", "B29", "B31"} <= swept_books
        assert not {"B30", "B32"} & swept_books
        delta_books = [b for b, _ in sweeps["Short-leg delta (spreads-only)"]]
        assert "B01" not in delta_books and delta_books == ["B23", "B24"]

    def test_sweep_verdict_directions_and_sample_gate(self):
        from backend.analysis import _sweep_verdict
        from backend.models import KnobPointSchema

        def pt(exp, n=10):
            return KnobPointSchema(book_id="B", knob_value="x", expectancy_after_haircut=exp, closed_trades=n)

        assert _sweep_verdict([pt(1.0), pt(2.0), pt(3.0)]) == "monotonic ↑"
        assert _sweep_verdict([pt(3.0), pt(1.0), pt(-2.0)]) == "monotonic ↓"
        assert _sweep_verdict([pt(1.0), pt(5.0), pt(2.0)]) == "non-monotonic"
        # One thin point silences the whole verdict.
        assert _sweep_verdict([pt(1.0), pt(2.0, n=4), pt(3.0)]) == "insufficient data"
        assert _sweep_verdict([pt(1.0), pt(None), pt(3.0)]) == "insufficient data"
        assert _sweep_verdict([pt(1.0)]) == "insufficient data"

    @pytest.mark.asyncio
    async def test_report_ranks_books_and_resolves_sweep_points(self, session_maker):
        from backend.analysis import leaderboard_report
        from backend.models import PositionModel, TradingControlModel

        async with session_maker() as session:
            for book_id in ("B01", "B07", "B25"):
                session.add(_book(book_id))
                session.add(
                    TradingControlModel(
                        scope=book_id,
                        state="ACTIVE",
                        reason="init",
                        actor="system",
                        changed_at="2026-08-01T00:00:00+00:00",
                    )
                )
            # B07 closes a winner (+$60 − $5 haircut = 55); B01 stays empty.
            session.add(
                PositionModel(
                    id="p1",
                    underlying="XSP",
                    strategy_type="BULL_PUT_SPREAD",
                    execution_mode="PAPER",
                    legs=[],
                    entry_date="2026-08-01",
                    expiration_date="2026-09-18",
                    entry_premium=1.0,
                    premium_direction="CREDIT",
                    current_value_per_share=0.4,
                    contracts=1,
                    max_profit=1.0,
                    max_loss=2.0,
                    notes="",
                    rolls=0,
                    status="CLOSED",
                    journal={},
                    book_id="B07",
                )
            )
            await session.commit()
        async with session_maker() as session:
            report = await leaderboard_report(session)
        # Measured books rank above unmeasured ones.
        assert report.ranked[0].id == "B07"
        assert report.ranked[0].expectancy_after_haircut == pytest.approx(55.0)
        dte = next(s for s in report.sweeps if s.dimension == "Target DTE")
        # Only seeded-and-present books appear as points; verdict honest.
        assert [p.book_id for p in dte.points] == ["B07", "B01", "B25"]
        assert dte.verdict == "insufficient data"

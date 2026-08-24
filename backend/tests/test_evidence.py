"""Tests for the evidence verdict report (backend/evidence.py, #716).

Pins the verdict function against fixture ledgers: 'insufficient' as the
default with no evidence, the pooled math (net profit, CI, drawdown,
worst loss) against hand-computed fixtures, and each verdict tier reached
only through the EXISTING machinery it composes (Live Gate eligibility,
the null-drill percentile when supplied) — never a new threshold.
"""

from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import get_db
from backend.empirical_null_drill import BookNullComparison, NullDrillReport
from backend.evidence import EVIDENCE_VERDICT_POLICY_VERSION, evidence_verdict_report
from backend.models import AuditEventModel, Base, BookModel, ClosurePostMortemModel, PositionModel

NOW = datetime(2026, 8, 24, 22, 0, tzinfo=UTC)


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


def _book(book_id: str = "B01", status: str = "ACTIVE", created_at: str = "2026-04-01T00:00:00+00:00") -> BookModel:
    return BookModel(
        id=book_id,
        name=f"lab {book_id}",
        config={},
        config_version=1,
        config_hash="cafe1234",
        starting_capital=10000.0,
        cash_balance=10000.0,
        status=status,
        created_at=created_at,
    )


_POS_SEQ = iter(range(10_000))


def _position(book_id: str, entry: float, exit_value: float, **overrides) -> PositionModel:
    defaults: dict = {
        "id": f"p{next(_POS_SEQ)}",
        "underlying": "XSP",
        "strategy_type": "BULL_PUT_SPREAD",
        "execution_mode": "PAPER",
        "legs": [],
        "entry_date": "2026-08-01",
        "expiration_date": "2026-09-18",
        "entry_premium": entry,
        "premium_direction": "CREDIT",
        "current_value_per_share": exit_value,
        "contracts": 1,
        "max_profit": entry,
        "max_loss": 3.0 - entry,
        "notes": "",
        "rolls": 0,
        "status": "CLOSED",
        "journal": {},
        "book_id": book_id,
    }
    defaults.update(overrides)
    return PositionModel(**defaults)


def _pm(pm_id: str, pos_id: str, exit_date: str = "2026-08-10") -> ClosurePostMortemModel:
    return ClosurePostMortemModel(
        id=pm_id,
        position_id=pos_id,
        outcome="WIN",
        realized_pnl=0.0,
        actual_underlying_move_pct=0.0,
        exit_date=exit_date,
        exit_trigger="PROFIT_TARGET",
        lesson_tags=[],
        user_override_logged=False,
    )


async def _run(maker, **kwargs):
    async with maker() as session:
        return await evidence_verdict_report(session, now=NOW, **kwargs)


class TestInsufficientIsTheDefault:
    @pytest.mark.asyncio
    async def test_empty_ledger_is_insufficient(self, session_maker):
        report = await _run(session_maker)
        assert report.verdict == "insufficient"
        assert report.closed_trades == 0
        assert report.expected_net_profit is None
        assert report.expected_net_profit_ci_low is None
        assert report.max_drawdown == 0.0
        assert report.worst_observed_loss == 0.0
        assert report.books_raced == 0
        assert report.variants_tested == 0
        assert report.variants_abandoned == 0
        assert report.policy_version == EVIDENCE_VERDICT_POLICY_VERSION
        assert report.evidence_through == report.as_of  # no explicit cutoff given

    @pytest.mark.asyncio
    async def test_some_closed_trades_but_no_book_clears_the_gate_stays_insufficient(self, session_maker):
        async with session_maker() as session:
            session.add(_book())
            pos = _position("B01", entry=1.0, exit_value=0.5)
            session.add(pos)
            session.add(_pm("pm1", pos.id))
            await session.commit()
        report = await _run(session_maker)
        assert report.verdict == "insufficient"
        assert report.closed_trades == 1


class TestPooledMath:
    @pytest.mark.asyncio
    async def test_net_profit_and_worst_loss_against_a_hand_computed_fixture(self, session_maker):
        # Winner: +50 raw, -5 haircut = 45. Loser: -40 raw, -5 haircut = -45.
        async with session_maker() as session:
            session.add(_book())
            win = _position("B01", entry=1.0, exit_value=0.5, entry_date="2026-08-01")
            loss = _position("B01", entry=1.0, exit_value=1.4, entry_date="2026-08-02")
            session.add_all([win, loss])
            session.add(_pm("pm_win", win.id, exit_date="2026-08-05"))
            session.add(_pm("pm_loss", loss.id, exit_date="2026-08-10"))
            await session.commit()
        report = await _run(session_maker)
        assert report.closed_trades == 2
        assert report.expected_net_profit == pytest.approx(0.0)  # 45 + (-45)
        assert report.worst_observed_loss == pytest.approx(-45.0)

    @pytest.mark.asyncio
    async def test_drawdown_walks_the_pooled_lab_wide_equity_curve_in_exit_order(self, session_maker):
        # +45, -45, +45 in exit-date order -> peak 45, trough 0, drawdown 45.
        async with session_maker() as session:
            session.add(_book())
            a = _position("B01", entry=1.0, exit_value=0.5, entry_date="2026-08-01")  # +45 haircut
            b = _position("B01", entry=1.0, exit_value=1.4, entry_date="2026-08-02")  # -45 haircut
            c = _position("B01", entry=1.0, exit_value=0.5, entry_date="2026-08-03")  # +45 haircut
            session.add_all([a, b, c])
            session.add(_pm("pm_a", a.id, exit_date="2026-08-05"))
            session.add(_pm("pm_b", b.id, exit_date="2026-08-10"))
            session.add(_pm("pm_c", c.id, exit_date="2026-08-15"))
            await session.commit()
        report = await _run(session_maker)
        assert report.max_drawdown == pytest.approx(45.0)

    @pytest.mark.asyncio
    async def test_evidence_through_cutoff_excludes_later_trades_reproducibly(self, session_maker):
        async with session_maker() as session:
            session.add(_book())
            early = _position("B01", entry=1.0, exit_value=0.5, entry_date="2026-08-01")
            late = _position("B01", entry=1.0, exit_value=0.5, entry_date="2026-08-20")
            session.add_all([early, late])
            session.add(_pm("pm_early", early.id, exit_date="2026-08-05"))
            session.add(_pm("pm_late", late.id, exit_date="2026-08-22"))
            await session.commit()
        cutoff_report = await _run(session_maker, evidence_through="2026-08-10")
        full_report = await _run(session_maker)
        assert cutoff_report.closed_trades == 1
        assert full_report.closed_trades == 2
        assert cutoff_report.evidence_through == "2026-08-10"
        # Reproducibility: re-running with the SAME cutoff is byte-identical.
        again = await _run(session_maker, evidence_through="2026-08-10")
        assert again.model_dump(exclude={"as_of"}) == cutoff_report.model_dump(exclude={"as_of"})

    @pytest.mark.asyncio
    async def test_b00_and_b32_are_excluded_from_pooled_evidence(self, session_maker):
        async with session_maker() as session:
            session.add(_book("B00"))
            session.add(_book("B32"))
            for book_id in ("B00", "B32"):
                pos = _position(book_id, entry=1.0, exit_value=0.5)
                session.add(pos)
                session.add(_pm(f"pm_{book_id}", pos.id))
            await session.commit()
        report = await _run(session_maker)
        assert report.closed_trades == 0
        assert report.expected_net_profit is None


class TestBookVariants:
    @pytest.mark.asyncio
    async def test_counts_raced_and_abandoned_variants_excluding_legacy_and_reserved(self, session_maker):
        async with session_maker() as session:
            session.add(_book("B00", status="LEGACY"))
            session.add(_book("B01", status="ACTIVE"))
            session.add(_book("B02", status="RETIRED"))
            session.add(_book("B03", status="RESERVED"))
            await session.commit()
        report = await _run(session_maker)
        assert report.books_raced == 2  # B01 + B02, not B00 (legacy) or B03 (never raced)
        assert report.variants_tested == 2
        assert report.variants_abandoned == 1  # B02


class TestAnomalyAndBreachCounts:
    @pytest.mark.asyncio
    async def test_counts_envelope_breaches_and_urgent_anomaly_events(self, session_maker):
        async with session_maker() as session:
            session.add(_book())
            session.add(
                AuditEventModel(
                    run_at="2026-08-05T00:00:00+00:00",
                    book_id="B01",
                    event_type="ENVELOPE_BREACH_POSTHOC",
                    actor="anomaly",
                    payload={},
                )
            )
            session.add(
                AuditEventModel(
                    run_at="2026-08-06T00:00:00+00:00",
                    book_id="B01",
                    event_type="ORDER_REJECTED",  # also urgent, per digest.py
                    actor="executor",
                    payload={},
                )
            )
            session.add(
                AuditEventModel(
                    run_at="2026-08-07T00:00:00+00:00",
                    book_id="B01",
                    event_type="ORDER_SUBMITTED",  # routine, not urgent
                    actor="executor",
                    payload={},
                )
            )
            await session.commit()
        report = await _run(session_maker)
        assert report.envelope_breaches == 1
        assert report.anomaly_events == 2  # the breach itself + the rejection


class TestVerdictComposition:
    @pytest.mark.asyncio
    async def test_promising_when_a_book_clears_the_four_base_criteria_but_not_full_eligible(self, session_maker):
        async with session_maker() as session:
            session.add(_book(created_at="2026-04-01T00:00:00+00:00"))  # >3 months before NOW
            for i in range(30):
                pos = _position("B01", entry=1.0, exit_value=0.5, entry_date=f"2026-07-{i % 28 + 1:02d}")
                session.add(pos)
                session.add(_pm(f"pm{i}", pos.id, exit_date=f"2026-07-{i % 28 + 1:02d}"))
            await session.commit()
        report = await _run(session_maker)
        # 30 winning trades clear trades/months/breaches/expectancy, but
        # ADR-0010's further conditions are still 'not_yet_evaluated' (#215)
        # so the book cannot be fully `eligible` yet.
        assert report.verdict == "promising"
        assert "B01" in report.verdict_basis

    @pytest.mark.asyncio
    async def test_compelling_when_a_book_is_fully_eligible_with_no_null_drill_supplied(
        self, session_maker, monkeypatch
    ):
        import backend.console as console_mod

        async with session_maker() as session:
            session.add(_book(created_at="2026-04-01T00:00:00+00:00"))
            for i in range(30):
                pos = _position("B01", entry=1.0, exit_value=0.5, entry_date=f"2026-07-{i % 28 + 1:02d}")
                session.add(pos)
                session.add(_pm(f"pm{i}", pos.id, exit_date=f"2026-07-{i % 28 + 1:02d}"))
            await session.commit()
        all_ok = tuple(c.model_copy(update={"status": "ok"}) for c in console_mod.ADR_0010_PENDING_CONDITIONS)
        monkeypatch.setattr(console_mod, "ADR_0010_PENDING_CONDITIONS", all_ok)
        report = await _run(session_maker)
        assert report.verdict == "compelling"

    @pytest.mark.asyncio
    async def test_compelling_requires_the_null_drill_percentile_when_a_snapshot_is_supplied(
        self, session_maker, monkeypatch
    ):
        import backend.console as console_mod

        async with session_maker() as session:
            session.add(_book(created_at="2026-04-01T00:00:00+00:00"))
            for i in range(30):
                pos = _position("B01", entry=1.0, exit_value=0.5, entry_date=f"2026-07-{i % 28 + 1:02d}")
                session.add(pos)
                session.add(_pm(f"pm{i}", pos.id, exit_date=f"2026-07-{i % 28 + 1:02d}"))
            await session.commit()
        all_ok = tuple(c.model_copy(update={"status": "ok"}) for c in console_mod.ADR_0010_PENDING_CONDITIONS)
        monkeypatch.setattr(console_mod, "ADR_0010_PENDING_CONDITIONS", all_ok)

        # A null-drill snapshot where B01 does NOT clear the 95th percentile
        # -> eligible alone is not enough once a snapshot exists to check.
        weak_drill = NullDrillReport(
            n_books=1,
            n_pooled_trades=30,
            n_iterations=100,
            seed=1,
            books=[
                BookNullComparison(
                    book_id="B01",
                    n_trades=30,
                    expectancy=40.0,
                    expectancy_se=5.0,
                    expectancy_percentile_in_null=80.0,
                    expectancy_minus_se_percentile_in_null=60.0,
                )
            ],
        )
        weak_report = await _run(session_maker, null_drill=weak_drill)
        assert weak_report.verdict == "promising"  # falls back — still clears the base four

        strong_drill = NullDrillReport(
            n_books=1,
            n_pooled_trades=30,
            n_iterations=100,
            seed=1,
            books=[
                BookNullComparison(
                    book_id="B01",
                    n_trades=30,
                    expectancy=40.0,
                    expectancy_se=5.0,
                    expectancy_percentile_in_null=99.0,
                    expectancy_minus_se_percentile_in_null=97.0,
                )
            ],
        )
        strong_report = await _run(session_maker, null_drill=strong_drill)
        assert strong_report.verdict == "compelling"

    @pytest.mark.asyncio
    async def test_failed_overrides_a_passing_book_when_pooled_evidence_is_a_confident_loser(self, session_maker):
        # 30 trades, every one a big loser -> the pooled 95% CI sits
        # entirely below zero even though nothing here claims any single
        # book's own gate status.
        async with session_maker() as session:
            session.add(_book(created_at="2026-04-01T00:00:00+00:00"))
            for i in range(30):
                pos = _position("B01", entry=1.0, exit_value=2.5, entry_date=f"2026-07-{i % 28 + 1:02d}")
                session.add(pos)
                session.add(_pm(f"pm{i}", pos.id, exit_date=f"2026-07-{i % 28 + 1:02d}"))
            await session.commit()
        report = await _run(session_maker)
        assert report.verdict == "failed"
        assert report.expected_net_profit_ci_high is not None
        assert report.expected_net_profit_ci_high < 0.0


class TestEndpoint:
    @pytest.mark.asyncio
    async def test_endpoint_serves_the_report(self, session_maker, client):
        resp = await client.get("/api/analysis/evidence-verdict")
        assert resp.status_code == 200
        body = resp.json()
        assert body["verdict"] == "insufficient"
        assert body["closed_trades"] == 0
        assert body["policy_version"] == EVIDENCE_VERDICT_POLICY_VERSION

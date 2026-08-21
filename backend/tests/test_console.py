"""Tests for the supervision-console read model (backend/console.py, #73).

The Books tab numbers ARE the Live Gate evidence presentation — win rate,
expectancy after the slippage haircut, breaches, months — so each metric's
math is pinned here, along with the console API endpoints.
"""

import json
from datetime import UTC, datetime

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.console import (
    SLIPPAGE_HAIRCUT_PER_CONTRACT,
    book_summaries,
    executor_status,
    realized_pnl,
)
from backend.database import get_db
from backend.models import (
    AuditEventModel,
    Base,
    BookModel,
    FillModel,
    OrderModel,
    PositionModel,
    ReconciliationRunModel,
    TradingControlModel,
)

NOW = datetime(2026, 8, 18, 22, 0, tzinfo=UTC)
OLD_START = "2026-04-01T00:00:00+00:00"  # >3 months before NOW
FRESH_START = "2026-08-10T00:00:00+00:00"


@pytest_asyncio.fixture
async def session_maker(monkeypatch, tmp_path):
    monkeypatch.setenv("HALT_FILE", str(tmp_path / "HALT"))
    monkeypatch.setenv("EXECUTOR_HEARTBEAT_FILE", str(tmp_path / "heartbeat.json"))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield maker
    await engine.dispose()


def _book(book_id: str = "B01", created_at: str = OLD_START, **overrides) -> BookModel:
    defaults: dict = {
        "id": book_id,
        "name": f"lab {book_id}",
        "config": {"engine_variant": "V1", "underlying": "XSP", "envelope": {}},
        "config_version": 1,
        "config_hash": "cafe1234",
        "starting_capital": 10000.0,
        "cash_balance": 10000.0,
        "status": "ACTIVE",
        "created_at": created_at,
        "last_mtm": None,
    }
    defaults.update(overrides)
    return BookModel(**defaults)


_POS_SEQ = iter(range(10_000))


def _position(book_id: str, status: str, entry: float, exit_value: float, **overrides) -> PositionModel:
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
        "status": status,
        "journal": {},
        "book_id": book_id,
    }
    defaults.update(overrides)
    return PositionModel(**defaults)


async def _summaries(maker, now=NOW):
    async with maker() as session:
        return await book_summaries(session, now=now)


class TestRealizedPnl:
    def test_credit_and_debit_mirror_the_manual_close_math(self):
        credit = _position("B01", "CLOSED", entry=1.0, exit_value=0.4)
        assert realized_pnl(credit) == 60.0
        debit = _position("B01", "CLOSED", entry=1.0, exit_value=1.8, premium_direction="DEBIT", contracts=2)
        assert realized_pnl(debit) == 160.0


class TestConfigEraScoping:
    """#534 (Audit II R4): the Live Gate attaches to (book, config_hash) —
    a seed-sync starts a new evidence era, and pooling eras lets eligibility
    trip on trades from a config that no longer exists."""

    def _sync_audit(self, book_id: str, run_at: str) -> AuditEventModel:
        return AuditEventModel(
            run_at=run_at,
            book_id=book_id,
            event_type="BOOK_CONFIG_SYNCED",
            actor="system",
            payload={"old_hash": "old12345", "new_hash": "cafe1234"},
        )

    @pytest.mark.asyncio
    async def test_synced_book_pools_only_current_era_trades(self, session_maker):
        async with session_maker() as session:
            session.add(_book())  # current hash cafe1234
            session.add(self._sync_audit("B01", "2026-08-10T22:00:00+00:00"))
            # Two old-era trades, one current-era, one unknowable (NULL).
            session.add(_position("B01", "CLOSED", entry=1.0, exit_value=0.5, config_hash="old12345"))
            session.add(_position("B01", "CLOSED", entry=1.0, exit_value=0.5, config_hash="old12345"))
            session.add(_position("B01", "CLOSED", entry=1.0, exit_value=0.5, config_hash="cafe1234"))
            session.add(_position("B01", "CLOSED", entry=1.0, exit_value=0.5, config_hash=None))
            await session.commit()
        (summary,) = await _summaries(session_maker)
        assert summary.closed_trades == 1  # only the current era counts
        assert summary.live_gate.closed_trades == 1

    @pytest.mark.asyncio
    async def test_never_synced_book_counts_legacy_null_hash_rows(self, session_maker):
        # Pre-#284 rows carry no hash; while the config has never changed
        # they ARE the current era and must keep counting.
        async with session_maker() as session:
            session.add(_book())
            session.add(_position("B01", "CLOSED", entry=1.0, exit_value=0.5, config_hash=None))
            session.add(_position("B01", "CLOSED", entry=1.0, exit_value=0.5, config_hash="cafe1234"))
            await session.commit()
        (summary,) = await _summaries(session_maker)
        assert summary.closed_trades == 2

    @pytest.mark.asyncio
    async def test_months_clock_restarts_at_the_era_boundary(self, session_maker):
        # Three months of evidence under a retired config is not three
        # months under this one.
        async with session_maker() as session:
            session.add(_book(created_at=OLD_START))  # >3 months before NOW
            session.add(self._sync_audit("B01", FRESH_START))  # era began days ago
            await session.commit()
        (summary,) = await _summaries(session_maker)
        assert not summary.live_gate.months_ok

    @pytest.mark.asyncio
    async def test_prior_era_breach_rows_do_not_poison_the_gate(self, session_maker):
        # Audit II R4 (#533): false breach rows written before the config
        # sync (old-era positions judged against a reduced envelope) belong
        # to a retired era — era-scoping the count un-poisons them without
        # touching the append-only table. Current-era breaches still count.
        async with session_maker() as session:
            session.add(_book())
            session.add(
                AuditEventModel(
                    run_at="2026-08-05T22:00:00+00:00",  # before the sync
                    book_id="B01",
                    event_type="ENVELOPE_BREACH_POSTHOC",
                    actor="anomaly",
                    payload={"detail": "old-era false positive"},
                )
            )
            session.add(self._sync_audit("B01", FRESH_START))  # 2026-08-10
            await session.commit()
        (summary,) = await _summaries(session_maker)
        assert summary.live_gate.breaches == 0
        assert summary.live_gate.breaches_ok
        # A breach written AFTER the sync still counts — the defect signal
        # the criterion exists for survives the scoping.
        async with session_maker() as session:
            session.add(
                AuditEventModel(
                    run_at="2026-08-15T22:00:00+00:00",
                    book_id="B01",
                    event_type="ENVELOPE_BREACH_POSTHOC",
                    actor="anomaly",
                    payload={"detail": "current-era real breach"},
                )
            )
            await session.commit()
        (summary,) = await _summaries(session_maker)
        assert summary.live_gate.breaches == 1
        assert not summary.live_gate.breaches_ok


class TestBookSummaries:
    @pytest.mark.asyncio
    async def test_empty_book_has_no_rates_and_fails_the_gate(self, session_maker):
        async with session_maker() as session:
            session.add(_book(created_at=FRESH_START))
            await session.commit()
        (summary,) = await _summaries(session_maker)
        assert summary.closed_trades == 0
        assert summary.win_rate is None
        assert summary.expectancy_after_haircut is None
        gate = summary.live_gate
        assert not gate.trades_ok and not gate.months_ok and not gate.expectancy_ok
        assert gate.breaches_ok  # zero breaches so far is a pass
        assert not gate.eligible

    @pytest.mark.asyncio
    async def test_win_rate_and_expectancy_haircut_math(self, session_maker):
        async with session_maker() as session:
            session.add(_book())
            # +50 winner and -40 loser, $5/contract haircut each → expectancy 0.0
            session.add(_position("B01", "CLOSED", entry=1.0, exit_value=0.5, entry_date="2026-08-01"))
            session.add(_position("B01", "EXPIRED", entry=1.0, exit_value=1.4, entry_date="2026-08-02"))
            await session.commit()
        (summary,) = await _summaries(session_maker)
        assert summary.closed_trades == 2
        assert summary.win_rate == 0.5
        assert summary.expectancy_after_haircut == pytest.approx(
            ((50.0 - SLIPPAGE_HAIRCUT_PER_CONTRACT) + (-40.0 - SLIPPAGE_HAIRCUT_PER_CONTRACT)) / 2
        )
        assert summary.live_gate.expectancy_ok  # >= 0 passes

    @pytest.mark.asyncio
    async def test_expectancy_nets_ledgered_commissions(self, session_maker):
        # H1 (#276): the haircut proxies slippage; commissions come from the
        # fills ledger and are netted on top, per trade.
        async with session_maker() as session:
            session.add(_book())
            winner = _position("B01", "CLOSED", entry=1.0, exit_value=0.5, entry_date="2026-08-01")
            session.add(winner)
            await session.commit()
            session.add(
                OrderModel(
                    id="o_c1",
                    book_id="B01",
                    position_id=winner.id,
                    order_ref="basis:B01:o_c1:open",
                    ib_order_id=1,
                    ib_perm_id=1,
                    action="OPEN",
                    combo_legs={},
                    order_type="LIMIT",
                    limit_price=-1.0,
                    decision_midpoint=-1.0,
                    status="FILLED",
                    submitted_at="t0",
                    completed_at="t1",
                )
            )
            session.add(
                FillModel(
                    exec_id="e_c1",
                    order_id="o_c1",
                    book_id="B01",
                    con_id=1,
                    side="SLD",
                    quantity=1.0,
                    price=1.0,
                    commission=2.10,
                    fill_time="t1",
                    raw={},
                )
            )
            session.add(
                FillModel(
                    exec_id="e_c2",
                    order_id="o_c1",
                    book_id="B01",
                    con_id=2,
                    side="BOT",
                    quantity=1.0,
                    price=0.5,
                    commission=1.90,
                    fill_time="t1",
                    raw={},
                )
            )
            await session.commit()
        (summary,) = await _summaries(session_maker)
        # +50 P&L, $5 haircut, $4 commissions across both legs
        assert summary.expectancy_after_haircut == pytest.approx(50.0 - SLIPPAGE_HAIRCUT_PER_CONTRACT - 4.0)

    @pytest.mark.asyncio
    async def test_max_drawdown_is_peak_to_trough_in_entry_order(self, session_maker):
        async with session_maker() as session:
            session.add(_book())
            session.add(_position("B01", "CLOSED", entry=1.0, exit_value=0.5, entry_date="2026-08-01"))  # +50
            session.add(_position("B01", "CLOSED", entry=1.0, exit_value=1.4, entry_date="2026-08-02"))  # -40
            session.add(_position("B01", "CLOSED", entry=1.0, exit_value=1.3, entry_date="2026-08-03"))  # -30
            await session.commit()
        (summary,) = await _summaries(session_maker)
        assert summary.max_drawdown == 70.0  # peak +50 → trough -20

    @pytest.mark.asyncio
    async def test_months_criterion_uses_book_creation(self, session_maker):
        async with session_maker() as session:
            session.add(_book("B01", created_at=OLD_START))
            session.add(_book("B02", created_at=FRESH_START))
            await session.commit()
        old, fresh = await _summaries(session_maker)
        assert old.live_gate.months_ok
        assert not fresh.live_gate.months_ok

    @pytest.mark.asyncio
    async def test_envelope_breach_fails_zero_breaches(self, session_maker):
        async with session_maker() as session:
            session.add(_book())
            session.add(
                AuditEventModel(
                    run_at="2026-08-10T22:00:00+00:00",
                    book_id="B01",
                    event_type="ENVELOPE_BREACH_POSTHOC",
                    actor="anomaly",
                    payload={},
                )
            )
            await session.commit()
        (summary,) = await _summaries(session_maker)
        assert summary.live_gate.breaches == 1
        assert not summary.live_gate.breaches_ok
        assert not summary.live_gate.eligible

    @pytest.mark.asyncio
    async def test_gate_eligible_when_all_four_criteria_pass(self, session_maker):
        async with session_maker() as session:
            session.add(_book(created_at=OLD_START))
            for i in range(30):
                session.add(
                    _position("B01", "CLOSED", entry=1.0, exit_value=0.5, entry_date=f"2026-07-{i % 28 + 1:02d}")
                )
            await session.commit()
        (summary,) = await _summaries(session_maker)
        assert summary.live_gate.trades_ok
        assert summary.live_gate.eligible

    @pytest.mark.asyncio
    async def test_control_state_fails_closed_without_a_row(self, session_maker):
        async with session_maker() as session:
            session.add(_book("B01"))
            session.add(_book("B02"))
            session.add(TradingControlModel(scope="B02", state="ACTIVE", reason="", actor="t", changed_at="t0"))
            await session.commit()
        no_row, with_row = await _summaries(session_maker)
        assert no_row.control_state == "HALT_ENTRIES"
        assert with_row.control_state == "ACTIVE"

    @pytest.mark.asyncio
    async def test_legacy_b00_is_excluded_and_deployment_counted(self, session_maker):
        async with session_maker() as session:
            session.add(_book("B00", status="LEGACY"))
            session.add(_book("B01"))
            # open credit spread: max_loss 2.0/share × 1 contract = $200 = 2% of basis
            session.add(_position("B01", "OPEN", entry=1.0, exit_value=1.0))
            await session.commit()
        summaries = await _summaries(session_maker)
        assert [s.id for s in summaries] == ["B01"]
        assert summaries[0].open_positions == 1
        assert summaries[0].deployed_pct == 2.0

    @pytest.mark.asyncio
    async def test_pnl_comes_from_last_mtm(self, session_maker):
        async with session_maker() as session:
            session.add(_book(last_mtm=10275.5))
            await session.commit()
        (summary,) = await _summaries(session_maker)
        assert summary.pnl == 275.5


class TestExecutorStatus:
    async def _status(self, maker, now=NOW):
        async with maker() as session:
            return await executor_status(session, now=now)

    @pytest.mark.asyncio
    async def test_missing_heartbeat_reads_stale(self, session_maker):
        status = await self._status(session_maker)
        assert status.stale
        assert status.heartbeat_at is None

    @pytest.mark.asyncio
    async def test_fresh_heartbeat(self, session_maker, tmp_path):
        (tmp_path / "heartbeat.json").write_text(
            json.dumps(
                {
                    "at": "2026-08-18T21:30:00+00:00",
                    "broker_ok": True,
                    "reconciliation": "CLEAN",
                    "entries_placed": 2,
                    "closes_placed": 1,
                }
            )
        )
        status = await self._status(session_maker)
        assert not status.stale
        assert status.heartbeat_age_hours == 0.5
        assert status.broker_ok is True
        assert status.entries_placed == 2

    @pytest.mark.asyncio
    async def test_heartbeat_older_than_24h_reads_stale(self, session_maker, tmp_path):
        (tmp_path / "heartbeat.json").write_text(json.dumps({"at": "2026-08-16T21:00:00+00:00", "broker_ok": True}))
        status = await self._status(session_maker)
        assert status.stale

    @pytest.mark.asyncio
    async def test_friday_heartbeat_stays_fresh_all_weekend(self, session_maker, tmp_path):
        # #545 L3: STALE_AFTER_HOURS=24 against a Mon-Fri task painted the
        # console red from Saturday evening straight through Monday
        # ~18:45 — indistinguishable from a genuinely dead Friday run.
        # Staleness is now trading-day-based: a Friday-evening heartbeat
        # stays fresh through Saturday and Sunday, since Friday remains
        # the last trading day all weekend.
        (tmp_path / "heartbeat.json").write_text(json.dumps({"at": "2026-08-21T22:45:00+00:00", "broker_ok": True}))
        saturday = datetime(2026, 8, 22, 20, 0, tzinfo=UTC)
        sunday = datetime(2026, 8, 23, 20, 0, tzinfo=UTC)
        assert not (await self._status(session_maker, now=saturday)).stale
        assert not (await self._status(session_maker, now=sunday)).stale

    @pytest.mark.asyncio
    async def test_friday_heartbeat_goes_stale_once_monday_is_the_last_trading_day(self, session_maker, tmp_path):
        (tmp_path / "heartbeat.json").write_text(json.dumps({"at": "2026-08-21T22:45:00+00:00", "broker_ok": True}))
        monday_morning = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)  # before Monday's own run
        assert (await self._status(session_maker, now=monday_morning)).stale

    @pytest.mark.asyncio
    async def test_corrupt_heartbeat_reads_stale_not_crash(self, session_maker, tmp_path):
        (tmp_path / "heartbeat.json").write_text("{not json")
        status = await self._status(session_maker)
        assert status.stale

    @pytest.mark.asyncio
    async def test_last_digest_delivery_status_surfaces(self, session_maker):
        # H2 (#277): a failed push must be visible somewhere — this is where.
        async with session_maker() as session:
            session.add(
                AuditEventModel(
                    run_at="2026-08-18T23:00:00+00:00",
                    book_id=None,
                    event_type="DIGEST_COMPOSED",
                    actor="executor",
                    payload={"title": "t", "body": "b", "priority": "default", "pushed": False},
                )
            )
            await session.commit()
        status = await self._status(session_maker)
        assert status.last_digest_at == "2026-08-18T23:00:00+00:00"
        assert status.last_digest_pushed is False

    @pytest.mark.asyncio
    async def test_last_reconciliation_from_db(self, session_maker, tmp_path):
        async with session_maker() as session:
            session.add(
                ReconciliationRunModel(
                    run_at="2026-08-17T22:00:00+00:00", broker_snapshot={}, books_expected={}, result="CLEAN"
                )
            )
            session.add(
                ReconciliationRunModel(
                    run_at="2026-08-18T22:00:00+00:00", broker_snapshot={}, books_expected={}, result="DRIFT"
                )
            )
            await session.commit()
        status = await self._status(session_maker)
        assert status.last_reconciliation_at == "2026-08-18T22:00:00+00:00"
        assert status.last_reconciliation_result == "DRIFT"

    @pytest.mark.asyncio
    async def test_no_reconciliation_run_reads_resolved_as_none(self, session_maker):
        status = await self._status(session_maker)
        assert status.last_reconciliation_resolved is None

    @pytest.mark.asyncio
    async def test_unresolved_drift_reads_resolved_false(self, session_maker):
        # #478: "recon DRIFT" must not look identical before and after a
        # human records the resolution.
        async with session_maker() as session:
            session.add(
                ReconciliationRunModel(
                    run_at="2026-08-18T22:00:00+00:00", broker_snapshot={}, books_expected={}, result="DRIFT"
                )
            )
            await session.commit()
        status = await self._status(session_maker)
        assert status.last_reconciliation_result == "DRIFT"
        assert status.last_reconciliation_resolved is False

    @pytest.mark.asyncio
    async def test_resolved_drift_reads_resolved_true(self, session_maker):
        async with session_maker() as session:
            session.add(
                ReconciliationRunModel(
                    run_at="2026-08-18T22:00:00+00:00",
                    broker_snapshot={},
                    books_expected={},
                    result="DRIFT",
                    resolved_at="2026-08-18T23:00:00+00:00",
                    resolution="handled",
                )
            )
            await session.commit()
        status = await self._status(session_maker)
        assert status.last_reconciliation_result == "DRIFT"
        assert status.last_reconciliation_resolved is True

    @pytest.mark.asyncio
    async def test_executor_status_shares_unresolved_drift_preference_with_recon_endpoint(self, session_maker):
        # Same query as /api/reconciliation/latest (#474, #478): an
        # unresolved DRIFT must not be shadowed by a later CLEAN run here
        # either, or the strip badge and the recon panel would disagree.
        async with session_maker() as session:
            session.add(
                ReconciliationRunModel(
                    run_at="2026-08-18T22:00:00+00:00", broker_snapshot={}, books_expected={}, result="DRIFT"
                )
            )
            session.add(
                ReconciliationRunModel(
                    run_at="2026-08-19T22:00:00+00:00", broker_snapshot={}, books_expected={}, result="CLEAN"
                )
            )
            await session.commit()
        status = await self._status(session_maker)
        assert status.last_reconciliation_result == "DRIFT"
        assert status.last_reconciliation_resolved is False

    @pytest.mark.asyncio
    async def test_no_digest_reads_urgent_pushed_as_none(self, session_maker):
        status = await self._status(session_maker)
        assert status.last_urgent_pushed is None

    @pytest.mark.asyncio
    async def test_digest_with_no_urgent_lines_reads_urgent_pushed_as_none(self, session_maker):
        async with session_maker() as session:
            session.add(
                AuditEventModel(
                    run_at="2026-08-18T23:00:00+00:00",
                    book_id=None,
                    event_type="DIGEST_COMPOSED",
                    actor="executor",
                    payload={"title": "t", "body": "b", "priority": "default", "pushed": True, "urgent_pushed": None},
                )
            )
            await session.commit()
        status = await self._status(session_maker)
        assert status.last_urgent_pushed is None

    @pytest.mark.asyncio
    async def test_failed_urgent_push_surfaces(self, session_maker):
        # #478: urgent_pushed=False (delivery failed) must be visible
        # somewhere, same as last_digest_pushed=False.
        async with session_maker() as session:
            session.add(
                AuditEventModel(
                    run_at="2026-08-18T23:00:00+00:00",
                    book_id=None,
                    event_type="DIGEST_COMPOSED",
                    actor="executor",
                    payload={
                        "title": "t",
                        "body": "b",
                        "priority": "urgent",
                        "pushed": True,
                        "urgent_pushed": False,
                    },
                )
            )
            await session.commit()
        status = await self._status(session_maker)
        assert status.last_urgent_pushed is False


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


async def _audit(maker, run_at: str, book_id: str | None, event_type: str) -> None:
    async with maker() as session:
        session.add(
            AuditEventModel(run_at=run_at, book_id=book_id, event_type=event_type, actor="executor", payload={})
        )
        await session.commit()


class TestApi:
    @pytest.mark.asyncio
    async def test_books_endpoint(self, client, session_maker):
        async with session_maker() as session:
            session.add(_book())
            await session.commit()
        resp = await client.get("/api/books")
        assert resp.status_code == 200
        data = resp.json()
        assert data["books"][0]["id"] == "B01"
        assert data["books"][0]["live_gate"]["closed_trades_required"] == 30

    @pytest.mark.asyncio
    async def test_audit_events_filters(self, client, session_maker):
        await _audit(session_maker, "2026-08-18T22:00:00+00:00", "B01", "ORDER_SUBMITTED")
        await _audit(session_maker, "2026-08-18T22:01:00+00:00", "B02", "ORDER_SUBMITTED")
        await _audit(session_maker, "2026-08-17T22:00:00+00:00", "B01", "CONTROL_CHECK")

        resp = await client.get("/api/audit-events", params={"book_id": "B01"})
        assert {e["event_type"] for e in resp.json()} == {"ORDER_SUBMITTED", "CONTROL_CHECK"}

        resp = await client.get("/api/audit-events", params={"date": "2026-08-18"})
        assert {e["book_id"] for e in resp.json()} == {"B01", "B02"}

        resp = await client.get("/api/audit-events", params={"event_type": "CONTROL_CHECK"})
        assert len(resp.json()) == 1

    @pytest.mark.asyncio
    async def test_audit_events_event_type_filter_is_substring_and_case_insensitive(self, session_maker, client):
        # #479: exact match made "reject" silently return nothing instead of
        # ORDER_REJECTED/CLOSE_REJECTED/etc — a misleading empty state.
        await _audit(session_maker, "2026-08-18T22:00:00+00:00", "B01", "ORDER_REJECTED")
        await _audit(session_maker, "2026-08-18T22:01:00+00:00", "B01", "CLOSE_REJECTED")
        await _audit(session_maker, "2026-08-18T22:02:00+00:00", "B01", "ORDER_SUBMITTED")

        resp = await client.get("/api/audit-events", params={"event_type": "reject"})
        assert {e["event_type"] for e in resp.json()} == {"ORDER_REJECTED", "CLOSE_REJECTED"}

    @pytest.mark.asyncio
    async def test_audit_events_newest_first_and_limited(self, client, session_maker):
        for i in range(5):
            await _audit(session_maker, f"2026-08-18T22:00:0{i}+00:00", "B01", "CONTROL_CHECK")
        resp = await client.get("/api/audit-events", params={"limit": 3})
        events = resp.json()
        assert len(events) == 3
        assert events[0]["run_at"] > events[-1]["run_at"]

    @pytest.mark.asyncio
    async def test_audit_events_urgent_flag_matches_digest_set(self, session_maker, client):
        # #474: the console's urgent flag is server-computed from the SAME
        # exported set the nightly urgent push uses — never a second list.
        await _audit(session_maker, "2026-08-18T22:00:00+00:00", "B01", "DUPLICATE_ORDER")
        await _audit(session_maker, "2026-08-18T22:01:00+00:00", "B01", "EXPIRY_SETTLEMENT_BLOCKED_PARTIAL")
        await _audit(session_maker, "2026-08-18T22:02:00+00:00", "B01", "CRASH_ALERT")
        await _audit(session_maker, "2026-08-18T22:03:00+00:00", "B01", "ORDER_SUBMITTED")
        resp = await client.get("/api/audit-events")
        by_type = {e["event_type"]: e["urgent"] for e in resp.json()}
        assert by_type["DUPLICATE_ORDER"] is True
        assert by_type["EXPIRY_SETTLEMENT_BLOCKED_PARTIAL"] is True
        assert by_type["CRASH_ALERT"] is True
        assert by_type["ORDER_SUBMITTED"] is False

    @pytest.mark.asyncio
    async def test_executor_status_endpoint(self, client):
        resp = await client.get("/api/executor/status")
        assert resp.status_code == 200
        assert resp.json()["stale"] is True

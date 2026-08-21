"""Tests for the executor digest and urgent-push tiering (backend/digest.py, #72)."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.digest import URGENT_EVENT_TYPES, compose_executor_digest, is_urgent_event_type, urgent_events
from backend.executor import ExecutorRunSummary
from backend.models import (
    AuditEventModel,
    Base,
    BookModel,
    GateEventModel,
    OrderModel,
    PositionModel,
    TradingControlModel,
)

TODAY = "2026-08-18"


@pytest_asyncio.fixture
async def session_maker(tmp_path, monkeypatch):
    monkeypatch.setenv("HALT_FILE", str(tmp_path / "HALT"))  # sentinel absent by default
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        session.add(
            BookModel(
                id="B01",
                name="V0 on XSP",
                config={"engine_variant": "V0", "underlying": "XSP", "envelope": {}},
                config_version=1,
                config_hash="h",
                starting_capital=10000.0,
                cash_balance=10120.0,
                status="ACTIVE",
                created_at="t0",
                last_mtm=10060.0,
            )
        )
        session.add(TradingControlModel(scope="GLOBAL", state="ACTIVE", reason="", actor="t", changed_at="t0"))
        await session.commit()
    yield maker
    await engine.dispose()


async def _digest(maker, summary=None):
    async with maker() as session:
        return await compose_executor_digest(session, summary or ExecutorRunSummary(), TODAY)


async def _digest_since(maker, since, summary=None):
    async with maker() as session:
        return await compose_executor_digest(session, summary or ExecutorRunSummary(), TODAY, since=since)


class TestSections:
    @pytest.mark.asyncio
    async def test_quiet_night(self, session_maker):
        title, body, priority = await _digest(session_maker)
        assert "all quiet" in title
        assert priority == "default"
        assert "Reconciliation: SKIPPED" in body  # never silent about reconciliation

    @pytest.mark.asyncio
    async def test_clean_reconciliation_stated_explicitly(self, session_maker):
        summary = ExecutorRunSummary(reconciliation="CLEAN")
        _, body, _ = await _digest(session_maker, summary)
        assert "Reconciliation clean" in body

    @pytest.mark.asyncio
    async def test_halt_banner_is_first_line_and_escalates(self, session_maker):
        async with session_maker() as session:
            row = await session.get(TradingControlModel, "GLOBAL")
            row.state = "HALT_ENTRIES"
            row.reason = "RECONCILIATION_DRIFT: 2 discrepancies"
            row.changed_at = f"{TODAY}T01:00:00+00:00"
            await session.commit()
        title, body, priority = await _digest(session_maker, ExecutorRunSummary(reconciliation="CLEAN"))
        assert body.splitlines()[0].startswith("⛔ GLOBAL HALT_ENTRIES")
        assert "HALTED" in title
        assert priority == "high"

    @pytest.mark.asyncio
    async def test_sentinel_halt_appears_in_banner(self, session_maker, monkeypatch, tmp_path):
        halt = tmp_path / "HALT"
        halt.write_text("stop")
        monkeypatch.setenv("HALT_FILE", str(halt))
        _, body, priority = await _digest(session_maker)
        assert "SENTINEL HALT" in body.splitlines()[0]
        assert priority == "high"

    @pytest.mark.asyncio
    async def test_books_section_shows_pnl_positions_and_gate_progress(self, session_maker):
        async with session_maker() as session:
            session.add(
                PositionModel(
                    id="p_closed",
                    underlying="XSP",
                    strategy_type="BULL_PUT_SPREAD",
                    execution_mode="PAPER",
                    legs=[],
                    entry_date="2026-08-01",
                    expiration_date="2026-09-18",
                    entry_premium=1.0,
                    premium_direction="CREDIT",
                    current_value_per_share=0.5,
                    contracts=1,
                    max_profit=1.0,
                    max_loss=2.0,
                    notes="",
                    rolls=0,
                    status="CLOSED",
                    journal={},
                    book_id="B01",
                )
            )
            await session.commit()
        _, body, _ = await _digest(session_maker)
        assert "B01 [V0/XSP] P&L +60" in body
        assert "gate 1/30" in body  # closed trades count toward the Live Gate

    @pytest.mark.asyncio
    async def test_idle_books_collapse_into_one_line_naming_ids(self, session_maker):
        # 22 books of roster every night buries the signal (#160/ADR-0009),
        # but absence must never be silent — idle ids stay in the digest.
        async with session_maker() as session:
            for book_id in ("B07", "B11"):
                session.add(
                    BookModel(
                        id=book_id,
                        name=f"idle {book_id}",
                        config={"engine_variant": "V0", "underlying": "XSP", "envelope": {}},
                        config_version=1,
                        config_hash="h",
                        starting_capital=10000.0,
                        cash_balance=10000.0,
                        status="ACTIVE",
                        created_at="t0",
                    )
                )
            await session.commit()
        _, body, _ = await _digest(session_maker)
        assert "B01 [V0/XSP] P&L +60" in body  # active book keeps its detail line
        assert "2 book(s) idle" in body
        assert "B07 B11" in body
        assert "B07 [V0/XSP]" not in body  # no per-book roster line for idles

    @pytest.mark.asyncio
    async def test_book_with_resting_order_is_awaiting_fill_not_idle(self, session_maker):
        # Entries are ORDERS until the next fill sync creates positions — on
        # the first armed night every submitting book was listed idle (#225).
        async with session_maker() as session:
            for book_id, order_status in (("B07", "SUBMITTED"), ("B11", None)):
                session.add(
                    BookModel(
                        id=book_id,
                        name=f"book {book_id}",
                        config={"engine_variant": "V0", "underlying": "XSP", "envelope": {}},
                        config_version=1,
                        config_hash="h",
                        starting_capital=10000.0,
                        cash_balance=10000.0,
                        status="ACTIVE",
                        created_at="t0",
                    )
                )
                if order_status:
                    session.add(
                        OrderModel(
                            id=f"o_{book_id}",
                            book_id=book_id,
                            position_id=None,
                            order_ref=f"basis:{book_id}:o1:open",
                            ib_order_id=2,
                            ib_perm_id=2,
                            action="OPEN",
                            combo_legs={"strategy_type": "BULL_PUT_SPREAD", "legs": [], "quantity": 1},
                            order_type="LIMIT",
                            limit_price=-1.05,
                            decision_midpoint=-1.05,
                            status=order_status,
                            submitted_at=f"{TODAY}T21:00:00",
                            completed_at=None,
                            encumbered_risk=200.0,
                        )
                    )
            await session.commit()
        _, body, _ = await _digest(session_maker)
        assert "1 book(s) awaiting fill (orders resting at broker): B07" in body
        assert "1 book(s) idle" in body
        assert "B11" in body.split("idle")[1]  # only the orderless book is idle

    @pytest.mark.asyncio
    async def test_regime_consensus_renders_one_line(self, session_maker):
        from backend.models import RegimeReadingModel

        async with session_maker() as session:
            for v in ("V0", "V1", "V3"):
                session.add(RegimeReadingModel(date=TODAY, book_id="ALL", engine_variant=v, regime="CALM_BULL"))
            session.add(RegimeReadingModel(date=TODAY, book_id="ALL", engine_variant="V2", regime="INSUFFICIENT_DATA"))
            await session.commit()
        _, body, _ = await _digest(session_maker)
        assert "Regime: CALM_BULL (all variants) (V2 insufficient data)" in body

    @pytest.mark.asyncio
    async def test_regime_split_names_the_dissenter(self, session_maker):
        from backend.models import RegimeReadingModel

        async with session_maker() as session:
            for v in ("V0", "V1", "V3"):
                session.add(RegimeReadingModel(date=TODAY, book_id="ALL", engine_variant=v, regime="CALM_BULL"))
            session.add(RegimeReadingModel(date=TODAY, book_id="ALL", engine_variant="V2", regime="TRENDING_BEAR"))
            await session.commit()
        _, body, _ = await _digest(session_maker)
        assert "Regime split: CALM_BULL (V0 V1 V3) / TRENDING_BEAR (V2)" in body

    @pytest.mark.asyncio
    async def test_events_after_utc_midnight_still_appear(self, session_maker):
        # EST-season runs start 23:45 UTC (#259): rows written after the UTC
        # rollover carry tomorrow's date — a date-prefix filter dropped them,
        # emptying digests and urgent pushes all winter. Run-start timestamps
        # must include them.
        from backend.digest import urgent_events

        since = f"{TODAY}T23:45:00+00:00"
        async with session_maker() as session:
            session.add(
                AuditEventModel(
                    run_at="2026-08-19T00:10:00+00:00",  # after midnight UTC, same run
                    book_id="B01",
                    event_type="ORDER_REJECTED",
                    actor="executor",
                    payload={"error": "post-midnight rejection"},
                )
            )
            session.add(
                GateEventModel(
                    run_at="2026-08-19T00:11:00+00:00",
                    book_id="B01",
                    gate="MAX_DEPLOYED",
                    result="BLOCK",
                    context={},
                )
            )
            await session.commit()
        _, body, _ = await _digest_since(session_maker, since)
        assert "Gate B01:MAX_DEPLOYED blocked ×1" in body
        async with session_maker() as session:
            urgent = await urgent_events(session, since)
        assert any("post-midnight rejection" in u for u in urgent)

    def test_blocked_lines_group_identical_reasons(self):
        from backend.digest import _grouped_blocked
        from backend.executor import BlockedEntry

        lines = _grouped_blocked(
            [
                BlockedEntry("B02", "variant V1 reading unavailable"),
                BlockedEntry("B03", "variant V1 reading unavailable"),
                BlockedEntry("B05", "variant V1 reading unavailable"),
                BlockedEntry("B09", "pb unpriceable (IWM)"),
                BlockedEntry(None, "STALE_DATA — live telemetry unavailable, no new entries"),
            ]
        )
        assert "Blocked (variant V1 reading unavailable): B02 B03 B05" in lines
        assert "Blocked: B09: pb unpriceable (IWM)" in lines
        assert "Blocked: ALL: STALE_DATA — live telemetry unavailable, no new entries" in lines
        assert len(lines) == 3

    def test_colons_in_reasons_cannot_break_grouping(self):
        # The old seam parsed "BOOK: reason" strings; a creative reason with
        # colons silently broke grouping. Data can't be mis-parsed.
        from backend.digest import _grouped_blocked
        from backend.executor import BlockedEntry

        lines = _grouped_blocked(
            [
                BlockedEntry("B07", "gated (MAX_DEPLOYED: cap $5000)"),
                BlockedEntry("B08", "gated (MAX_DEPLOYED: cap $5000)"),
            ]
        )
        assert lines == ["Blocked (gated (MAX_DEPLOYED: cap $5000)): B07 B08"]

    @pytest.mark.asyncio
    async def test_since_fallback_excludes_yesterdays_post_midnight_leftovers(self, session_maker):
        # #545 L2: the manual/test-path fallback since = f"{today}T00:00:00"
        # mixes a MARKET date with UTC run_at rows — in EST season the
        # previous evening's run posts events after 00:00 UTC, which still
        # carry the previous MARKET date but a run_at whose UTC date prefix
        # already matches TODAY. Naive "{today}T00:00:00" (no tz, no
        # evening-window offset) let those leftovers re-enter tonight's
        # sections; the real fallback is the same evening-window start the
        # duplicate-order check uses.
        async with session_maker() as session:
            session.add(
                GateEventModel(
                    book_id="B01",
                    run_at=f"{TODAY}T00:15:00+00:00",  # yesterday evening's session, post-midnight UTC
                    gate="MAX_DEPLOYED",
                    result="BLOCK",
                    context={},
                )
            )
            await session.commit()
        _, body, _ = await _digest(session_maker)  # since=None: exercises the fallback
        assert "Gate B01:MAX_DEPLOYED blocked" not in body

    @pytest.mark.asyncio
    async def test_gate_hits_and_fills_sections(self, session_maker):
        async with session_maker() as session:
            session.add(
                GateEventModel(
                    book_id="B01", run_at=f"{TODAY}T22:00:00", gate="MAX_DEPLOYED", result="BLOCK", context={}
                )
            )
            session.add(
                OrderModel(
                    id="o1",
                    book_id="B01",
                    position_id=None,
                    order_ref="basis:B01:o1:open",
                    ib_order_id=1,
                    ib_perm_id=1,
                    action="OPEN",
                    combo_legs={"strategy_type": "BULL_PUT_SPREAD", "legs": [], "quantity": 1},
                    order_type="LIMIT",
                    limit_price=-1.05,
                    decision_midpoint=-1.05,
                    status="FILLED",
                    submitted_at=f"{TODAY}T21:00:00",
                    completed_at=f"{TODAY}T22:00:00",
                    encumbered_risk=200.0,
                )
            )
            await session.commit()
        _, body, _ = await _digest(session_maker)
        assert "Gate B01:MAX_DEPLOYED blocked ×1" in body
        assert "Filled B01 BULL_PUT_SPREAD (OPEN) @ limit -1.05" in body

    @pytest.mark.asyncio
    async def test_anomalies_and_drift_escalate(self, session_maker):
        summary = ExecutorRunSummary(reconciliation="DRIFT", anomalies=["PNL_SHOCK(B01): day move $2000"])
        _title, body, priority = await _digest(session_maker, summary)
        assert "Reconciliation DRIFT" in body
        assert "⛔ PNL_SHOCK(B01)" in body
        assert priority == "high"


class TestUrgentTiering:
    async def _add_event(self, maker, event_type, actor="executor", payload=None):
        async with maker() as session:
            session.add(
                AuditEventModel(
                    run_at=f"{TODAY}T22:00:00+00:00",
                    book_id="B01",
                    event_type=event_type,
                    actor=actor,
                    payload=payload or {},
                )
            )
            await session.commit()

    @pytest.mark.asyncio
    async def test_rule_firings_are_urgent(self, session_maker):
        await self._add_event(session_maker, "PNL_SHOCK", actor="anomaly", payload={"detail": "day move $2000"})
        async with session_maker() as session:
            lines = await urgent_events(session, TODAY)
        # #600: the plain-English label (from B01's own config underlying,
        # XSP — no open position in this fixture) rides alongside the raw
        # book_id rather than replacing it.
        assert lines == ["PNL_SHOCK (B01 — XSP): day move $2000"]

    @pytest.mark.asyncio
    async def test_automated_halts_are_urgent_but_console_halts_are_not(self, session_maker):
        await self._add_event(
            session_maker, "CONTROL_STATE_CHANGED", actor="anomaly", payload={"reason": "REPEATED_REJECTION: 2"}
        )
        await self._add_event(
            session_maker, "CONTROL_STATE_CHANGED", actor="console", payload={"reason": "manual drill"}
        )
        async with session_maker() as session:
            lines = await urgent_events(session, TODAY)
        assert len(lines) == 1
        assert "HALT by anomaly" in lines[0]

    @pytest.mark.asyncio
    async def test_routine_events_never_interrupt(self, session_maker):
        for event in ("ORDER_SUBMITTED", "CONTROL_CHECK", "ENTRY_FILLED", "INTENT_EXPIRED"):
            await self._add_event(session_maker, event)
        async with session_maker() as session:
            lines = await urgent_events(session, TODAY)
        assert lines == []  # push fatigue is a safety failure

    @pytest.mark.asyncio
    async def test_yesterdays_events_are_not_tonights_alerts(self, session_maker):
        async with session_maker() as session:
            session.add(
                AuditEventModel(
                    run_at="2026-08-17T22:00:00+00:00",
                    book_id="B01",
                    event_type="ORDER_REJECTED",
                    actor="executor",
                    payload={},
                )
            )
            await session.commit()
        async with session_maker() as session:
            lines = await urgent_events(session, TODAY)
        assert lines == []


class TestIsUrgentEventType:
    """#474: is_urgent_event_type is the single source of truth shared by the
    nightly urgent push AND the console's AuditEventSchema.urgent flag."""

    @pytest.mark.parametrize("event_type", sorted(URGENT_EVENT_TYPES))
    def test_every_listed_type_is_urgent(self, event_type):
        assert is_urgent_event_type(event_type) is True

    def test_crash_alert_is_urgent(self):
        assert is_urgent_event_type("CRASH_ALERT") is True

    @pytest.mark.parametrize(
        "event_type", ["EXPIRY_SETTLEMENT_BLOCKED_PARTIAL", "EXPIRY_SETTLEMENT_BLOCKED_STALE_MARK"]
    )
    def test_expiry_settlement_blocked_prefix_is_urgent(self, event_type):
        assert is_urgent_event_type(event_type) is True

    @pytest.mark.parametrize("event_type", ["ORDER_SUBMITTED", "CONTROL_CHECK", "ENTRY_FILLED", "INTENT_EXPIRED"])
    def test_routine_events_are_not_urgent(self, event_type):
        assert is_urgent_event_type(event_type) is False

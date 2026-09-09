"""Tests for the executor digest and urgent-push tiering (backend/digest.py, #72)."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.anomaly import ZOMBIE_FILL, format_anomaly_line, run_post_session_anomalies
from backend.digest import (
    NTFY_BODY_LIMIT_BYTES,
    URGENT_EVENT_TYPES,
    build_digest_data,
    compose_executor_digest,
    is_urgent_event_type,
    render_human,
    render_log_line,
    render_log_lines,
    urgent_events,
)
from backend.executor import BlockedEntry, ExecutorRunSummary
from backend.models import (
    AuditEventModel,
    Base,
    BookModel,
    FillModel,
    GateEventModel,
    OrderModel,
    PositionModel,
    RegimeReadingModel,
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


async def _digest(maker, summary=None, format="log"):
    async with maker() as session:
        return await compose_executor_digest(session, summary or ExecutorRunSummary(), TODAY, format=format)


async def _digest_since(maker, since, summary=None, format="log"):
    async with maker() as session:
        return await compose_executor_digest(
            session, summary or ExecutorRunSummary(), TODAY, since=since, format=format
        )


class TestBrokerUnavailableLine:
    """#823: a classified needs-a-human code becomes the specific instruction;
    an unclassified failure keeps the generic line but appends every captured
    API error so the cause is never swallowed again."""

    @pytest.mark.asyncio
    async def test_classified_code_becomes_the_action_needed_instruction(self, session_maker):
        summary = ExecutorRunSummary(
            broker_ok=False,
            broker_api_errors=[(10141, "Paper trading disclaimer must first be accepted for API connection.")],
        )
        title, body, priority = await _digest(session_maker, summary)
        assert "⛔ ACTION NEEDED:" in body
        assert "paper-trading disclaimer" in body
        assert "Client Portal" in body
        assert "IB Gateway unreachable" not in body  # replaced, not duplicated
        assert priority == "high"
        assert title.isascii()  # #598: the ntfy TITLE header must stay ASCII

    @pytest.mark.asyncio
    async def test_unclassified_failure_keeps_the_generic_line_and_appends_captured_errors(self, session_maker):
        summary = ExecutorRunSummary(
            broker_ok=False,
            broker_api_errors=[(2110, "Connectivity between TWS and server is broken.")],
        )
        _, body, _ = await _digest(session_maker, summary)
        assert "⚠ IB Gateway unreachable — no orders were possible tonight" in body
        assert "broker API error 2110: Connectivity between TWS and server is broken." in body
        assert "ACTION NEEDED" not in body

    @pytest.mark.asyncio
    async def test_failure_with_no_captured_errors_is_the_unchanged_generic_line(self, session_maker):
        summary = ExecutorRunSummary(broker_ok=False)
        _, body, _ = await _digest(session_maker, summary)
        assert "⚠ IB Gateway unreachable — no orders were possible tonight" in body
        assert "broker API error" not in body


class TestSections:
    @pytest.mark.asyncio
    async def test_quiet_night(self, session_maker):
        title, body, priority = await _digest(session_maker)
        assert "all quiet" in title
        assert priority == "default"
        assert "Reconciliation: SKIPPED" in body  # never silent about reconciliation

    @pytest.mark.asyncio
    async def test_fully_blocked_night_carries_the_count_in_the_title(self, session_maker):
        from backend.executor import BlockedEntry

        summary = ExecutorRunSummary(entries_blocked=[BlockedEntry(f"B{i:02d}", "STALE_DATA") for i in range(20)])
        title, _, priority = await _digest(session_maker, summary)
        assert title == "basis executor: 20 blocked"
        assert priority == "default"

    @pytest.mark.asyncio
    async def test_entries_and_blocked_both_appear_in_the_title(self, session_maker):
        from backend.executor import BlockedEntry

        summary = ExecutorRunSummary(
            entries_placed=["B01:enter"] * 3,
            entries_blocked=[BlockedEntry(f"B{i:02d}", "STALE_DATA") for i in range(20)],
        )
        title, _, _ = await _digest(session_maker, summary)
        assert title == "basis executor: 3 entered, 20 blocked"

    @pytest.mark.asyncio
    async def test_day_expired_exit_is_informational_never_the_headline(self, session_maker):
        # #959: a DAY exit that ran out its session unfilled, re-issued this
        # same run — the re-issued close already earns its own title bit via
        # closes_placed (unchanged); day_expired must add nothing to the
        # title, only an informational body line pairing the two.
        from backend.executor import DayExpiredExit

        summary = ExecutorRunSummary(
            day_expired=[DayExpiredExit(order_ref="basis:B07:o_old:close", position_id="pos_1", reissue_limit=1.05)],
            closes_placed=["basis:B07:o_new:close"],
        )
        title, body, priority = await _digest(session_maker, summary)
        assert title == "basis executor: 1 closing"
        assert priority == "default"
        assert "Exit unfilled today: basis:B07:o_old:close — re-issued at +1.05" in body
        assert "Close submitted: basis:B07:o_new:close" in body

    @pytest.mark.asyncio
    async def test_day_expired_exit_without_a_same_run_reissue_still_stays_informational(self, session_maker):
        from backend.executor import DayExpiredExit

        summary = ExecutorRunSummary(
            day_expired=[DayExpiredExit(order_ref="basis:B07:o_old:close", position_id="pos_1")],
        )
        title, body, _ = await _digest(session_maker, summary)
        assert title == "basis executor: all quiet"
        assert "Exit unfilled today: basis:B07:o_old:close" in body
        assert "re-issued" not in body

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
    async def _add_event(self, maker, event_type, actor="executor", payload=None, book_id="B01"):
        async with maker() as session:
            session.add(
                AuditEventModel(
                    run_at=f"{TODAY}T22:00:00+00:00",
                    book_id=book_id,
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
    async def test_broker_rejection_reason_surfaces_in_the_urgent_line(self, session_maker):
        # #627: the broker's own rejection text (recovered via the
        # completedStatus capture shim) is the most specific detail
        # available — it must reach the urgent push, not just the order_ref.
        await self._add_event(
            session_maker,
            "ORDER_REJECTED",
            actor="executor",
            payload={
                "order_ref": "basis:B01:o_rej:open",
                "reason": "Rejected by System: Guaranteed-to-Lose combination orders are not allowed",
            },
        )
        async with session_maker() as session:
            lines = await urgent_events(session, TODAY)
        assert lines == [
            "ORDER_REJECTED (B01 — XSP): Rejected by System: Guaranteed-to-Lose combination orders are not allowed"
        ]

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
    async def test_self_clear_resume_is_labeled_distinctly_from_a_halt(self, session_maker):
        # #927: anomaly.py's self-clear writes the SAME event type
        # (CONTROL_STATE_CHANGED, actor="anomaly") to move a scope back to
        # ACTIVE — mislabeling it "HALT by anomaly" would tell the operator
        # the opposite of what happened.
        await self._add_event(
            session_maker,
            "CONTROL_STATE_CHANGED",
            actor="anomaly",
            payload={
                "state": "ACTIVE",
                "reason": "REPEATED_REJECTION evidence expired — auto-cleared by anomaly sweep",
            },
        )
        async with session_maker() as session:
            lines = await urgent_events(session, TODAY)
        assert len(lines) == 1
        assert "RESUMED by anomaly" in lines[0]

    @pytest.mark.asyncio
    async def test_empty_evidence_finding_never_renders_a_dangling_clears_suffix(self, session_maker):
        # #929 LOW-7: ZOMBIE_FILL composes no clear_condition — "nothing
        # evidence-worthy beyond `detail`" (AnomalyFinding.evidence's
        # docstring) — so the "— clears:" suffix must never appear for it,
        # on any of the three surfaces a firing renders on: the ntfy/digest
        # one-liner (format_anomaly_line), the control banner (row.reason,
        # via _compose_reason), and the urgent push line (urgent_events).
        since = f"{TODAY}T22:00:00+00:00"
        async with session_maker() as session:
            session.add(
                OrderModel(
                    id="o_zomb",
                    book_id="B01",
                    position_id=None,
                    order_ref="basis:B01:o_zomb:open",
                    ib_order_id=1,
                    ib_perm_id=1,
                    action="OPEN",
                    combo_legs={"legs": [], "quantity": 1},
                    order_type="LIMIT",
                    limit_price=-1.0,
                    decision_midpoint=-1.0,
                    status="CANCELLED",
                    submitted_at="t0",
                    completed_at="t1",
                    encumbered_risk=0.0,
                )
            )
            session.add(
                FillModel(
                    exec_id="x_zomb_1",
                    order_id="o_zomb",
                    book_id="B01",
                    con_id=1,
                    side="SLD",
                    quantity=1.0,
                    price=1.0,
                    commission=1.0,
                    fill_time=f"{TODAY}T23:31:00+00:00",
                )
            )
            await session.commit()
            findings = await run_post_session_anomalies(session, TODAY, since=since)
        (finding,) = [f for f in findings if f.rule == ZOMBIE_FILL]
        assert finding.clear_condition == ""
        assert "clears" not in format_anomaly_line(finding)  # surface 1: ntfy/digest one-liner

        async with session_maker() as session:
            lines = await urgent_events(session, since)
        assert lines  # sanity: the finding did reach the urgent push
        assert "clears" not in "\n".join(lines)  # surface 2: urgent push line(s)

        async with session_maker() as session:
            _title, body, _priority = await compose_executor_digest(session, ExecutorRunSummary(), TODAY, since=since)
        assert "clears" not in body  # surface 3: control banner (row.reason)

    @pytest.mark.asyncio
    async def test_suppressed_anomaly_repeat_does_not_interrupt(self, session_maker):
        # #922: anomaly.py's dedup marks a standing breach's repeat
        # occurrence alert_suppressed — it still ledgers (test_anomaly.py
        # covers that) but must not reach the urgent push.
        await self._add_event(
            session_maker,
            "ENVELOPE_BREACH_POSTHOC",
            actor="anomaly",
            payload={"detail": "position p1 risk $261 > $250", "alert_suppressed": True},
        )
        async with session_maker() as session:
            lines = await urgent_events(session, TODAY)
        assert lines == []

    @pytest.mark.asyncio
    async def test_first_occurrence_anomaly_still_interrupts(self, session_maker):
        await self._add_event(
            session_maker,
            "ENVELOPE_BREACH_POSTHOC",
            actor="anomaly",
            payload={"detail": "position p1 risk $261 > $250", "alert_suppressed": False},
        )
        async with session_maker() as session:
            lines = await urgent_events(session, TODAY)
        assert len(lines) == 1
        assert "position p1 risk $261 > $250" in lines[0]

    @pytest.mark.asyncio
    async def test_clear_condition_and_refire_ride_along_on_the_urgent_line(self, session_maker):
        # #928: the finding's own audit event carries the full evidence
        # breakdown only in payload["evidence"] (not rendered here — the
        # console's audit-events view renders that from the raw payload) but
        # the clear condition and re-fire marker are short enough to fold
        # into this one-line push.
        await self._add_event(
            session_maker,
            "REPEATED_REJECTION",
            actor="anomaly",
            payload={
                "detail": "16 rejections across trailing 3 sessions",
                "evidence": {"by_session": [{"date": "2026-08-27", "count": 15, "dominant_reason": "gateway burst"}]},
                "clear_condition": "clears once tonight adds no new rejections and the 2026-08-27 session ages out",
                "refire_of": "re-fire of the 2026-08-27 incident",
            },
        )
        async with session_maker() as session:
            lines = await urgent_events(session, TODAY)
        assert len(lines) == 1
        assert "16 rejections across trailing 3 sessions" in lines[0]
        assert "clears once tonight adds no new rejections" in lines[0]
        assert "re-fire of the 2026-08-27 incident" in lines[0]
        assert "by_session" not in lines[0]  # full breakdown stays audit-payload-only

    @pytest.mark.asyncio
    async def test_latching_first_firing_clear_condition_appears_exactly_once(self, session_maker):
        # #929 round-2 LOW-5b: a night that both fires the finding and
        # latches HALT_ENTRIES writes both the finding's own event AND the
        # CONTROL_STATE_CHANGED transition — MEDIUM-4b's strip exists so the
        # clear condition rides on exactly one of those two urgent lines,
        # not both.
        await self._add_event(
            session_maker,
            "REPEATED_REJECTION",
            actor="anomaly",
            book_id=None,
            payload={
                "detail": "2 rejections tonight",
                "clear_condition": "clears once a following session adds no new rejections",
                "refire_of": None,
            },
        )
        await self._add_event(
            session_maker,
            "CONTROL_STATE_CHANGED",
            actor="anomaly",
            book_id=None,
            payload={
                "state": "HALT_ENTRIES",
                "reason": "REPEATED_REJECTION: 2 rejections tonight — clears: clears once a following "
                "session adds no new rejections",
            },
        )
        async with session_maker() as session:
            lines = await urgent_events(session, TODAY)
        joined = "\n".join(lines)
        assert joined.count("clears once a following session adds no new rejections") == 1

    @pytest.mark.asyncio
    async def test_latching_first_firing_end_to_end_clear_condition_appears_exactly_once(self, session_maker):
        # #929 round-2 LOW-5b, the real pipeline (not hand-built payloads):
        # run_post_session_anomalies both records the finding's own event
        # AND latches HALT_ENTRIES (writing CONTROL_STATE_CHANGED) in the
        # same run — urgent_events must still only carry the clear condition
        # once across every line it emits.
        since = f"{TODAY}T22:00:00+00:00"
        async with session_maker() as session:
            session.add(
                AuditEventModel(
                    run_at=f"{TODAY}T22:00:00+00:00",
                    book_id="B01",
                    event_type="ORDER_REJECTED",
                    actor="executor",
                    payload={},
                )
            )
            session.add(
                AuditEventModel(
                    run_at=f"{TODAY}T22:05:00+00:00",
                    book_id="B01",
                    event_type="ORDER_REJECTED",
                    actor="executor",
                    payload={},
                )
            )
            await session.commit()
            await run_post_session_anomalies(session, TODAY, since=since)
        async with session_maker() as session:
            lines = await urgent_events(session, since)
        joined = "\n".join(lines)
        assert joined.count("clears once a following session adds no new rejections") == 1
        assert "HALT by anomaly: REPEATED_REJECTION: 2 rejections tonight" in joined

    @pytest.mark.asyncio
    async def test_suppressed_refire_clear_condition_renders_once_on_the_control_line(self, session_maker):
        # #929 round-2 MEDIUM-3: a deduped ENVELOPE re-fire after an operator
        # RESUME can be _should_alert-suppressed (no finding line rendered)
        # while still refreshing the control row's reason via refresh_reason
        # — leaving the CONTROL_STATE_CHANGED line as the ONLY carrier of the
        # clear condition. The strip must not delete it there too, since
        # that would drop it from the push entirely.
        await self._add_event(
            session_maker,
            "ENVELOPE_BREACH_POSTHOC",
            actor="anomaly",
            payload={"detail": "position p1 risk $261 > $250", "alert_suppressed": True},
        )
        await self._add_event(
            session_maker,
            "CONTROL_STATE_CHANGED",
            actor="anomaly",
            payload={
                "state": "HALT_ENTRIES",
                "reason": "ENVELOPE_BREACH_POSTHOC: position p1 risk $261 > $250 — clears: clears once the "
                "breach resolves — re-fire of the 2026-08-15 incident",
            },
        )
        async with session_maker() as session:
            lines = await urgent_events(session, TODAY)
        assert len(lines) == 1  # the suppressed finding line never rendered
        assert "clears once the breach resolves" in lines[0]
        assert "re-fire of the 2026-08-15 incident" in lines[0]

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

    @pytest.mark.asyncio
    async def test_ack_held_line_at_the_control_state_changed_tier(self, session_maker):
        # #931: neither a CONTROL_STATE_CHANGED (the row's state isn't
        # moving) nor gated by is_urgent_event_type — rendered unconditionally,
        # same tier as the halt/resume lines above.
        await self._add_event(
            session_maker,
            "ANOMALY_ACK_HELD",
            actor="anomaly",
            payload={
                "rule": "ENVELOPE_BREACH_POSTHOC",
                "scope": "B01",
                "ack_since": "2026-08-18",
                "identity": ["per_trade:p1"],
            },
        )
        async with session_maker() as session:
            lines = await urgent_events(session, TODAY)
        assert len(lines) == 1
        assert "ACKNOWLEDGED" in lines[0]
        assert "since 2026-08-18" in lines[0]
        assert "per_trade:p1" in lines[0]
        assert "B01" in lines[0]

    @pytest.mark.asyncio
    async def test_ack_cleared_line_at_the_control_state_changed_tier(self, session_maker):
        await self._add_event(
            session_maker,
            "ANOMALY_ACK_CLEARED",
            actor="anomaly",
            payload={"rule": "ENVELOPE_BREACH_POSTHOC", "scope": "B01"},
        )
        async with session_maker() as session:
            lines = await urgent_events(session, TODAY)
        assert len(lines) == 1
        assert "ACK CLEARED" in lines[0]
        assert "ENVELOPE_BREACH_POSTHOC" in lines[0]


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

    @pytest.mark.parametrize(
        "event_type",
        ["ORDER_SUBMITTED", "CONTROL_CHECK", "ENTRY_FILLED", "INTENT_EXPIRED", "ORDER_DAY_EXPIRED"],
    )
    def test_routine_events_are_not_urgent(self, event_type):
        assert is_urgent_event_type(event_type) is False


class TestHumanDigestRenderer:
    """Tests for the human-readable ntfy digest renderer (#982)."""

    @pytest.mark.asyncio
    async def test_leading_sentence_structure_and_fleet_counts(self, session_maker):
        # 1 book trading (B01 has position), 2 books idle (B02, B03), 1 awaiting fill (B04)
        async with session_maker() as session:
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
                    current_value_per_share=0.5,
                    contracts=1,
                    max_profit=1.0,
                    max_loss=2.0,
                    notes="",
                    rolls=0,
                    status="OPEN",
                    journal={},
                    book_id="B01",
                )
            )
            for book_id in ("B02", "B03"):
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
            session.add(
                BookModel(
                    id="B04",
                    name="awaiting B04",
                    config={"engine_variant": "V0", "underlying": "XSP", "envelope": {}},
                    config_version=1,
                    config_hash="h",
                    starting_capital=10000.0,
                    cash_balance=10000.0,
                    status="ACTIVE",
                    created_at="t0",
                )
            )
            session.add(
                OrderModel(
                    id="o_b04",
                    book_id="B04",
                    position_id=None,
                    order_ref="basis:B04:o1:open",
                    ib_order_id=4,
                    ib_perm_id=4,
                    action="OPEN",
                    combo_legs={"strategy_type": "BULL_PUT_SPREAD", "legs": [], "quantity": 1},
                    order_type="LIMIT",
                    limit_price=-1.05,
                    decision_midpoint=-1.05,
                    status="SUBMITTED",
                    submitted_at=f"{TODAY}T21:00:00",
                    completed_at=None,
                    encumbered_risk=200.0,
                )
            )
            # Add regime readings: 4 detectors EVENT_CATALYST, 3 detectors CALM_BULL
            for variant in ["V0", "V1", "V2", "V3"]:
                session.add(
                    RegimeReadingModel(
                        date=TODAY,
                        book_id="ALL",
                        engine_variant=variant,
                        regime="EVENT_CATALYST",
                    )
                )
            for variant in ["V4", "V5", "V6"]:
                session.add(
                    RegimeReadingModel(
                        date=TODAY,
                        book_id="ALL",
                        engine_variant=variant,
                        regime="CALM_BULL",
                    )
                )
            await session.commit()

        summary = ExecutorRunSummary(
            entries_blocked=[BlockedEntry("B02", "xsp_bps_v1 thin credit (|0.3| < 0.4)")],
            reconciliation="CLEAN",
        )
        _, body, _ = await _digest(session_maker, summary, format="human")
        first_line = body.splitlines()[0]

        # Fleet counts
        assert "1 book trading" in first_line
        assert "2 idle" in first_line
        assert "1 awaiting fill" in first_line
        assert "1 blocked" in first_line

        # Plain-English regime consensus with detector counts
        assert "4 of 7 detectors see an event-driven market; short-premium entries are held" in first_line

        # A blocked entry is the system working, not something the operator
        # must do — the action slot says so rather than crying wolf nightly.
        assert first_line.endswith("; nothing needs you tonight.")
        # The two idle books: one blocked on a named filter, one scanned
        # under a live regime with no fit — dominant reason is a tie broken
        # toward the more directly evidenced one.
        assert "2 books idle (mostly entry filters unmet)" in body
        assert "Blocked: B02: credit too thin (|0.3| < 0.4) (xsp_bps_v1)" in body

    @pytest.mark.asyncio
    async def test_operator_action_names_the_halt_drift_or_anomaly(self, session_maker):
        summary = ExecutorRunSummary(reconciliation="DRIFT")
        _, body, _ = await _digest(session_maker, summary, format="human")
        assert "action needed: resolve reconciliation drift." in body.splitlines()[0]

        summary = ExecutorRunSummary(reconciliation="CLEAN", anomalies=["PNL_SHOCK(B01): day move $2000"])
        _, body, _ = await _digest(session_maker, summary, format="human")
        assert "action needed: review PNL_SHOCK(B01)." in body.splitlines()[0]

        summary = ExecutorRunSummary(
            entries_blocked=[BlockedEntry(None, "STALE_DATA — live telemetry unavailable, no new entries")]
        )
        _, body, _ = await _digest(session_maker, summary, format="human")
        assert "action needed: STALE_DATA — live telemetry unavailable, no new entries." in body.splitlines()[0]

    def test_every_enforced_regime_has_words(self):
        # The leading sentence's entries clause is read off the enforced
        # regime→strategy table; a regime added there without words here
        # would render as a raw enum with no entries clause.
        from backend.digest import REGIME_WORDS
        from backend.eligibility import REGIME_ALLOWED_STRATEGIES

        assert set(REGIME_WORDS) == set(REGIME_ALLOWED_STRATEGIES)

    def test_every_strategy_type_has_words(self):
        from typing import get_args

        from backend.digest import STRATEGY_WORDS
        from backend.models import StrategyType

        assert set(STRATEGY_WORDS) == set(get_args(StrategyType))

    @pytest.mark.asyncio
    async def test_words_beside_fractions_per_book_row(self, session_maker):
        async with session_maker() as session:
            # 1 open position (contracts=1, max_loss=2.0 -> $200 deployed)
            session.add(
                PositionModel(
                    id="p_open",
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
                    status="OPEN",
                    journal={},
                    book_id="B01",
                )
            )
            # 2 closed positions
            for i in range(2):
                session.add(
                    PositionModel(
                        id=f"p_closed_{i}",
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

        _, body, _ = await _digest(session_maker, format="human")
        # Words beside fractions
        assert "2 of 30 closed trades toward the live gate" in body
        assert "1 of 8 positions open" in body
        assert "$200 of $10,000 deployed" in body
        # Ensure dense abbreviations are NOT used in the human per-book row
        assert "gate 2/30" not in body
        assert "pos 1/8" not in body

    @pytest.mark.asyncio
    async def test_blocked_rows_in_words_with_position_details(self, session_maker):
        async with session_maker() as session:
            session.add(
                PositionModel(
                    id="pos_o_tail123",
                    underlying="XSP",
                    strategy_type="LONG_PUT",
                    playbook_id="xsp_tail_put_v1",
                    execution_mode="PAPER",
                    legs=[],
                    entry_date="2026-09-05",
                    expiration_date="2026-10-16",
                    entry_premium=1.0,
                    premium_direction="CREDIT",
                    current_value_per_share=0.5,
                    contracts=1,
                    max_profit=1.0,
                    max_loss=2.0,
                    notes="",
                    rolls=0,
                    status="OPEN",
                    journal={},
                    book_id="B01",
                )
            )
            await session.commit()

        summary = ExecutorRunSummary(
            entries_blocked=[
                BlockedEntry("B01", "xsp_tail_put_v1 dedup (open: pos_o_tail123)"),
                BlockedEntry("B02", "variant V1 reading unavailable"),
                BlockedEntry("B03", "variant V1 reading unavailable"),
                BlockedEntry("B04", "xsp_ic_v1 dedup (open: pos_gone)"),
                BlockedEntry("B05", "consensus 2/3 on CALM_BULL"),
                BlockedEntry("B06", "spy_bps_v1 gated (MAX_DEPLOYED)"),
            ]
        )
        _, body, _ = await _digest(session_maker, summary, format="human")
        # Position named by underlying + strategy + open date with ID in parens
        assert "Blocked: B01: already holds an XSP tail put opened 2026-09-05 (pos_o_tail123)" in body
        # Identical non-dedup block reasons grouped
        assert "Blocked (variant V1 reading unavailable): B02, B03" in body
        # A dedup whose position is no longer there renders the raw reason —
        # never a guessed underlying/strategy.
        assert "Blocked: B04: xsp_ic_v1 dedup (open: pos_gone)" in body
        assert "Blocked: B05: only 2 of 3 engines agree on CALM_BULL" in body
        assert "Blocked: B06: stopped by the risk envelope (MAX_DEPLOYED) (spy_bps_v1)" in body
        # The log line keeps the executor's own strings, byte for byte.
        _, log_body, _ = await _digest(session_maker, summary, format="log")
        assert "Blocked: B01: xsp_tail_put_v1 dedup (open: pos_o_tail123)" in log_body
        assert "Blocked (variant V1 reading unavailable): B02 B03" in log_body

    @pytest.mark.asyncio
    async def test_idle_books_collapse_without_ids_in_human_view(self, session_maker):
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

        _, body, _ = await _digest(session_maker, format="human")
        # B01 (P&L +60) is trading; the two seeded books are idle. No regime
        # reading was written tonight, so the only honest reason is that one.
        assert "2 books idle (regime reading unavailable)" in body
        # IDs must NOT appear in the idle collapse line
        for line in body.splitlines():
            if "idle" in line.lower() and "books" in line.lower():
                assert "B07" not in line
                assert "B11" not in line
                assert "B01" not in line

    @pytest.mark.asyncio
    async def test_idle_reason_is_derived_from_the_runs_own_evidence(self, session_maker):
        # Four idle books, each explained by something the digest already
        # holds: B07 by its own block, B08 by a gate BLOCK event, B09 by its
        # variant reading INSUFFICIENT_DATA, B10 by nothing — scanned under a
        # live regime and produced no candidate.
        async with session_maker() as session:
            for book_id, variant in (("B07", "V0"), ("B08", "V0"), ("B09", "V2"), ("B10", "V0")):
                session.add(
                    BookModel(
                        id=book_id,
                        name=f"idle {book_id}",
                        config={"engine_variant": variant, "underlying": "XSP", "envelope": {}},
                        config_version=1,
                        config_hash="h",
                        starting_capital=10000.0,
                        cash_balance=10000.0,
                        status="ACTIVE",
                        created_at="t0",
                    )
                )
            session.add(
                GateEventModel(
                    book_id="B08", run_at=f"{TODAY}T22:00:00", gate="MAX_DEPLOYED", result="BLOCK", context={}
                )
            )
            session.add(RegimeReadingModel(date=TODAY, book_id="ALL", engine_variant="V0", regime="CALM_BULL"))
            session.add(RegimeReadingModel(date=TODAY, book_id="ALL", engine_variant="V2", regime="INSUFFICIENT_DATA"))
            await session.commit()
        summary = ExecutorRunSummary(entries_blocked=[BlockedEntry("B07", "xsp_bps_v1 unpriceable (zero mid)")])
        async with session_maker() as session:
            data = await build_digest_data(session, summary, TODAY)
        assert data.idle_book_ids == ["B07", "B08", "B09", "B10"]
        assert data.idle_reason_counts == {
            "entry filters unmet": 2,
            "regime reading unavailable": 1,
            "no playbook fits tonight's regime": 1,
        }
        assert "4 books idle (mostly entry filters unmet)" in render_human(data)
        # One shared reason drops the qualifier.
        halted = ExecutorRunSummary(
            entries_blocked=[BlockedEntry(b, "x halted (GLOBAL=HALT_ENTRIES)") for b in ("B07", "B08", "B09", "B10")]
        )
        async with session_maker() as session:
            data = await build_digest_data(session, halted, TODAY)
        assert "4 books idle (entries halted)" in render_human(data)

    @pytest.mark.asyncio
    async def test_benchmark_and_reconciliation_in_a_single_sentence(self, session_maker):
        summary = ExecutorRunSummary(reconciliation="CLEAN")
        _, body, _ = await _digest(session_maker, summary, format="human")
        assert "Reconciliation clean." in body


class TestGateHorizon:
    """Tests for the Live Gate horizon cadence calculation (#982)."""

    @pytest.mark.asyncio
    async def test_not_computable_yet_when_fewer_than_two_closed_trades(self, session_maker):
        # 0 closed trades
        _, body, _ = await _digest(session_maker, format="human")
        assert "At this cadence the earliest book reaches 30 closed trades: not computable yet" in body

        # 1 closed trade
        async with session_maker() as session:
            session.add(
                PositionModel(
                    id="p_one",
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
        _, body, _ = await _digest(session_maker, format="human")
        assert "At this cadence the earliest book reaches 30 closed trades: not computable yet" in body

    @pytest.mark.asyncio
    async def test_cadence_projected_when_two_or_more_closed_trades(self, session_maker):
        async with session_maker() as session:
            for i in range(5):
                session.add(
                    PositionModel(
                        id=f"p_closed_{i}",
                        underlying="XSP",
                        strategy_type="BULL_PUT_SPREAD",
                        execution_mode="PAPER",
                        legs=[],
                        entry_date="2026-08-01",
                        expiration_date="2026-08-10",
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

        _, body, _ = await _digest(session_maker, format="human")
        assert "At this cadence the earliest book reaches 30 closed trades around " in body

    @pytest.mark.asyncio
    async def test_already_reached_when_thirty_closed_trades(self, session_maker):
        async with session_maker() as session:
            for i in range(30):
                session.add(
                    PositionModel(
                        id=f"p_closed_{i}",
                        underlying="XSP",
                        strategy_type="BULL_PUT_SPREAD",
                        execution_mode="PAPER",
                        legs=[],
                        entry_date="2026-08-01",
                        expiration_date="2026-08-10",
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

        _, body, _ = await _digest(session_maker, format="human")
        assert "At this cadence the earliest book reaches 30 closed trades: already reached" in body


class TestNtfyBodyLimit:
    """Tests for the 4,096-byte ntfy notification size limit (#982)."""

    @pytest.mark.asyncio
    async def test_human_body_truncated_to_fit_ntfy_limit(self, session_maker):
        summary = ExecutorRunSummary(
            notes=["Note " + "x" * 200 for _ in range(30)]  # ~6,000 bytes of notes
        )
        _, body, _ = await _digest(session_maker, summary, format="human")
        encoded = body.encode("utf-8")
        assert len(encoded) <= NTFY_BODY_LIMIT_BYTES
        assert body.splitlines()[-1] == "[… cut for ntfy's size limit — full digest in the executor log]"
        # The log line is never cut — it is the full record.
        _, log_body, _ = await _digest(session_maker, summary, format="log")
        assert log_body.count("Note xxx") == 30

    @pytest.mark.asyncio
    async def test_full_matrix_drops_the_roster_before_the_tail(self, session_maker):
        # Forty trading books overflow the limit on their own. The per-book
        # roster goes first (it lives in the log), so the reconciliation and
        # horizon lines at the tail — the ones a person reads for — survive.
        async with session_maker() as session:
            for i in range(2, 42):
                session.add(
                    BookModel(
                        id=f"B{i:02d}",
                        name=f"book {i}",
                        config={"engine_variant": "V0", "underlying": "XSP", "envelope": {}},
                        config_version=1,
                        config_hash="h",
                        starting_capital=10000.0,
                        cash_balance=10000.0,
                        status="ACTIVE",
                        created_at="t0",
                        last_mtm=10000.0 + i,
                    )
                )
            await session.commit()
        summary = ExecutorRunSummary(reconciliation="CLEAN", notes=["Calendar coverage ends 2026-10-01"])
        _, body, _ = await _digest(session_maker, summary, format="human")
        assert len(body.encode("utf-8")) <= NTFY_BODY_LIMIT_BYTES
        assert "41 trading book rows omitted for length" in body
        assert "Reconciliation clean." in body
        assert "Calendar coverage ends 2026-10-01" in body
        assert "cut for ntfy" not in body
        assert "B41 [V0/XSP]" not in body


class TestTwoRenderersOneDataModel:
    """Tests that both renderers derive from the same unified DigestData model (#982)."""

    @pytest.mark.asyncio
    async def test_renderers_contract(self, session_maker):
        async with session_maker() as session:
            data = await build_digest_data(session, ExecutorRunSummary(), TODAY)
            log_lines = render_log_lines(data)
            log_str = render_log_line(data)
            human_str = render_human(data)

            assert isinstance(log_lines, list)
            assert log_str == "\n".join(log_lines)
            assert isinstance(human_str, str)
            # Log format uses dense fractions
            assert "gate 0/30" in log_str
            # Human format does not use dense fractions
            assert "gate 0/30" not in human_str

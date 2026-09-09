"""Tests for the executor digest and urgent-push tiering (backend/digest.py, #72)."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import backend.digest as digest_module
from backend.anomaly import ZOMBIE_FILL, format_anomaly_line, run_post_session_anomalies
from backend.digest import (
    _BANNER_BUDGET_BYTES,
    IDLE_BROKER_UNREACHABLE,
    IDLE_ENTRIES_HALTED,
    IDLE_FILTERS_UNMET,
    IDLE_NO_SIGNAL,
    IDLE_RUN_WIDE_BLOCK,
    IDLE_SPEC_BLOCKED,
    NTFY_BODY_LIMIT_BYTES,
    URGENT_EVENT_TYPES,
    DigestData,
    _bounded_banner,
    _compute_gate_horizon,
    _fit_ntfy_length,
    compose_executor_digest,
    compose_executor_digest_renderings,
    fleet_counts,
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


def assert_one_bucket_per_book(data: DigestData) -> None:
    """The fleet-count invariant spec/supervision.md states in prose: every
    book is in exactly one of trading / idle / awaiting / blocked, so the
    four counts sum to the fleet, and the idle reasons sum to the idle
    bucket. Run on every DigestData this file builds (see `session_maker`)."""
    counts = fleet_counts(data)
    assert counts.trading + counts.idle + counts.awaiting + counts.blocked == len(data.book_rows), counts
    assert sum(data.idle_reason_counts.values()) == counts.idle, data.idle_reason_counts


async def build_digest_data(
    session: AsyncSession, summary: ExecutorRunSummary, today: str | None = None, since: str | None = None
) -> DigestData:
    data = await digest_module.build_digest_data(session, summary, today=today, since=since)
    assert_one_bucket_per_book(data)
    return data


@pytest_asyncio.fixture
async def session_maker(tmp_path, monkeypatch):
    monkeypatch.setenv("HALT_FILE", str(tmp_path / "HALT"))  # sentinel absent by default
    # Every fixture the file builds, whichever entry point it uses, is held
    # to the one-bucket-per-book invariant (compose_* resolve the module
    # attribute at call time, so the patch covers them too).
    real_build = digest_module.build_digest_data

    async def checked_build(
        session: AsyncSession, summary: ExecutorRunSummary, today: str | None = None, since: str | None = None
    ) -> DigestData:
        data = await real_build(session, summary, today=today, since=since)
        assert_one_bucket_per_book(data)
        return data

    monkeypatch.setattr(digest_module, "build_digest_data", checked_build)
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


def _idle_book(book_id: str, variant: str = "V0") -> BookModel:
    return BookModel(
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


def _audit_row(event_type: str, book_id: str | None, payload: dict, run_at: str = f"{TODAY}T22:00:00+00:00"):
    return AuditEventModel(run_at=run_at, book_id=book_id, event_type=event_type, actor="executor", payload=payload)


SINCE = f"{TODAY}T21:00:00+00:00"


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
            _title, body, _priority = await compose_executor_digest(
                session, ExecutorRunSummary(), TODAY, since=since, format="log"
            )
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

        # Fleet counts are one bucket per book — four books, four slots
        # (B01 trading, B03 idle, B04 awaiting, B02 blocked by the run).
        assert first_line.startswith("1 book trading, 1 idle, 1 awaiting fill, 1 blocked; ")

        # Plain-English regime consensus with detector counts. Entries are
        # decided per book from its own variant's reading, so on a split
        # the majority's entries clause is scoped to its variants and the
        # minority is named with its own reading (LOW-2 of the #983
        # re-review: books on V4–V6 are open for income entries tonight).
        assert (
            "4 of 7 detectors see an event-driven market; short-premium entries are held for books on V0 V1 V2 V3; "
            "V4 V5 V6 read a calm bull market; nothing needs you tonight."
        ) in first_line

        # A blocked entry is the system working, not something the operator
        # must do — the action slot says so rather than crying wolf nightly.
        assert first_line.endswith("; nothing needs you tonight.")
        # B03 was not blocked, not gated, not halted, its variant read a
        # live regime, and the executor recorded nothing else for it — the
        # digest says exactly that, never a named cause.
        assert f"1 book idle ({IDLE_NO_SIGNAL})" in body
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

    @pytest.mark.asyncio
    async def test_operator_action_names_an_urgent_event_never_the_reassurance(self, session_maker):
        # H1 of the #983 review: CLOSE_LADDER_EXHAUSTED is written as an
        # audit row only — no halt, no anomaly finding, no BlockedEntry —
        # so the urgent push carried it while the digest beside it said
        # "nothing needs you tonight". The action slot now reads the same
        # rows the urgent push sends.
        async with session_maker() as session:
            session.add(_audit_row("CLOSE_LADDER_EXHAUSTED", "B01", {"detail": "3 rungs conceded, still resting"}))
            await session.commit()
        summary = ExecutorRunSummary(reconciliation="CLEAN")
        _, body, _ = await _digest_since(session_maker, SINCE, summary, format="human")
        first_line = body.splitlines()[0]
        assert "nothing needs you tonight" not in first_line
        assert first_line.endswith(
            "; action needed: CLOSE_LADDER_EXHAUSTED (B01 — XSP): 3 rungs conceded, still resting."
        )
        # The digest and the urgent push come from one read.
        async with session_maker() as session:
            renderings = await compose_executor_digest_renderings(session, summary, TODAY, since=SINCE)
        assert renderings.urgent_lines == ["CLOSE_LADDER_EXHAUSTED (B01 — XSP): 3 rungs conceded, still resting"]

    @pytest.mark.asyncio
    async def test_several_urgent_events_name_the_first_and_count_the_rest(self, session_maker):
        async with session_maker() as session:
            session.add(_audit_row("ORDER_LOST_AT_BROKER", "B01", {"order_ref": "basis:B01:o1:open"}))
            session.add(_audit_row("STALE_MARK_CLOSE_SKIPPED", "B01", {"detail": "mark 3 sessions old"}))
            await session.commit()
        _, body, _ = await _digest_since(session_maker, SINCE, ExecutorRunSummary(reconciliation="CLEAN"), "human")
        first_line = body.splitlines()[0]
        assert "action needed: ORDER_LOST_AT_BROKER (B01 — XSP): basis:B01:o1:open (+1 more in the urgent push)." in (
            first_line
        )

    @pytest.mark.asyncio
    async def test_clean_night_still_reads_nothing_needs_you(self, session_maker):
        # The reassurance survives on a night with no urgent event — and an
        # acknowledgment held is a state the push restates, not an action.
        async with session_maker() as session:
            session.add(
                AuditEventModel(
                    run_at=f"{TODAY}T22:00:00+00:00",
                    book_id="B01",
                    event_type="ANOMALY_ACK_HELD",
                    actor="anomaly",
                    payload={
                        "rule": "ENVELOPE_BREACH_POSTHOC",
                        "ack_since": "2026-08-18",
                        "identity": ["per_trade:p1"],
                    },
                )
            )
            await session.commit()
        _, body, _ = await _digest_since(session_maker, SINCE, ExecutorRunSummary(reconciliation="CLEAN"), "human")
        assert body.splitlines()[0].endswith("; nothing needs you tonight.")

    @pytest.mark.asyncio
    async def test_operator_action_scopes_a_book_halt_to_that_book(self, session_maker):
        async with session_maker() as session:
            session.add(_idle_book("B04"))
            session.add(
                TradingControlModel(
                    scope="B04", state="HALT_ENTRIES", reason="PARTIAL_FILL", actor="anomaly", changed_at="t1"
                )
            )
            await session.commit()
        _, body, _ = await _digest(session_maker, ExecutorRunSummary(reconciliation="CLEAN"), format="human")
        assert "action needed: entries are halted for B04, resolve it in the console." in body.splitlines()[1]

        async with session_maker() as session:
            row = await session.get(TradingControlModel, "GLOBAL")
            row.state = "HALT_ENTRIES"
            await session.commit()
        _, body, _ = await _digest(session_maker, ExecutorRunSummary(reconciliation="CLEAN"), format="human")
        assert "action needed: entries are halted fleet-wide, resolve it in the console." in body

    @pytest.mark.asyncio
    async def test_many_halted_books_are_named_up_to_a_cap_then_counted(self, session_maker):
        # The leading sentence's scope list grows with the same input as the
        # banner; it names a handful and counts the rest.
        async with session_maker() as session:
            for i in range(2, 10):
                session.add(_idle_book(f"B{i:02d}"))
                session.add(
                    TradingControlModel(
                        scope=f"B{i:02d}", state="HALT_ENTRIES", reason="PNL_SHOCK", actor="anomaly", changed_at="t1"
                    )
                )
            await session.commit()
        _, body, _ = await _digest(session_maker, ExecutorRunSummary(reconciliation="CLEAN"), format="human")
        assert "action needed: entries are halted for B02 B03 B04 B05 B06 (+3 more), resolve it in the console." in body

    @pytest.mark.asyncio
    async def test_unanimous_regime_states_the_entries_clause_fleet_wide(self, session_maker):
        async with session_maker() as session:
            for v in ("V0", "V1", "V2"):
                session.add(RegimeReadingModel(date=TODAY, book_id="ALL", engine_variant=v, regime="CALM_BULL"))
            await session.commit()
        _, body, _ = await _digest(session_maker, ExecutorRunSummary(reconciliation="CLEAN"), format="human")
        assert "; 3 of 3 detectors see a calm bull market; income entries are open; " in body.splitlines()[0]

    @pytest.mark.asyncio
    async def test_split_regime_scopes_the_clause_and_names_the_insufficient_variants(self, session_maker):
        async with session_maker() as session:
            for v, regime in (
                ("V0", "CALM_BULL"),
                ("V1", "CALM_BULL"),
                ("V2", "TRENDING_BEAR"),
                ("V3", "INSUFFICIENT_DATA"),
            ):
                session.add(RegimeReadingModel(date=TODAY, book_id="ALL", engine_variant=v, regime=regime))
            await session.commit()
        _, body, _ = await _digest(session_maker, ExecutorRunSummary(reconciliation="CLEAN"), format="human")
        assert (
            "; 2 of 4 detectors see a calm bull market; income entries are open for books on V0 V1; "
            "V2 reads a trending bear market; V3 reports insufficient data; "
        ) in body.splitlines()[0]

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
        # reading was written tonight, but the ledger records nothing for
        # either book (a real run would have BLOCKED them on the missing
        # reading), so the digest says it does not know.
        assert f"2 books idle ({IDLE_NO_SIGNAL})" in body
        # IDs must NOT appear in the idle collapse line
        for line in body.splitlines():
            if "idle" in line.lower() and "books" in line.lower():
                assert "B07" not in line
                assert "B11" not in line
                assert "B01" not in line

    @pytest.mark.asyncio
    async def test_idle_reason_is_derived_from_the_runs_own_evidence(self, session_maker):
        # Four books holding nothing: B07 is blocked by the run itself (its
        # own bucket, not idle in the human body — the log line still lists
        # it idle); B08 by a gate BLOCK event, which the executor always
        # records beside a book-scoped BlockedEntry (LOW-1 of the #983
        # re-review: a gate block is the blocked bucket, never an idle
        # reason); B09 on a variant reading INSUFFICIENT_DATA, likewise
        # blocked by the executor; B10 by nothing the ledger records.
        async with session_maker() as session:
            for book_id, variant in (("B07", "V0"), ("B08", "V0"), ("B09", "V2"), ("B10", "V0")):
                session.add(_idle_book(book_id, variant))
            session.add(
                GateEventModel(
                    book_id="B08", run_at=f"{TODAY}T22:00:00", gate="MAX_DEPLOYED", result="BLOCK", context={}
                )
            )
            session.add(RegimeReadingModel(date=TODAY, book_id="ALL", engine_variant="V0", regime="CALM_BULL"))
            session.add(RegimeReadingModel(date=TODAY, book_id="ALL", engine_variant="V2", regime="INSUFFICIENT_DATA"))
            await session.commit()
        summary = ExecutorRunSummary(
            entries_blocked=[
                BlockedEntry("B07", "xsp_bps_v1 unpriceable (zero mid)"),
                BlockedEntry("B08", "xsp_bps_v1 gated (MAX_DEPLOYED)"),
                BlockedEntry("B09", "variant V2 reading unavailable"),
            ]
        )
        async with session_maker() as session:
            data = await build_digest_data(session, summary, TODAY)
        assert data.idle_book_ids == ["B07", "B08", "B09", "B10"]
        assert data.blocked_book_ids == ["B07", "B08", "B09"]
        assert data.idle_reason_counts == {IDLE_NO_SIGNAL: 1}
        human = render_human(data)
        assert f"1 book idle ({IDLE_NO_SIGNAL})" in human
        assert human.splitlines()[0].startswith("1 book trading, 1 idle, 3 blocked; ")
        assert "Blocked: B08: stopped by the risk envelope (MAX_DEPLOYED) (xsp_bps_v1)" in human
        assert "4 book(s) idle (no positions, gate 0/30): B07 B08 B09 B10" in render_log_line(data)
        # One shared reason drops the qualifier.
        async with session_maker() as session:
            row = await session.get(TradingControlModel, "GLOBAL")
            row.state = "HALT_ENTRIES"
            await session.commit()
            data = await build_digest_data(session, ExecutorRunSummary(), TODAY)
        assert f"4 books idle ({IDLE_ENTRIES_HALTED})" in render_human(data)

    @pytest.mark.asyncio
    async def test_idle_reason_reads_the_executors_own_entry_audit_rows(self, session_maker):
        # H2 of the #983 review: SCAN_BLOCKED and SPEC_HARD_BLOCKED drop a
        # book out of the entry loop with an audit row and no BlockedEntry.
        # Those rows are on the ledger tonight, so the digest reads them.
        async with session_maker() as session:
            for book_id in ("B07", "B08", "B09"):
                session.add(_idle_book(book_id))
            session.add(_audit_row("SCAN_BLOCKED", "B07", {"reason": "MAX_POSITIONS: 8/8"}))
            session.add(
                _audit_row("SPEC_HARD_BLOCKED", "B08", {"playbook": "xsp_bps_v1", "blocks": ["CAPITAL_EXCEEDED"]})
            )
            session.add(RegimeReadingModel(date=TODAY, book_id="ALL", engine_variant="V0", regime="CALM_BULL"))
            await session.commit()
        async with session_maker() as session:
            data = await build_digest_data(session, ExecutorRunSummary(), TODAY, since=SINCE)
        assert data.idle_reason_counts == {IDLE_FILTERS_UNMET: 1, IDLE_SPEC_BLOCKED: 1, IDLE_NO_SIGNAL: 1}
        # Yesterday's row is not tonight's evidence.
        async with session_maker() as session:
            data = await build_digest_data(session, ExecutorRunSummary(), TODAY, since=f"{TODAY}T23:00:00+00:00")
        assert data.idle_reason_counts == {IDLE_NO_SIGNAL: 3}

    @pytest.mark.asyncio
    async def test_idle_books_never_read_a_regime_cause_the_ledger_does_not_carry(self, session_maker):
        # H2 failure scenario A: every GLD playbook came back ineligible on
        # a missing price history — the executor discards that reason, so
        # the digest must not claim the regime gate (or anything else).
        async with session_maker() as session:
            for i in range(6):
                session.add(_idle_book(f"B{i + 10:02d}"))
            session.add(RegimeReadingModel(date=TODAY, book_id="ALL", engine_variant="V0", regime="CALM_BULL"))
            await session.commit()
        _, body, _ = await _digest(session_maker, ExecutorRunSummary(reconciliation="CLEAN"), format="human")
        assert f"6 books idle ({IDLE_NO_SIGNAL})" in body
        assert "regime" not in body.split("idle (")[1].split(")")[0]

    @pytest.mark.asyncio
    async def test_run_wide_block_explains_every_idle_book(self, session_maker):
        # H2 failure scenario B: the entry phase aborted after a roll broker
        # error — the scan never reached the books, and the idle line says
        # so instead of asserting they were scanned under a live regime.
        async with session_maker() as session:
            for book_id in ("B07", "B08"):
                session.add(_idle_book(book_id))
            session.add(RegimeReadingModel(date=TODAY, book_id="ALL", engine_variant="V0", regime="CALM_BULL"))
            await session.commit()
        summary = ExecutorRunSummary(
            entries_blocked=[BlockedEntry(None, "entry phase aborted after roll broker error")], reconciliation="CLEAN"
        )
        _, body, _ = await _digest(session_maker, summary, format="human")
        assert "action needed: entry phase aborted after roll broker error." in body.splitlines()[0]
        assert f"2 books idle ({IDLE_RUN_WIDE_BLOCK})" in body

    @pytest.mark.asyncio
    async def test_mid_fleet_abort_explains_the_unscanned_books(self, session_maker):
        # LOW-3 of the #983 re-review: an order-path BrokerError aborts the
        # entry phase mid-fleet with an ENTRY_PHASE_ABORTED row (book None)
        # and no run-wide BlockedEntry — the offending book is blocked, the
        # books after it were never scanned, and the row says so.
        async with session_maker() as session:
            for book_id in ("B07", "B08", "B09"):
                session.add(_idle_book(book_id))
            session.add(RegimeReadingModel(date=TODAY, book_id="ALL", engine_variant="V0", regime="CALM_BULL"))
            session.add(_audit_row("ORDER_REJECTED", "B07", {"order_ref": "basis:B07:o1:open", "error": "boom"}))
            session.add(_audit_row("ENTRY_PHASE_ABORTED", None, {"after": "B07:xsp_bps_v1"}))
            await session.commit()
        summary = ExecutorRunSummary(
            entries_blocked=[BlockedEntry("B07", "xsp_bps_v1 rejected — submission phase aborted")],
            reconciliation="CLEAN",
        )
        async with session_maker() as session:
            data = await build_digest_data(session, summary, TODAY, since=SINCE)
        assert data.blocked_book_ids == ["B07"]
        assert data.idle_reason_counts == {IDLE_RUN_WIDE_BLOCK: 2}
        # A book-scoped ENTRY_PHASE_ABORTED row (none is written today) is
        # not a run-wide abort; yesterday's row is not tonight's evidence.
        async with session_maker() as session:
            data = await build_digest_data(session, summary, TODAY, since=f"{TODAY}T23:00:00+00:00")
        assert data.idle_reason_counts == {IDLE_NO_SIGNAL: 2}

    @pytest.mark.asyncio
    async def test_a_scanned_books_own_reason_survives_a_later_mid_run_abort(self, session_maker):
        # LOW-1 of the #983 re-review: entry_audit.phase_aborted is a
        # run-level flag, not evidence about any one book — a book the run
        # DID reach and record SCAN_BLOCKED for keeps that reason instead of
        # being relabelled run-wide just because the phase later aborted for
        # a book after it.
        async with session_maker() as session:
            for book_id in ("B07", "B08"):
                session.add(_idle_book(book_id))
            session.add(RegimeReadingModel(date=TODAY, book_id="ALL", engine_variant="V0", regime="CALM_BULL"))
            session.add(_audit_row("SCAN_BLOCKED", "B07", {}))
            session.add(_audit_row("ENTRY_PHASE_ABORTED", None, {"after": "B08:xsp_bps_v1"}))
            await session.commit()
        summary = ExecutorRunSummary(reconciliation="CLEAN")
        async with session_maker() as session:
            data = await build_digest_data(session, summary, TODAY, since=SINCE)
        assert data.idle_reason_counts == {IDLE_FILTERS_UNMET: 1, IDLE_RUN_WIDE_BLOCK: 1}

    @pytest.mark.asyncio
    async def test_broker_unreachable_explains_idle_books_over_no_signal(self, session_maker):
        # L2 of the #983 re-review: the broker-unreachable rung was live but
        # unpinned by any test.
        async with session_maker() as session:
            for book_id in ("B07", "B08"):
                session.add(_idle_book(book_id))
            session.add(RegimeReadingModel(date=TODAY, book_id="ALL", engine_variant="V0", regime="CALM_BULL"))
            await session.commit()
        summary = ExecutorRunSummary(
            broker_ok=False, broker_api_errors=[(2110, "Connectivity between TWS and server is broken.")]
        )
        async with session_maker() as session:
            data = await build_digest_data(session, summary, TODAY, since=SINCE)
        assert data.idle_reason_counts == {IDLE_BROKER_UNREACHABLE: 2}

    @pytest.mark.asyncio
    async def test_one_halted_book_does_not_halt_thirty(self, session_maker):
        # M1 of the #983 review: a book-scoped HALT_ENTRIES row explains
        # that book only; the other thirty read on their own merits.
        async with session_maker() as session:
            for i in range(2, 33):
                session.add(_idle_book(f"B{i:02d}"))
            session.add(
                TradingControlModel(
                    scope="B04", state="HALT_ENTRIES", reason="PARTIAL_FILL", actor="anomaly", changed_at="t1"
                )
            )
            session.add(RegimeReadingModel(date=TODAY, book_id="ALL", engine_variant="V0", regime="CALM_BULL"))
            await session.commit()
        async with session_maker() as session:
            data = await build_digest_data(session, ExecutorRunSummary(reconciliation="CLEAN"), TODAY)
        assert data.halted_scopes == ["B04"]
        assert data.idle_reason_counts == {IDLE_ENTRIES_HALTED: 1, IDLE_NO_SIGNAL: 30}
        assert f"31 books idle (mostly {IDLE_NO_SIGNAL})" in render_human(data)

    @pytest.mark.asyncio
    async def test_all_variants_insufficient_data_renders_in_both_forms(self, session_maker):
        # LOW-1 of the #983 review: the pre-#982 log line rendered this night
        # as "Regime split:  (…)" with an empty group; the log line now says
        # what it means, and both renderings are pinned here.
        async with session_maker() as session:
            for v in ("V0", "V1"):
                session.add(RegimeReadingModel(date=TODAY, book_id="ALL", engine_variant=v, regime="INSUFFICIENT_DATA"))
            await session.commit()
        _, log_body, _ = await _digest(session_maker, format="log")
        assert "Regime: INSUFFICIENT_DATA (V0 V1 insufficient data)" in log_body
        assert "Regime split" not in log_body
        _, body, _ = await _digest(session_maker, format="human")
        assert "all 2 detectors report insufficient data" in body.splitlines()[0]

    @pytest.mark.asyncio
    async def test_benchmark_and_reconciliation_in_a_single_sentence(self, session_maker):
        summary = ExecutorRunSummary(reconciliation="CLEAN")
        _, body, _ = await _digest(session_maker, summary, format="human")
        assert "Reconciliation clean." in body


class TestCatalystConfound:
    """#994: the digest counts book-nights the regime race could not
    discriminate — a book whose own reading reached the catalyst entry
    filter (regime already permitted it) while some other variant read
    EVENT_CATALYST outright the same night."""

    @pytest.mark.asyncio
    async def test_confounded_book_night_renders_in_both_forms(self, session_maker):
        async with session_maker() as session:
            session.add(_idle_book("B07", variant="V1"))
            # V0 read EVENT_CATALYST tonight (do-nothing outright); B07 is
            # on V1, whose own reading (CALM_BULL) already passed the
            # regime gate — the catalyst filter is what actually blocked it.
            session.add(RegimeReadingModel(date=TODAY, book_id="ALL", engine_variant="V0", regime="EVENT_CATALYST"))
            session.add(RegimeReadingModel(date=TODAY, book_id="ALL", engine_variant="V1", regime="CALM_BULL"))
            session.add(
                _audit_row(
                    "ENTRY_NOT_TAKEN",
                    "B07",
                    {
                        "stage": "ineligible",
                        "reason": "Entry filter: catalyst within 14 DTE — this playbook blocks new entries around events.",
                        "reasons": [
                            "Entry filter: catalyst within 14 DTE — this playbook blocks new entries around events."
                        ],
                    },
                )
            )
            await session.commit()
        async with session_maker() as session:
            data = await build_digest_data(session, ExecutorRunSummary(), TODAY, since=SINCE)
        assert data.catalyst_confound.confounded == 1
        assert data.catalyst_confound.total == 1
        line = "1 of 1 book-nights tonight were indistinguishable across variants (catalyst block)"
        assert line in render_human(data)
        assert line in render_log_line(data)

    @pytest.mark.asyncio
    async def test_clean_night_renders_no_line(self, session_maker):
        # A book blocked by something other than the catalyst filter, on a
        # night nothing read EVENT_CATALYST, is not confounded — the race
        # actually discriminated, so there is nothing to say.
        async with session_maker() as session:
            session.add(_idle_book("B08", variant="V1"))
            session.add(RegimeReadingModel(date=TODAY, book_id="ALL", engine_variant="V1", regime="CALM_BULL"))
            session.add(
                _audit_row(
                    "ENTRY_NOT_TAKEN",
                    "B08",
                    {
                        "stage": "ineligible",
                        "reason": "Entry filter: VIX=12.0 outside required range [15-40].",
                        "reasons": ["Entry filter: VIX=12.0 outside required range [15-40]."],
                    },
                )
            )
            await session.commit()
        async with session_maker() as session:
            data = await build_digest_data(session, ExecutorRunSummary(), TODAY, since=SINCE)
        assert data.catalyst_confound.confounded == 0
        assert data.catalyst_confound.total == 1
        _, human, _ = await _digest(session_maker, format="human")
        _, log, _ = await _digest(session_maker, format="log")
        assert "indistinguishable across variants" not in human
        assert "indistinguishable across variants" not in log

    @pytest.mark.asyncio
    async def test_catalyst_block_with_no_event_catalyst_reading_is_not_confounded(self, session_maker):
        # Every variant tonight read a live, entry-permitting regime — the
        # catalyst filter blocked B09 alone, with nothing to be
        # indistinguishable from, so it does not count.
        async with session_maker() as session:
            session.add(_idle_book("B09", variant="V1"))
            session.add(RegimeReadingModel(date=TODAY, book_id="ALL", engine_variant="V1", regime="CALM_BULL"))
            session.add(
                _audit_row(
                    "ENTRY_NOT_TAKEN",
                    "B09",
                    {
                        "stage": "ineligible",
                        "reason": "Entry filter: catalyst within 14 DTE — this playbook blocks new entries around events.",
                        "reasons": [
                            "Entry filter: catalyst within 14 DTE — this playbook blocks new entries around events."
                        ],
                    },
                )
            )
            await session.commit()
        async with session_maker() as session:
            data = await build_digest_data(session, ExecutorRunSummary(), TODAY, since=SINCE)
        assert data.catalyst_confound.confounded == 0
        assert data.catalyst_confound.total == 1


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

    def test_corrupt_entry_date_degrades_to_not_computable_instead_of_raising(self):
        # LOW-4 of the #983 review: a parseable-but-absurd entry_date makes
        # the rate arbitrarily small; the line degrades, the push survives.
        line = _compute_gate_horizon(TODAY, fleet_closed_trades=2, leading_book_closed=2, first_entry_date="0001-01-01")
        assert line.endswith(": not computable yet")
        assert _compute_gate_horizon(TODAY, 2, 2, "not-a-date").endswith(": not computable yet")
        assert "around " in _compute_gate_horizon(TODAY, 2, 2, "2026-08-01")


class TestNtfyBodyLimit:
    """Tests for the 4,096-byte ntfy notification size limit (#982)."""

    @staticmethod
    def _lines(last_len: int) -> list[str]:
        # 40 lines of 100 bytes joined by "\n" (4,039 bytes) + "\n" + last.
        return ["a" * 100] * 40 + ["b" * last_len]

    def test_exactly_the_limit_is_untouched(self):
        lines = self._lines(56)
        body = "\n".join(lines)
        assert len(body.encode("utf-8")) == NTFY_BODY_LIMIT_BYTES
        assert _fit_ntfy_length(lines) == body

    def test_one_byte_over_is_cut_to_whole_lines_under_the_limit(self):
        lines = self._lines(57)
        assert len("\n".join(lines).encode("utf-8")) == NTFY_BODY_LIMIT_BYTES + 1
        fitted = _fit_ntfy_length(lines)
        assert len(fitted.encode("utf-8")) <= NTFY_BODY_LIMIT_BYTES
        kept = fitted.splitlines()
        assert kept[-1] == "[… cut for ntfy's size limit — full digest in the executor log]"
        assert kept[:-1] == lines[: len(kept) - 1]  # whole lines from the top, in order
        assert "b" * 57 not in fitted

    def test_one_multibyte_char_over_is_cut(self):
        lines = self._lines(55) + ["—"]  # 4,096 + "\n" + 3 bytes
        fitted = _fit_ntfy_length(lines)
        assert len(fitted.encode("utf-8")) <= NTFY_BODY_LIMIT_BYTES
        assert fitted.endswith("[… cut for ntfy's size limit — full digest in the executor log]")

    def test_control_banner_survives_a_pathological_line_and_the_fit_is_total(self):
        # LOW-5 of the #983 review, then M1 of the re-review: a pathological
        # line right after the banner leaves the banner and the marker; and
        # NOTHING is exempt from the limit — over it ntfy delivers the whole
        # message as an attachment, so an over-long body that "carries the
        # halt" is exactly the notification that never shows the halt.
        banner = "⛔ GLOBAL HALT_ENTRIES since 2026-08-18T01:00 — RECONCILIATION_DRIFT"
        fitted = _fit_ntfy_length([banner, "x" * (NTFY_BODY_LIMIT_BYTES + 1)])
        assert fitted.splitlines() == [banner, "[… cut for ntfy's size limit — full digest in the executor log]"]
        long_banner = "⛔ " + "r" * (NTFY_BODY_LIMIT_BYTES + 1)
        fitted = _fit_ntfy_length([long_banner, "tail"])
        assert len(fitted.encode("utf-8")) <= NTFY_BODY_LIMIT_BYTES
        assert fitted == "[… cut for ntfy's size limit — full digest in the executor log]"

    def test_bounded_banner_keeps_whole_lines_and_counts_the_rest(self):
        # M1 of the #983 re-review: halts latch and the banner re-emits a
        # line per un-resumed row every night, so it is bounded at the
        # source — whole lines within its budget, the rest one counted line.
        line = "⛔ B{:02d} — XSP HALT_ENTRIES since 2026-08-18T01:00 — PNL_SHOCK: day move $2,000 — clears: operator resume"
        rows = [line.format(i) for i in range(2, 42)]
        assert _bounded_banner(rows[:3]) == rows[:3]
        bounded = _bounded_banner(rows)
        assert len("\n".join(bounded).encode("utf-8")) <= _BANNER_BUDGET_BYTES
        kept = bounded[:-1]
        assert kept == rows[: len(kept)] and 0 < len(kept) < len(rows)
        assert (
            bounded[-1]
            == f"⛔ +{len(rows) - len(kept)} more scopes halted — every row is in the executor log and the console"
        )
        # One over-long first line is kept whole (the fit downstream is the
        # last resort); the count still names every other row.
        huge = "⛔ " + "r" * _BANNER_BUDGET_BYTES
        assert _bounded_banner([huge, *rows[:2]]) == [
            huge,
            "⛔ +2 more scopes halted — every row is in the executor log and the console",
        ]

    @pytest.mark.asyncio
    async def test_banner_alone_over_the_limit_still_fits_with_its_first_line_intact(self, session_maker):
        # M1 boundary: forty latched book halts whose banner alone exceeds
        # 4,096 bytes. The body comes in under the limit, the first banner
        # line and the count survive, and the log line keeps every row.
        async with session_maker() as session:
            for i in range(2, 42):
                session.add(_idle_book(f"B{i:02d}"))
                session.add(
                    TradingControlModel(
                        scope=f"B{i:02d}",
                        state="HALT_ENTRIES",
                        reason="PNL_SHOCK: day move $2,000 on a $10,000 basis — clears: operator resume",
                        actor="anomaly",
                        changed_at=f"{TODAY}T01:00:00+00:00",
                    )
                )
            await session.commit()
        async with session_maker() as session:
            data = await build_digest_data(session, ExecutorRunSummary(reconciliation="CLEAN"), TODAY)
        assert len(data.banner) == 40
        assert len("\n".join(data.banner).encode("utf-8")) > NTFY_BODY_LIMIT_BYTES
        body = render_human(data)
        assert len(body.encode("utf-8")) <= NTFY_BODY_LIMIT_BYTES
        lines = body.splitlines()
        assert lines[0] == data.banner[0]
        assert lines[0].startswith("⛔ B02 — XSP HALT_ENTRIES since ")
        assert any(
            line.startswith("⛔ +")
            and line.endswith("more scopes halted — every row is in the executor log and the console")
            for line in lines
        )
        assert "action needed: entries are halted for B02 B03 B04 B05 B06 (+35 more)" in body
        assert "cut for ntfy" not in body
        log_body = render_log_line(data)
        assert log_body.count("HALT_ENTRIES since") == 40

    @pytest.mark.asyncio
    async def test_halted_night_with_an_enormous_tail_keeps_the_banner(self, session_maker):
        async with session_maker() as session:
            row = await session.get(TradingControlModel, "GLOBAL")
            row.state = "HALT_ENTRIES"
            row.reason = "RECONCILIATION_DRIFT: 2 discrepancies"
            row.changed_at = f"{TODAY}T01:00:00+00:00"
            await session.commit()
        summary = ExecutorRunSummary(reconciliation="DRIFT", notes=["Note " + "x" * 200 for _ in range(30)])
        _, body, _ = await _digest(session_maker, summary, format="human")
        assert len(body.encode("utf-8")) <= NTFY_BODY_LIMIT_BYTES
        assert body.splitlines()[0].startswith("⛔ GLOBAL HALT_ENTRIES")
        assert body.splitlines()[-1] == "[… cut for ntfy's size limit — full digest in the executor log]"

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

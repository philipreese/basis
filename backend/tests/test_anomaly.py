"""Tests for the anomaly auto-halt rules (backend/anomaly.py, #71).

One test per rule trigger from spec/supervision.md §6.2–6.3, plus the
escalation-only guarantee (FLATTEN_REQUESTED is never downgraded) and the
audit trail every firing must leave.
"""

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.anomaly import (
    ENVELOPE_BREACH_POSTHOC,
    PERMISSIONS_REFUSED,
    PNL_SHOCK,
    PREVIEW_INFRA_FAILURE,
    REPEATED_REJECTION,
    ZOMBIE_FILL,
    AnomalyFinding,
    _should_alert,
    _trailing_market_sessions,
    book_mtm,
    check_duplicate_order,
    check_order_leg_collision,
    check_preview_infra_failure,
    check_repeated_rejection,
    check_zombie_fills,
    classify_preview_refusal,
    entry_signature,
    run_post_session_anomalies,
)
from backend.models import (
    AuditEventModel,
    Base,
    BookModel,
    FillModel,
    OrderModel,
    PositionModel,
    TradingControlModel,
)
from backend.trading_control import ACTIVE, GLOBAL_SCOPE, HALT_ENTRIES

TODAY = "2026-08-18"


def _book(book_id: str = "B01", cash: float = 10000.0) -> BookModel:
    return BookModel(
        id=book_id,
        name=book_id,
        config={"engine_variant": "V0", "underlying": "XSP", "envelope": {}},
        config_version=1,
        config_hash="h",
        starting_capital=10000.0,
        cash_balance=cash,
        status="ACTIVE",
        created_at="t0",
    )


def _position(pos_id: str, book_id: str = "B01", max_loss: float = 2.0, current: float = 1.0) -> PositionModel:
    return PositionModel(
        id=pos_id,
        underlying="XSP",
        strategy_type="BULL_PUT_SPREAD",
        execution_mode="PAPER",
        legs=[
            {
                "option_type": "PUT",
                "direction": "SHORT",
                "strike": 610.0,
                "expiration": "2026-12-18",
                "delta": -0.3,
                "theta": 0.02,
                "vega": 0.1,
                "gamma": 0.01,
            }
        ],
        entry_date="2026-08-10",
        expiration_date="2026-12-18",
        entry_premium=1.0,
        premium_direction="CREDIT",
        current_value_per_share=current,
        contracts=1,
        max_profit=1.0,
        max_loss=max_loss,
        notes="",
        rolls=0,
        status="OPEN",
        journal={},
        book_id=book_id,
    )


def _rejection(day: str, event_type: str = "ORDER_REJECTED") -> AuditEventModel:
    return AuditEventModel(
        run_at=f"{day}T22:00:00+00:00", book_id="B01", event_type=event_type, actor="executor", payload={}
    )


@pytest_asyncio.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        session.add(_book("B01"))
        session.add(TradingControlModel(scope="GLOBAL", state="ACTIVE", reason="", actor="t", changed_at="t0"))
        session.add(TradingControlModel(scope="B01", state="ACTIVE", reason="", actor="t", changed_at="t0"))
        await session.commit()
    yield maker
    await engine.dispose()


async def _sweep(maker):
    async with maker() as session:
        return await run_post_session_anomalies(session, TODAY)


async def _run_sweep_on(maker, today: str):
    async with maker() as session:
        return await run_post_session_anomalies(session, today)


async def _state(maker, scope: str) -> str:
    async with maker() as session:
        return (await session.get(TradingControlModel, scope)).state


class TestRepeatedRejection:
    @pytest.mark.asyncio
    async def test_two_rejections_tonight_halt_globally(self, session_maker):
        async with session_maker() as session:
            session.add_all([_rejection(TODAY), _rejection(TODAY)])
            await session.commit()
        findings = await _sweep(session_maker)
        assert [f.rule for f in findings] == [REPEATED_REJECTION]
        assert await _state(session_maker, "GLOBAL") == "HALT_ENTRIES"

    @pytest.mark.asyncio
    async def test_two_preview_refusals_tonight_halt_globally(self, session_maker):
        # #744: ENTRY_PREVIEW_REFUSED predates this enumeration and was
        # missing from it — a broken builder/pricing path repeatedly failing
        # IBKR's whatIf preview accumulated audit rows but never tripped the
        # halt. Pooled with real rejections at the same threshold (see
        # anomaly.py's _REJECTION_EVENTS comment).
        async with session_maker() as session:
            session.add_all([_rejection(TODAY, "ENTRY_PREVIEW_REFUSED"), _rejection(TODAY, "ENTRY_PREVIEW_REFUSED")])
            await session.commit()
        findings = await _sweep(session_maker)
        assert [f.rule for f in findings] == [REPEATED_REJECTION]
        assert await _state(session_maker, "GLOBAL") == "HALT_ENTRIES"

    @pytest.mark.asyncio
    async def test_preview_refusal_pools_with_a_real_rejection(self, session_maker):
        async with session_maker() as session:
            session.add_all([_rejection(TODAY, "ENTRY_PREVIEW_REFUSED"), _rejection(TODAY, "ORDER_REJECTED")])
            await session.commit()
        findings = await _sweep(session_maker)
        assert [f.rule for f in findings] == [REPEATED_REJECTION]

    @pytest.mark.asyncio
    async def test_one_rejection_tonight_is_tolerated(self, session_maker):
        async with session_maker() as session:
            session.add(_rejection(TODAY))
            await session.commit()
        findings = await _sweep(session_maker)
        assert findings == []
        assert await _state(session_maker, "GLOBAL") == "ACTIVE"

    @pytest.mark.asyncio
    async def test_three_across_trailing_sessions_halt(self, session_maker):
        # TODAY is Tuesday 2026-08-18; the 3 trading sessions on/before it
        # are 08-14 (Fri), 08-17 (Mon), 08-18 (Tue) — 08-15/16 are a weekend.
        async with session_maker() as session:
            session.add_all([_rejection("2026-08-14"), _rejection("2026-08-17"), _rejection(TODAY)])
            await session.commit()
        findings = await _sweep(session_maker)
        assert [f.rule for f in findings] == [REPEATED_REJECTION]

    @pytest.mark.asyncio
    async def test_trailing_buckets_by_market_date_not_utc_prefix(self, session_maker):
        # #537: a UTC date-prefix bucket merges two distinct ET evenings when
        # the earlier one's run finishes after 00:00 UTC (19:15 ET = 00:15
        # UTC the next day) — both events land under the SAME UTC date. That
        # false merge frees up a bucket slot, so sorted(buckets)[:3] reaches
        # back to a 4th real session and reports "4 rejections across
        # trailing 3 sessions" instead of the correct 3. Bucketing by MARKET
        # date keeps each evening in its own bucket.
        async with session_maker() as session:
            session.add_all(
                [
                    # Jan 12 evening, run finishes past midnight UTC.
                    AuditEventModel(
                        run_at="2026-01-13T00:15:00+00:00",
                        book_id="B01",
                        event_type="ORDER_REJECTED",
                        actor="executor",
                        payload={},
                    ),
                    # Jan 13 evening — UTC-prefix bucketing collides this
                    # with the row above (both read as UTC date 2026-01-13).
                    AuditEventModel(
                        run_at="2026-01-13T23:50:00+00:00",
                        book_id="B01",
                        event_type="ORDER_REJECTED",
                        actor="executor",
                        payload={},
                    ),
                    _rejection("2026-01-14"),
                    _rejection("2026-01-15"),  # today
                ]
            )
            await session.commit()
        async with session_maker() as session:
            findings = await run_post_session_anomalies(session, "2026-01-15")
        [finding] = [f for f in findings if f.rule == REPEATED_REJECTION]
        assert finding.detail == "3 rejections across trailing 3 sessions"


class TestRepeatedRejectionAgeBound:
    """#927: the trailing bucket is bounded to the last 3 MARKET SESSIONS BY
    CALENDAR (_trailing_market_sessions), not the 3 most recent dates that
    happen to have a rejection — a single stale burst must roll off after 3
    real sessions, even if no later session has a rejection of its own."""

    @pytest.mark.asyncio
    async def test_burst_trips_while_still_within_the_trailing_window(self, session_maker):
        # 2026-08-27 (Thu) burst + one more rejection the next session
        # (08-28, Fri) — both fall within the 3 trading sessions ending
        # 08-28: {08-26 Wed, 08-27 Thu, 08-28 Fri}.
        async with session_maker() as session:
            session.add_all([_rejection("2026-08-27"), _rejection("2026-08-27"), _rejection("2026-08-28")])
            await session.commit()
            finding = await check_repeated_rejection(session, "2026-08-28")
        assert finding is not None
        assert finding.detail == "3 rejections across trailing 3 sessions"

    @pytest.mark.asyncio
    async def test_burst_ages_out_once_a_third_session_has_passed(self, session_maker):
        # The same 08-27 burst, plus one unrelated rejection on 08-31 (Mon),
        # evaluated on 09-01 (Tue). The 3 trading sessions ending 09-01 are
        # {08-28 Fri, 08-31 Mon, 09-01 Tue} — 08-27 (Thu) is the 4th session
        # back and has rolled off. One in-window rejection (08-31) is not
        # enough on its own to trip either threshold.
        async with session_maker() as session:
            session.add_all([_rejection("2026-08-27"), _rejection("2026-08-27"), _rejection("2026-08-31")])
            await session.commit()
            finding = await check_repeated_rejection(session, "2026-09-01")
        assert finding is None

    @pytest.mark.asyncio
    async def test_aged_out_window_does_not_trip_the_full_sweep_either(self, session_maker):
        # Same shape as the issue's incident, run through the full sweep
        # (not just check_repeated_rejection directly) to confirm nothing
        # else in the pipeline re-derives a halt from the stale burst.
        async with session_maker() as session:
            session.add_all([_rejection("2026-08-27"), _rejection("2026-08-27"), _rejection("2026-08-31")])
            await session.commit()
        async with session_maker() as session:
            findings = await run_post_session_anomalies(session, "2026-09-01")
        assert findings == []
        assert await _state(session_maker, "GLOBAL") == "ACTIVE"


class TestTrailingMarketSessions:
    def test_skips_the_weekend(self):
        # 3 sessions ending Tuesday 2026-08-18 skip the 08-15/16 weekend.
        assert _trailing_market_sessions("2026-08-18", 3) == frozenset({"2026-08-14", "2026-08-17", "2026-08-18"})

    def test_skips_a_market_holiday_too(self):
        # Labor Day 2026-09-07 (Mon) is a full closure — the 3 sessions
        # ending 09-08 (Tue) skip both the 09-05/06 weekend AND 09-07.
        assert _trailing_market_sessions("2026-09-08", 3) == frozenset({"2026-09-03", "2026-09-04", "2026-09-08"})


class TestPreviewInfraFailure:
    """#927: whatIfOrder API-error/timeout refusals are a gateway failure,
    not evidence the broker's rules are wrong — classified 'infra' by
    classify_preview_refusal, excluded from REPEATED_REJECTION, and given
    their own same-night burst threshold (>=3) so an outage still halts
    loudly the night it happens."""

    @pytest.mark.asyncio
    async def test_infra_events_do_not_count_toward_repeated_rejection(self, session_maker):
        since = f"{TODAY}T16:00:00+00:00"
        async with session_maker() as session:
            for reason in (
                "whatIfOrder resolved with an API error instead of an order state: []",
                "whatIfOrder timed out - no usable order state within 30s",
            ):
                e = _rejection(TODAY, "ENTRY_PREVIEW_REFUSED")
                e.payload = {"reason": reason, "playbook": "spy_bull_put_spread_v1"}
                session.add(e)
            await session.commit()
            findings = await run_post_session_anomalies(session, TODAY, since=since)
        assert [f.rule for f in findings] == []
        assert await _state(session_maker, "GLOBAL") == "ACTIVE"

    @pytest.mark.asyncio
    async def test_same_night_infra_burst_halts_globally(self, session_maker):
        since = f"{TODAY}T16:00:00+00:00"
        async with session_maker() as session:
            for _ in range(3):
                e = _rejection(TODAY, "ENTRY_PREVIEW_REFUSED")
                e.payload = {
                    "reason": "whatIfOrder resolved with an API error instead of an order state: []",
                    "playbook": "spy_bull_put_spread_v1",
                }
                session.add(e)
            await session.commit()
            findings = await run_post_session_anomalies(session, TODAY, since=since)
        assert [f.rule for f in findings] == [PREVIEW_INFRA_FAILURE]
        assert findings[0].scope == GLOBAL_SCOPE
        assert findings[0].latches is True
        assert await _state(session_maker, "GLOBAL") == "HALT_ENTRIES"

    @pytest.mark.asyncio
    async def test_two_infra_events_are_below_threshold(self, session_maker):
        since = f"{TODAY}T16:00:00+00:00"
        async with session_maker() as session:
            for _ in range(2):
                e = _rejection(TODAY, "ENTRY_PREVIEW_REFUSED")
                e.payload = {
                    "reason": "whatIfOrder timed out - no usable order state within 30s",
                    "playbook": "spy_bull_put_spread_v1",
                }
                session.add(e)
            await session.commit()
            finding, evaluated = await check_preview_infra_failure(session, since)
        assert finding is None
        assert evaluated is True


def _zombie_order(order_id: str = "o_zomb", order_ref: str = "basis:B01:o_zomb:open") -> OrderModel:
    return OrderModel(
        id=order_id,
        book_id="B01",
        position_id=None,
        order_ref=order_ref,
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


def _zombie_fill(order_id: str, fill_time: str) -> FillModel:
    return FillModel(
        exec_id=f"x_{order_id}",
        order_id=order_id,
        book_id="B01",
        con_id=1,
        side="SLD",
        quantity=1.0,
        price=1.0,
        commission=1.0,
        fill_time=fill_time,
    )


class TestSelfClearingHalts:
    """#927: an anomaly-actor HALT_ENTRIES whose ENTIRE provenance — every
    rule that has contributed to it since it was last ACTIVE, read from the
    audit ledger, not the control row's `reason` prose — is self-clearable
    and cleanly re-evaluated this sweep lifts itself, with a
    CONTROL_STATE_CHANGED audit event naming what expired. Operator/ntfy
    halts never auto-lift, and a scope tainted by even one non-clearable
    rule's evidence stays halted no matter how stale that evidence gets."""

    @pytest.mark.asyncio
    async def test_aged_out_rejection_halt_self_clears(self, session_maker):
        # Night 1 (08-28): the burst trips REPEATED_REJECTION and halts GLOBAL.
        async with session_maker() as session:
            session.add_all([_rejection("2026-08-27"), _rejection("2026-08-27"), _rejection("2026-08-28")])
            await session.commit()
        findings = await _run_sweep_on(session_maker, "2026-08-28")
        assert [f.rule for f in findings] == [REPEATED_REJECTION]
        assert await _state(session_maker, "GLOBAL") == "HALT_ENTRIES"

        # Night 2 (09-01): the 08-27 burst has aged out of the trailing
        # window and nothing else trips — the halt should self-clear.
        findings = await _run_sweep_on(session_maker, "2026-09-01")
        assert findings == []
        assert await _state(session_maker, "GLOBAL") == "ACTIVE"

        async with session_maker() as session:
            events = (
                (
                    await session.execute(
                        select(AuditEventModel)
                        .filter_by(event_type="CONTROL_STATE_CHANGED")
                        .order_by(AuditEventModel.id)
                    )
                )
                .scalars()
                .all()
            )
        clear_events = [e for e in events if e.payload.get("state") == ACTIVE]
        assert len(clear_events) == 1
        assert clear_events[0].actor == "anomaly"
        assert "REPEATED_REJECTION" in clear_events[0].payload["reason"]

    @pytest.mark.asyncio
    async def test_operator_halt_is_never_lifted(self, session_maker):
        async with session_maker() as session:
            row = await session.get(TradingControlModel, "GLOBAL")
            row.state = HALT_ENTRIES
            row.reason = f"{REPEATED_REJECTION}: manual investigation"
            row.actor = "console"
            row.changed_at = "t0"
            await session.commit()
        # An otherwise-clean sweep (no rejection evidence at all) must not
        # touch an operator-set halt, even though the parsed rule name is
        # self-clearable.
        findings = await _run_sweep_on(session_maker, TODAY)
        assert findings == []
        assert await _state(session_maker, "GLOBAL") == "HALT_ENTRIES"

    @pytest.mark.asyncio
    async def test_duplicate_order_halt_is_never_self_cleared(self, session_maker):
        # DUPLICATE_ORDER latches at entry-staging time (executor.py), not
        # in this sweep — the sweep never re-derives its evidence, so "no
        # finding this sweep" must not be read as "cleared." executor.py
        # audits the event book-scoped (book_id="B01", the offending book)
        # even though the halt it causes always lands on GLOBAL —
        # provenance must still attribute it to GLOBAL rather than missing
        # it because the event's book_id isn't "GLOBAL".
        async with session_maker() as session:
            row = await session.get(TradingControlModel, "GLOBAL")
            row.state = HALT_ENTRIES
            row.reason = "DUPLICATE_ORDER: playbook_x in B01"
            row.actor = "anomaly"
            row.changed_at = "t0"
            session.add(
                AuditEventModel(
                    run_at="2026-08-17T22:00:00+00:00",
                    book_id="B01",
                    event_type="DUPLICATE_ORDER",
                    actor="executor",
                    payload={"playbook": "playbook_x"},
                )
            )
            await session.commit()
        findings = await _run_sweep_on(session_maker, TODAY)
        assert findings == []
        assert await _state(session_maker, "GLOBAL") == "HALT_ENTRIES"

    @pytest.mark.asyncio
    async def test_flatten_requested_is_never_lifted_by_self_clear(self, session_maker):
        async with session_maker() as session:
            row = await session.get(TradingControlModel, "GLOBAL")
            row.state = "FLATTEN_REQUESTED"
            row.reason = f"{REPEATED_REJECTION}: escalated"
            row.actor = "anomaly"
            row.changed_at = "t0"
            await session.commit()
        findings = await _run_sweep_on(session_maker, TODAY)
        assert findings == []
        assert await _state(session_maker, "GLOBAL") == "FLATTEN_REQUESTED"

    @pytest.mark.asyncio
    async def test_halt_with_no_ledger_provenance_never_clears(self, session_maker):
        # An anomaly-actor HALT_ENTRIES with zero halting-rule events in the
        # ledger has no evidence for the sweep to judge — fail closed.
        async with session_maker() as session:
            row = await session.get(TradingControlModel, "GLOBAL")
            row.state = HALT_ENTRIES
            row.reason = f"{REPEATED_REJECTION}: manual investigation"
            row.actor = "anomaly"
            row.changed_at = "t0"
            await session.commit()
        findings = await _run_sweep_on(session_maker, TODAY)
        assert findings == []
        assert await _state(session_maker, "GLOBAL") == "HALT_ENTRIES"

    @pytest.mark.asyncio
    async def test_zombie_fill_halt_never_self_clears(self, session_maker):
        # #927 point 2: ZOMBIE_FILL is SINCE-bounded, not re-derived from
        # scratch — a halt it causes must never lift, no matter how many
        # clean sweeps follow.
        since = f"{TODAY}T22:00:00+00:00"
        async with session_maker() as session:
            session.add(_zombie_order())
            session.add(_zombie_fill("o_zomb", f"{TODAY}T23:31:00+00:00"))
            await session.commit()
        async with session_maker() as session:
            findings = await run_post_session_anomalies(session, TODAY, since=since)
        assert [f.rule for f in findings] == [ZOMBIE_FILL]
        assert await _state(session_maker, "GLOBAL") == "HALT_ENTRIES"

        findings = await _run_sweep_on(session_maker, "2026-09-05")  # since=None, perfectly quiet
        assert findings == []
        assert await _state(session_maker, "GLOBAL") == "HALT_ENTRIES"

    @pytest.mark.asyncio
    async def test_pnl_shock_halt_never_self_clears(self, session_maker):
        # #927 point 2: check_pnl_shock's baseline is overwritten every run,
        # so the shocked move reads as ~0 the very next sweep by
        # construction — that must never be mistaken for "resolved."
        await _sweep(session_maker)  # baseline 10000
        async with session_maker() as session:
            session.add(_position("p1", current=20.0))  # -$2000 move
            await session.commit()
        findings = await _sweep(session_maker)
        assert [f.rule for f in findings] == [PNL_SHOCK]
        assert await _state(session_maker, "B01") == "HALT_ENTRIES"

        # Unchanged position: the baseline now already reflects the shock,
        # so this sweep measures zero move — still must not clear.
        findings = await _sweep(session_maker)
        assert findings == []
        assert await _state(session_maker, "B01") == "HALT_ENTRIES"

    @pytest.mark.asyncio
    async def test_multi_rule_provenance_blocks_clear_even_after_originating_rule_ages_out(self, session_maker):
        # Night 1 (08-28): REPEATED_REJECTION halts GLOBAL.
        async with session_maker() as session:
            session.add_all([_rejection("2026-08-27"), _rejection("2026-08-27"), _rejection("2026-08-28")])
            await session.commit()
        findings = await _run_sweep_on(session_maker, "2026-08-28")
        assert [f.rule for f in findings] == [REPEATED_REJECTION]
        assert await _state(session_maker, "GLOBAL") == "HALT_ENTRIES"

        # Night 2 (08-29): GLOBAL is already halted, but a zombie fill still
        # fires and _halt still writes its finding event even though the
        # control row itself doesn't move.
        async with session_maker() as session:
            session.add(_zombie_order())
            session.add(_zombie_fill("o_zomb", "2026-08-29T23:31:00+00:00"))
            await session.commit()
        async with session_maker() as session:
            findings = await run_post_session_anomalies(session, "2026-08-29", since="2026-08-29T22:00:00+00:00")
        # REPEATED_REJECTION may also still be within its trailing window
        # here — the point is only that ZOMBIE_FILL's finding event lands
        # in the ledger even though GLOBAL was already halted.
        assert ZOMBIE_FILL in [f.rule for f in findings]
        assert await _state(session_maker, "GLOBAL") == "HALT_ENTRIES"

        # Night 3 (09-01): the 08-27 rejection burst has aged out and there
        # is no new zombie fill — but the zombie's PAST finding is still in
        # GLOBAL's provenance window, and ZOMBIE_FILL is not self-clearable.
        # The rejection rule aging out must not vacate the zombie's claim.
        findings = await _run_sweep_on(session_maker, "2026-09-01")
        assert findings == []
        assert await _state(session_maker, "GLOBAL") == "HALT_ENTRIES"

    @pytest.mark.asyncio
    async def test_since_none_sweep_never_clears_an_infra_halt(self, session_maker):
        since = f"{TODAY}T16:00:00+00:00"
        async with session_maker() as session:
            for _ in range(3):
                e = _rejection(TODAY, "ENTRY_PREVIEW_REFUSED")
                e.payload = {
                    "reason": "whatIfOrder resolved with an API error instead of an order state: []",
                    "playbook": "spy_bull_put_spread_v1",
                }
                session.add(e)
            await session.commit()
        async with session_maker() as session:
            findings = await run_post_session_anomalies(session, TODAY, since=since)
        assert [f.rule for f in findings] == [PREVIEW_INFRA_FAILURE]
        assert await _state(session_maker, "GLOBAL") == "HALT_ENTRIES"

        # since=None never evaluates PREVIEW_INFRA_FAILURE at all — must not
        # be read as "the rule ran and found nothing."
        findings = await _run_sweep_on(session_maker, TODAY)  # since=None
        assert findings == []
        assert await _state(session_maker, "GLOBAL") == "HALT_ENTRIES"

    @pytest.mark.asyncio
    async def test_config_hash_rotation_blocks_envelope_self_clear(self, session_maker):
        # #927 HIGH-3: B01 breaches the envelope and halts, with every
        # position stamped under the book's ORIGINAL config_hash.
        async with session_maker() as session:
            for i in range(9):
                pos = _position(f"p{i}")
                pos.config_hash = "h"  # matches _book()'s default config_hash
                session.add(pos)
            await session.commit()
        findings = await _sweep(session_maker)
        assert ENVELOPE_BREACH_POSTHOC in [f.rule for f in findings]
        assert await _state(session_maker, "B01") == "HALT_ENTRIES"

        # A seeds.py-style edit rotates the book's config_hash. Every open
        # position is now prior-era — the era filter excludes them all, so
        # tonight's check judges nothing (not "judged and clean").
        async with session_maker() as session:
            book = await session.get(BookModel, "B01")
            book.config_hash = "newhash1"
            await session.commit()
        findings = await _sweep(session_maker)
        assert ENVELOPE_BREACH_POSTHOC not in [f.rule for f in findings]  # era filter hides the breach
        assert await _state(session_maker, "B01") == "HALT_ENTRIES"  # but must NOT have cleared


class TestPnlShock:
    @pytest.mark.asyncio
    async def test_first_run_sets_baseline_without_halting(self, session_maker):
        findings = await _sweep(session_maker)
        assert findings == []
        async with session_maker() as session:
            book = await session.get(BookModel, "B01")
        assert book.last_mtm == 10000.0
        assert book.last_mtm_at is not None

    @pytest.mark.asyncio
    async def test_every_mark_lands_in_the_equity_curve(self, session_maker):
        # last_mtm alone is overwritten nightly — the curve must persist
        # (#239), and a same-day rerun overwrites its row, not duplicates.
        from sqlalchemy import select

        from backend.models import BookMtmHistoryModel

        await _sweep(session_maker)
        await _sweep(session_maker)  # same-day rerun
        async with session_maker() as session:
            rows = (await session.execute(select(BookMtmHistoryModel).filter_by(book_id="B01"))).scalars().all()
        assert len(rows) == 1
        assert rows[0].mtm == 10000.0
        assert rows[0].date == rows[0].date[:10]  # bare ISO date, not a timestamp

    @pytest.mark.asyncio
    async def test_shock_move_halts_the_book(self, session_maker):
        await _sweep(session_maker)  # baseline 10000
        async with session_maker() as session:
            # Credit position whose buy-back cost exploded: equity drops $2,000
            session.add(_position("p1", current=20.0))
            await session.commit()
        findings = await _sweep(session_maker)
        assert [f.rule for f in findings] == [PNL_SHOCK]
        assert findings[0].scope == "B01"
        assert await _state(session_maker, "B01") == "HALT_ENTRIES"
        assert await _state(session_maker, "GLOBAL") == "ACTIVE"  # scoped, not global

    @pytest.mark.asyncio
    async def test_multiday_gap_records_instead_of_halting(self, session_maker):
        # M4 (#280): a mark gap (missed nights) makes the move multi-session —
        # it must not trip the ONE-day shock threshold.
        async with session_maker() as session:
            book = await session.get(BookModel, "B01")
            book.last_mtm = 10000.0
            book.last_mtm_at = "2026-08-11T22:00:00+00:00"  # a week before TODAY
            session.add(_position("p1", current=20.0))  # -$2,000 vs baseline
            await session.commit()
        findings = await _sweep(session_maker)
        assert findings == []
        async with session_maker() as session:
            events = (await session.execute(select(AuditEventModel))).scalars().all()
        assert any(e.event_type == "PNL_SHOCK_SKIPPED_GAP" for e in events)
        assert await _state(session_maker, "B01") == "ACTIVE"

    def test_market_days_use_the_market_date_not_utc(self):
        # Audit II R2 (#419): a run committing after 00:00 UTC stamped the
        # NEXT UTC day; counting from it under-reads the gap by one, and a
        # genuinely missed night looks covered.
        from backend.anomaly import _market_days_between

        # 2026-08-20 01:30 UTC == Wednesday 2026-08-19 21:30 ET.
        late_commit = "2026-08-20T01:30:00+00:00"
        assert _market_days_between(late_commit, "2026-08-21") == 2  # Thu + Fri
        # Naive inputs (plain market dates) are unchanged.
        assert _market_days_between("2026-08-19T21:30:00", "2026-08-21") == 2

    @pytest.mark.asyncio
    async def test_normal_drift_updates_baseline_quietly(self, session_maker):
        await _sweep(session_maker)
        async with session_maker() as session:
            session.add(_position("p1", current=2.0))  # -$200 move: inside 15% of $10K
            await session.commit()
        findings = await _sweep(session_maker)
        assert findings == []
        async with session_maker() as session:
            book = await session.get(BookModel, "B01")
        assert book.last_mtm == 9800.0

    def test_book_mtm_signs(self):
        book = _book(cash=10120.0)
        credit_pos = _position("p1", current=0.6)  # buy-back liability $60
        assert book_mtm(book, [credit_pos]) == 10060.0
        debit_pos = _position("p2", current=3.0)
        debit_pos.premium_direction = "DEBIT"
        assert book_mtm(book, [debit_pos]) == 10420.0


class TestEnvelopeBreach:
    @pytest.mark.asyncio
    async def test_too_many_positions(self, session_maker):
        async with session_maker() as session:
            for i in range(9):
                session.add(_position(f"p{i}"))
            await session.commit()
        findings = await _sweep(session_maker)
        assert ENVELOPE_BREACH_POSTHOC in [f.rule for f in findings]
        assert await _state(session_maker, "B01") == "HALT_ENTRIES"

    @pytest.mark.asyncio
    async def test_oversize_position(self, session_maker):
        async with session_maker() as session:
            session.add(_position("p1", max_loss=3.0))  # $300 > $250 cap
            await session.commit()
        findings = await _sweep(session_maker)
        (finding,) = [f for f in findings if f.rule == ENVELOPE_BREACH_POSTHOC]
        assert "p1" in finding.detail

    @pytest.mark.asyncio
    async def test_over_deployed(self, session_maker):
        async with session_maker() as session:
            session.add(_position("p1", max_loss=26.0))  # $2600
            session.add(_position("p2", max_loss=25.0))  # $2500 → $5100 > $5000
            await session.commit()
        findings = await _sweep(session_maker)
        details = " ".join(f.detail for f in findings if f.rule == ENVELOPE_BREACH_POSTHOC)
        assert "deployed" in details

    @pytest.mark.asyncio
    async def test_prior_era_positions_are_not_breaches(self, session_maker):
        # Audit II R4 (#533): a seeds.py envelope reduction is not a gate
        # bypass — old-era positions passed the gates they were entered
        # under, and nightly false breach rows would permanently poison the
        # Live Gate's zero-breaches criterion (append-only, no expunge).
        async with session_maker() as session:
            book = (await session.execute(select(BookModel).filter_by(id="B01"))).scalar_one()
            book.config = {"engine_variant": "V0", "underlying": "XSP", "envelope": {"max_deployed_pct": 40.0}}
            book.config_hash = "newhash1"  # the sync just landed a reduced cap
            for i in range(2):
                pos = _position(f"p{i}", max_loss=26.0)  # $5200 deployed — legal under the OLD 50% cap
                pos.config_hash = "oldhash1"  # decided under the prior era
                session.add(pos)
            await session.commit()
        findings = await _sweep(session_maker)
        assert ENVELOPE_BREACH_POSTHOC not in [f.rule for f in findings]

    @pytest.mark.asyncio
    async def test_current_era_positions_still_breach(self, session_maker):
        # The defect-detection half must survive #533's scoping: same-era
        # positions violating the envelope remain a real code-defect signal.
        async with session_maker() as session:
            book = (await session.execute(select(BookModel).filter_by(id="B01"))).scalar_one()
            book.config_hash = "newhash1"
            for i in range(2):
                pos = _position(f"p{i}", max_loss=26.0)  # $5200 > $5000 cap
                pos.config_hash = "newhash1"  # THIS era's gates let it through
                session.add(pos)
            await session.commit()
        findings = await _sweep(session_maker)
        assert ENVELOPE_BREACH_POSTHOC in [f.rule for f in findings]

    @pytest.mark.asyncio
    async def test_clean_book_is_quiet(self, session_maker):
        async with session_maker() as session:
            session.add(_position("p1"))
            await session.commit()
        assert await _sweep(session_maker) == []

    @pytest.mark.asyncio
    async def test_over_concentrated_bucket(self, session_maker):
        # #680: the fifth envelope limit, max_same_strategy_expiry (default
        # 2) — three positions all share the default BULL_PUT_SPREAD@
        # 2026-12-18 bucket ($200 max_loss each keeps MAX_POSITIONS/
        # MAX_DEPLOYED clean, isolating this to the concentration check).
        async with session_maker() as session:
            for i in range(3):
                session.add(_position(f"p{i}"))
            await session.commit()
        findings = await _sweep(session_maker)
        (finding,) = [f for f in findings if f.rule == ENVELOPE_BREACH_POSTHOC]
        assert "BULL_PUT_SPREAD@2026-12-18" in finding.detail
        assert "3" in finding.detail

    @pytest.mark.asyncio
    async def test_at_the_concentration_cap_is_not_a_breach(self, session_maker):
        async with session_maker() as session:
            for i in range(2):
                session.add(_position(f"p{i}"))
            await session.commit()
        assert await _sweep(session_maker) == []

    @pytest.mark.asyncio
    async def test_different_buckets_are_not_pooled_together(self, session_maker):
        # Three positions, but split 2/1 across two DIFFERENT expirations —
        # neither bucket alone exceeds the cap.
        async with session_maker() as session:
            for i in range(2):
                session.add(_position(f"p{i}"))
            other = _position("p_other")
            other.expiration_date = "2027-01-15"
            other.legs[0]["expiration"] = "2027-01-15"
            session.add(other)
            await session.commit()
        assert await _sweep(session_maker) == []


class TestAlertDedup:
    """#922: ENVELOPE_BREACH_POSTHOC is a standing condition — the same open
    position stays in breach every run until it closes. The ledger
    (AuditEventModel) still records every occurrence; only the ntfy push
    (payload["alert_suppressed"]) dedupes."""

    @pytest.mark.asyncio
    async def test_first_breach_alerts(self, session_maker):
        async with session_maker() as session:
            session.add(_position("p1", max_loss=3.0))  # $300 > $250 cap
            await session.commit()
        await _sweep(session_maker)
        async with session_maker() as session:
            (event,) = (
                (await session.execute(select(AuditEventModel).filter_by(event_type=ENVELOPE_BREACH_POSTHOC)))
                .scalars()
                .all()
            )
        assert event.payload["alert_suppressed"] is False

    @pytest.mark.asyncio
    async def test_unchanged_repeat_is_suppressed_from_the_push(self, session_maker):
        async with session_maker() as session:
            session.add(_position("p1", max_loss=3.0))
            await session.commit()
        await _sweep(session_maker)
        findings = await _sweep(session_maker)  # same breach, second night
        assert ENVELOPE_BREACH_POSTHOC in [f.rule for f in findings]  # still in the ledger/digest
        async with session_maker() as session:
            events = (
                (
                    await session.execute(
                        select(AuditEventModel)
                        .filter_by(event_type=ENVELOPE_BREACH_POSTHOC)
                        .order_by(AuditEventModel.id)
                    )
                )
                .scalars()
                .all()
            )
        assert len(events) == 2  # both nights ledgered
        assert [e.payload["alert_suppressed"] for e in events] == [False, True]
        assert await _state(session_maker, "B01") == "HALT_ENTRIES"  # halt still applied every time

    @pytest.mark.asyncio
    async def test_magnitude_increase_past_the_band_realerts(self, session_maker):
        async with session_maker() as session:
            session.add(_position("p1", max_loss=3.0))  # $300, ratio 1.2
            await session.commit()
        await _sweep(session_maker)
        async with session_maker() as session:
            pos = await session.get(PositionModel, "p1")
            pos.max_loss = 6.0  # $600, ratio 2.4 — well past the +10% band
            await session.commit()
        await _sweep(session_maker)
        await _sweep(session_maker)  # unchanged at the NEW magnitude, third night
        async with session_maker() as session:
            events = (
                (
                    await session.execute(
                        select(AuditEventModel)
                        .filter_by(event_type=ENVELOPE_BREACH_POSTHOC)
                        .order_by(AuditEventModel.id)
                    )
                )
                .scalars()
                .all()
            )
        # Night 3 is suppressed against night 2's ratio (2.4), not night 1's
        # (1.2) — the baseline only updates on an actual alert (#922).
        assert [e.payload["alert_suppressed"] for e in events] == [False, False, True]

    @pytest.mark.asyncio
    async def test_new_position_breaching_after_the_old_one_closes_still_alerts(self, session_maker):
        # #922 regression: the dedup key must carry the breaching position's
        # identity, not just (rule, scope) — otherwise a resolved breach on
        # p1 leaves a stale last-alerted magnitude that can wrongly suppress
        # a FRESH breach on a different position later (a new gate bypass,
        # exactly what this rule exists to catch, going out silently).
        async with session_maker() as session:
            session.add(_position("p1", max_loss=3.0))  # $300, ratio 1.2
            await session.commit()
        await _sweep(session_maker)
        async with session_maker() as session:
            pos = await session.get(PositionModel, "p1")
            pos.status = "CLOSED"  # p1's breach resolves
            session.add(_position("p2", max_loss=2.6))  # ratio 1.04 — LOWER than p1's
            await session.commit()
        await _sweep(session_maker)
        async with session_maker() as session:
            events = (
                (
                    await session.execute(
                        select(AuditEventModel)
                        .filter_by(event_type=ENVELOPE_BREACH_POSTHOC)
                        .order_by(AuditEventModel.id)
                    )
                )
                .scalars()
                .all()
            )
        assert [e.payload["alert_suppressed"] for e in events] == [False, False]

    @pytest.mark.asyncio
    async def test_small_increase_within_the_band_stays_suppressed(self, session_maker):
        async with session_maker() as session:
            session.add(_position("p1", max_loss=3.0))  # ratio 1.2
            await session.commit()
        await _sweep(session_maker)
        async with session_maker() as session:
            pos = await session.get(PositionModel, "p1")
            pos.max_loss = 3.1  # ratio 1.24 — under the 1.32 re-alert threshold
            await session.commit()
        await _sweep(session_maker)
        async with session_maker() as session:
            events = (
                (
                    await session.execute(
                        select(AuditEventModel)
                        .filter_by(event_type=ENVELOPE_BREACH_POSTHOC)
                        .order_by(AuditEventModel.id)
                    )
                )
                .scalars()
                .all()
            )
        assert [e.payload["alert_suppressed"] for e in events] == [False, True]

    @pytest.mark.asyncio
    async def test_non_standing_rules_are_never_deduped(self, session_maker):
        # REPEATED_REJECTION/PNL_SHOCK/etc. are scoped to what happened
        # THIS run — two separate incidents on different nights must both
        # interrupt, so sub_breaches stays empty and _should_alert never
        # checks the dedup table for them.
        finding = AnomalyFinding(REPEATED_REJECTION, GLOBAL_SCOPE, "2 rejections tonight")
        async with session_maker() as session:
            first = await _should_alert(session, finding)
            second = await _should_alert(session, finding)
        assert first is True
        assert second is True

    @pytest.mark.asyncio
    async def test_book_level_only_breach_of_a_new_kind_still_alerts(self, session_maker):
        # #924 HIGH-1 regression, purely book-level (dedup_key == "" in the
        # pre-#924 design — no per-trade breach anywhere in this test): a
        # standing BUCKET breach must not mask a brand-new, unrelated COUNT
        # breach appearing later on the same book. The old design shared one
        # key ("" for every book-level sub-check) and one finding-wide
        # worst_ratio across both kinds, so the count breach's lower ratio
        # (1.125) never cleared the bucket breach's higher baseline (1.5)
        # and was silently suppressed from the urgent push.
        async with session_maker() as session:
            for pid in ("p1", "p2", "p3"):
                session.add(_position(pid))  # same bucket, $200 each — no per-trade breach
            await session.commit()
        await _sweep(session_maker)  # night 1: bucket breach 3 > 2, ratio 1.5 -- alerts

        async with session_maker() as session:
            for i in range(6):
                pos = _position(f"q{i}")
                pos.expiration_date = f"2027-02-{i + 1:02d}"
                pos.legs[0]["expiration"] = pos.expiration_date
                session.add(pos)
            await session.commit()
        await _sweep(session_maker)  # night 2: NEW count breach 9 > 8, ratio 1.125

        async with session_maker() as session:
            events = (
                (
                    await session.execute(
                        select(AuditEventModel)
                        .filter_by(event_type=ENVELOPE_BREACH_POSTHOC)
                        .order_by(AuditEventModel.id)
                    )
                )
                .scalars()
                .all()
            )
        assert [e.payload["alert_suppressed"] for e in events] == [False, False]

    @pytest.mark.asyncio
    async def test_new_book_level_breach_after_the_old_one_resolves_still_alerts(self, session_maker):
        # #924 HIGH-2 regression, purely book-level (dedup_key == "" in the
        # pre-#924 design): once a book-level breach fully resolves, its
        # anomaly_alert_state row must be cleared — otherwise a later,
        # DIFFERENT book-level breach inherits the resolved breach's stale
        # baseline and never re-alerts (the pre-#924 <= comparison also
        # meant a decrease alone would have suppressed and kept the old
        # high baseline in place, forever).
        async with session_maker() as session:
            for pid in ("p1", "p2", "p3"):
                session.add(_position(pid))  # bucket breach 3 > 2, ratio 1.5
            await session.commit()
        await _sweep(session_maker)  # night 1: alerts

        async with session_maker() as session:
            for pid in ("p2", "p3"):
                (await session.get(PositionModel, pid)).status = "CLOSED"
            await session.commit()
        findings = await _sweep(session_maker)  # night 2: fully clean, no finding
        assert findings == []

        async with session_maker() as session:
            for i in range(8):
                pos = _position(f"q{i}")
                pos.expiration_date = f"2027-03-{i + 1:02d}"
                pos.legs[0]["expiration"] = pos.expiration_date
                session.add(pos)
            await session.commit()
        await _sweep(session_maker)  # night 3: NEW count breach 9 > 8, ratio 1.125 (p1 + 8 new)

        async with session_maker() as session:
            events = (
                (
                    await session.execute(
                        select(AuditEventModel)
                        .filter_by(event_type=ENVELOPE_BREACH_POSTHOC)
                        .order_by(AuditEventModel.id)
                    )
                )
                .scalars()
                .all()
            )
        assert [e.payload["alert_suppressed"] for e in events] == [False, False]


class TestEscalationOnly:
    @pytest.mark.asyncio
    async def test_flatten_requested_is_never_downgraded(self, session_maker):
        async with session_maker() as session:
            row = await session.get(TradingControlModel, "B01")
            row.state = "FLATTEN_REQUESTED"
            await session.commit()
            for i in range(9):
                session.add(_position(f"p{i}"))
            await session.commit()
        findings = await _sweep(session_maker)
        assert findings  # the breach is still found and audited...
        assert await _state(session_maker, "B01") == "FLATTEN_REQUESTED"  # ...but never downgraded

    @pytest.mark.asyncio
    async def test_every_firing_leaves_an_audit_event(self, session_maker):
        async with session_maker() as session:
            session.add_all([_rejection(TODAY), _rejection(TODAY)])
            await session.commit()
        await _sweep(session_maker)
        async with session_maker() as session:
            events = (
                (await session.execute(select(AuditEventModel).filter_by(event_type=REPEATED_REJECTION)))
                .scalars()
                .all()
            )
        assert len(events) == 1
        assert events[0].actor == "anomaly"


class TestDuplicateOrder:
    @pytest.mark.asyncio
    async def test_same_legs_same_book_same_day_is_duplicate(self, session_maker):
        legs = (("XSP261218P00610000", "SHORT", 1), ("XSP261218P00605000", "LONG", 1))
        async with session_maker() as session:
            session.add(
                OrderModel(
                    id="o1",
                    book_id="B01",
                    position_id=None,
                    order_ref="basis:B01:o1:open",
                    ib_order_id=1,
                    ib_perm_id=1,
                    action="OPEN",
                    combo_legs={
                        "legs": [
                            {
                                "occ": "XSP261218P00610000",
                                "direction": "SHORT",
                                "option_type": "PUT",
                                "strike": 610.0,
                                "expiration": "2026-12-18",
                            },
                            {
                                "occ": "XSP261218P00605000",
                                "direction": "LONG",
                                "option_type": "PUT",
                                "strike": 605.0,
                                "expiration": "2026-12-18",
                            },
                        ],
                        "quantity": 1,
                    },
                    order_type="LIMIT",
                    limit_price=-1.0,
                    decision_midpoint=-1.0,
                    status="SUBMITTED",
                    submitted_at=f"{TODAY}T22:00:00+00:00",
                    completed_at=None,
                    encumbered_risk=200.0,
                )
            )
            await session.commit()
            # The window is a timestamp (market_evening_window_start, #275) —
            # >= matching; the old date-prefix match was dead against it.
            window = f"{TODAY}T16:00:00+00:00"
            assert await check_duplicate_order(session, "B01", legs, window) is True
            # Different book, later window, different legs → not duplicates
            assert await check_duplicate_order(session, "B02", legs, window) is False
            assert await check_duplicate_order(session, "B01", legs, "2026-08-19T16:00:00+00:00") is False
            other = (("XSP261218P00600000", "SHORT", 1), ("XSP261218P00595000", "LONG", 1))
            assert await check_duplicate_order(session, "B01", other, window) is False

    @pytest.mark.asyncio
    async def test_staged_intent_counts_as_duplicate(self, session_maker):
        # A STAGED row has no timestamps but is always this run's intent —
        # the sync expires stale STAGED rows before the entry phase (#275).
        legs = (("XSP261218P00610000", "SHORT", 1),)
        async with session_maker() as session:
            session.add(
                OrderModel(
                    id="o_staged",
                    book_id="B01",
                    position_id=None,
                    order_ref="basis:B01:o_staged:open",
                    ib_order_id=None,
                    ib_perm_id=None,
                    action="OPEN",
                    combo_legs={
                        "legs": [
                            {
                                "occ": "XSP261218P00610000",
                                "direction": "SHORT",
                                "option_type": "PUT",
                                "strike": 610.0,
                                "expiration": "2026-12-18",
                            }
                        ],
                        "quantity": 1,
                    },
                    order_type="LIMIT",
                    limit_price=-1.0,
                    decision_midpoint=-1.0,
                    status="STAGED",
                    submitted_at=None,
                    completed_at=None,
                    encumbered_risk=200.0,
                )
            )
            await session.commit()
            window = f"{TODAY}T16:00:00+00:00"
            assert await check_duplicate_order(session, "B01", legs, window) is True

    def test_signature_is_order_insensitive(self):
        a = (("X1", "SHORT", 1), ("X2", "LONG", 1))
        b = (("X2", "LONG", 1), ("X1", "SHORT", 1))
        assert entry_signature("B01", a) == entry_signature("B01", b)

    def test_signature_includes_ratio_so_different_ratio_never_overmatches(self):
        # #740: a hypothetical 1x structure on the same strikes as a BWB's
        # 2x body must NOT read as a duplicate of it — ratio is part of the
        # fingerprint, not just the leg identity.
        one_x = (("X1", "SHORT", 1), ("X2", "LONG", 1))
        two_x = (("X1", "SHORT", 2), ("X2", "LONG", 1))
        assert entry_signature("B01", one_x) != entry_signature("B01", two_x)

    @pytest.mark.asyncio
    async def test_bwb_duplicate_is_blocked_ratio_expanded_stored_legs_collapse_to_match(self, session_maker):
        # #740: the stored order's combo_legs['legs'] carries the BWB body's
        # ratio EXPANDED into two duplicate dicts (executor.py's legs_meta
        # convention) — this must still be recognized as a duplicate of a
        # candidate whose own signature uses the AGGREGATED (occ, direction,
        # ratio) form, not silently pass because the raw lengths differ (4
        # stored dicts vs. 3 candidate legs).
        bwb_legs_meta = [
            {
                "occ": "XSP261218P00620000",
                "direction": "LONG",
                "option_type": "PUT",
                "strike": 620.0,
                "expiration": "2026-12-18",
            },
            {
                "occ": "XSP261218P00610000",
                "direction": "SHORT",
                "option_type": "PUT",
                "strike": 610.0,
                "expiration": "2026-12-18",
            },
            {
                "occ": "XSP261218P00610000",
                "direction": "SHORT",
                "option_type": "PUT",
                "strike": 610.0,
                "expiration": "2026-12-18",
            },
            {
                "occ": "XSP261218P00600000",
                "direction": "LONG",
                "option_type": "PUT",
                "strike": 600.0,
                "expiration": "2026-12-18",
            },
        ]
        async with session_maker() as session:
            session.add(
                OrderModel(
                    id="o_bwb",
                    book_id="B18",
                    position_id=None,
                    order_ref="basis:B18:o_bwb:open",
                    ib_order_id=1,
                    ib_perm_id=1,
                    action="OPEN",
                    combo_legs={"legs": bwb_legs_meta, "quantity": 1},
                    order_type="LIMIT",
                    limit_price=-1.0,
                    decision_midpoint=-1.0,
                    status="SUBMITTED",
                    submitted_at=f"{TODAY}T22:00:00+00:00",
                    completed_at=None,
                    encumbered_risk=200.0,
                )
            )
            await session.commit()
            window = f"{TODAY}T16:00:00+00:00"
            # Candidate side: the AGGREGATED form the executor actually
            # builds from `combo` — one entry per distinct leg, ratio 2 on
            # the body.
            candidate_legs = (
                ("XSP261218P00620000", "LONG", 1),
                ("XSP261218P00610000", "SHORT", 2),
                ("XSP261218P00600000", "LONG", 1),
            )
            assert await check_duplicate_order(session, "B18", candidate_legs, window) is True

    @pytest.mark.asyncio
    async def test_a_1x_structure_on_the_same_strikes_as_a_stored_bwb_is_not_flagged(self, session_maker):
        # The other direction of #740's fix: normalizing must not become
        # OVER-eager — a genuinely different (non-ratio) structure sharing
        # the BWB's strikes must not false-positive as its duplicate.
        bwb_legs_meta = [
            {
                "occ": "XSP261218P00620000",
                "direction": "LONG",
                "option_type": "PUT",
                "strike": 620.0,
                "expiration": "2026-12-18",
            },
            {
                "occ": "XSP261218P00610000",
                "direction": "SHORT",
                "option_type": "PUT",
                "strike": 610.0,
                "expiration": "2026-12-18",
            },
            {
                "occ": "XSP261218P00610000",
                "direction": "SHORT",
                "option_type": "PUT",
                "strike": 610.0,
                "expiration": "2026-12-18",
            },
            {
                "occ": "XSP261218P00600000",
                "direction": "LONG",
                "option_type": "PUT",
                "strike": 600.0,
                "expiration": "2026-12-18",
            },
        ]
        async with session_maker() as session:
            session.add(
                OrderModel(
                    id="o_bwb2",
                    book_id="B18",
                    position_id=None,
                    order_ref="basis:B18:o_bwb2:open",
                    ib_order_id=1,
                    ib_perm_id=1,
                    action="OPEN",
                    combo_legs={"legs": bwb_legs_meta, "quantity": 1},
                    order_type="LIMIT",
                    limit_price=-1.0,
                    decision_midpoint=-1.0,
                    status="SUBMITTED",
                    submitted_at=f"{TODAY}T22:00:00+00:00",
                    completed_at=None,
                    encumbered_risk=200.0,
                )
            )
            await session.commit()
            window = f"{TODAY}T16:00:00+00:00"
            one_x_candidate = (
                ("XSP261218P00620000", "LONG", 1),
                ("XSP261218P00610000", "SHORT", 1),  # ratio 1, not 2 — a different structure
                ("XSP261218P00600000", "LONG", 1),
            )
            assert await check_duplicate_order(session, "B18", one_x_candidate, window) is False

    @pytest.mark.asyncio
    async def test_ordinary_vertical_duplicate_detection_is_unchanged(self, session_maker):
        # Regression coverage: the pre-#740 ordinary (non-ratio) vertical
        # spread behavior — both a real duplicate and a genuinely different
        # spread — is unaffected by the ratio-aware signature change.
        legs_meta = [
            {
                "occ": "XSP261218P00610000",
                "direction": "SHORT",
                "option_type": "PUT",
                "strike": 610.0,
                "expiration": "2026-12-18",
            },
            {
                "occ": "XSP261218P00605000",
                "direction": "LONG",
                "option_type": "PUT",
                "strike": 605.0,
                "expiration": "2026-12-18",
            },
        ]
        async with session_maker() as session:
            session.add(
                OrderModel(
                    id="o_vert",
                    book_id="B01",
                    position_id=None,
                    order_ref="basis:B01:o_vert:open",
                    ib_order_id=1,
                    ib_perm_id=1,
                    action="OPEN",
                    combo_legs={"legs": legs_meta, "quantity": 1},
                    order_type="LIMIT",
                    limit_price=-1.0,
                    decision_midpoint=-1.0,
                    status="SUBMITTED",
                    submitted_at=f"{TODAY}T22:00:00+00:00",
                    completed_at=None,
                    encumbered_risk=200.0,
                )
            )
            await session.commit()
            window = f"{TODAY}T16:00:00+00:00"
            duplicate = (("XSP261218P00610000", "SHORT", 1), ("XSP261218P00605000", "LONG", 1))
            different = (("XSP261218P00600000", "SHORT", 1), ("XSP261218P00595000", "LONG", 1))
            assert await check_duplicate_order(session, "B01", duplicate, window) is True
            assert await check_duplicate_order(session, "B01", different, window) is False


class TestZombieFills:
    """#481 A-F5: a fresh fill on an already-terminal order is the signature
    of a ghost/zombie fill — money moved at the broker that no pending row
    accounts for. Previously completely silent."""

    def _cancelled_order(self) -> OrderModel:
        return OrderModel(
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

    def _fill(self, fill_time: str) -> FillModel:
        return FillModel(
            exec_id="x_zomb_1",
            order_id="o_zomb",
            book_id="B01",
            con_id=1,
            side="SLD",
            quantity=1.0,
            price=1.0,
            commission=1.0,
            fill_time=fill_time,
        )

    @pytest.mark.asyncio
    async def test_fresh_fill_on_a_terminal_order_is_flagged(self, session_maker):
        since = f"{TODAY}T22:00:00+00:00"
        async with session_maker() as session:
            session.add(self._cancelled_order())
            session.add(self._fill(f"{TODAY}T23:31:00+00:00"))  # after run start
            await session.commit()
            finding, evaluated = await check_zombie_fills(session, since=since)
        assert finding is not None
        assert finding.rule == ZOMBIE_FILL
        assert "basis:B01:o_zomb:open" in finding.detail
        assert evaluated is True

    @pytest.mark.asyncio
    async def test_old_fills_on_a_resolved_partial_are_not_zombies(self, session_maker):
        # resolve_partial_order's latch release leaves a CANCELLED row with
        # OLD fills — the designated workflow, flagged nightly forever if
        # the rule weren't scoped to fills backfilled since run start.
        since = f"{TODAY}T22:00:00+00:00"
        async with session_maker() as session:
            session.add(self._cancelled_order())
            session.add(self._fill(f"{TODAY}T13:31:00+00:00"))  # morning, pre-run
            await session.commit()
            assert (await check_zombie_fills(session, since=since))[0] is None

    @pytest.mark.asyncio
    async def test_fresh_fills_terminalized_by_resolution_tonight_are_not_zombies(self, session_maker):
        # #546 F5: the sync latches PARTIAL tonight (fresh fill_time). If the
        # operator external-closes + resolve_partial_order DURING this same
        # run window — before the anomalies phase runs — the now-CANCELLED
        # row carries exactly the fresh fills that latch was reporting. A
        # global halt for doing precisely what the latch asked is a false
        # positive; RESOLUTION_PARTIAL_TERMINALIZED tonight for this ref is
        # the designated workflow, not a zombie.
        since = f"{TODAY}T22:00:00+00:00"
        async with session_maker() as session:
            session.add(self._cancelled_order())
            session.add(self._fill(f"{TODAY}T23:31:00+00:00"))  # after run start
            session.add(
                AuditEventModel(
                    run_at=f"{TODAY}T23:40:00+00:00",  # after run start, before this check
                    book_id="B01",
                    event_type="RESOLUTION_PARTIAL_TERMINALIZED",
                    actor="resolution",
                    payload={"order_ref": "basis:B01:o_zomb:open", "released_encumbrance": 0.0, "reason": "explained"},
                )
            )
            await session.commit()
            assert (await check_zombie_fills(session, since=since))[0] is None

    @pytest.mark.asyncio
    async def test_resolution_on_a_different_ref_does_not_shadow_a_real_zombie(self, session_maker):
        since = f"{TODAY}T22:00:00+00:00"
        async with session_maker() as session:
            session.add(self._cancelled_order())
            session.add(self._fill(f"{TODAY}T23:31:00+00:00"))
            session.add(
                AuditEventModel(
                    run_at=f"{TODAY}T23:40:00+00:00",
                    book_id="B01",
                    event_type="RESOLUTION_PARTIAL_TERMINALIZED",
                    actor="resolution",
                    payload={"order_ref": "basis:B01:o_other:open", "released_encumbrance": 0.0, "reason": "x"},
                )
            )
            await session.commit()
            finding, evaluated = await check_zombie_fills(session, since=since)
        assert finding is not None
        assert "basis:B01:o_zomb:open" in finding.detail
        assert evaluated is True


class TestPreviewRefusalClassification:
    """#853: preview refusals split into handled classes; only 'other' pools
    into REPEATED_REJECTION."""

    def test_reason_classes(self):
        assert (
            classify_preview_refusal(
                "Error 201: Order rejected - reason:Cannot have open orders on both sides of the same US Option contract."
            )
            == "collision"
        )
        assert classify_preview_refusal("Error 201: Riskless combination orders are not allowed.") == "riskless"
        assert (
            classify_preview_refusal("Error 201: you do not have trading permissions for this options strategy.")
            == "permissions"
        )
        # #927: gateway/infra failures — IBKR's whatIf answer itself was
        # unusable, not an actual broker-rule refusal of the candidate.
        assert (
            classify_preview_refusal("whatIfOrder resolved with an API error instead of an order state: []") == "infra"
        )
        assert classify_preview_refusal("whatIfOrder timed out - no usable order state within 30s") == "infra"
        # Anything unrecognized (including reasons from before the
        # broker-text capture existed) stays in the conservative pool.
        assert classify_preview_refusal("whatIfOrder returned no usable margin figure") == "other"

    @pytest.mark.asyncio
    async def test_classified_refusals_do_not_count_toward_the_halt(self, session_maker):
        async with session_maker() as session:
            for reason in (
                "Cannot have open orders on both sides of the same US Option contract",
                "Riskless combination orders are not allowed",
                "Cannot have open orders on both sides of the same US Option contract",
            ):
                e = _rejection(TODAY, "ENTRY_PREVIEW_REFUSED")
                e.payload = {"reason": reason, "playbook": "spy_bull_put_spread_v1"}
                session.add(e)
            await session.commit()
        findings = await _sweep(session_maker)
        assert findings == []

    @pytest.mark.asyncio
    async def test_unclassified_refusals_still_halt(self, session_maker):
        async with session_maker() as session:
            for _ in range(2):
                e = _rejection(TODAY, "ENTRY_PREVIEW_REFUSED")
                e.payload = {"reason": "whatIfOrder returned no usable margin figure"}
                session.add(e)
            await session.commit()
        findings = await _sweep(session_maker)
        assert [f.rule for f in findings] == [REPEATED_REJECTION]
        assert await _state(session_maker, "GLOBAL") == "HALT_ENTRIES"


class TestPermissionsRefusals:
    @pytest.mark.asyncio
    async def test_single_permissions_refusal_alerts_without_halting(self, session_maker):
        since = f"{TODAY}T16:00:00+00:00"
        async with session_maker() as session:
            e = _rejection(TODAY, "ENTRY_PREVIEW_REFUSED")
            e.payload = {
                "reason": "you do not have trading permissions for this options strategy",
                "playbook": "xsp_calendar_v1",
            }
            session.add(e)
            await session.commit()
            findings = await run_post_session_anomalies(session, TODAY, since=since)
        assert [f.rule for f in findings] == [PERMISSIONS_REFUSED]
        assert findings[0].latches is False
        assert "xsp_calendar_v1" in findings[0].detail
        # Non-latching: the finding is audited but GLOBAL control stays ACTIVE.
        assert await _state(session_maker, "GLOBAL") == "ACTIVE"
        async with session_maker() as session:
            rows = (
                (
                    await session.execute(
                        select(AuditEventModel).filter(AuditEventModel.event_type == PERMISSIONS_REFUSED)
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 1


class TestOrderLegCollision:
    def _resting(self, ref: str, action: str, legs: list[tuple[str, str]], status: str = "SUBMITTED") -> OrderModel:
        return OrderModel(
            id=ref,
            book_id=ref.split(":")[1],
            position_id=None,
            order_ref=ref,
            ib_order_id=1,
            ib_perm_id=1,
            action=action,
            combo_legs={
                "legs": [
                    {"occ": occ, "direction": d, "option_type": "PUT", "strike": 700.0, "expiration": "2026-10-16"}
                    for occ, d in legs
                ],
                "quantity": 1,
            },
            order_type="LIMIT",
            limit_price=-1.0,
            decision_midpoint=-1.0,
            status=status,
            submitted_at=f"{TODAY}T22:00:00+00:00",
            completed_at=None,
            encumbered_risk=100.0,
        )

    @pytest.mark.asyncio
    async def test_opposite_side_of_a_resting_open_order_collides(self, session_maker):
        async with session_maker() as session:
            session.add(self._resting("basis:B12:o_a:open", "OPEN", [("XSP261016P00766000", "LONG")]))
            await session.commit()
            hit = await check_order_leg_collision(session, (("XSP261016P00766000", "SHORT"),))
            assert hit == "basis:B12:o_a:open"

    @pytest.mark.asyncio
    async def test_same_side_does_not_collide(self, session_maker):
        async with session_maker() as session:
            session.add(self._resting("basis:B12:o_a:open", "OPEN", [("XSP261016P00766000", "LONG")]))
            await session.commit()
            assert await check_order_leg_collision(session, (("XSP261016P00766000", "LONG"),)) is None

    @pytest.mark.asyncio
    async def test_close_order_sides_are_inverted(self, session_maker):
        # A TP rider (action CLOSE) closing a LONG leg rests as a SELL on
        # that contract — a candidate SHORT on the same occ is the SAME
        # broker side (no collision) while a candidate LONG collides.
        async with session_maker() as session:
            session.add(self._resting("basis:B12:o_a:open:tp", "CLOSE", [("XSP261016P00766000", "LONG")]))
            await session.commit()
            assert await check_order_leg_collision(session, (("XSP261016P00766000", "SHORT"),)) is None
            assert (
                await check_order_leg_collision(session, (("XSP261016P00766000", "LONG"),)) == "basis:B12:o_a:open:tp"
            )

    @pytest.mark.asyncio
    async def test_terminal_orders_do_not_collide(self, session_maker):
        async with session_maker() as session:
            session.add(
                self._resting("basis:B12:o_a:open", "OPEN", [("XSP261016P00766000", "LONG")], status="CANCELLED")
            )
            await session.commit()
            assert await check_order_leg_collision(session, (("XSP261016P00766000", "SHORT"),)) is None

    @pytest.mark.asyncio
    async def test_unrelated_contracts_do_not_collide(self, session_maker):
        async with session_maker() as session:
            session.add(self._resting("basis:B12:o_a:open", "OPEN", [("XSP261016P00766000", "LONG")]))
            await session.commit()
            assert await check_order_leg_collision(session, (("XSP261016P00761000", "SHORT"),)) is None

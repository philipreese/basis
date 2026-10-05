"""The short evening push (#1116) and its plain-English fill lines (#1115).

The 2026-10-02 push ran ~30 lines (fixtures/digest_2026-10-02.txt): per-book
P&L, every blocked reason, seven separate "Gate … blocked ×1" lines. Android
shows the first few. The push is now at most six ranked lines; the full
detail moved to the DIGEST_COMPOSED row (`detail_body`, `log_body`)."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import backend.digest as digest_module
from backend.digest import (
    IDLE_NO_SIGNAL,
    PUSH_BODY_LIMIT_BYTES,
    PUSH_MAX_LINES,
    BlockedDigestRow,
    BookDigestRow,
    DigestData,
    UrgentLine,
    _title_and_priority,
    action_lines,
    compose_executor_digest_renderings,
    render_detail,
    render_human,
    render_log_line,
)
from backend.executor import BlockedEntry, ExecutorRunSummary
from backend.models import Base, BookModel, FillModel, OrderModel, PositionModel

FIXTURE = Path(__file__).parent / "fixtures" / "digest_2026-10-02.txt"
BENCHMARK = "Benchmark: $10K in SPY → $9,981 (-0.2%) since 2026-08-27 (price return, excl. dividends)"


def _book(book_id: str, pnl: float = 0.0, open_positions: int = 0, closed: int = 0, **flags: bool) -> BookDigestRow:
    return BookDigestRow(
        book_id=book_id,
        variant="V0",
        underlying="XSP",
        pnl=pnl,
        open_positions=open_positions,
        max_positions=8,
        deployed_dollars=0.0,
        basis_dollars=10000.0,
        deployed_pct=0.0,
        closed_trades=closed,
        is_idle=flags.get("is_idle", False),
        is_awaiting=flags.get("is_awaiting", False),
    )


def _night(**overrides: object) -> DigestData:
    base = DigestData(
        banner=[],
        halted_scopes=[],
        regime=None,
        broker_ok=True,
        broker_instruction=None,
        broker_api_errors=[],
        fills=[],
        positions_created_count=0,
        closes_placed=[],
        entries_placed=[],
        blocked_entries=[],
        blocked_rows=[],
        intents_expired=[],
        day_expired=[],
        book_rows=[_book("B01", pnl=40.0, open_positions=1)],
        idle_book_ids=[],
        awaiting_book_ids=[],
        gate_hits=[],
        benchmark_line=BENCHMARK,
        reconciliation="CLEAN",
        anomalies=[],
        notes=[],
        gate_horizon="At this cadence the earliest book reaches 30 closed trades: not computable yet",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _blocked(book: str, reason: str) -> BlockedEntry:
    return BlockedEntry(book, reason)


def _night_of_2026_10_02() -> DigestData:
    """The fixture night, rebuilt as data: 4 entries submitted, one B12 fill,
    22 blocked entries across 9 books, 4 trading / 24 idle / 2 awaiting / 5
    blocked books, clean reconciliation."""
    blocked = [_blocked("B32", "spy_tail_put_v1 dedup (open: pos_o_249a851a)")]
    blocked += [
        _blocked(b, "spy_bull_put_spread_v1 leg collision (resting order)") for b in ("B03", "B05", "B06", "B12", "B19")
    ]
    blocked += [_blocked("B10", "spy_iron_condor_v1 unpriceable (GLD)")]
    blocked += [_blocked(b, "spy_iron_condor_v1 unpriceable (SPY)") for b in ("B05", "B06", "B20")]
    blocked += [_blocked(b, "spy_iron_condor_v1 unpriceable (XSP)") for b in ("B02", "B03", "B12", "B19")]
    blocked += [_blocked("B29", "consensus 1/3 on EVENT_CATALYST")]
    gated = ("B02", "B03", "B05", "B06", "B12", "B19", "B20")
    blocked += [_blocked(b, "spy_bull_call_spread_v1 gated (MAX_LOSS_PER_TRADE)") for b in gated]
    assert len(blocked) == 22

    trading = [_book("B07", -20, 0, 1), _book("B10", -298, 1, 2), _book("B12", 55, 5, 1), _book("B32", -176, 1, 0)]
    blocked_books = ["B03", "B05", "B06", "B19", "B29"]
    idle = [f"B{n}" for n in range(40, 64)] + blocked_books
    awaiting = ["B02", "B20"]
    rows = trading + [_book(b, is_idle=True) for b in idle] + [_book(b, is_awaiting=True) for b in awaiting]
    return _night(
        fills=["Filled B12 BEAR_CALL_SPREAD (OPEN) @ limit -0.84 (decision mid -0.84)"],
        positions_created_count=1,
        entries_placed=[
            "basis:B02:o_2130d9a0:open",
            "basis:B20:o_d479baca:open",
            "basis:B10:o_8bd5e627:open",
            "basis:B10:o_2a3f106e:open",
        ],
        blocked_entries=blocked,
        blocked_rows=[BlockedDigestRow(book_id=b.book_id, reason=b.reason) for b in blocked],
        book_rows=rows,
        idle_book_ids=idle,
        awaiting_book_ids=awaiting,
        blocked_book_ids=blocked_books,
        idle_reason_counts={IDLE_NO_SIGNAL: 24},
        gate_hits=[f"Gate {b}:MAX_LOSS_PER_TRADE blocked ×1" for b in gated],
    )


class TestPinnedNight20261002:
    def test_the_old_push_is_what_the_issue_complains_about(self):
        title, _, body = FIXTURE.read_text(encoding="utf-8").partition("\n---\n")
        assert title.strip() == "basis executor: 4 entered, 22 blocked"
        assert len(body.strip().splitlines()) == 29

    def test_now_fits_a_glance(self):
        data = _night_of_2026_10_02()
        title, priority = _title_and_priority(data)
        assert title == "basis: all clear - 4 entered, 1 filled, 22 blocked"
        assert title.isascii()  # #598
        assert priority == "default"
        assert render_human(data).splitlines() == [
            "Filled B12 BEAR_CALL_SPREAD (OPEN) @ limit -0.84 (decision mid -0.84)",
            "4 entries submitted (B02, B10 ×2, B20)",
            (
                "4 books trading, 2 awaiting fill, 24 idle (no entry signal recorded); "
                "22 entries blocked (mostly no price, risk cap)"
            ),
            f"Lab P&L −$439; {BENCHMARK}; Reconciliation clean.",
        ]

    def test_detail_keeps_everything_the_push_dropped(self):
        data = _night_of_2026_10_02()
        detail, log = render_detail(data), render_log_line(data)
        assert "B10 [V0/XSP] P&L -298" in detail
        assert "Blocked (stopped by the risk envelope (MAX_LOSS_PER_TRADE) (spy_bull_call_spread_v1))" in detail
        assert "Gate B02:MAX_LOSS_PER_TRADE blocked ×1" in log
        assert "Entry submitted: basis:B10:o_2a3f106e:open" in log
        assert "Gate " not in render_human(data)


class TestPushShape:
    def test_typical_night(self):
        data = _night(
            fill_notices=[
                (
                    "B10 opened a GLD bear put spread (bets GLD falls). Paid $235. Max gain $265 if GLD ≤ $375 on "
                    "Nov 20; max loss $235 if GLD > $380. Breakeven $377.65."
                )
            ],
            fills=["Filled B10 BEAR_PUT_SPREAD (OPEN) @ limit +2.35 (decision mid +2.35)"],
            entries_placed=["basis:B07:o_1:open"],
        )
        body = render_human(data)
        assert body.splitlines() == [
            data.fill_notices[0],
            "1 entry submitted (B07)",
            "1 book trading",
            f"Lab P&L +$40; {BENCHMARK}; Reconciliation clean.",
        ]
        assert _title_and_priority(data)[0] == "basis: all clear - 1 entered, 1 filled"

    def test_busy_night_truncates_from_the_middle_and_keeps_the_summary(self):
        data = _night(
            fill_notices=[f"B{n:02d} opened a thing" for n in range(8)],
            closes_placed=["basis:B07:o_1:close", "basis:B07:o_2:close", "basis:B12:o_3:close"],
            entries_placed=["basis:B02:o_4:open"],
            anomalies=["ZOMBIE_FILL: something odd"],
        )
        lines = render_human(data).splitlines()
        assert len(lines) == PUSH_MAX_LINES
        assert lines[0] == "⛔ ZOMBIE_FILL: something odd"  # what needs you leads
        assert lines[1] == "B00 opened a thing"
        assert lines[2] == "B01 opened a thing"
        assert lines[3] == "…and 8 more in the console"  # 6 fills + closes + entries
        assert lines[4] == "1 book trading"
        assert lines[5].endswith("Reconciliation clean.")

    def test_action_needed_night(self):
        data = _night(
            banner=[
                "⛔ GLOBAL HALT_ENTRIES since 2026-10-02T01:00 — RECONCILIATION_DRIFT",
                "⛔ B04 HALT_ENTRIES since x — y",
            ],
            halted_scopes=["GLOBAL", "B04"],
            reconciliation="DRIFT",
            urgent_lines=[
                UrgentLine("ORDER_LOST_AT_BROKER (B01 — XSP): basis:B01:o1:open", needs_action=True),
                UrgentLine("acknowledgment held", needs_action=False),
            ],
        )
        title, priority = _title_and_priority(data)
        assert title == "basis: HALTED - 3 things need you - quiet night"
        assert title.isascii()
        assert priority == "high"
        lines = render_human(data).splitlines()
        assert lines[:3] == [
            "⛔ GLOBAL HALT_ENTRIES since 2026-10-02T01:00 — RECONCILIATION_DRIFT (+1 more halted)",
            "⛔ ORDER_LOST_AT_BROKER (B01 — XSP): basis:B01:o1:open",
            "⛔ Reconciliation DRIFT — entries halted until resolved",
        ]
        assert lines[-1].endswith("⛔ Reconciliation DRIFT — entries halted until resolved.")

    def test_one_thing_needs_you_title(self):
        data = _night(
            blocked_rows=[BlockedDigestRow(book_id=None, reason="STALE_DATA")],
            blocked_entries=[BlockedEntry(None, "STALE_DATA")],
        )
        assert _title_and_priority(data)[0] == "basis: 1 thing needs you - 1 blocked"
        lines = render_human(data).splitlines()
        assert lines[0] == "⛔ Blocked: ALL: STALE_DATA"
        assert lines[1] == "1 book trading; 1 entry blocked (all other)"

    def test_broker_lines(self):
        unreachable = _night(broker_ok=False, broker_api_errors=[(2110, "broken")])
        assert action_lines(unreachable) == [
            "⚠ IB Gateway unreachable — no orders were possible tonight (1 broker API error in the log)"
        ]
        assert action_lines(_night(broker_ok=False)) == ["⚠ IB Gateway unreachable — no orders were possible tonight"]
        instructed = _night(broker_ok=False, broker_instruction="accept the disclaimer")
        assert action_lines(instructed) == ["⛔ ACTION NEEDED: accept the disclaimer"]

    def test_stand_down_stays_in_the_push(self):
        from backend.digest import StandDown

        data = _night(stand_down=StandDown(active=True, stage="ineligible", books=2, sessions=3))
        assert (
            render_human(data).splitlines()[0]
            == "⚠ stand-down: all 2 books took no entry (ineligible), 3 sessions running"
        )
        assert _title_and_priority(data)[0] == "basis: all clear - quiet night"  # information, not an action

    def test_runaway_line_is_clipped_and_the_body_stays_under_the_cap(self):
        data = _night(anomalies=["X" * 5000, "é" * 5000, "⛔" * 5000, "Y" * 5000, "Z" * 5000])
        body = render_human(data)
        assert len(body.encode("utf-8")) <= PUSH_BODY_LIMIT_BYTES
        assert all(len(line.encode("utf-8")) <= 320 for line in body.splitlines())
        assert body.splitlines()[0].endswith("…")

    def test_fleet_line_variants(self):
        one_kind = _night(blocked_entries=[BlockedEntry("B02", "spy_x gated (MAX_LOSS)")] * 2)
        assert "2 entries blocked (all risk cap)" in render_human(one_kind)
        other = _night(
            blocked_entries=[BlockedEntry("B02", "something new"), BlockedEntry("B03", "spy_x thin credit")],
            book_rows=[_book("B01", 1.0, 1), _book("B02", 1.0, 1)],
        )
        assert (
            render_human(other).splitlines()[0] == "2 books trading; 2 entries blocked (mostly credit too thin, other)"
        )
        idle_one = _night(book_rows=[_book("B01", 0.0, 1), _book("B09", is_idle=True)], idle_book_ids=["B09"])
        assert render_human(idle_one).splitlines()[0] == "1 book trading, 1 idle"

    def test_closes_line_and_no_benchmark(self):
        data = _night(closes_placed=["basis:B07:o_1:close"], benchmark_line=None)
        lines = render_human(data).splitlines()
        assert lines[0] == "1 close submitted (B07)"
        assert lines[-1] == "Lab P&L +$40; Reconciliation clean."
        assert _title_and_priority(data)[0] == "basis: all clear - 1 closing"


# ---------------------------------------------------------------------------
# Plain-English fill lines from the fills ledger (#1115 in the digest)
# ---------------------------------------------------------------------------

TODAY = "2026-10-02"
SINCE = f"{TODAY}T21:00:00+00:00"
GLD_LEGS = [
    {
        "occ": "GLD261120P00380000",
        "option_type": "PUT",
        "direction": "LONG",
        "strike": 380.0,
        "expiration": "2026-11-20",
    },
    {
        "occ": "GLD261120P00375000",
        "option_type": "PUT",
        "direction": "SHORT",
        "strike": 375.0,
        "expiration": "2026-11-20",
    },
]


@pytest_asyncio.fixture
async def maker(tmp_path, monkeypatch):
    monkeypatch.setenv("HALT_FILE", str(tmp_path / "HALT"))
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with m() as s:
        s.add(
            BookModel(
                id="B10",
                name="V0 on GLD",
                config={"engine_variant": "V0", "underlying": "GLD", "envelope": {}},
                config_version=1,
                config_hash="h",
                starting_capital=10000.0,
                cash_balance=10000.0,
                status="ACTIVE",
                created_at="t0",
            )
        )
        s.add(
            PositionModel(
                id="pos_1",
                underlying="GLD",
                strategy_type="BEAR_PUT_SPREAD",
                legs=GLD_LEGS,
                entry_date="2026-10-02",
                expiration_date="2026-11-20",
                entry_premium=2.35,
                premium_direction="DEBIT",
                current_value_per_share=2.35,
                contracts=1,
                max_profit=265.0,
                max_loss=235.0,
                notes="",
                status="OPEN",
                journal={},
                book_id="B10",
            )
        )
        await s.commit()
    yield m
    await engine.dispose()


def _order(oid: str, ref: str, action: str, meta: dict, position_id: str | None = "pos_1") -> OrderModel:
    return OrderModel(
        id=oid,
        book_id="B10",
        position_id=position_id,
        order_ref=ref,
        action=action,
        combo_legs=meta,
        limit_price=2.35,
        decision_midpoint=2.35,
        status="FILLED",
        completed_at=f"{TODAY}T22:00:00+00:00",
    )


def _fill(exec_id: str, order_id: str, side: str, price: float, qty: float = 1.0) -> FillModel:
    return FillModel(
        exec_id=exec_id,
        order_id=order_id,
        book_id="B10",
        con_id=int(exec_id[1:]),
        side=side,
        quantity=qty,
        price=price,
        fill_time=f"{TODAY}T21:30:00+00:00",
    )


async def _renderings(m: async_sessionmaker[AsyncSession]):
    async with m() as s:
        return await compose_executor_digest_renderings(
            s, ExecutorRunSummary(reconciliation="CLEAN"), TODAY, since=SINCE
        )


OPEN_META = {"legs": GLD_LEGS, "quantity": 1, "strategy_type": "BEAR_PUT_SPREAD", "underlying": "GLD"}


class TestLedgerFillLines:
    @pytest.mark.asyncio
    async def test_open_fill_reads_in_plain_english(self, maker):
        async with maker() as s:
            s.add(_order("o_1", "basis:B10:o_1:open", "OPEN", OPEN_META))
            s.add_all([_fill("e1", "o_1", "BOT", 10.70), _fill("e2", "o_1", "SLD", 8.35)])
            await s.commit()
        r = await _renderings(maker)
        assert r.human_body.splitlines()[0] == (
            "B10 opened a GLD bear put spread (bets GLD falls). Paid $235. "
            "Max gain $265 if GLD ≤ $375 on Nov 20; max loss $235 if GLD > $380. Breakeven $377.65."
        )
        assert "Filled B10 BEAR_PUT_SPREAD (OPEN) @ limit +2.35" in r.log_body  # slippage evidence kept
        assert r.title == "basis: all clear - 1 filled"

    @pytest.mark.asyncio
    async def test_tp_close_reports_realized_pnl(self, maker):
        meta = {"legs": GLD_LEGS, "quantity": 1, "strategy_type": "BEAR_PUT_SPREAD", "exit_trigger": "PROFIT_TARGET"}
        async with maker() as s:
            s.add(_order("o_1_tp", "basis:B10:o_1:open:tp", "CLOSE", meta))
            s.add_all([_fill("e1", "o_1_tp", "SLD", 14.00), _fill("e2", "o_1_tp", "BOT", 10.45)])
            await s.commit()
        r = await _renderings(maker)
        assert r.human_body.splitlines()[0] == (
            "B10 closed its GLD bear put spread for +$120 (profit target). Collected $355 to close; entry paid $235."
        )

    @pytest.mark.asyncio
    async def test_short_fill_ledger_falls_back_to_the_raw_line(self, maker):
        async with maker() as s:
            s.add(_order("o_1", "basis:B10:o_1:open", "OPEN", OPEN_META))
            s.add(_fill("e1", "o_1", "BOT", 10.70))  # one leg on the ledger
            await s.commit()
        r = await _renderings(maker)
        assert r.human_body.splitlines()[0].startswith("Filled B10 BEAR_PUT_SPREAD (OPEN) @ limit +2.35")

    @pytest.mark.asyncio
    async def test_formatter_crash_falls_back_to_the_raw_line(self, maker):
        async with maker() as s:
            s.add(_order("o_1", "basis:B10:o_1:open", "OPEN", OPEN_META))
            s.add_all([_fill("e1", "o_1", "BOT", 10.70), _fill("e2", "o_1", "SLD", 8.35)])
            await s.commit()
        with patch.object(digest_module, "describe_option_order", side_effect=RuntimeError("bug")):
            r = await _renderings(maker)
        assert r.human_body.splitlines()[0].startswith("Filled B10 BEAR_PUT_SPREAD (OPEN)")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("ref", "meta", "position_id"),
        [
            ("basis:B36:s_1:share", OPEN_META, "pos_1"),  # share refs never reach the option formatter
            ("manual-ref", OPEN_META, "pos_1"),
            ("basis:B10:o_1:open", {**OPEN_META, "legs": [{"option_type": "X"}]}, "pos_1"),
            ("basis:B10:o_1:open", {**OPEN_META, "quantity": 0}, "pos_1"),
            ("basis:B10:o_1:open", {k: v for k, v in OPEN_META.items() if k != "underlying"}, None),
        ],
    )
    async def test_unstatable_orders_return_none(self, maker, ref, meta, position_id):
        async with maker() as s:
            order = _order("o_1", ref, "OPEN", meta, position_id)
            s.add(order)
            s.add_all([_fill("e1", "o_1", "BOT", 10.70), _fill("e2", "o_1", "SLD", 8.35)])
            await s.commit()
            assert await digest_module._plain_fill_line(s, order) is None

    @pytest.mark.asyncio
    async def test_underlying_and_strategy_from_the_position(self, maker):
        meta = {"legs": GLD_LEGS, "quantity": 1, "exit_trigger": "TIME_RULE"}
        async with maker() as s:
            order = _order("o_2", "basis:B10:o_2:close", "CLOSE", meta)
            s.add(order)
            s.add_all([_fill("e1", "o_2", "SLD", 3.00), _fill("e2", "o_2", "BOT", 1.50)])
            await s.commit()
            line = await digest_module._plain_fill_line(s, order)
        assert (
            line == "B10 closed its GLD bear put spread for −$85 (time exit). Collected $150 to close; entry paid $235."
        )


def test_detail_body_is_persisted_shape():
    # The renderings carry all three bodies; the executor persists each.
    data = _night()
    assert render_detail(data) != render_human(data)

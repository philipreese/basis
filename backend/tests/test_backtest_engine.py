"""Tests for the backtest engine (#796 PR-3): driver, fills, settlement, clock guard.

All fixtures are tiny synthetic corpora built through the PR-1 stores
(txt -> build_chain_db, closes CSVs) — never the real corpus, no mocks of
production decision code, no network. Close histories are generous
synthetic series long enough to clear the SMA/RV warmup gates.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from pathlib import Path

import pytest

import backend.dates
from backend.backtest.chain_store import ChainStore, build_chain_db
from backend.backtest.clock_guard import ReplayClockError, poisoned_clock
from backend.backtest.closes_store import ClosesStore
from backend.backtest.driver import (
    ReplayBook,
    ReplayConfig,
    ReplayPreconditionError,
    ReplayResult,
    replay_config_from_seeds,
    run_replay,
)
from backend.backtest.fills import (
    COMMISSION_PER_CONTRACT,
    Abandoned,
    CloseFill,
    EntryFill,
    fill_close,
    fill_entry,
    mark_value,
    snap_strike,
)
from backend.backtest.settlement import intrinsic_settlement_value
from backend.calendars import is_trading_day
from backend.models import (
    OperationalJournalEntrySchema,
    OptionLegSchema,
    PlaybookDefinitionSchema,
    PositionSchema,
    TradeSpecLeg,
)
from backend.observation import run_lifecycle_scan
from backend.seeds import SEED_PORTFOLIO_CONFIG

# ---------------------------------------------------------------------------
# Synthetic corpus builders
# ---------------------------------------------------------------------------

_HEADER = (
    "[QUOTE_UNIXTIME], [QUOTE_READTIME], [QUOTE_DATE], [QUOTE_TIME_HOURS],"
    " [UNDERLYING_LAST], [EXPIRE_DATE], [EXPIRE_UNIX], [DTE], [C_DELTA],"
    " [C_GAMMA], [C_VEGA], [C_THETA], [C_RHO], [C_IV], [C_VOLUME], [C_LAST],"
    " [C_SIZE], [C_BID], [C_ASK], [STRIKE], [P_BID], [P_ASK], [P_SIZE],"
    " [P_LAST], [P_DELTA], [P_GAMMA], [P_VEGA], [P_THETA], [P_RHO], [P_IV],"
    " [P_VOLUME], [STRIKE_DISTANCE], [STRIKE_DISTANCE_PCT]"
)

# (bid, ask) per side; None = side left unquoted in the vendor file.
PriceFn = Callable[[str, float], tuple[float | None, float | None]]


def _chain_line(
    quote_date: str, expire: str, strike: float, und: float, quote: tuple[float | None, float | None]
) -> str:
    bid, ask = quote
    bid_s = "" if bid is None else f"{bid:.2f}"
    ask_s = "" if ask is None else f"{ask:.2f}"
    cells = [
        "1600000000",
        f"{quote_date} 16:00",
        quote_date,
        "16.0",
        f"{und:.2f}",
        expire,
        "1610000000",
        "30.0",
        "0.25",  # C_DELTA
        "",
        "",
        "",
        "",
        "",
        "",
        "",
        "10 x 12",
        bid_s,  # C_BID
        ask_s,  # C_ASK
        f"{strike:.1f}",
        bid_s,  # P_BID
        ask_s,  # P_ASK
        "11 x 13",
        "",
        "-0.30",  # P_DELTA
        "",
        "",
        "",
        "",
        "",
        "",
        "5.0",
        "0.01",
    ]
    return ", ".join(f" {c} " for c in cells)


def _weekdays(start: datetime.date, end: datetime.date) -> list[datetime.date]:
    days = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += datetime.timedelta(days=1)
    return days


def _fridays(start: datetime.date, end: datetime.date) -> list[datetime.date]:
    return [d for d in _weekdays(start, end) if d.weekday() == 4]


def _build_chain(
    tmp_path: Path,
    quote_days: list[datetime.date],
    expiries: list[datetime.date],
    price_fn: PriceFn,
    *,
    strikes: tuple[int, int] = (250, 311),
    underlying: str = "SPY",
    name: str = "chains.db",
) -> ChainStore:
    txt_dir = tmp_path / f"txt_{name}"
    txt_dir.mkdir(parents=True, exist_ok=True)
    lines = [_HEADER]
    for day in quote_days:
        for expiry in expiries:
            if expiry < day:
                continue
            for strike in range(*strikes):
                lines.append(
                    _chain_line(
                        day.isoformat(),
                        expiry.isoformat(),
                        float(strike),
                        280.0,
                        price_fn(day.isoformat(), float(strike)),
                    )
                )
    (txt_dir / "fixture.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    db_path = tmp_path / name
    build_chain_db(txt_dir, underlying, db_path)
    return ChainStore(db_path)


def _spy_closes(replay_start: datetime.date, replay_end: datetime.date, *, skip: set[str] = frozenset()) -> list[str]:
    """~320 weekdays of history ending at replay_end: a noisy phase (high
    realized vol) followed by a smooth +0.2%/day phase covering the last 70
    pre-replay days and the replay window — CALM_BULL by construction
    (price > 1.01*SMA20, VIX_NORMAL, low RV-rank pseudo-IVR, flat return)."""
    smooth_start_idx_from_end = 70 + len(_weekdays(replay_start, replay_end))
    hist_start = replay_start - datetime.timedelta(days=640)
    days = [d for d in _weekdays(hist_start, replay_end)]
    days = days[-(250 + smooth_start_idx_from_end) :]
    lines = []
    price = 250.0
    n = len(days)
    for i, day in enumerate(days):
        if i >= n - smooth_start_idx_from_end:
            price = price * 1.002
        else:
            price = 250.0 + (5.0 if i % 2 else -5.0)
        if day.isoformat() not in skip:
            lines.append(f"{day.isoformat()},{price:.4f}")
    return ["date,close"] + lines


def _flat_closes(replay_start: datetime.date, replay_end: datetime.date, value: float) -> list[str]:
    hist_start = replay_start - datetime.timedelta(days=640)
    return ["date,close"] + [f"{d.isoformat()},{value:.4f}" for d in _weekdays(hist_start, replay_end)]


def _build_closes(tmp_path: Path, files: dict[str, list[str]]) -> ClosesStore:
    closes_dir = tmp_path / "closes"
    closes_dir.mkdir(parents=True, exist_ok=True)
    for name, lines in files.items():
        (closes_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ClosesStore(closes_dir)


def _playbook(
    pb_id: str = "bps_a", *, target_dte: int = 38, exit_dte: int = 21, profit: float = 50.0, stop: float = 200.0
) -> PlaybookDefinitionSchema:
    return PlaybookDefinitionSchema(
        id=pb_id,
        version="1.0",
        name=f"test {pb_id}",
        underlying_ticker="SPY",
        strategy_type="BULL_PUT_SPREAD",
        enabled=True,
        entry_filters={
            "min_ivr": 0.0,
            "max_ivr": 100.0,
            "vix_range": (10.0, 40.0),
            "required_trend": "ANY",
            "block_catalyst_14dte": False,
            "require_catalyst_14dte": False,
        },
        execution_specs={
            "target_dte": target_dte,
            "short_leg_delta": 0.16,
            "long_leg_delta": 0.05,
            "spread_width_dollars": 3.0,
            "straddle_atm": False,
        },
        exit_rules={"profit_take_pct": profit, "stop_loss_pct": stop, "mandatory_exit_dte": exit_dte},
    )


def _config(
    start: datetime.date,
    end: datetime.date,
    playbooks: tuple[PlaybookDefinitionSchema, ...],
    *,
    books: tuple[ReplayBook, ...] | None = None,
) -> ReplayConfig:
    if books is None:
        books = (
            ReplayBook(
                book_id="B90",
                underlying="SPY",
                config={"engine_variant": "V0", "underlying": "SPY", "envelope": {"max_positions": 1}},
            ),
        )
    return ReplayConfig(start=start, end=end, books=books, playbooks=playbooks, portfolio=SEED_PORTFOLIO_CONFIG)


# Entry-phase chain pricing: put value ~ strike/10, $0.05 spread. A 3-wide
# bull put spread then fills worst-side at exactly net -0.25 per share
# (credit), whatever strikes the production derivation picks.
def _entry_pricing(_day: str, strike: float) -> tuple[float | None, float | None]:
    return (round(strike / 10, 2), round(strike / 10 + 0.05, 2))


# Collapse pricing: put value ~ strike/100 — the credit spread's mark drops
# to 0.03, tripping the 50% profit-take P1.
def _collapsed_pricing(_day: str, strike: float) -> tuple[float | None, float | None]:
    return (round(strike / 100, 2), round(strike / 100 + 0.05, 2))


def _events(result: ReplayResult, kind: str) -> list:
    return [e for e in result.events if e.kind == kind]


# A window with no FOMC/CPI inside the next 3 trading days of its start and
# quiet enough weeks around it; 2019-07-15 is a Monday.
JUL15 = datetime.date(2019, 7, 15)


# ---------------------------------------------------------------------------
# fills.py unit tests
# ---------------------------------------------------------------------------


class TestFills:
    def _snap(self, tmp_path: Path, price_fn: PriceFn, strikes: tuple[int, int] = (250, 311)):
        store = _build_chain(tmp_path, [JUL15], [datetime.date(2019, 8, 16)], price_fn, strikes=strikes)
        snap = store.snapshot("SPY", JUL15)
        assert snap is not None
        return snap

    def test_snap_strike_nearest(self, tmp_path: Path) -> None:
        snap = self._snap(tmp_path, _entry_pricing)
        assert snap_strike(snap, "2019-08-16", "P", 263.4) == 263.0
        assert snap_strike(snap, "2019-08-16", "C", 263.6) == 264.0

    def test_snap_strike_tie_breaks_away_from_money(self, tmp_path: Path) -> None:
        # Only even strikes listed: 263.0 is exactly between 262 and 264.
        def even_only(day: str, strike: float) -> tuple[float | None, float | None]:
            return _entry_pricing(day, strike)

        store = _build_chain(tmp_path, [JUL15], [datetime.date(2019, 8, 16)], even_only, strikes=(250, 311))
        # Rebuild with only even strikes by filtering at fixture level.
        txt_dir = tmp_path / "txt_even"
        txt_dir.mkdir()
        lines = [_HEADER]
        for strike in (262.0, 264.0):
            lines.append(_chain_line(JUL15.isoformat(), "2019-08-16", strike, 280.0, _entry_pricing("", strike)))
        (txt_dir / "even.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        db = tmp_path / "even.db"
        build_chain_db(txt_dir, "SPY", db)
        snap = ChainStore(db).snapshot("SPY", JUL15)
        assert snap is not None
        # Puts snap DOWN (farther OTM below spot), calls snap UP.
        assert snap_strike(snap, "2019-08-16", "P", 263.0) == 262.0
        assert snap_strike(snap, "2019-08-16", "C", 263.0) == 264.0
        assert store is not None  # keep the broader store alive for symmetry

    def test_fill_entry_worst_side_and_commission(self, tmp_path: Path) -> None:
        snap = self._snap(tmp_path, _entry_pricing)
        legs = [
            TradeSpecLeg(action="SELL", option_type="PUT", strike=263.0, expiration_date="2019-08-16", quantity=1),
            TradeSpecLeg(action="BUY", option_type="PUT", strike=260.0, expiration_date="2019-08-16", quantity=1),
        ]
        fill = fill_entry(legs, snap, contracts=1)
        assert isinstance(fill, EntryFill)
        # SELL at bid (26.30), BUY at ask (26.05): net = 26.05 - 26.30 = -0.25.
        assert fill.net_per_share == pytest.approx(-0.25)
        assert fill.commission == pytest.approx(2 * COMMISSION_PER_CONTRACT)

    def test_fill_entry_missing_expiration_abandons(self, tmp_path: Path) -> None:
        snap = self._snap(tmp_path, _entry_pricing)
        legs = [TradeSpecLeg(action="SELL", option_type="PUT", strike=263.0, expiration_date="2019-09-20", quantity=1)]
        result = fill_entry(legs, snap, contracts=1)
        assert isinstance(result, Abandoned)
        assert result.reason == "NO_EXPIRATION"

    def test_fill_entry_missing_side_abandons_whole_spread(self, tmp_path: Path) -> None:
        def no_bid(_day: str, strike: float) -> tuple[float | None, float | None]:
            return (None, round(strike / 10 + 0.05, 2))

        snap = self._snap(tmp_path, no_bid)
        legs = [
            TradeSpecLeg(action="SELL", option_type="PUT", strike=263.0, expiration_date="2019-08-16", quantity=1),
            TradeSpecLeg(action="BUY", option_type="PUT", strike=260.0, expiration_date="2019-08-16", quantity=1),
        ]
        result = fill_entry(legs, snap, contracts=1)
        assert isinstance(result, Abandoned)
        assert result.reason == "MISSING_SIDE"

    def test_fill_close_worst_side(self, tmp_path: Path) -> None:
        snap = self._snap(tmp_path, _collapsed_pricing)
        legs = [
            {"option_type": "PUT", "direction": "SHORT", "strike": 263.0, "expiration": "2019-08-16"},
            {"option_type": "PUT", "direction": "LONG", "strike": 260.0, "expiration": "2019-08-16"},
        ]
        fill = fill_close(legs, snap, contracts=1)
        assert isinstance(fill, CloseFill)
        # LONG sells at bid (2.60), SHORT buys at ask (2.68): net -0.08.
        assert fill.exit_value_per_share == pytest.approx(-0.08)
        assert fill.commission == pytest.approx(2 * COMMISSION_PER_CONTRACT)

    def test_fill_close_missing_side_abandons(self, tmp_path: Path) -> None:
        def no_ask(_day: str, strike: float) -> tuple[float | None, float | None]:
            return (round(strike / 100, 2), None)

        snap = self._snap(tmp_path, no_ask)
        legs = [{"option_type": "PUT", "direction": "SHORT", "strike": 263.0, "expiration": "2019-08-16"}]
        result = fill_close(legs, snap, contracts=1)
        assert isinstance(result, Abandoned)
        assert result.reason == "MISSING_SIDE"

    def test_mark_value_mid_and_one_sided_none(self, tmp_path: Path) -> None:
        snap = self._snap(tmp_path, _entry_pricing)
        legs = [
            {"option_type": "PUT", "direction": "SHORT", "strike": 263.0, "expiration": "2019-08-16"},
            {"option_type": "PUT", "direction": "LONG", "strike": 260.0, "expiration": "2019-08-16"},
        ]
        # CREDIT: short mid - long mid = 26.325 - 26.025 = 0.30.
        assert mark_value(legs, "CREDIT", snap) == pytest.approx(0.30)

        def one_sided(_day: str, strike: float) -> tuple[float | None, float | None]:
            return (None, round(strike / 10, 2))

        snap2 = self._snap(tmp_path, one_sided)
        assert mark_value(legs, "CREDIT", snap2) is None


class TestSettlementMath:
    def test_mirrors_production_intrinsic_netting(self) -> None:
        legs = [
            {"option_type": "PUT", "direction": "SHORT", "strike": 265.0, "expiration": "x"},
            {"option_type": "PUT", "direction": "LONG", "strike": 260.0, "expiration": "x"},
        ]
        # Underlying at 262: short put intrinsic 3, long put 0 -> credit owes 3.
        assert intrinsic_settlement_value(legs, "CREDIT", 262.0) == pytest.approx(3.0)
        # OTM expiry: worthless spread settles at exactly 0.
        assert intrinsic_settlement_value(legs, "CREDIT", 280.0) == 0.0
        call_legs = [
            {"option_type": "CALL", "direction": "LONG", "strike": 260.0, "expiration": "x"},
            {"option_type": "CALL", "direction": "SHORT", "strike": 265.0, "expiration": "x"},
        ]
        assert intrinsic_settlement_value(call_legs, "DEBIT", 270.0) == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# clock_guard.py
# ---------------------------------------------------------------------------


class TestClockGuard:
    def _position(self) -> PositionSchema:
        return PositionSchema(
            id="p1",
            underlying="SPY",
            strategy_type="BULL_PUT_SPREAD",
            legs=[
                OptionLegSchema(
                    option_type="PUT",
                    direction="SHORT",
                    strike=263.0,
                    expiration="2026-12-18",
                    delta=0.0,
                    theta=0.0,
                    vega=0.0,
                )
            ],
            entry_date="2026-08-01",
            expiration_date="2026-12-18",
            entry_premium=0.25,
            premium_direction="CREDIT",
            current_value_per_share=0.25,
            contracts=1,
            max_profit=0.25,
            max_loss=2.75,
            notes="",
            status="OPEN",
            journal=OperationalJournalEntrySchema(
                core_thesis_rationale="t",
                structural_invalidation="t",
                expected_underlying_move_pct=0.0,
                pre_trade_emotional_state="Calm",
                pre_trade_confidence_rating=3,
            ),
        )

    def test_decision_function_without_today_raises_inside_guard(self) -> None:
        pos = self._position()
        with poisoned_clock(), pytest.raises(ReplayClockError):
            # run_lifecycle_scan holds its own `from backend.dates import
            # market_today` binding — the guard must reach it there.
            run_lifecycle_scan(pos, current_regime="CALM_BULL", spy_price=280.0, catalyst_dates=[])

    def test_restored_after_guard(self) -> None:
        pos = self._position()
        with poisoned_clock():
            pass
        assert isinstance(backend.dates.market_today(), datetime.date)
        result = run_lifecycle_scan(pos, current_regime="CALM_BULL", spy_price=280.0, catalyst_dates=[])
        assert "priority" in result

    def test_explicit_today_is_fine_inside_guard(self) -> None:
        pos = self._position()
        with poisoned_clock():
            result = run_lifecycle_scan(
                pos, current_regime="CALM_BULL", spy_price=280.0, catalyst_dates=[], today=datetime.date(2026, 8, 20)
            )
        assert "priority" in result


# ---------------------------------------------------------------------------
# driver.py — preconditions and seed loader
# ---------------------------------------------------------------------------


class TestPreconditions:
    def _stores(self, tmp_path: Path) -> tuple[ChainStore, ClosesStore]:
        chain = _build_chain(tmp_path, [JUL15], [datetime.date(2019, 8, 16)], _entry_pricing)
        closes = _build_closes(
            tmp_path,
            {"SPY.csv": _spy_closes(JUL15, JUL15), "VIX.csv": _flat_closes(JUL15, JUL15, 18.0)},
        )
        return chain, closes

    def test_range_outside_calendar_coverage_refused(self, tmp_path: Path) -> None:
        chain, closes = self._stores(tmp_path)
        config = _config(datetime.date(2024, 1, 2), datetime.date(2024, 1, 3), (_playbook(),))
        with pytest.raises(ReplayPreconditionError, match="calendar coverage"):
            run_replay(config, chain, closes)

    def test_missing_vix_closes_refused(self, tmp_path: Path) -> None:
        chain = _build_chain(tmp_path, [JUL15], [datetime.date(2019, 8, 16)], _entry_pricing)
        closes = _build_closes(tmp_path, {"SPY.csv": _spy_closes(JUL15, JUL15)})
        with pytest.raises(ReplayPreconditionError, match="VIX"):
            run_replay(_config(JUL15, JUL15, (_playbook(),)), chain, closes)

    def test_chain_missing_book_underlying_refused(self, tmp_path: Path) -> None:
        chain, closes = self._stores(tmp_path)
        books = (ReplayBook("B91", "IWM", {"engine_variant": "V0", "underlying": "IWM", "envelope": {}}),)
        with pytest.raises(ReplayPreconditionError, match="IWM"):
            run_replay(_config(JUL15, JUL15, (_playbook(),), books=books), chain, closes)

    def test_start_after_end_refused(self, tmp_path: Path) -> None:
        chain, closes = self._stores(tmp_path)
        with pytest.raises(ReplayPreconditionError, match="after end"):
            run_replay(_config(JUL15, JUL15 - datetime.timedelta(days=1), (_playbook(),)), chain, closes)


class TestSeedLoader:
    def test_builds_from_production_seeds(self) -> None:
        config = replay_config_from_seeds(
            datetime.date(2019, 1, 2), datetime.date(2019, 6, 28), book_ids=("B01", "B04")
        )
        assert {b.book_id for b in config.books} == {"B01", "B04"}
        assert config.books[0].underlying in ("XSP", "SPY")
        assert any(pb.id == "spy_iron_condor_v1" for pb in config.playbooks)
        assert config.portfolio["account"]["total_nav"] == 10000.0


# ---------------------------------------------------------------------------
# driver.py — end-to-end replays
# ---------------------------------------------------------------------------


class TestReplayEndToEnd:
    def test_entry_stages_and_fills_next_day_worst_side(self, tmp_path: Path) -> None:
        start, end = JUL15, JUL15 + datetime.timedelta(days=3)  # Mon..Thu
        days = [d for d in _weekdays(start, end) if is_trading_day(d)]
        expiries = _fridays(start, start + datetime.timedelta(days=60))
        chain = _build_chain(tmp_path, days, expiries, _entry_pricing)
        closes = _build_closes(
            tmp_path, {"SPY.csv": _spy_closes(start, end), "VIX.csv": _flat_closes(start, end, 18.0)}
        )
        result = run_replay(_config(start, end, (_playbook(),)), chain, closes)

        staged = _events(result, "ENTRY_STAGED")
        filled = _events(result, "ENTRY_FILLED")
        assert staged and staged[0].date == start.isoformat()
        assert filled and filled[0].date == days[1].isoformat()
        assert filled[0].detail["net_per_share"] == pytest.approx(-0.25)
        assert filled[0].detail["commission"] == pytest.approx(1.30)
        assert result.counters.entries_filled == 1

        pos = result.positions[0]
        assert pos["status"] == "OPEN"
        assert pos["premium_direction"] == "CREDIT"
        assert pos["entry_premium"] == pytest.approx(0.25)
        # Cash: +credit 25.00 - commission 1.30.
        assert result.book_cash["B90"] == pytest.approx(10000.0 + 25.0 - 1.30)

    def test_second_same_night_candidate_blocked_by_book_gates(self, tmp_path: Path) -> None:
        # Two playbooks, one position slot: the scan cannot see the pending
        # order, but evaluate_book_gates counts its encumbrance — the second
        # same-evening candidate must gate-block (MAX_POSITIONS), exactly
        # the production behavior the in-memory session exists to preserve.
        start, end = JUL15, JUL15 + datetime.timedelta(days=1)
        days = [d for d in _weekdays(start, end) if is_trading_day(d)]
        expiries = _fridays(start, start + datetime.timedelta(days=60))
        chain = _build_chain(tmp_path, days, expiries, _entry_pricing)
        closes = _build_closes(
            tmp_path, {"SPY.csv": _spy_closes(start, end), "VIX.csv": _flat_closes(start, end, 18.0)}
        )
        result = run_replay(_config(start, end, (_playbook("bps_a"), _playbook("bps_b"))), chain, closes)
        blocked = [e for e in _events(result, "ENTRY_BLOCKED") if e.detail.get("reason") == "gates"]
        assert blocked and "MAX_POSITIONS" in blocked[0].detail["gates"]
        assert result.counters.entries_staged == 1

    def test_missing_chain_day_abandons_entry(self, tmp_path: Path) -> None:
        start, end = JUL15, JUL15 + datetime.timedelta(days=1)
        expiries = _fridays(start, start + datetime.timedelta(days=60))
        # Chain exists ONLY on the staging day — the fill day has no snapshot.
        chain = _build_chain(tmp_path, [start], expiries, _entry_pricing)
        closes = _build_closes(
            tmp_path, {"SPY.csv": _spy_closes(start, end), "VIX.csv": _flat_closes(start, end, 18.0)}
        )
        result = run_replay(_config(start, end, (_playbook(),)), chain, closes)
        abandoned = _events(result, "ENTRY_ABANDONED")
        assert abandoned and abandoned[0].detail["reason"] == "NO_SNAPSHOT"
        assert result.counters.entries_abandoned == 1
        assert result.positions == []
        assert result.book_cash["B90"] == pytest.approx(10000.0)

    def test_unpriceable_leg_abandons_entry_counted(self, tmp_path: Path) -> None:
        start, end = JUL15, JUL15 + datetime.timedelta(days=1)
        days = [d for d in _weekdays(start, end) if is_trading_day(d)]
        expiries = _fridays(start, start + datetime.timedelta(days=60))

        def bidless(_day: str, strike: float) -> tuple[float | None, float | None]:
            return (None, round(strike / 10 + 0.05, 2))  # bids gone -> SELL leg unpriceable

        chain = _build_chain(tmp_path, days, expiries, bidless)
        closes = _build_closes(
            tmp_path, {"SPY.csv": _spy_closes(start, end), "VIX.csv": _flat_closes(start, end, 18.0)}
        )
        result = run_replay(_config(start, end, (_playbook(),)), chain, closes)
        abandoned = _events(result, "ENTRY_ABANDONED")
        assert abandoned and abandoned[0].detail["reason"] == "MISSING_SIDE"
        assert result.positions == []

    def test_sign_inverted_worst_side_credit_abandons(self, tmp_path: Path) -> None:
        # Flat pricing: every strike quotes 1.00/1.10, so the worst-side net
        # of a credit spread is +0.10 (a debit) — the #621-mirror sign gate
        # must refuse the fill rather than book an inverted credit.
        start, end = JUL15, JUL15 + datetime.timedelta(days=1)
        days = [d for d in _weekdays(start, end) if is_trading_day(d)]
        expiries = _fridays(start, start + datetime.timedelta(days=60))
        chain = _build_chain(tmp_path, days, expiries, lambda _d, _s: (1.00, 1.10))
        closes = _build_closes(
            tmp_path, {"SPY.csv": _spy_closes(start, end), "VIX.csv": _flat_closes(start, end, 18.0)}
        )
        result = run_replay(_config(start, end, (_playbook(),)), chain, closes)
        abandoned = _events(result, "ENTRY_ABANDONED")
        assert abandoned and abandoned[0].detail["reason"] == "SIGN_INVERTED"

    def _floor_book(self, ratio: float) -> tuple:
        return (
            ReplayBook(
                book_id="B90",
                underlying="SPY",
                config={
                    "engine_variant": "V0",
                    "underlying": "SPY",
                    "envelope": {"max_positions": 1},
                    "min_credit_ratio": ratio,
                },
            ),
        )

    def test_thin_credit_fill_abandons_for_knob_on_book(self, tmp_path: Path) -> None:
        # Replay mirror of the production minimum-credit floor (#820): the
        # 3-wide bull put spread fills worst-side at exactly -0.25, under a
        # 0.15 * 3.00 = 0.45 floor — the knob-on book abandons it, counted,
        # with the THIN_CREDIT reason alongside NO_SNAPSHOT/SIGN_INVERTED.
        start, end = JUL15, JUL15 + datetime.timedelta(days=1)
        days = [d for d in _weekdays(start, end) if is_trading_day(d)]
        expiries = _fridays(start, start + datetime.timedelta(days=60))
        chain = _build_chain(tmp_path, days, expiries, _entry_pricing)
        closes = _build_closes(
            tmp_path, {"SPY.csv": _spy_closes(start, end), "VIX.csv": _flat_closes(start, end, 18.0)}
        )
        result = run_replay(_config(start, end, (_playbook(),), books=self._floor_book(0.15)), chain, closes)
        abandoned = _events(result, "ENTRY_ABANDONED")
        assert abandoned and abandoned[0].detail["reason"] == "THIN_CREDIT"
        assert result.counters.entries_abandoned == 1
        assert result.positions == []
        assert result.book_cash["B90"] == pytest.approx(10000.0)

    def test_credit_above_the_floor_still_fills_for_knob_on_book(self, tmp_path: Path) -> None:
        # 0.05 * 3.00 = 0.15 floor < the 0.25 worst-side credit — the knob-on
        # book fills exactly as a knob-off book would (the knob-off case is
        # every other entry test in this file: golden parity by construction).
        start, end = JUL15, JUL15 + datetime.timedelta(days=1)
        days = [d for d in _weekdays(start, end) if is_trading_day(d)]
        expiries = _fridays(start, start + datetime.timedelta(days=60))
        chain = _build_chain(tmp_path, days, expiries, _entry_pricing)
        closes = _build_closes(
            tmp_path, {"SPY.csv": _spy_closes(start, end), "VIX.csv": _flat_closes(start, end, 18.0)}
        )
        result = run_replay(_config(start, end, (_playbook(),), books=self._floor_book(0.05)), chain, closes)
        assert _events(result, "ENTRY_FILLED")
        assert not _events(result, "ENTRY_ABANDONED")
        assert result.counters.entries_filled == 1

    def test_p1_exit_stages_and_fills_next_day(self, tmp_path: Path) -> None:
        start = JUL15
        end = start + datetime.timedelta(days=3)  # Mon..Thu
        days = [d for d in _weekdays(start, end) if is_trading_day(d)]
        expiries = _fridays(start, start + datetime.timedelta(days=60))

        # Entry pricing on Mon/Tue; collapse from Wed on: the mark drops to
        # 0.03 (profit 0.22 >= 50% of 0.25) -> P1 profit target Wed, close
        # fills worst-side Thursday.
        def phased(day: str, strike: float) -> tuple[float | None, float | None]:
            if day <= days[1].isoformat():
                return _entry_pricing(day, strike)
            return _collapsed_pricing(day, strike)

        chain = _build_chain(tmp_path, days, expiries, phased)
        closes = _build_closes(
            tmp_path, {"SPY.csv": _spy_closes(start, end), "VIX.csv": _flat_closes(start, end, 18.0)}
        )
        result = run_replay(_config(start, end, (_playbook(),)), chain, closes)

        close_staged = _events(result, "CLOSE_STAGED")
        close_filled = _events(result, "CLOSE_FILLED")
        assert close_staged and close_staged[0].date == days[2].isoformat()
        assert close_staged[0].detail["trigger"].startswith("P1")
        assert close_filled and close_filled[0].date == days[3].isoformat()
        # LONG sells at bid, SHORT buys at ask under collapsed pricing: -0.08.
        assert close_filled[0].detail["exit_value_per_share"] == pytest.approx(-0.08)
        pos = result.positions[0]
        assert pos["status"] == "CLOSED"
        assert pos["current_value_per_share"] == pytest.approx(0.08)
        # Cash: +25.00 credit -1.30 -8.00 buyback -1.30.
        assert result.book_cash["B90"] == pytest.approx(10000.0 + 25.0 - 1.30 - 8.0 - 1.30)

    def test_intrinsic_settlement_at_expiry(self, tmp_path: Path) -> None:
        start = JUL15
        # target_dte 16 -> Friday snap lands ~2019-08-02; run through it.
        end = datetime.date(2019, 8, 2)
        days = [d for d in _weekdays(start, end) if is_trading_day(d)]
        expiries = _fridays(start, start + datetime.timedelta(days=60))
        chain = _build_chain(tmp_path, days, expiries, _entry_pricing)
        closes = _build_closes(
            tmp_path, {"SPY.csv": _spy_closes(start, end), "VIX.csv": _flat_closes(start, end, 18.0)}
        )
        # No P1 triggers (stop 1000%, profit take 1000%), no time exit
        # (mandatory_exit_dte 0): the position rides to expiration.
        playbook = _playbook("bps_hold", target_dte=16, exit_dte=0, profit=1000.0, stop=1000.0)
        result = run_replay(_config(start, end, (playbook,)), chain, closes)

        settled = _events(result, "SETTLED")
        assert len(settled) == 1
        # SPY closes ~283+ while the derived put strikes sit ~13+ points
        # below spot: OTM expiry settles the credit spread at exactly 0.
        assert settled[0].detail["settled_value_per_share"] == 0.0
        pos = result.positions[0]
        assert pos["status"] == "EXPIRED"
        assert settled[0].date == pos["expiration_date"]
        assert result.counters.settlements == 1
        # Full credit kept: +25.00 - entry commission 1.30, no settlement fee.
        assert result.book_cash["B90"] == pytest.approx(10000.0 + 25.0 - 1.30)

    def test_stale_telemetry_day_blocks_entries(self, tmp_path: Path) -> None:
        start, end = JUL15, JUL15 + datetime.timedelta(days=1)
        days = [d for d in _weekdays(start, end) if is_trading_day(d)]
        expiries = _fridays(start, start + datetime.timedelta(days=60))
        chain = _build_chain(tmp_path, days, expiries, _entry_pricing)
        closes = _build_closes(
            tmp_path,
            {
                "SPY.csv": _spy_closes(start, end, skip={days[1].isoformat()}),
                "VIX.csv": _flat_closes(start, end, 18.0),
            },
        )
        result = run_replay(_config(start, end, (_playbook(),)), chain, closes)
        assert result.counters.stale_telemetry_days == 1
        stale_days = {e.date for e in _events(result, "STALE_TELEMETRY")}
        assert days[1].isoformat() in stale_days
        # The day-1 staging still fills on day 2 (fills need chains, not SPY).
        assert result.counters.entries_filled == 1
        # But no NEW entry staged on the stale day.
        assert all(e.date != days[1].isoformat() for e in _events(result, "ENTRY_STAGED"))

    def test_insufficient_data_variant_sits_out(self, tmp_path: Path) -> None:
        # A V2 book with no VIX3M series: INSUFFICIENT_DATA -> the book sits
        # out the day (production behavior, not an error).
        start = JUL15
        chain = _build_chain(tmp_path, [start], _fridays(start, start + datetime.timedelta(days=60)), _entry_pricing)
        closes = _build_closes(
            tmp_path, {"SPY.csv": _spy_closes(start, start), "VIX.csv": _flat_closes(start, start, 18.0)}
        )
        books = (ReplayBook("B92", "SPY", {"engine_variant": "V2", "underlying": "SPY", "envelope": {}}),)
        result = run_replay(_config(start, start, (_playbook(),), books=books), chain, closes)
        sit_outs = _events(result, "SIT_OUT")
        assert sit_outs and sit_outs[0].detail["variant"] == "V2"
        assert result.counters.entries_staged == 0


# ---------------------------------------------------------------------------
# V1 hysteresis threading
# ---------------------------------------------------------------------------


class TestV1Hysteresis:
    def test_prior_day_inputs_suppress_the_flip(self, tmp_path: Path) -> None:
        # Day 1: R = VIX/VIX3M = 1.00 -> backwardation EVENT_CATALYST latch.
        # Day 2: R = 0.96 (< 0.97) — WITHOUT the prior-day latch this reads
        # HIGH_VOL_NEUTRAL, but hysteresis requires TWO consecutive closes
        # below 0.97, so day 2 must hold EVENT_CATALYST.
        # Day 3: second consecutive R < 0.97 -> the latch releases.
        start = JUL15
        end = start + datetime.timedelta(days=2)  # Mon..Wed
        days = [d for d in _weekdays(start, end) if is_trading_day(d)]
        chain = _build_chain(tmp_path, days, _fridays(start, start + datetime.timedelta(days=60)), _entry_pricing)
        vix_by_day = {days[0].isoformat(): 20.0, days[1].isoformat(): 19.2, days[2].isoformat(): 19.2}
        vix_lines = ["date,close"]
        for d in _weekdays(start - datetime.timedelta(days=640), end):
            vix_lines.append(f"{d.isoformat()},{vix_by_day.get(d.isoformat(), 18.0):.4f}")
        closes = _build_closes(
            tmp_path,
            {
                "SPY.csv": _spy_closes(start, end),
                "VIX.csv": vix_lines,
                "VIX3M.csv": _flat_closes(start, end, 20.0),
            },
        )
        result = run_replay(_config(start, end, (_playbook(),)), chain, closes)
        readings = {e.date: e.detail for e in _events(result, "READINGS")}
        assert readings[days[0].isoformat()]["V1"] == "EVENT_CATALYST"  # backwardation fires
        assert readings[days[1].isoformat()]["V1"] == "EVENT_CATALYST"  # hysteresis holds the flip
        assert readings[days[2].isoformat()]["V1"] == "HIGH_VOL_NEUTRAL"  # two closes < 0.97 release


# ---------------------------------------------------------------------------
# Determinism and no-look-ahead
# ---------------------------------------------------------------------------


class TestDeterminismAndLookAhead:
    def _corpus(
        self,
        tmp_path: Path,
        start: datetime.date,
        end: datetime.date,
        *,
        chain_through: datetime.date | None = None,
        closes_through: datetime.date | None = None,
    ) -> tuple[ChainStore, ClosesStore]:
        chain_end = chain_through or end
        closes_end = closes_through or end
        days = [d for d in _weekdays(start, chain_end) if is_trading_day(d)]
        expiries = _fridays(start, start + datetime.timedelta(days=60))

        def phased(day: str, strike: float) -> tuple[float | None, float | None]:
            # Collapse mid-window so exits happen too.
            if day <= (start + datetime.timedelta(days=2)).isoformat():
                return _entry_pricing(day, strike)
            return _collapsed_pricing(day, strike)

        chain = _build_chain(tmp_path, days, expiries, phased, name=f"chains_{chain_end.isoformat()}.db")
        closes = _build_closes(
            tmp_path / f"closes_{closes_end.isoformat()}",
            {"SPY.csv": _spy_closes(start, closes_end), "VIX.csv": _flat_closes(start, closes_end, 18.0)},
        )
        return chain, closes

    def test_two_identical_runs_are_identical(self, tmp_path: Path) -> None:
        start, end = JUL15, JUL15 + datetime.timedelta(days=8)
        chain, closes = self._corpus(tmp_path, start, end)
        config = _config(start, end, (_playbook(),))
        first = run_replay(config, chain, closes)
        second = run_replay(config, chain, closes)
        assert first == second
        assert first.events  # non-trivial run

    def test_truncating_the_corpus_after_day_t_does_not_change_day_t(self, tmp_path: Path) -> None:
        # NO LOOK-AHEAD (non-negotiable): everything decided on or before T
        # must be byte-identical whether or not the corpus extends past T.
        start = JUL15
        end = start + datetime.timedelta(days=8)
        t = start + datetime.timedelta(days=3)  # Thursday
        full_chain, full_closes = self._corpus(tmp_path / "full", start, end)
        cut_chain, cut_closes = self._corpus(tmp_path / "cut", start, end, chain_through=t, closes_through=t)

        full = run_replay(_config(start, end, (_playbook(),)), full_chain, full_closes)
        truncated = run_replay(_config(start, t, (_playbook(),)), cut_chain, cut_closes)

        t_iso = t.isoformat()
        full_through_t = [e for e in full.events if e.date <= t_iso]
        assert full_through_t == truncated.events

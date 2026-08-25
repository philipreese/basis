"""driver.py — the replay day loop (#796 PR-3): the real rules, replayed.

REPLAY THE REAL RULES: the production pure functions are called verbatim —
``scan_opportunities``, ``generate_trade_spec``, ``compute_regime``, the
``classify_v1..v4`` variant classifiers, ``run_lifecycle_scan``,
``resolve_book_config``, ``_book_playbooks``, ``_book_scan_config`` and
``evaluate_book_gates`` (the last against an in-memory SQLite session on
the production schema, fidelity over reimplementation). This module
reimplements ONLY the orchestration the nightly executor performs around
them, and every place production code could not be reused the replica
carries a comment citing the production file:line it mirrors, so drift is
findable. Every decision call passes ``today=`` explicitly; the whole loop
runs inside ``poisoned_clock()`` so any defaulted wall-clock read dies
loudly (ReplayClockError) instead of silently looking ahead.

Fill timing: entries decided on day T stage that evening and fill on the
NEXT trading day's chain at the worst side (fills.py); triggered closes
likewise; positions reaching their expiration settle intrinsically
(settlement.py). Marks come from chain mids daily; a day without a usable
quote keeps the prior mark, counted.

Fail-closed preconditions (refuse to run, never warn): a date range outside
CALENDAR_COVERAGE_START/END (#795 — outside it the calendars silently
report "no holiday, no catalyst", wrong in the flattering direction); a
chain DB with no rows at all for a book's underlying; a closes store
missing SPY or VIX. A missing settlement close mid-run raises too — a
settlement value is money, never guessed.

Declared assumptions specific to this driver (beyond fills.py's):
- Historical IV-rank feeds don't exist, so ``underlying_ivrs`` is the
  RV-rank pseudo-IVR (#139's own math, regime_variants.rv_rank) for every
  underlying INCLUDING SPY — production's live IVR feed has no historical
  counterpart, and the pseudo-IVR is the mechanism production itself uses
  when no IV source exists.
- Catalyst dates are the seeded FOMC/CPI calendar (#795 backfill) merged
  exactly as production's nightly refresh merges them
  (catalyst_calendar.merge_catalysts); no earnings calendar exists
  historically, so scoped-catalyst playbooks never fire in replay.
- A trading day with no SPY close in the store is a stale-telemetry day:
  entries are blocked (production executor.py:1698-1702 posture); fills,
  settlement, marks and exits still run off the prior session's state.

Determinism: zero randomness anywhere; two identical runs produce
identical ReplayResults (pinned by test).
"""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.backtest.chain_store import ChainSnapshot, ChainStore
from backend.backtest.clock_guard import poisoned_clock
from backend.backtest.closes_store import ClosesStore
from backend.backtest.fills import Abandoned, fill_close, fill_entry, mark_value
from backend.backtest.settlement import intrinsic_settlement_value
from backend.book_gates import (
    BookConfig,
    CandidateOrder,
    credit_book_cash,
    evaluate_book_gates,
    resolve_book_config,
    stage_order,
)
from backend.calendars import CALENDAR_COVERAGE_END, CALENDAR_COVERAGE_START, is_trading_day
from backend.catalyst_calendar import merge_catalysts
from backend.executor import CONSENSUS_VARIANTS, _book_playbooks, _book_scan_config
from backend.market_data import format_occ_symbol, snapshot_from_closes
from backend.models import (
    Base,
    BookModel,
    MarketStateSchema,
    OrderModel,
    PlaybookDefinitionSchema,
    PortfolioConfigModel,
    PositionModel,
    TradeSpec,
)
from backend.observation import calculate_dte, run_lifecycle_scan
from backend.opportunity import capped_playbooks, generate_trade_spec, scan_opportunities
from backend.regime import compute_regime
from backend.regime_variants import (
    INSUFFICIENT_DATA,
    V3_CATALYST_WINDOW_TRADING_DAYS,
    V3_MIN_VIX_CLOSES,
    _ratio_regime,
    catalysts_within_trading_days,
    classify_v1,
    classify_v2,
    classify_v3,
    classify_v4,
    major_catalyst_within,
    percentile_rank,
    realized_vol_20d,
    rv_rank,
    sma,
)
from backend.seeds import LAB_BOOKS, SEED_PLAYBOOKS, SEED_PORTFOLIO_CONFIG, _config_hash
from backend.states import POSITION_OPEN_STATUS
from backend.telemetry import telemetry_key

#: Effectively "the whole series" — mirrors persist_regime_readings reading
#: index_history unbounded (regime_variants.py:391-393).
_HISTORY_DAYS = 5000


class ReplayPreconditionError(Exception):
    """The replay refused to start: a fail-closed precondition failed."""


class ReplayDataError(Exception):
    """The corpus lacked data the replay cannot proceed without (settlement)."""


@dataclass(frozen=True)
class ReplayBook:
    """One lab book to replay — same config shape as BookModel.config."""

    book_id: str
    underlying: str
    config: dict


@dataclass(frozen=True)
class ReplayConfig:
    """Everything a replay run is parameterized by."""

    start: datetime.date
    end: datetime.date
    books: tuple[ReplayBook, ...]
    playbooks: tuple[PlaybookDefinitionSchema, ...]
    portfolio: dict  # same shape as seeds.SEED_PORTFOLIO_CONFIG


def replay_config_from_seeds(
    start: datetime.date,
    end: datetime.date,
    book_ids: tuple[str, ...] | None = None,
) -> ReplayConfig:
    """Build a ReplayConfig from the production seed definitions (seeds.py) —
    the same books, playbooks and portfolio config the live lab races."""
    books = tuple(
        ReplayBook(book_id=b["id"], underlying=b["config"]["underlying"], config=b["config"])
        for b in LAB_BOOKS
        if book_ids is None or b["id"] in book_ids
    )
    playbooks = tuple(PlaybookDefinitionSchema(**p) for p in SEED_PLAYBOOKS)
    return ReplayConfig(start=start, end=end, books=books, playbooks=playbooks, portfolio=SEED_PORTFOLIO_CONFIG)


@dataclass(frozen=True)
class ReplayEvent:
    """One dated, typed record in the replay's decision/outcome stream."""

    date: str
    book_id: str | None
    kind: str
    detail: dict


@dataclass
class ReplayCounters:
    entries_staged: int = 0
    entries_filled: int = 0
    entries_abandoned: int = 0
    entries_blocked: int = 0
    closes_staged: int = 0
    closes_filled: int = 0
    closes_abandoned: int = 0
    settlements: int = 0
    stale_marks: int = 0
    sit_out_days: int = 0
    stale_telemetry_days: int = 0


@dataclass
class ReplayResult:
    events: list[ReplayEvent]
    counters: ReplayCounters
    book_cash: dict[str, float]
    positions: list[dict]


@dataclass
class _PendingEntry:
    order_id: str
    book_id: str
    playbook: PlaybookDefinitionSchema
    spec: TradeSpec
    entry_regime: str
    staged_on: datetime.date


@dataclass
class _PendingClose:
    position_id: str
    trigger: str
    reason: str
    staged_on: datetime.date


@dataclass
class _DayState:
    """Yesterday's telemetry carried forward for stale-telemetry days."""

    v0_regime: str
    v0_scores: dict[str, float]
    spy_price: float
    spy_sma20: float
    vix_close: float
    spy_daily_return: float
    readings: dict[str, str]


@dataclass
class _Sim:
    """Mutable replay state threaded through the day loop."""

    session_maker: async_sessionmaker[AsyncSession]
    events: list[ReplayEvent] = field(default_factory=list)
    counters: ReplayCounters = field(default_factory=ReplayCounters)
    pending_entries: list[_PendingEntry] = field(default_factory=list)
    pending_closes: list[_PendingClose] = field(default_factory=list)
    stale_marks_by_position: dict[str, int] = field(default_factory=dict)
    v1_prior_inputs: dict | None = None  # V1 hysteresis threading (regime_variants.py:464-472)
    last_day_state: _DayState | None = None


def _check_preconditions(config: ReplayConfig, chain_store: ChainStore, closes_store: ClosesStore) -> None:
    if config.start > config.end:
        raise ReplayPreconditionError(f"start {config.start} is after end {config.end}")
    if config.start < CALENDAR_COVERAGE_START or config.end > CALENDAR_COVERAGE_END:
        raise ReplayPreconditionError(
            f"replay range {config.start}..{config.end} lies outside verified calendar coverage "
            f"{CALENDAR_COVERAGE_START}..{CALENDAR_COVERAGE_END} (#795) — outside it the calendars "
            "silently report no holidays/catalysts, wrong in the flattering direction"
        )
    for symbol in ("SPY", "VIX"):
        if closes_store.daily_closes(symbol, through=config.end, days=1) is None:
            raise ReplayPreconditionError(f"closes store has no {symbol}.csv — regime inputs cannot be assembled")
    for underlying in sorted({b.underlying for b in config.books}):
        if not chain_store.available_dates(underlying):
            raise ReplayPreconditionError(f"chain DB has no rows for {underlying} — fills cannot be priced")


def run_replay(config: ReplayConfig, chain_store: ChainStore, closes_store: ClosesStore) -> ReplayResult:
    """Run the full replay. Synchronous entry point; the loop itself runs
    under asyncio because evaluate_book_gates is async (used UNMODIFIED)."""
    _check_preconditions(config, chain_store, closes_store)
    return asyncio.run(_run(config, chain_store, closes_store))


async def _run(config: ReplayConfig, chain_store: ChainStore, closes_store: ClosesStore) -> ReplayResult:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        sim = _Sim(session_maker=maker)
        async with maker() as session:
            await _seed_books(session, config)
            # The entire replay runs inside the clock guard: any production
            # code path that consults market_today() (a defaulted today=)
            # raises ReplayClockError instead of silently reading 2026.
            with poisoned_clock():
                day = config.start
                while day <= config.end:
                    if is_trading_day(day):
                        await _replay_day(sim, session, config, chain_store, closes_store, day)
                    day += datetime.timedelta(days=1)
            return await _build_result(sim, session)
    finally:
        await engine.dispose()


async def _seed_books(session: AsyncSession, config: ReplayConfig) -> None:
    """Seed the sim DB's books on the production schema — evaluate_book_gates
    reads BookModel rows directly (book status, config, config_hash)."""
    for book in config.books:
        basis = resolve_book_config(book.config).envelope.basis
        session.add(
            BookModel(
                id=book.book_id,
                name=f"replay {book.book_id}",
                config=book.config,
                config_version=1,
                config_hash=_config_hash(book.config),
                starting_capital=basis,
                cash_balance=basis,
                status="ACTIVE",
                created_at=config.start.isoformat(),
            )
        )
    await session.commit()


def _closes(closes_store: ClosesStore, symbol: str, through: datetime.date) -> list[float]:
    rows = closes_store.daily_closes(symbol, through=through, days=_HISTORY_DAYS) or []
    return [close for _, close in rows]


def _underlying_telemetry_from_closes(
    closes_store: ClosesStore, symbols: list[str], through: datetime.date
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    """Per-underlying (prices, sma20s, pseudo-IVRs) — replicates
    regime_variants.underlying_telemetry (regime_variants.py:146-166), which
    cannot be reused directly because it reads the production index_history
    table; the math (>=21 closes -> price+SMA20, rv_rank -> pseudo-IVR) is
    identical, sourced from the ClosesStore instead."""
    prices: dict[str, float] = {}
    smas: dict[str, float] = {}
    pseudo_ivrs: dict[str, float] = {}
    for symbol in symbols:
        closes = _closes(closes_store, symbol, through)
        if len(closes) >= 21:
            prices[symbol] = closes[-1]
            sma20 = sma(closes, 20)
            if sma20 is not None:
                smas[symbol] = round(sma20, 4)
        rank = rv_rank(closes)
        if rank is not None:
            pseudo_ivrs[symbol] = rank
    return prices, smas, pseudo_ivrs


def _compute_readings(
    sim: _Sim,
    closes_store: ClosesStore,
    today: datetime.date,
    catalysts: list[str],
    spy_closes: list[float],
    state_ivrs: dict[str, float],
) -> tuple[dict[str, str], dict[str, float], str]:
    """All variants' readings for tonight, via the PURE classifiers only.

    Input assembly replicates persist_regime_readings
    (regime_variants.py:423-580) exactly — that function cannot be reused
    because it reads/writes the production DB; the per-variant guard
    conditions, input dicts and INSUFFICIENT_DATA arms are mirrored 1:1,
    with V1's prior-day inputs threaded across days by the driver
    (sim.v1_prior_inputs, mirroring _prior_reading_inputs at
    regime_variants.py:396-404 including its behavior of returning an
    INSUFFICIENT_DATA row's inputs)."""
    results: dict[str, str] = {}
    spy_snapshot = snapshot_from_closes(spy_closes)
    assert spy_snapshot is not None  # caller guarantees a live-telemetry day

    vix_closes = _closes(closes_store, "VIX", today)
    vix3m_closes = _closes(closes_store, "VIX3M", today)
    vix9d_closes = _closes(closes_store, "VIX9D", today)
    vix = vix_closes[-1] if vix_closes else None
    vix3m = vix3m_closes[-1] if vix3m_closes else None
    vix9d = vix9d_closes[-1] if vix9d_closes else None
    spy_close = spy_closes[-1]
    sma200 = sma(spy_closes, 200)
    rv20 = realized_vol_20d(spy_closes)
    major_soon = major_catalyst_within(catalysts, today)

    # V0 — the control: compute_regime with the same inputs the nightly
    # refresh feeds it (operator.py:182-190).
    v0_regime, v0_scores = compute_regime(
        spy_price=spy_snapshot.price,
        spy_sma20=spy_snapshot.sma20,
        vix_close=vix or 0.0,
        underlying_ivrs=state_ivrs,
        spy_daily_return=spy_snapshot.daily_return,
        catalyst_dates=catalysts,
        today=today,
    )
    results["V0"] = v0_regime

    # V1 — mirrors regime_variants.py:463-484, hysteresis threaded manually.
    if vix and vix3m and sma200:
        regime, inputs = classify_v1(
            vix=vix,
            vix3m=vix3m,
            spy_close=spy_close,
            spy_sma200=sma200,
            major_catalyst_soon=major_soon,
            prior_inputs=sim.v1_prior_inputs,
        )
    else:
        regime = INSUFFICIENT_DATA
        inputs = {"have_vix": bool(vix), "have_vix3m": bool(vix3m), "have_sma200": sma200 is not None}
    sim.v1_prior_inputs = inputs
    results["V1"] = regime

    # V2 — mirrors regime_variants.py:486-506.
    if vix and vix3m and sma200 and rv20 is not None:
        regime, _ = classify_v2(
            vix=vix, vix3m=vix3m, spy_close=spy_close, spy_sma200=sma200, rv20=rv20, major_catalyst_soon=major_soon
        )
    else:
        regime = INSUFFICIENT_DATA
    results["V2"] = regime

    # V3 — mirrors regime_variants.py:508-535.
    if vix and vix3m and sma200 and len(vix_closes) >= V3_MIN_VIX_CLOSES:
        major_5td, minor_5td = catalysts_within_trading_days(catalysts, today, V3_CATALYST_WINDOW_TRADING_DAYS)
        regime, _ = classify_v3(
            vix=vix,
            vix3m=vix3m,
            spy_close=spy_close,
            spy_sma200=sma200,
            vix_percentile=percentile_rank(vix_closes[-252:]),
            major_catalyst_soon=major_5td,
            minor_catalyst_soon=minor_5td,
        )
    else:
        regime = INSUFFICIENT_DATA
    results["V3"] = regime

    # V4 — mirrors regime_variants.py:538-555 (observation-only).
    if vix and vix9d and sma200:
        regime, _ = classify_v4(
            vix9d=vix9d, vix=vix, spy_close=spy_close, spy_sma200=sma200, major_catalyst_soon=major_soon
        )
    else:
        regime = INSUFFICIENT_DATA
    results["V4"] = regime

    # V5/V6 — mirrors regime_variants.py:557-578.
    for variant, label, num_sym, den_sym in (("V5", "HYG/LQD", "HYG", "LQD"), ("V6", "RSP/SPY", "RSP", "SPY")):
        num_closes = _closes(closes_store, num_sym, today)
        den_closes = _closes(closes_store, den_sym, today)
        outcome = _ratio_regime(label, num_closes, den_closes, spy_close, sma200) if sma200 is not None else None
        results[variant] = outcome[0] if outcome is not None else INSUFFICIENT_DATA

    return results, v0_scores, v0_regime


async def _replay_day(
    sim: _Sim,
    session: AsyncSession,
    config: ReplayConfig,
    chain_store: ChainStore,
    closes_store: ClosesStore,
    today: datetime.date,
) -> None:
    """One trading day, in the production run's phase order (executor.py
    module docstring): fills first (yesterday's stagings), then expiry
    settlement, then marks + Layer A closes, then Layer C entries."""
    iso = today.isoformat()
    snapshots: dict[str, ChainSnapshot | None] = {}

    def snapshot_for(underlying: str) -> ChainSnapshot | None:
        if underlying not in snapshots:
            snapshots[underlying] = chain_store.snapshot(underlying, today)
        return snapshots[underlying]

    await _fill_pending(sim, session, today, snapshot_for)
    await _settle_expired(sim, session, closes_store, today)

    # ---- Telemetry for day T --------------------------------------------
    spy_rows = closes_store.daily_closes("SPY", through=today, days=_HISTORY_DAYS) or []
    spy_closes = [close for _, close in spy_rows]
    telemetry_live = bool(spy_rows) and spy_rows[-1][0] == iso and len(spy_closes) >= 2
    catalysts = merge_catalysts([], today)  # seeded FOMC/CPI calendar, merged as production does (#131)

    non_spy = sorted({b.underlying for b in config.books if telemetry_key(b.underlying) != "SPY"})
    prices, smas, pseudo_ivrs = _underlying_telemetry_from_closes(closes_store, non_spy, today)
    # Declared assumption: the RV-rank pseudo-IVR stands in for the live IVR
    # feed for SPY-scale underlyings too (module docstring).
    spy_rank = rv_rank(spy_closes)
    state_ivrs = {"SPY": spy_rank} if spy_rank is not None else {}

    if telemetry_live:
        readings, v0_scores, v0_regime = _compute_readings(sim, closes_store, today, catalysts, spy_closes, state_ivrs)
        spy_snapshot = snapshot_from_closes(spy_closes)
        assert spy_snapshot is not None
        vix_row = closes_store.latest_close("VIX", today)
        day_state = _DayState(
            v0_regime=v0_regime,
            v0_scores=v0_scores,
            spy_price=spy_snapshot.price,
            spy_sma20=spy_snapshot.sma20,
            vix_close=vix_row[1] if vix_row else 0.0,
            spy_daily_return=spy_snapshot.daily_return,
            readings=readings,
        )
        sim.last_day_state = day_state
        sim.events.append(ReplayEvent(iso, None, "READINGS", dict(readings)))
    else:
        sim.counters.stale_telemetry_days += 1
        sim.events.append(ReplayEvent(iso, None, "STALE_TELEMETRY", {}))
        day_state = sim.last_day_state
        if day_state is None:
            return  # nothing known yet — no marks, no exits, no entries

    await _mark_positions(sim, session, today, snapshot_for)
    await _stage_closes(sim, session, config, today, day_state, prices, catalysts)

    if telemetry_live:
        await _stage_entries(sim, session, config, today, day_state, prices, smas, pseudo_ivrs, state_ivrs, catalysts)


def _same_type_width(type_strikes: Iterable[tuple[str, float]]) -> float:
    """Widest same-option-type strike span — the replay twin of the width
    bound _try_place_entry computes (#282) and _fill_pending's own max_loss
    recompute below. 0.0 when no option type carries two strikes (calendars,
    straddles/strangles)."""
    pairs = list(type_strikes)
    spans = []
    for opt_type in ("CALL", "PUT"):
        strikes = [strike for leg_type, strike in pairs if leg_type == opt_type]
        if len(strikes) >= 2:
            spans.append(max(strikes) - min(strikes))
    return max(spans) if spans else 0.0


async def _fill_pending(
    sim: _Sim,
    session: AsyncSession,
    today: datetime.date,
    snapshot_for: Callable[[str], ChainSnapshot | None],
) -> None:
    """Fill yesterday's staged entries/closes on today's chain, worst-side."""
    iso = today.isoformat()

    due_entries = [p for p in sim.pending_entries if p.staged_on < today]
    sim.pending_entries = [p for p in sim.pending_entries if p.staged_on >= today]
    for pending in due_entries:
        order = await session.get(OrderModel, pending.order_id)
        assert order is not None
        book = await session.get(BookModel, pending.book_id)
        assert book is not None
        book_cfg = resolve_book_config(book.config)
        underlying = book_cfg.underlying or pending.spec.underlying
        snap = snapshot_for(underlying)
        result = (
            fill_entry(pending.spec.legs, snap, contracts=1)
            if snap is not None
            else Abandoned("NO_SNAPSHOT", f"no {underlying} chain on {iso}")
        )
        if isinstance(result, Abandoned):
            reason, detail = result.reason, result.detail
        elif result.net_per_share == 0.0:
            # Mirrors the production zero-mid unpriceable skip (executor.py:2059-2062).
            reason, detail = "ZERO_NET", "worst-side net is exactly 0.0"
        elif (pending.spec.premium_direction == "CREDIT") != (result.net_per_share < 0):
            # Mirrors the #621 sign gate (executor.py:2074-2089): a credit
            # structure must net a receipt; worst-side on a wide market can
            # invert a thin credit and must never book as a fill.
            reason, detail = "SIGN_INVERTED", f"{pending.spec.premium_direction} netted {result.net_per_share}"
        elif (
            book_cfg.min_credit_ratio is not None
            and pending.spec.premium_direction == "CREDIT"
            and (fill_width := _same_type_width((leg.option_type, leg.strike) for leg in result.legs))
            and abs(result.net_per_share) < book_cfg.min_credit_ratio * fill_width
        ):
            # Mirrors the production minimum-credit floor (#820, B34's knob;
            # executor.py's ENTRY_REFUSED_THIN_CREDIT): a knob-on book never
            # books a worst-side credit under min_credit_ratio of the
            # same-type width. Zero width (no multi-strike span) leaves the
            # floor inert — no denominator — matching production exactly.
            reason, detail = (
                "THIN_CREDIT",
                f"|{result.net_per_share}| < {book_cfg.min_credit_ratio} * {fill_width} width",
            )
        else:
            reason = None
            detail = ""
        if reason is not None:
            order.status = "CANCELLED"
            order.completed_at = iso
            await session.commit()
            sim.counters.entries_abandoned += 1
            sim.events.append(
                ReplayEvent(
                    iso,
                    pending.book_id,
                    "ENTRY_ABANDONED",
                    {"playbook": pending.playbook.id, "reason": reason, "detail": detail},
                )
            )
            continue

        net = result.net_per_share
        quantity = 1
        # Position construction mirrors _order_to_position's entry branch
        # (executor.py:1013-1102): entry_premium=|net|, direction from the
        # sign, and max_loss recomputed from the same-type width bound
        # (executor.py:1052-1059) with the decision-time estimate as the
        # zero-span/zero-net fallback.
        legs_meta = [
            {
                "occ": format_occ_symbol(underlying, leg.expiration, leg.option_type, leg.strike),
                "option_type": leg.option_type,
                "direction": "LONG" if leg.action == "BUY" else "SHORT",
                "strike": float(leg.strike),
                "expiration": leg.expiration,
            }
            for leg in result.legs
            for _ in range(leg.ratio)  # expand ratio like executor.py:2039-2050
        ]
        max_loss_ps = order.encumbered_risk / (100 * quantity)
        width_bound = _same_type_width((leg["option_type"], leg["strike"]) for leg in legs_meta)
        if width_bound and net != 0:
            max_loss_ps = width_bound - abs(net) if net < 0 else abs(net)
        pos_id = f"pos_{pending.order_id}"
        session.add(
            PositionModel(
                id=pos_id,
                underlying=underlying,
                strategy_type=pending.spec.strategy_type,
                legs=[
                    {
                        "option_type": leg["option_type"],
                        "direction": leg["direction"],
                        "strike": leg["strike"],
                        "expiration": leg["expiration"],
                        "delta": 0.0,
                        "theta": 0.0,
                        "vega": 0.0,
                        "gamma": 0.0,
                    }
                    for leg in legs_meta
                ],
                entry_date=iso,
                expiration_date=pending.spec.expiration_date,
                entry_premium=abs(net),
                premium_direction="CREDIT" if net < 0 else "DEBIT",
                current_value_per_share=abs(net),
                contracts=quantity,
                max_profit=abs(net) if net < 0 else 999999.0,
                max_loss=max_loss_ps,
                notes=f"Replay entry {order.order_ref}",
                rolls=0,
                status="OPEN",
                journal={
                    "core_thesis_rationale": f"Replay entry per playbook (order {order.order_ref})",
                    "structural_invalidation": "Playbook exit rules govern",
                    "expected_underlying_move_pct": 0.0,
                    "pre_trade_emotional_state": "Calm",
                    "pre_trade_confidence_rating": 3,
                    "entry_regime": pending.entry_regime,
                },
                playbook_id=pending.playbook.id,
                playbook_version=pending.playbook.version,
                playbook_snapshot=pending.playbook.model_dump(),
                last_priced_at=iso,
                config_hash=order.config_hash,
                book_id=pending.book_id,
            )
        )
        order.status = "FILLED"
        order.completed_at = iso
        # Cash mirrors executor.py:1137 (credit received / debit paid), plus
        # the declared flat commission (fills.py assumption 2).
        await credit_book_cash(session, pending.book_id, -net * 100 * quantity)
        await credit_book_cash(session, pending.book_id, -result.commission)
        await session.commit()
        sim.counters.entries_filled += 1
        sim.events.append(
            ReplayEvent(
                iso,
                pending.book_id,
                "ENTRY_FILLED",
                {
                    "position_id": pos_id,
                    "playbook": pending.playbook.id,
                    "net_per_share": net,
                    "commission": result.commission,
                    "legs": [(leg.option_type, leg.action, leg.strike) for leg in result.legs],
                },
            )
        )

    due_closes = [p for p in sim.pending_closes if p.staged_on < today]
    sim.pending_closes = [p for p in sim.pending_closes if p.staged_on >= today]
    for pending in due_closes:
        pos = await session.get(PositionModel, pending.position_id)
        if pos is None or pos.status != POSITION_OPEN_STATUS:
            continue  # settled/closed since staging — nothing to do
        snap = snapshot_for(pos.underlying)
        result = (
            fill_close(pos.legs, snap, pos.contracts)
            if snap is not None
            else Abandoned("NO_SNAPSHOT", f"no {pos.underlying} chain on {iso}")
        )
        if isinstance(result, Abandoned):
            # The position stays OPEN; tomorrow's lifecycle scan re-triggers
            # the close — the replay analog of the production escalation
            # ladder re-staging an unfilled close each evening.
            sim.counters.closes_abandoned += 1
            sim.events.append(
                ReplayEvent(
                    iso,
                    pos.book_id,
                    "CLOSE_ABANDONED",
                    {"position_id": pos.id, "reason": result.reason, "detail": result.detail},
                )
            )
            continue
        exit_value = result.exit_value_per_share
        # Mirrors _order_to_position's close branch (executor.py:956-993):
        # the exit price IS the final mark; cash moves by the SIGNED per-share
        # flow (negative = buying back a credit spread).
        pos.status = "CLOSED"
        pos.current_value_per_share = abs(exit_value)
        pos.last_priced_at = iso
        await credit_book_cash(session, pos.book_id, exit_value * 100 * pos.contracts)
        await credit_book_cash(session, pos.book_id, -result.commission)
        await session.commit()
        sim.counters.closes_filled += 1
        sim.events.append(
            ReplayEvent(
                iso,
                pos.book_id,
                "CLOSE_FILLED",
                {
                    "position_id": pos.id,
                    "trigger": pending.trigger,
                    "exit_value_per_share": exit_value,
                    "commission": result.commission,
                },
            )
        )


async def _settle_expired(sim: _Sim, session: AsyncSession, closes_store: ClosesStore, today: datetime.date) -> None:
    """Cash-settle OPEN positions whose expiration has passed — mirrors the
    production _settle_expired core (executor.py:680-787): intrinsic from the
    underlying's close on the expiration date, position -> EXPIRED, cash by
    (value if DEBIT else -value) x 100 x contracts (executor.py:759-760).
    The underlying resolves through telemetry_key (executor.py:887). The
    production PARTIAL/multi-expiration guards have no replay counterpart
    (the driver never creates those states). No commission: cash settlement
    is not a closing trade (declared)."""
    iso = today.isoformat()
    rows = (await session.execute(select(PositionModel).filter_by(status=POSITION_OPEN_STATUS))).scalars().all()
    for pos in sorted(rows, key=lambda p: p.id):
        if not pos.expiration_date or pos.expiration_date > iso:
            continue
        expire = datetime.date.fromisoformat(pos.expiration_date)
        close_row = closes_store.latest_close(telemetry_key(pos.underlying), through=expire)
        if close_row is None:
            raise ReplayDataError(
                f"no {telemetry_key(pos.underlying)} close at or before {pos.expiration_date} — "
                f"cannot settle {pos.id}; settlement value is money, never guessed"
            )
        # #793 dating rule: SPX/XSP expire_dates are LAST-TRADING-day dated,
        # so this at-or-before close IS that day's close — the declared
        # AM-settlement approximation (settlement.py module docstring).
        value = intrinsic_settlement_value(pos.legs, pos.premium_direction, close_row[1])
        pos.current_value_per_share = value
        pos.status = "EXPIRED"
        pos.last_priced_at = iso
        await credit_book_cash(
            session, pos.book_id, (value if pos.premium_direction == "DEBIT" else -value) * 100 * pos.contracts
        )
        await session.commit()
        sim.pending_closes = [p for p in sim.pending_closes if p.position_id != pos.id]
        sim.counters.settlements += 1
        sim.events.append(
            ReplayEvent(
                iso,
                pos.book_id,
                "SETTLED",
                {"position_id": pos.id, "settled_value_per_share": value, "underlying_close": close_row[1]},
            )
        )


async def _mark_positions(
    sim: _Sim,
    session: AsyncSession,
    today: datetime.date,
    snapshot_for: Callable[[str], ChainSnapshot | None],
) -> None:
    """Mark every OPEN position from today's chain mids; a leg without a
    two-sided quote keeps the prior mark, staleness counted (fills.py
    assumption 6)."""
    iso = today.isoformat()
    rows = (await session.execute(select(PositionModel).filter_by(status=POSITION_OPEN_STATUS))).scalars().all()
    for pos in sorted(rows, key=lambda p: p.id):
        snap = snapshot_for(pos.underlying)
        value = mark_value(pos.legs, pos.premium_direction, snap) if snap is not None else None
        if value is None:
            sim.counters.stale_marks += 1
            sim.stale_marks_by_position[pos.id] = sim.stale_marks_by_position.get(pos.id, 0) + 1
            continue
        pos.current_value_per_share = value
        pos.last_priced_at = iso
    await session.commit()


async def _stage_closes(
    sim: _Sim,
    session: AsyncSession,
    config: ReplayConfig,
    today: datetime.date,
    day_state: _DayState,
    prices: dict[str, float],
    catalysts: list[str],
) -> None:
    """Layer A: scan every OPEN position and stage next-day closes.

    Trigger logic replicates _layer_a_closes (executor.py:1192-1244): the
    production run_lifecycle_scan verdict (P1 closes tonight), plus the
    executor's own mandatory time exit (P2 DTE verdict upgraded to
    P1_TIME_EXIT off the frozen playbook snapshot) and B28's regime-flip
    exit — replicated rather than reused because the production function is
    welded to the broker session and control-plane tables."""
    iso = today.isoformat()
    already_pending = {p.position_id for p in sim.pending_closes}
    book_configs = {b.book_id: resolve_book_config(b.config) for b in config.books}
    rows = (await session.execute(select(PositionModel).filter_by(status=POSITION_OPEN_STATUS))).scalars().all()
    for pos in sorted(rows, key=lambda p: p.id):
        if pos.id in already_pending:
            continue
        scan = run_lifecycle_scan(
            pos.to_schema(),
            current_regime=day_state.v0_regime,
            spy_price=day_state.spy_price,
            catalyst_dates=catalysts,
            today=today,
            underlying_prices=prices,
        )
        priority, reason = scan["priority"], scan["reason"]
        if not priority.startswith("P1"):
            cfg = book_configs.get(pos.book_id)
            entry_regime = (pos.journal or {}).get("entry_regime") or ""
            current = day_state.readings.get((cfg.variant if cfg else None) or "V0")
            exit_dte = ((pos.playbook_snapshot or {}).get("exit_rules") or {}).get("mandatory_exit_dte", 21)
            dte = calculate_dte(pos.expiration_date, today)
            if (
                cfg is not None
                and cfg.exit_on_regime_flip
                and entry_regime
                and current
                and current != INSUFFICIENT_DATA
                and current != entry_regime
            ):
                priority = "P1_REGIME_FLIP"
                reason = f"REGIME_FLIP: entered under {entry_regime}, now {current}"
            elif dte <= exit_dte:
                priority = "P1_TIME_EXIT"
                reason = f"TIME_EXIT: {dte} DTE <= mandatory {exit_dte} DTE"
            else:
                continue
        sim.pending_closes.append(_PendingClose(position_id=pos.id, trigger=priority, reason=reason, staged_on=today))
        sim.counters.closes_staged += 1
        sim.events.append(
            ReplayEvent(
                iso, pos.book_id, "CLOSE_STAGED", {"position_id": pos.id, "trigger": priority, "reason": reason}
            )
        )


async def _stage_entries(
    sim: _Sim,
    session: AsyncSession,
    config: ReplayConfig,
    today: datetime.date,
    day_state: _DayState,
    prices: dict[str, float],
    smas: dict[str, float],
    pseudo_ivrs: dict[str, float],
    state_ivrs: dict[str, float],
    catalysts: list[str],
) -> None:
    """Layer C per book — replicates _layer_c_entries' orchestration
    (executor.py:1704-1808) over the production pure functions, staging
    entries for next-day fills instead of placing broker orders."""
    iso = today.isoformat()
    readings = day_state.readings
    playbooks = list(config.playbooks)
    config_model = PortfolioConfigModel(
        id=1,
        account=config.portfolio["account"],
        risk_profile=config.portfolio["risk_profile"],
        portfolio_greek_limits=config.portfolio["portfolio_greek_limits"],
    )

    for book in config.books:
        book_config = resolve_book_config(book.config)
        variant = book_config.variant or "V0"
        regime = readings.get(variant)
        if regime is None or regime == INSUFFICIENT_DATA:
            # Production behavior, not an error (executor.py:1724-1729):
            # a book whose variant cannot read the regime sits out the day.
            sim.counters.sit_out_days += 1
            sim.events.append(ReplayEvent(iso, book.book_id, "SIT_OUT", {"variant": variant}))
            continue

        # Ensemble-consensus gate — mirrors executor.py:1738-1756.
        if book_config.require_consensus:
            votes = sum(1 for v in CONSENSUS_VARIANTS if readings.get(v) == regime)
            if votes < book_config.require_consensus:
                sim.counters.entries_blocked += 1
                sim.events.append(
                    ReplayEvent(
                        iso,
                        book.book_id,
                        "ENTRY_BLOCKED",
                        {"reason": "consensus", "votes": votes, "required": book_config.require_consensus},
                    )
                )
                continue

        # Vol-aware delta cap — mirrors the executor's knob seam (#816):
        # credit-structure short legs only, fail closed on a missing VIX
        # (the SAME sit-out posture as an unreadable regime above — never
        # the `vix_close or 20.0` fabrication the knob-off scan keeps).
        book_playbooks = _book_playbooks(playbooks, book_config)
        if book_config.delta_cap_vix is not None:
            if day_state.vix_close <= 0:  # 0.0 = the store's missing-VIX sentinel
                sim.counters.sit_out_days += 1
                sim.events.append(
                    ReplayEvent(
                        iso,
                        book.book_id,
                        "SIT_OUT",
                        {"reason": "DELTA_CAP_NO_VIX", "delta_cap_vix": book_config.delta_cap_vix},
                    )
                )
                continue
            book_playbooks = capped_playbooks(book_playbooks, book_config.delta_cap_vix, day_state.vix_close)

        book_positions = [
            p.to_schema()
            for p in (await session.execute(select(PositionModel).filter_by(book_id=book.book_id))).scalars().all()
        ]
        # State construction mirrors executor.py:1762-1770 (pseudo-IVRs
        # supplement, never overwrite, the state's own IVR entries).
        state_schema = MarketStateSchema(
            current_regime=regime,
            spy_price=day_state.spy_price,
            spy_sma20=day_state.spy_sma20,
            vix_close=day_state.vix_close,
            underlying_ivrs={**pseudo_ivrs, **state_ivrs},
            spy_daily_return=day_state.spy_daily_return,
            catalyst_dates=catalysts,
            regime_scores={k: float(v) for k, v in day_state.v0_scores.items()},
            underlying_prices=prices,
            underlying_sma20=smas,
        )
        scan_config = _book_scan_config(config_model, book_config.envelope)
        scan = scan_opportunities(
            playbooks=book_playbooks,
            market_state=state_schema,
            positions=book_positions,
            portfolio_config=scan_config,
            today=today,
            enforce_regime=not book_config.ignore_regime,
            enforce_ivr=not book_config.ignore_ivr,
            book_mode=True,
        )
        if scan.portfolio_blocked:
            sim.events.append(ReplayEvent(iso, book.book_id, "SCAN_BLOCKED", {"reason": scan.block_reason or ""}))
            continue
        for candidate in scan.candidates:
            if not candidate.eligible:
                continue
            spec_result = generate_trade_spec(
                candidate.playbook, state_schema, book_positions, scan_config, contracts=1, today=today
            )
            if spec_result.spec is None:
                sim.events.append(
                    ReplayEvent(
                        iso,
                        book.book_id,
                        "SPEC_HARD_BLOCKED",
                        {"playbook": candidate.playbook.id, "blocks": [b.check for b in spec_result.hard_blocks]},
                    )
                )
                continue
            await _stage_entry(sim, session, book, book_config, candidate.playbook, spec_result.spec, regime, today)


async def _stage_entry(
    sim: _Sim,
    session: AsyncSession,
    book: ReplayBook,
    book_config: BookConfig,
    playbook: PlaybookDefinitionSchema,
    spec: TradeSpec,
    regime: str,
    today: datetime.date,
) -> None:
    """Gate and stage one entry for next-day fill.

    Mirrors the gate-relevant half of _try_place_entry (executor.py:1990-2222):
    the per-playbook dedup gate (#411, executor.py:1998-2022), the
    CandidateOrder construction (executor.py:2023-2050, 2153-2160) and the
    UNMODIFIED evaluate_book_gates + stage_order calls. The quote-dependent
    guards there (zero-mid, #621 sign gate, width bound) run at FILL time in
    this replay instead — the replay has no decision-time quotes, only the
    fill-day chain — and the broker-side guards (preview, duplicate-order
    halt) have no replay counterpart."""
    iso = today.isoformat()
    underlying = book_config.underlying or spec.underlying

    if book_config.dedup_playbook_entries:
        # Mirrors executor.py:1998-2022.
        exit_dte = playbook.exit_rules.mandatory_exit_dte or 21
        same_playbook = (
            (
                await session.execute(
                    select(PositionModel).filter_by(
                        book_id=book.book_id, playbook_id=playbook.id, status=POSITION_OPEN_STATUS
                    )
                )
            )
            .scalars()
            .all()
        )
        blocking = [p.id for p in same_playbook if calculate_dte(p.expiration_date, today) > exit_dte]
        if blocking:
            sim.counters.entries_blocked += 1
            sim.events.append(
                ReplayEvent(iso, book.book_id, "ENTRY_BLOCKED", {"reason": "dedup", "playbook": playbook.id})
            )
            return

    legs_meta = []
    candidate_legs = []
    for leg in spec.legs:
        occ = format_occ_symbol(underlying, leg.expiration_date, leg.option_type, leg.strike)
        direction = "LONG" if leg.action == "BUY" else "SHORT"
        ratio = max(1, leg.quantity)
        candidate_legs.append((occ, direction))
        legs_meta.extend(
            [
                {
                    "occ": occ,
                    "option_type": leg.option_type,
                    "direction": direction,
                    "strike": float(leg.strike),
                    "expiration": leg.expiration_date,
                }
            ]
            * ratio
        )

    candidate = CandidateOrder(
        book_id=book.book_id,
        strategy_type=spec.strategy_type,
        expiration_date=spec.expiration_date,
        legs=tuple(candidate_legs),
        max_loss_per_share=spec.max_loss_dollars / 100.0,  # decision-time, executor.py:2141
        contracts=1,
    )
    decision = await evaluate_book_gates(session, candidate)
    if not decision.allowed:
        sim.counters.entries_blocked += 1
        sim.events.append(
            ReplayEvent(
                iso,
                book.book_id,
                "ENTRY_BLOCKED",
                {"reason": "gates", "playbook": playbook.id, "gates": list(decision.blocked_by())},
            )
        )
        return

    order_id = f"o_{iso}_{book.book_id}_{playbook.id}"
    ref = f"backtest:{book.book_id}:{order_id}:open"
    # decision_midpoint: the spec's analytic premium estimate, signed by
    # direction (negative = credit) — the replay has no decision-time quote.
    signed_estimate = -spec.limit_price_per_share if spec.premium_direction == "CREDIT" else spec.limit_price_per_share
    await stage_order(
        session,
        candidate,
        order_id=order_id,
        order_ref=ref,
        limit_price=signed_estimate,
        decision_midpoint=signed_estimate,
        combo_legs={
            "legs": legs_meta,
            "quantity": 1,
            "strategy_type": spec.strategy_type,
            "expiration_date": spec.expiration_date,
            "underlying": underlying,
            "playbook_id": playbook.id,
            "playbook_version": playbook.version,
            "playbook_snapshot": playbook.model_dump(),
            "entry_regime": regime,
        },
    )
    sim.pending_entries.append(
        _PendingEntry(
            order_id=order_id,
            book_id=book.book_id,
            playbook=playbook,
            spec=spec,
            entry_regime=regime,
            staged_on=today,
        )
    )
    sim.counters.entries_staged += 1
    sim.events.append(
        ReplayEvent(
            iso,
            book.book_id,
            "ENTRY_STAGED",
            {"playbook": playbook.id, "expiration": spec.expiration_date, "strategy": spec.strategy_type},
        )
    )


async def _build_result(sim: _Sim, session: AsyncSession) -> ReplayResult:
    books = (await session.execute(select(BookModel))).scalars().all()
    positions = (await session.execute(select(PositionModel))).scalars().all()
    return ReplayResult(
        events=sim.events,
        counters=sim.counters,
        book_cash={b.id: round(b.cash_balance, 2) for b in sorted(books, key=lambda b: b.id)},
        positions=[
            {
                "id": p.id,
                "book_id": p.book_id,
                "underlying": p.underlying,
                "strategy_type": p.strategy_type,
                "status": p.status,
                "entry_date": p.entry_date,
                "expiration_date": p.expiration_date,
                "premium_direction": p.premium_direction,
                "entry_premium": p.entry_premium,
                "current_value_per_share": p.current_value_per_share,
                "contracts": p.contracts,
                "stale_marks": sim.stale_marks_by_position.get(p.id, 0),
            }
            for p in sorted(positions, key=lambda p: p.id)
        ],
    )

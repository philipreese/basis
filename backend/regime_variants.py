"""regime_variants.py — the regime-engine race arms (design §5, #69).

V0 (control) is backend/regime.py exactly as-is; this module adds the two
evidence-first challengers and persists ALL variants' nightly outputs to
regime_readings — regime *disagreement* is the informative early signal,
long before 30 trades per book accumulate.

- V1 — term-structure two-gate: R = VIX/VIX3M (slope is the best-evidenced
  cheap timing signal for short premium), T = SPY > SMA200. Backwardation or
  a major catalyst within 3 trading days ⇒ EVENT_CATALYST (= Do Nothing —
  the long-vol menu entries ship disabled). Hysteresis: an EVENT fired on
  backwardation holds until R < 0.97 for two consecutive closes — sell the
  relief, not the panic.
- V2 — VRP-conditioned: VRP = VIX − RV20 (annualized 20-day realized vol).
  The seller's edge must actually be present (VRP > 0) before short premium
  is on the menu; VRP ≥ 2.0 vol points is the literature-informed ballpark
  for a full-size regime call (a tunable, not a truth).

Rows are keyed (date, book_id='ALL', variant): every variant's reading is
account-level market state — book assignment lives in the book's config
({"engine_variant": ...}), seeded by backend/database.py.

Missing inputs (e.g. VIX3M history not yet accumulated) persist as regime
INSUFFICIENT_DATA — never a silent skip; the pipeline treats a book whose
variant reads INSUFFICIENT_DATA as entries-blocked (STALE_DATA family).
"""

import datetime
import logging
import math
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import IndexHistoryModel, MarketStateModel, RegimeReadingModel
from backend.regime import parse_catalyst

logger = logging.getLogger(__name__)

V0 = "V0"
V1 = "V1"
V2 = "V2"
V3 = "V3"
ALL_BOOKS = "ALL"  # readings are account-level market state, not per-book
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

VRP_FULL_EDGE = 2.0  # vol points — long-run median VRP ballpark (tunable)
CATALYST_WINDOW_TRADING_DAYS = 3
HYSTERESIS_EXIT_R = 0.97
V3_CATALYST_WINDOW_TRADING_DAYS = 5  # repaired matrix: 14 calendar → 5 trading
V3_MIN_VIX_CLOSES = 60  # fewer and the 252-day percentile is too noisy to score


def _trading_days_until(today: datetime.date, target: datetime.date) -> int:
    """Weekdays in (today, target]. Approximation without a holiday calendar —
    the market-holiday guard lives in the Gateway lifecycle, not here."""
    days = 0
    d = today
    while d < target:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def major_catalyst_within(catalyst_dates: list[str], today: datetime.date) -> bool:
    """True when a MAJOR catalyst (FOMC/CPI-class) lands within the next 3
    trading days — the pre-event vol premium concentrates in roughly the
    final 24 hours, so even 3 days is conservative (design §5)."""
    for cat in catalyst_dates:
        cat_type, _ = parse_catalyst(cat, today)
        if cat_type != "MAJOR":
            continue
        match = re.search(r"\d{4}-\d{2}-\d{2}", cat)
        if not match:
            continue
        cat_date = datetime.date.fromisoformat(match.group(0))
        if cat_date >= today and _trading_days_until(today, cat_date) <= CATALYST_WINDOW_TRADING_DAYS:
            return True
    return False


def catalysts_within_trading_days(
    catalyst_dates: list[str], today: datetime.date, trading_days: int
) -> tuple[bool, bool]:
    """(major_soon, minor_soon) for catalysts within the next N trading days.
    V3's repaired catalyst dimension uses a 5-trading-day window instead of
    V0's 14 calendar days (design §5)."""
    major = minor = False
    for cat in catalyst_dates:
        cat_type, _ = parse_catalyst(cat, today)
        match = re.search(r"\d{4}-\d{2}-\d{2}", cat)
        if not match:
            continue
        cat_date = datetime.date.fromisoformat(match.group(0))
        if cat_date >= today and _trading_days_until(today, cat_date) <= trading_days:
            if cat_type == "MAJOR":
                major = True
            else:
                minor = True
    return major, minor


def percentile_rank(series: list[float]) -> float:
    """Percentile (0–100) of the latest value within the series."""
    current = series[-1]
    return round(100.0 * sum(1 for v in series if v <= current) / len(series), 1)


def realized_vol_20d(closes: list[float]) -> float | None:
    """Annualized 20-day close-to-close realized vol of SPY, in vol points."""
    if len(closes) < 21:
        return None
    recent = closes[-21:]
    log_returns = [math.log(recent[i] / recent[i - 1]) for i in range(1, len(recent))]
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    return math.sqrt(variance) * math.sqrt(252) * 100.0


def sma(closes: list[float], length: int) -> float | None:
    if len(closes) < length:
        return None
    return sum(closes[-length:]) / length


# Pseudo-IVR floor: fewer closes than this and an RV20 percentile rank is
# too noisy to gate on — the underlying stays blocked until history accrues.
RV_RANK_MIN_CLOSES = 60


def rv_rank(closes: list[float]) -> float | None:
    """Percentile rank (0–100) of the latest RV20 against up to a trailing
    year of rolling RV20 readings — the pseudo-IVR for underlyings with no
    IV-rank source (#139). None when history is too short to rank."""
    if len(closes) < RV_RANK_MIN_CLOSES:
        return None
    series: list[float] = []
    for i in range(21, len(closes) + 1):
        vol = realized_vol_20d(closes[i - 21 : i])
        if vol is not None:
            series.append(vol)
    return percentile_rank(series[-252:])


async def underlying_telemetry(
    session: AsyncSession, symbols: tuple[str, ...] | list[str]
) -> tuple[dict[str, float], dict[str, float], dict[str, float]]:
    """Per-underlying (prices, sma20s, pseudo-IVRs) from index_history for
    non-SPY-scale underlyings (#139). A symbol with insufficient history is
    simply absent — the scan then suppresses its playbooks (never trades
    blind off SPY telemetry)."""
    prices: dict[str, float] = {}
    smas: dict[str, float] = {}
    pseudo_ivrs: dict[str, float] = {}
    for symbol in symbols:
        closes = await _index_closes(session, symbol)
        if len(closes) >= 21:
            prices[symbol] = closes[-1]
            sma20 = sma(closes, 20)
            if sma20 is not None:
                smas[symbol] = round(sma20, 4)
        rank = rv_rank(closes)
        if rank is not None:
            pseudo_ivrs[symbol] = rank
    return prices, smas, pseudo_ivrs


def classify_v1(
    *,
    vix: float,
    vix3m: float,
    spy_close: float,
    spy_sma200: float,
    major_catalyst_soon: bool,
    prior_inputs: dict | None = None,
) -> tuple[str, dict]:
    """Term-structure two-gate. Returns (regime, inputs-for-the-record)."""
    r = vix / vix3m
    trend_up = spy_close > spy_sma200
    inputs: dict = {
        "R": round(r, 4),
        "trend_above_sma200": trend_up,
        "major_catalyst_within_3td": major_catalyst_soon,
        "backwardation_event": False,
    }
    if r >= 1.00:
        inputs["backwardation_event"] = True
        return "EVENT_CATALYST", inputs
    if major_catalyst_soon:
        return "EVENT_CATALYST", inputs
    # Hysteresis: a backwardation-fired EVENT holds until R < 0.97 for two
    # consecutive closes (this one and the previous one).
    if prior_inputs and prior_inputs.get("backwardation_event"):
        prior_r = prior_inputs.get("R")
        if not (r < HYSTERESIS_EXIT_R and prior_r is not None and prior_r < HYSTERESIS_EXIT_R):
            inputs["backwardation_event"] = True  # keep the latch visible to tomorrow's run
            inputs["hysteresis_hold"] = True
            return "EVENT_CATALYST", inputs
    if not trend_up:
        return "TRENDING_BEAR", inputs
    if r >= 0.95:
        return "HIGH_VOL_NEUTRAL", inputs
    return "CALM_BULL", inputs


def classify_v2(
    *,
    vix: float,
    vix3m: float,
    spy_close: float,
    spy_sma200: float,
    rv20: float,
    major_catalyst_soon: bool,
) -> tuple[str, dict]:
    """VRP-conditioned. Returns (regime, inputs-for-the-record)."""
    r = vix / vix3m
    trend_up = spy_close > spy_sma200
    vrp = vix - rv20
    inputs = {
        "R": round(r, 4),
        "trend_above_sma200": trend_up,
        "RV20": round(rv20, 2),
        "VRP": round(vrp, 2),
        "major_catalyst_within_3td": major_catalyst_soon,
    }
    if r >= 1.00 or vrp <= 0 or major_catalyst_soon:
        return "EVENT_CATALYST", inputs
    if vrp >= VRP_FULL_EDGE and trend_up:
        return "CALM_BULL", inputs
    if vrp >= VRP_FULL_EDGE:
        return "HIGH_VOL_NEUTRAL", inputs
    return "TRENDING_BEAR", inputs


def classify_v3(
    *,
    vix: float,
    vix3m: float,
    spy_close: float,
    spy_sma200: float,
    vix_percentile: float,
    major_catalyst_soon: bool,
    minor_catalyst_soon: bool,
) -> tuple[str, dict]:
    """Matrix repaired (design §5 V3): the V0 scoring-matrix shape and
    weights with the dimensions fixed — isolating whether the matrix's
    problem is its dimensions or its weights.

    1. Absolute-VIX dimension → VIX/VIX3M ratio buckets (<0.90 calm /
       0.90–1.00 elevated / >=1.00 backwardation), reusing the LOW /
       ELEVATED / HIGH weight rows (the NORMAL row goes unused).
    2. Per-underlying IVR → the VIX 252-day percentile, applied ONCE through
       the same classify_ivr thresholds/weights — this also removes the
       watchlist-size multiplier the V0 loop silently applied.
    3. SMA20 → SMA200; the daily-return dimension is dropped (it
       double-counted short-term price noise with SMA20).
    4. Catalyst window 14 calendar days → 5 trading days.
    """
    from backend.regime import REGIME_HIERARCHY, classify_ivr, classify_spy_sma

    r = vix / vix3m
    scores: dict[str, float] = {"CALM_BULL": 0.0, "HIGH_VOL_NEUTRAL": 0.0, "TRENDING_BEAR": 0.0, "EVENT_CATALYST": 0.0}

    # 1. Trend vs SMA200 — same labels and weights as V0's SMA dimension.
    sma_label = classify_spy_sma(spy_close, spy_sma200)
    if sma_label == "ABOVE_STRONG":
        scores["CALM_BULL"] += 2
        scores["TRENDING_BEAR"] -= 2
    elif sma_label == "ABOVE_FLAT":
        scores["CALM_BULL"] += 1
        scores["HIGH_VOL_NEUTRAL"] += 1
        scores["TRENDING_BEAR"] -= 1
    elif sma_label == "AT":
        scores["HIGH_VOL_NEUTRAL"] += 1
    elif sma_label == "BELOW_FLAT":
        scores["TRENDING_BEAR"] += 1
        scores["HIGH_VOL_NEUTRAL"] += 1
        scores["CALM_BULL"] -= 1
    else:  # BELOW_FALLING
        scores["TRENDING_BEAR"] += 2
        scores["CALM_BULL"] -= 2

    # 2. Term-structure ratio buckets with the VIX-dimension weight rows.
    if r < 0.90:
        r_label = "R_CALM"
        scores["CALM_BULL"] += 2
        scores["HIGH_VOL_NEUTRAL"] -= 1
        scores["TRENDING_BEAR"] -= 1
    elif r < 1.00:
        r_label = "R_ELEVATED"
        scores["HIGH_VOL_NEUTRAL"] += 2
        scores["TRENDING_BEAR"] += 1
        scores["CALM_BULL"] -= 1
    else:
        r_label = "R_BACKWARDATION"
        scores["TRENDING_BEAR"] += 2
        scores["HIGH_VOL_NEUTRAL"] += 1
        scores["CALM_BULL"] -= 2

    # 3. VIX percentile through the IVR weight rows, applied once.
    pct_label = classify_ivr(vix_percentile)
    if pct_label == "IVR_LOW":
        scores["CALM_BULL"] += 1
        scores["HIGH_VOL_NEUTRAL"] -= 2
    elif pct_label == "IVR_MODERATE":
        scores["CALM_BULL"] += 1
    elif pct_label == "IVR_ELEVATED":
        scores["HIGH_VOL_NEUTRAL"] += 2
        scores["EVENT_CATALYST"] += 1
    else:  # IVR_HIGH
        scores["HIGH_VOL_NEUTRAL"] += 1
        scores["TRENDING_BEAR"] += 1
        scores["EVENT_CATALYST"] += 1
        scores["CALM_BULL"] -= 1

    # 4. Catalyst dimension, 5-trading-day window.
    if major_catalyst_soon:
        scores["EVENT_CATALYST"] += 3
        scores["CALM_BULL"] -= 1
    elif minor_catalyst_soon:
        scores["EVENT_CATALYST"] += 1
    else:
        scores["CALM_BULL"] += 1
        scores["EVENT_CATALYST"] -= 2

    max_score = max(scores.values())
    tied = [reg for reg, s in scores.items() if s == max_score]
    winner = next(reg for reg in REGIME_HIERARCHY if reg in tied)

    inputs = {
        "R": round(r, 4),
        "r_label": r_label,
        "sma_label": sma_label,
        "vix_percentile": round(vix_percentile, 1),
        "pct_label": pct_label,
        "major_catalyst_within_5td": major_catalyst_soon,
        "minor_catalyst_within_5td": minor_catalyst_soon,
        "scores": scores,
    }
    return winner, inputs


async def _index_closes(session: AsyncSession, symbol: str) -> list[float]:
    rows = await session.execute(select(IndexHistoryModel).filter_by(symbol=symbol).order_by(IndexHistoryModel.date))
    return [r.close for r in rows.scalars().all()]


async def _prior_reading_inputs(session: AsyncSession, variant: str, before_date: str) -> dict | None:
    rows = await session.execute(
        select(RegimeReadingModel)
        .filter(RegimeReadingModel.engine_variant == variant, RegimeReadingModel.date < before_date)
        .order_by(RegimeReadingModel.date.desc())
        .limit(1)
    )
    row = rows.scalar_one_or_none()
    return row.inputs if row else None


async def _upsert_reading(
    session: AsyncSession, date: str, variant: str, regime: str, inputs: dict, scores: dict
) -> None:
    row = await session.get(RegimeReadingModel, (date, ALL_BOOKS, variant))
    if row is None:
        session.add(
            RegimeReadingModel(
                date=date, book_id=ALL_BOOKS, engine_variant=variant, regime=regime, inputs=inputs, scores=scores
            )
        )
    else:
        row.regime = regime
        row.inputs = inputs
        row.scores = scores


async def persist_regime_readings(session: AsyncSession, today: datetime.date | None = None) -> dict[str, str]:
    """Compute and persist every variant's reading for tonight. Returns
    {variant: regime} for the digest/log line."""
    today = today or datetime.datetime.now(datetime.UTC).date()
    date_str = today.isoformat()

    state = (await session.execute(select(MarketStateModel).filter_by(id=1))).scalar_one_or_none()
    if state is None:
        logger.warning("No market state — skipping regime readings")
        return {}
    catalysts = state.catalyst_dates or []
    major_soon = major_catalyst_within(catalysts, today)

    results: dict[str, str] = {}

    # V0 — the control: whatever the current matrix computed tonight.
    await _upsert_reading(
        session,
        date_str,
        V0,
        state.current_regime,
        {
            "spy_price": state.spy_price,
            "spy_sma20": state.spy_sma20,
            "vix_close": state.vix_close,
            "spy_daily_return": state.spy_daily_return,
        },
        state.regime_scores or {},
    )
    results[V0] = state.current_regime

    vix_closes = await _index_closes(session, "VIX")
    vix3m_closes = await _index_closes(session, "VIX3M")
    spy_closes = await _index_closes(session, "SPY")
    vix = vix_closes[-1] if vix_closes else (state.vix_close or None)
    vix3m = vix3m_closes[-1] if vix3m_closes else None
    spy_close = spy_closes[-1] if spy_closes else state.spy_price
    sma200 = sma(spy_closes, 200)
    rv20 = realized_vol_20d(spy_closes)

    if vix and vix3m and sma200:
        prior = await _prior_reading_inputs(session, V1, date_str)
        regime, inputs = classify_v1(
            vix=vix,
            vix3m=vix3m,
            spy_close=spy_close,
            spy_sma200=sma200,
            major_catalyst_soon=major_soon,
            prior_inputs=prior,
        )
        await _upsert_reading(session, date_str, V1, regime, inputs, {})
    else:
        regime = INSUFFICIENT_DATA
        await _upsert_reading(
            session,
            date_str,
            V1,
            regime,
            {"have_vix": bool(vix), "have_vix3m": bool(vix3m), "have_sma200": sma200 is not None},
            {},
        )
    results[V1] = regime

    if vix and vix3m and sma200 and rv20 is not None:
        regime, inputs = classify_v2(
            vix=vix, vix3m=vix3m, spy_close=spy_close, spy_sma200=sma200, rv20=rv20, major_catalyst_soon=major_soon
        )
        await _upsert_reading(session, date_str, V2, regime, inputs, {})
    else:
        regime = INSUFFICIENT_DATA
        await _upsert_reading(
            session,
            date_str,
            V2,
            regime,
            {
                "have_vix": bool(vix),
                "have_vix3m": bool(vix3m),
                "have_sma200": sma200 is not None,
                "have_rv20": rv20 is not None,
            },
            {},
        )
    results[V2] = regime

    if vix and vix3m and sma200 and len(vix_closes) >= V3_MIN_VIX_CLOSES:
        major_5td, minor_5td = catalysts_within_trading_days(catalysts, today, V3_CATALYST_WINDOW_TRADING_DAYS)
        regime, inputs = classify_v3(
            vix=vix,
            vix3m=vix3m,
            spy_close=spy_close,
            spy_sma200=sma200,
            vix_percentile=percentile_rank(vix_closes[-252:]),
            major_catalyst_soon=major_5td,
            minor_catalyst_soon=minor_5td,
        )
        await _upsert_reading(session, date_str, V3, regime, inputs, inputs.get("scores", {}))
    else:
        regime = INSUFFICIENT_DATA
        await _upsert_reading(
            session,
            date_str,
            V3,
            regime,
            {
                "have_vix": bool(vix),
                "have_vix3m": bool(vix3m),
                "have_sma200": sma200 is not None,
                "vix_closes": len(vix_closes),
            },
            {},
        )
    results[V3] = regime

    await session.commit()
    return results

"""console.py — read-model aggregation for the supervision console (#73).

Backs the Books tab and status strip (design §6.5): per-book summaries with
the ADR-0006 Live Gate checklist, and the executor status (heartbeat +
last reconciliation). Pure read paths — nothing here mutates ledgers.

Metric definitions:
- Realized P&L per closed trade uses the same formula as the manual close
  flow (credit: entry − exit; debit: exit − entry; ×100×contracts).
- Expectancy is the mean realized P&L per closed trade minus the slippage
  haircut — IBKR paper combo fills are optimistic (ADR-0007), so raw paper
  expectancy is never trusted.
- Expectancy SE (#656) is the sample standard error of that same per-trade
  haircut P&L: stdev(haircut P&Ls, n-1 denominator) / sqrt(n). The gate bar
  is expectancy − 1·SE ≥ 0, not a bare point estimate against 0.0 — see the
  ADR-0010 amendment (spec/decisions.md) for why a point estimate at n≈30
  is a coin flip for a true-zero-edge book, and why this is an interim
  floor rather than the final threshold.
- Max drawdown is peak-to-trough on the CUMULATIVE realized P&L of closed
  trades in entry-date order. There is no per-book equity history table yet
  (pre-launch schema policy, #94), so open-position marks are not included.
- "Zero breaches" counts ENVELOPE_BREACH_POSTHOC audit events for the book —
  a post-hoc envelope violation is the breach the Live Gate cares about.
- Stress episode (#215, ADR-0010 condition 1 as ratified in #738): a VIX
  close ≥ 25 or a ≥ 5% SPY drawdown from the gate window's running peak,
  from index_history, on a date the book's dollars at risk DURING THAT
  SESSION were ≥ half its normal deployment (the mean over the window's
  deployed days). A position counts for a session only from the market
  date AFTER its entry_date — entries are stamped by the 18:45 ET run,
  after the close that defines the episode. Bare "a position was open"
  overlap is reported but is not the bar. Windowed to the book's evidence
  era.
- Benchmark (#215, ADR-0010 condition 2): haircut-and-commission-net
  realized P&L of closed era trades (the same per-trade figure expectancy
  uses — ADR-0007's haircut applies to every P&L the gate judges) as a
  return on basis vs the SPY price return over the same window
  (backend/benchmark.py's shared definition, dividends excluded).
- ONE era clock (#984): the breach count, the months row and the stress and
  benchmark windows all measure from the book's evidence-era start — the
  last BOOK_CONFIG_SYNCED, else created_at — surfaced as era_start.
"""

import json
import logging
import math
import os
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.benchmark import spy_window_return
from backend.book_gates import LIVE_GATE_TRADES, resolve_book_config
from backend.calendars import snap_to_trading_day
from backend.dates import MARKET_TZ, market_date_of
from backend.models import (
    AuditEventModel,
    BenchmarkCheckSchema,
    BookModel,
    BookMtmHistoryModel,
    BookSummarySchema,
    ClosurePostMortemModel,
    ExecutorStatusSchema,
    FillModel,
    IndexHistoryModel,
    LiveGateChecklistSchema,
    LiveGateConditionSchema,
    OrderModel,
    PositionModel,
    StressEpisodeCheckSchema,
    TailHedgeMetricsSchema,
    TailMagnitudeCheckSchema,
    TradingControlModel,
)
from backend.pricing import capital_at_risk
from backend.states import POSITION_CLOSED_STATUSES, POSITION_OPEN_STATUS

logger = logging.getLogger(__name__)

# $0.05/share per combo round trip = $5 per contract per closed trade.
# Applied to paper expectancy before it can satisfy the Live Gate (ADR-0007).
SLIPPAGE_HAIRCUT_PER_CONTRACT = 5.0

LIVE_GATE_MONTHS = 3.0  # ADR-0006: ≥3 months of paper history per book

# #717: the tail-magnitude row's multiplier on the gate window's own worst
# observed trade — informational only, not a threshold (see
# TailMagnitudeCheckSchema).
TAIL_MAGNITUDE_MULTIPLIER = 3.0
_DAYS_PER_MONTH = 30.44

# ADR-0012 / #772: books judged on convexity, never expectancy — currently
# just the tail-hedge sleeve (B32). A frozenset, not a per-book config flag,
# because the ADR itself names the book explicitly and no second sleeve
# exists yet; a config-driven flag can follow if/when one does.
_TAIL_HEDGE_BOOK_IDS = frozenset({"B32"})

# ADR-0009 (#993): B35 is a single-arm hypothesis test, not a Live Gate
# candidate — judged on whether it produces catalyst-night closes and
# whether haircut expectancy justifies the arm, never against the Live
# Gate. A different reason than the tail-hedge sleeve above (B32 is judged
# on convexity; B35 is judged on its own expectancy, just not through this
# gate), so it gets its own named set rather than being folded into
# _TAIL_HEDGE_BOOK_IDS, whose meaning is specifically "tail-hedge sleeve."
_SINGLE_ARM_HYPOTHESIS_BOOK_IDS = frozenset({"B35"})

# ADR-0010's stress-episode trigger — ONE definition of "stress", shared by
# the Live Gate's stress-episode row (#215) and ADR-0012 metric (2), the
# tail-hedge sleeve's payoff during episodes (#772): the sleeve's payoff is
# only meaningful measured against the same episodes the gate counts.
STRESS_VIX_THRESHOLD = 25.0
STRESS_SPY_DRAWDOWN_PCT = 0.05
# #215 / #738 (operator-ratified, pre-registered in the ADR-0010 amendment):
# an episode counts for a book only if the book's dollars at risk on the
# episode date were at least this fraction of its NORMAL gate-window
# deployment — the mean daily dollars at risk over the window's DEPLOYED
# trading dates (days the book carried any position), not over flat days
# too: averaging over flat days would let a book that trades one day in
# four pass with a fraction of its usual size (the operator's "meaningful
# deployment" ruling is against the book's usual size when it IS in the
# market). Held ≠ exposed — a position two days from its mandatory exit
# satisfies "held" without the book's risk being live. One half: a book
# running at or above half its usual size through the episode has taken
# the test; a book that happened to be winding down has not. Chosen and
# registered BEFORE any book's window contains an episode, so it cannot be
# tuned around a leaderboard.
STRESS_DEPLOYMENT_FRACTION = 0.5

# ADR-0010 promotion conditions beyond the original ADR-0006 four (#655)
# that STILL have no detection machinery: every book's checklist carries
# these as 'not_yet_evaluated' — eligible stays un-claimable until each is
# either implemented or explicitly evaluated. The stress-episode and
# SPY-benchmark rows (#215) are computed per book in book_summaries and
# prepended to these; their keys are unchanged from the pending era.
ADR_0010_PENDING_CONDITIONS: tuple[LiveGateConditionSchema, ...] = (
    LiveGateConditionSchema(
        key="beats_same_engine_baseline",
        label="beats baseline",
        status="not_yet_evaluated",
        detail="ADR-0009 same-engine-baseline comparison — not yet implemented",
    ),
    LiveGateConditionSchema(
        key="composition_limit_respected",
        label="composition limit",
        status="not_yet_evaluated",
        detail="ADR-0010 at-most-one-single-knob-amendment rule — not yet implemented",
    ),
)

# The console paints the last-run timestamp red beyond this (design §6.5).
# Retained for the displayed age, but no longer the staleness VERDICT (#545
# L3): a flat 24h against a Mon-Fri task painted the console red every
# Saturday evening through Monday ~18:45, making a genuinely dead Friday
# run indistinguishable from ordinary weekend staleness. See _is_stale().
STALE_AFTER_HOURS = 24.0

# #674: re-exported alias — the vocabulary lives in backend/states.py now.
_CLOSED_STATUSES = POSITION_CLOSED_STATUSES

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def heartbeat_path() -> Path:
    """Where the executor's dead-man heartbeat lives (#72). The executor writes
    it; this module and the watchdog read it. Lives here so the API process
    never has to import the broker stack just to show a timestamp."""
    return Path(os.getenv("EXECUTOR_HEARTBEAT_FILE", str(_PROJECT_ROOT / "executor_heartbeat.json")))


def realized_pnl(position: PositionModel) -> float:
    """Same math as the manual close flow in main.py — one definition of P&L."""
    if position.premium_direction == "DEBIT":
        pnl = (position.current_value_per_share - position.entry_premium) * 100 * position.contracts
    else:
        pnl = (position.entry_premium - position.current_value_per_share) * 100 * position.contracts
    return round(pnl, 2)


def _months_since(iso_timestamp: str, now: datetime) -> float:
    try:
        started = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return max(0.0, (now - started).total_seconds() / 86400.0 / _DAYS_PER_MONTH)


def _expectancy_se(haircut_pnls: list[float]) -> float | None:
    """Sample standard error of the per-trade haircut P&L (#656): sample
    stdev (n-1 denominator) / sqrt(n). None below n=2 — a standard error
    from one trade, or zero, is not a number, and the gate condition below
    treats an undefined SE as not-passable rather than silently ok."""
    n = len(haircut_pnls)
    if n < 2:
        return None
    mean = sum(haircut_pnls) / n
    variance = sum((x - mean) ** 2 for x in haircut_pnls) / (n - 1)
    return math.sqrt(variance) / math.sqrt(n)


def _tail_magnitude_check(haircut_pnls: list[float], open_positions: list[PositionModel]) -> TailMagnitudeCheckSchema:
    """#717: informational-only hypothetical tail loss. largest_adverse_move
    is the single worst (most negative) gate-window closed trade's haircut
    P&L, 0.0 if there is none or every trade won — the literature review's
    point (Bailey/López de Prado line) is to name the number, not to invent
    one when there's nothing to measure yet. Per open position, the
    hypothetical loss under a move 3× that magnitude is capped at the
    position's own max_loss dollars: a defined-risk structure's real worst
    case never exceeds max_loss no matter how large the move gets, so the
    cap binding (not the raw 3× figure) is what the row is meant to show."""
    worst = min(haircut_pnls) if haircut_pnls else 0.0
    largest_adverse_move = abs(worst) if worst < 0 else 0.0
    hypothetical_tail_loss = sum(
        min(capital_at_risk(p.max_loss, p.contracts), TAIL_MAGNITUDE_MULTIPLIER * largest_adverse_move)
        for p in open_positions
    )
    return TailMagnitudeCheckSchema(
        largest_adverse_move=round(largest_adverse_move, 2),
        multiplier=TAIL_MAGNITUDE_MULTIPLIER,
        hypothetical_tail_loss=round(hypothetical_tail_loss, 2),
    )


def _max_drawdown(pnls_in_order: list[float]) -> float:
    peak = 0.0
    equity = 0.0
    drawdown = 0.0
    for pnl in pnls_in_order:
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return round(drawdown, 2)


def _bleed_rate_pct_per_month(mtm_rows: list[BookMtmHistoryModel], basis: float, now: datetime) -> float | None:
    """ADR-0012 metric (1): average monthly cost as a % of the sleeve basis,
    from book_mtm_history — the whole-history mark delta divided by elapsed
    months, expressed against basis. None below two dated marks or a
    zero-length window: a rate needs an elapsed span to divide by."""
    if len(mtm_rows) < 2 or basis <= 0:
        return None
    rows = sorted(mtm_rows, key=lambda r: r.date)
    first, last = rows[0], rows[-1]
    last_dt = datetime.fromisoformat(last.date).replace(tzinfo=UTC)
    months = _months_since(first.date, last_dt)
    if months <= 0:
        return None
    return round((last.mtm - first.mtm) / months / basis * 100.0, 4)


def _through_date(position: PositionModel, exit_dates: dict[str, str], today: str) -> str:
    """The last date a position counts as held: the post-mortem exit_date
    when one exists; otherwise the expiration date for a CLOSED/EXPIRED
    position with no post-mortem (it cannot have been held past expiry), or
    `today` for one still OPEN, so an open position always counts as held
    through the most recent mark. The no-post-mortem fallback is unreachable
    for any position closed through the app's own flows (main.py,
    executor.py and resolution.py all write a post-mortem atomically with
    the status flip) -- it guards legacy pre-#672 data only."""
    exit_date = exit_dates.get(position.id)
    if exit_date is not None:
        return exit_date
    if position.status in POSITION_CLOSED_STATUSES:
        return position.expiration_date
    return today


def _position_intervals(
    positions: list[PositionModel], exit_dates: dict[str, str], today: str
) -> list[tuple[str, str]]:
    """(entry_date, through-date) per position — see _through_date."""
    return [(p.entry_date, _through_date(p, exit_dates, today)) for p in positions]


def _had_open_position(intervals: list[tuple[str, str]], date: str) -> bool:
    """ADR-0012's reading of "held": entry_date through the through-date,
    inclusive at both ends — the sleeve's mark on its entry date already
    carries the new position, so its payoff INTO an episode entry date is
    real. The Live Gate's stress row uses the stricter _held_during_session
    instead; see there."""
    return any(start <= date <= end for start, end in intervals)


def _held_during_session(position: PositionModel, exit_dates: dict[str, str], today: str, date: str) -> bool:
    """Was the book exposed to `date`'s SESSION through this position? Only
    from the market date strictly AFTER entry_date: entry_date is stamped
    market_today() by the 18:45 ET run (executor.py) or a manual create
    (main.py), i.e. after the 16:15 ET close whose VIX/SPY print defines
    an episode — a position opened on the evening of the episode date did
    not exist during the stressed session, so it is not deployment against
    it. The through-date stays inclusive: a position closed by that
    evening's run, or expiring at that close, was live through the session."""
    return position.entry_date < date <= _through_date(position, exit_dates, today)


def _deployment_on(positions: list[PositionModel], exit_dates: dict[str, str], today: str, date: str) -> float:
    """Dollars at risk the book carried through `date`'s session: the sum of
    max_loss × contracts × 100 over every position held during that session
    (_held_during_session; same capital_at_risk figure the deployed% cell and
    the envelope gates use — one definition of deployment, so the stress row
    and the gates cannot disagree about it)."""
    return sum(
        capital_at_risk(p.max_loss, p.contracts) for p in positions if _held_during_session(p, exit_dates, today, date)
    )


def _window_start_date(iso_timestamp: str) -> str:
    """The market date a gate window opened — the era's BOOK_CONFIG_SYNCED
    run_at or the book's created_at, both UTC ISO timestamps; a date-only
    string is taken as-is (market_date_of's naive-input rule)."""
    try:
        return market_date_of(iso_timestamp).isoformat()
    except ValueError:
        return iso_timestamp[:10]


def _spy_drawdowns(spy_by_date: dict[str, float]) -> dict[str, float]:
    """Per date, the close-to-close drawdown from the running peak of the
    closes given (as a fraction of that peak; the peak starts at the first
    close given, so callers window the input to restart it). ONE walk,
    shared by the stress trigger (_stress_episode_dates) and the displayed
    deepest drawdown (_max_spy_drawdown) so the verdict and the number it is
    explained with cannot diverge."""
    drawdowns: dict[str, float] = {}
    peak: float | None = None
    for date in sorted(spy_by_date):
        close = spy_by_date[date]
        peak = close if peak is None else max(peak, close)
        drawdowns[date] = (peak - close) / peak if peak else 0.0
    return drawdowns


def _max_spy_drawdown(spy_by_date: dict[str, float]) -> float | None:
    """Deepest close-to-close drawdown from the running peak, as a fraction
    of that peak — the SPY arm of ADR-0010's trigger, reported whether or
    not it reached the 5% bar. None with no closes."""
    drawdowns = _spy_drawdowns(spy_by_date)
    return max(drawdowns.values()) if drawdowns else None


def _floor_pct(fraction: float) -> float:
    """A fraction as a percentage FLOORED (not rounded) to 2 dp, for display
    beside a threshold verdict: a 4.996% drawdown must never render as
    "5.00%" next to "no episode (≥5%)" — the shown figure may understate the
    real one by under a hundredth, never overstate it past the bar. The
    epsilon absorbs float noise so an exact 5% does not floor to 4.99."""
    return math.floor(fraction * 10_000.0 + 1e-6) / 100.0


def _max_adverse_excursion(
    mtm_rows: list[BookMtmHistoryModel], stress_dates: set[str], window_start: str
) -> float | None:
    """Informational (#738, composes with #717): how far the book's marks fell
    during the episode — the last mark BEFORE the first episode date but
    inside the gate window (or the first episode mark, if none precedes it)
    minus the lowest mark on any episode date, floored at 0. Era-scoped like
    every other number in the check: a mark from a retired era is not this
    era's reference. None without a mark on an episode date."""
    if not stress_dates:
        return None
    rows = sorted((r for r in mtm_rows if r.date >= window_start), key=lambda r: r.date)
    first_stress = min(stress_dates)
    episode_marks = [r.mtm for r in rows if r.date in stress_dates]
    if not episode_marks:
        return None
    before = [r.mtm for r in rows if r.date < first_stress]
    reference = before[-1] if before else episode_marks[0]
    return round(max(0.0, reference - min(episode_marks)), 2)


def _stress_episode_check(
    positions: list[PositionModel],
    exit_dates: dict[str, str],
    mtm_rows: list[BookMtmHistoryModel],
    vix_by_date: dict[str, float],
    spy_by_date: dict[str, float],
    window_start: str,
    window_end: str,
) -> StressEpisodeCheckSchema:
    """ADR-0010 condition 1 as ratified in #738: EPISODE × MEANINGFUL
    DEPLOYMENT, everything windowed to the book's evidence era. The SPY
    running peak restarts at window_start (the ADR says "the window's
    running peak", not all history); the trading calendar is the set of
    index_history dates inside the window. "Normal deployment" is the mean
    daily dollars at risk over the window's DEPLOYED dates — days the book
    carried anything through the session — so a book that is in the market
    one day in four is judged against its size on the days it is in, not
    against a flat-day-diluted average (see STRESS_DEPLOYMENT_FRACTION).
    Zero deployment on the episode date never passes (an episode date with
    exposure is itself a deployed date, so normal > 0 whenever
    episode_deployment > 0). Deployment on a date counts only positions
    entered on a PRIOR market date (_held_during_session): the executor
    stamps entries after the close that defines the episode."""
    vix = {d: c for d, c in vix_by_date.items() if window_start <= d <= window_end}
    spy = {d: c for d, c in spy_by_date.items() if window_start <= d <= window_end}
    stress_dates = _stress_episode_dates(vix, spy)
    calendar = sorted(set(vix) | set(spy))
    deployment_by_date = {d: _deployment_on(positions, exit_dates, window_end, d) for d in calendar}
    deployed_days = [v for v in deployment_by_date.values() if v > 0.0]
    normal = sum(deployed_days) / len(deployed_days) if deployed_days else 0.0
    required = STRESS_DEPLOYMENT_FRACTION * normal
    episode_deployment = max((deployment_by_date[d] for d in stress_dates), default=None)
    deployed = episode_deployment is not None and episode_deployment > 0.0 and episode_deployment >= required
    drawdown = _max_spy_drawdown(spy)
    return StressEpisodeCheckSchema(
        window_start=window_start,
        window_end=window_end,
        peak_vix_close=max(vix.values()) if vix else None,
        max_spy_drawdown_pct=_floor_pct(drawdown) if drawdown is not None else None,
        episode_dates=len(stress_dates),
        episode_while_position_open=any(
            _held_during_session(p, exit_dates, window_end, d) for d in stress_dates for p in positions
        ),
        episode_while_deployed=deployed,
        deployment_fraction_required=STRESS_DEPLOYMENT_FRACTION,
        normal_deployment=round(normal, 2),
        required_deployment=round(required, 2),
        episode_deployment=round(episode_deployment, 2) if episode_deployment is not None else None,
        max_adverse_excursion=_max_adverse_excursion(mtm_rows, stress_dates, window_start),
        ok=deployed,
    )


def _stress_episode_detail(check: StressEpisodeCheckSchema) -> str:
    # Supporting numbers render at 2 dp — the same precision the verdict
    # was taken at (VIX closes are quoted to 2 dp; the drawdown is floored,
    # see _floor_pct) — so a figure can never round INTO the bar it failed.
    vix = f"peak VIX {check.peak_vix_close:.2f}" if check.peak_vix_close is not None else "no VIX closes"
    spy = (
        f"max SPY drawdown {check.max_spy_drawdown_pct:.2f}%"
        if check.max_spy_drawdown_pct is not None
        else "no SPY closes"
    )
    head = f"{vix}, {spy} in {check.window_start}…{check.window_end}"
    if check.peak_vix_close is None and check.max_spy_drawdown_pct is None:
        return f"{head} — no index_history in the window: the row cannot be evaluated yet (fail-closed, not a verdict)"
    if check.episode_dates == 0:
        return f"{head} — no episode yet (VIX≥25 or ≥5% SPY drawdown): a calm window is an unfinished sample"
    deployment = (
        f"${check.episode_deployment:,.2f} at risk through the episode session vs "
        f"${check.normal_deployment:,.2f} normal on deployed days "
        f"(needs ≥{check.deployment_fraction_required:.0%} = ${check.required_deployment:,.2f})"
    )
    excursion = (
        f"; book max adverse excursion ${check.max_adverse_excursion:,.2f} (informational)"
        if check.max_adverse_excursion is not None
        else ""
    )
    if check.ok:
        return f"{head} — {check.episode_dates} episode date(s), {deployment}{excursion}"
    if not check.episode_while_position_open:
        return (
            f"{head} — {check.episode_dates} episode date(s) but no position was held through any session "
            "(a position entered on the episode evening does not count): a calm sample for this book"
        )
    return f"{head} — {check.episode_dates} episode date(s) held but not meaningfully deployed: {deployment}{excursion}"


def _benchmark_check(
    haircut_pnls: list[float], basis: float, spy_by_date: dict[str, float], window_start: str, window_end: str
) -> BenchmarkCheckSchema:
    """ADR-0010 condition 2: realized closed-trade P&L — net of the
    ADR-0007 slippage haircut and ledgered commissions, the identical
    per-trade figure expectancy, its SE, the tail row and the empirical-null
    drill all judge (raw paper P&L is never trusted, so it cannot be the one
    quantity in this checklist that escapes the haircut) — as a return on
    the book's basis vs the SPY price return over the same window (shared
    definition: backend/benchmark.py). Realized only: open-position marks
    are on SPY's side of the comparison but not the book's (see
    BenchmarkCheckSchema). Fail-closed on missing inputs."""
    book_return = sum(haircut_pnls) / basis * 100.0 if haircut_pnls and basis > 0 else None
    spy = spy_window_return(spy_by_date, window_start, window_end)
    spy_return = spy[2] * 100.0 if spy is not None else None
    return BenchmarkCheckSchema(
        window_start=window_start,
        window_end=window_end,
        book_return_pct=round(book_return, 4) if book_return is not None else None,
        spy_return_pct=round(spy_return, 4) if spy_return is not None else None,
        spy_start_date=spy[0] if spy is not None else None,
        spy_end_date=spy[1] if spy is not None else None,
        ok=book_return is not None and spy_return is not None and book_return > spy_return,
    )


def _benchmark_detail(check: BenchmarkCheckSchema) -> str:
    if check.book_return_pct is None:
        return f"no closed trades in {check.window_start}…{check.window_end} — nothing to compare against SPY yet"
    if check.spy_return_pct is None:
        return f"fewer than two SPY closes in {check.window_start}…{check.window_end} — SPY return unavailable"
    return (
        f"book realized {check.book_return_pct:+.2f}% on basis after haircut and commissions "
        f"vs SPY {check.spy_return_pct:+.2f}% price return "
        f"({check.spy_start_date}…{check.spy_end_date}, excl. dividends; open marks not on the book's side)"
    )


def _stress_episode_dates(vix_by_date: dict[str, float], spy_by_date: dict[str, float]) -> set[str]:
    """ADR-0010's stress-episode condition (reused verbatim, #772): a VIX
    close ≥25, OR a SPY close-to-close drawdown ≥5% from the running peak."""
    stress: set[str] = {date for date, close in vix_by_date.items() if close >= STRESS_VIX_THRESHOLD}
    stress.update(date for date, dd in _spy_drawdowns(spy_by_date).items() if dd >= STRESS_SPY_DRAWDOWN_PCT)
    return stress


def _stress_episode_payoff(
    mtm_rows: list[BookMtmHistoryModel], intervals: list[tuple[str, str]], stress_dates: set[str]
) -> tuple[str, float | None]:
    """ADR-0012 metric (2): the sleeve's P&L during ADR-0010 stress episodes
    — the only periods it exists for. 'no_episode_yet' (never 0.0 — zero
    would misread as "a stress episode happened and it broke even") until
    index_history actually shows one while the book held a position."""
    if not stress_dates:
        return "no_episode_yet", None
    rows = sorted(mtm_rows, key=lambda r: r.date)
    payoff = 0.0
    measured = False
    prev: BookMtmHistoryModel | None = None
    for row in rows:
        if prev is not None and row.date in stress_dates and _had_open_position(intervals, row.date):
            payoff += row.mtm - prev.mtm
            measured = True
        prev = row
    if not measured:
        return "no_episode_yet", None
    return "measured", round(payoff, 2)


def _portfolio_contribution(all_mtm_rows: list[BookMtmHistoryModel], excluded_book_id: str) -> float | None:
    """ADR-0012 metric (3): does lab-total max drawdown improve with the
    sleeve included? (without-sleeve drawdown) − (with-sleeve drawdown) —
    positive means the sleeve REDUCED lab-wide drawdown. None below two
    dated marks across the lab (nothing to walk a curve over)."""
    by_date_with: dict[str, float] = {}
    by_date_without: dict[str, float] = {}
    for row in all_mtm_rows:
        by_date_with[row.date] = by_date_with.get(row.date, 0.0) + row.mtm
        if row.book_id != excluded_book_id:
            by_date_without[row.date] = by_date_without.get(row.date, 0.0) + row.mtm
    if len(by_date_with) < 2:
        return None
    with_curve = [by_date_with[d] for d in sorted(by_date_with)]
    without_curve = [by_date_without[d] for d in sorted(by_date_without)]
    dd_with = _max_drawdown([with_curve[i] - with_curve[i - 1] for i in range(1, len(with_curve))])
    dd_without = _max_drawdown([without_curve[i] - without_curve[i - 1] for i in range(1, len(without_curve))])
    return round(dd_without - dd_with, 2)


async def book_summaries(session: AsyncSession, now: datetime | None = None) -> list[BookSummarySchema]:
    """One row per lab book for the Books tab (B00 legacy excluded)."""
    now = now or datetime.now(UTC)
    books = (await session.execute(select(BookModel).filter(BookModel.id != "B00"))).scalars().all()
    controls = {row.scope: row.state for row in (await session.execute(select(TradingControlModel))).scalars().all()}
    # Shared inputs for the ADR-0010 stress-episode and benchmark rows (#215)
    # and the tail-hedge sleeve's metrics (ADR-0012 / #772) — fetched once,
    # not per book.
    all_mtm_rows = list((await session.execute(select(BookMtmHistoryModel))).scalars().all())
    mtm_rows_by_book: dict[str, list[BookMtmHistoryModel]] = {}
    for row in all_mtm_rows:
        mtm_rows_by_book.setdefault(row.book_id, []).append(row)
    pm_rows = (
        await session.execute(select(ClosurePostMortemModel.position_id, ClosurePostMortemModel.exit_date))
    ).all()
    exit_date_by_position: dict[str, str] = dict(pm_rows)
    index_rows = (
        (await session.execute(select(IndexHistoryModel).filter(IndexHistoryModel.symbol.in_(("VIX", "SPY")))))
        .scalars()
        .all()
    )
    vix_by_date = {r.date: r.close for r in index_rows if r.symbol == "VIX"}
    spy_by_date = {r.date: r.close for r in index_rows if r.symbol == "SPY"}
    # ADR-0012 metric (2) reads the sleeve's payoff over ALL-history episodes
    # (the sleeve exists for every episode, not a gate window's worth).
    stress_dates = _stress_episode_dates(vix_by_date, spy_by_date)
    # The MARKET date (dates.py's rule), not the UTC date: every interval
    # below is compared against index_history and position dates, which are
    # market dates. Before #215 the ADR-0012 intervals used the UTC date,
    # which ran an OPEN position's upper bound one day ahead for any `now`
    # between 00:00 and ~04:00 UTC; the market date is the correction.
    today = now.astimezone(MARKET_TZ).date().isoformat()
    # Config-era boundaries (#534): the Live Gate attaches to
    # (book, config_hash) — a seed-sync starts a NEW evidence era, and
    # pooling eras would let eligibility trip on trades from a config that
    # no longer exists. The last BOOK_CONFIG_SYNCED per book marks the
    # current era's start; never-synced books run from created_at.
    sync_rows = (
        (await session.execute(select(AuditEventModel).filter_by(event_type="BOOK_CONFIG_SYNCED"))).scalars().all()
    )
    era_start_by_book: dict[str, str] = {}
    for row in sync_rows:
        if row.book_id and row.run_at > era_start_by_book.get(row.book_id, ""):
            era_start_by_book[row.book_id] = row.run_at
    breach_rows = (
        (await session.execute(select(AuditEventModel).filter_by(event_type="ENVELOPE_BREACH_POSTHOC"))).scalars().all()
    )
    breaches_by_book: dict[str, int] = {}
    for row in breach_rows:
        # Only the current era's breaches count (#533): a breach row written
        # before the last config sync belongs to a retired era — including
        # any FALSE rows a pre-#533 sweep wrote by judging old-era positions
        # against a reduced envelope. Era-scoping the count un-poisons them
        # without touching the append-only table. (Both timestamps come from
        # datetime.now(UTC).isoformat() — same format family, so the string
        # compare is sound.)
        if row.book_id and row.run_at >= era_start_by_book.get(row.book_id, ""):
            breaches_by_book[row.book_id] = breaches_by_book.get(row.book_id, 0) + 1

    summaries: list[BookSummarySchema] = []
    for book in sorted(books, key=lambda b: b.id):
        config = resolve_book_config(book.config)
        positions = (await session.execute(select(PositionModel).filter_by(book_id=book.id))).scalars().all()
        open_positions = [p for p in positions if p.status == POSITION_OPEN_STATUS]
        # Current-era evidence only (#534): positions stamped with the
        # book's CURRENT config_hash. NULL-hash rows (pre-#284 legacy) count
        # only while the book has never been synced — after a sync their era
        # is unknowable and they must not top up the new era's counts.
        never_synced = book.id not in era_start_by_book
        era_positions = [
            p for p in positions if p.config_hash == book.config_hash or (p.config_hash is None and never_synced)
        ]
        closed = sorted((p for p in era_positions if p.status in _CLOSED_STATUSES), key=lambda p: p.entry_date)

        closed_pnls = [realized_pnl(p) for p in closed]
        wins = sum(1 for pnl in closed_pnls if pnl > 0.01)
        win_rate = wins / len(closed) if closed else None
        # Commissions are real (#276, audit H1): the gate expectancy nets out
        # each trade's ledgered commissions ON TOP of the slippage haircut —
        # the haircut proxies fill quality (ADR-0007), never broker fees.
        commission_rows = (
            await session.execute(
                select(OrderModel.position_id, FillModel.commission)
                .join(FillModel, FillModel.order_id == OrderModel.id)
                .filter(OrderModel.book_id == book.id, OrderModel.position_id.is_not(None))
            )
        ).all()
        commissions_by_pos: dict[str, float] = {}
        for pos_id, commission in commission_rows:
            commissions_by_pos[pos_id] = commissions_by_pos.get(pos_id, 0.0) + (commission or 0.0)
        haircut_pnls = [
            pnl - SLIPPAGE_HAIRCUT_PER_CONTRACT * p.contracts - commissions_by_pos.get(p.id, 0.0)
            for pnl, p in zip(closed_pnls, closed, strict=True)
        ]
        expectancy = sum(haircut_pnls) / len(haircut_pnls) if haircut_pnls else None
        expectancy_se = _expectancy_se(haircut_pnls)
        # #656: the bar is expectancy − 1·SE ≥ 0, not a point estimate
        # against 0.0 — see the ADR-0010 amendment for why. n<2 (SE
        # undefined) is not passable, not silently ok.
        expectancy_ok = expectancy is not None and expectancy_se is not None and (expectancy - expectancy_se) >= 0.0

        deployed = sum(capital_at_risk(p.max_loss, p.contracts) for p in open_positions)
        deployed_pct = deployed / config.envelope.basis * 100.0
        pnl = (book.last_mtm - book.starting_capital) if book.last_mtm is not None else 0.0

        breaches = breaches_by_book.get(book.id, 0)
        # The months clock restarts with the era (#534): three months of
        # evidence under a RETIRED config is not three months under this one.
        months = _months_since(era_start_by_book.get(book.id, book.created_at), now)
        # #215 / #984: ONE era clock. The gate WINDOW is the evidence era —
        # the same instant the months row and the breach count above measure
        # from (era start when the book has been synced, else created_at) —
        # and the positions it reads are era_positions, so the stress and
        # benchmark rows judge exactly the evidence the as_raced_config_hash
        # below vouches for. era_start surfaces that instant's market date.
        window_start = _window_start_date(era_start_by_book.get(book.id, book.created_at))
        stress_check = _stress_episode_check(
            era_positions,
            exit_date_by_position,
            mtm_rows_by_book.get(book.id, []),
            vix_by_date,
            spy_by_date,
            window_start,
            today,
        )
        benchmark_check = _benchmark_check(haircut_pnls, config.envelope.basis, spy_by_date, window_start, today)
        additional_conditions = [
            LiveGateConditionSchema(
                key="stress_episode_observed",
                label="stress episode",
                status="ok" if stress_check.ok else "fail",
                detail=_stress_episode_detail(stress_check),
            ),
            LiveGateConditionSchema(
                key="beats_spy_benchmark",
                label="beats SPY",
                status="ok" if benchmark_check.ok else "fail",
                detail=_benchmark_detail(benchmark_check),
            ),
            *ADR_0010_PENDING_CONDITIONS,
        ]
        gate = LiveGateChecklistSchema(
            closed_trades=len(closed),
            closed_trades_required=LIVE_GATE_TRADES,
            trades_ok=len(closed) >= LIVE_GATE_TRADES,
            months_elapsed=round(months, 2),
            months_required=LIVE_GATE_MONTHS,
            months_ok=months >= LIVE_GATE_MONTHS,
            breaches=breaches,
            breaches_ok=breaches == 0,
            era_start=window_start,
            expectancy_after_haircut=round(expectancy, 2) if expectancy is not None else None,
            expectancy_se=round(expectancy_se, 2) if expectancy_se is not None else None,
            expectancy_ok=expectancy_ok,
            stress_episode_ok=stress_check.ok,
            stress_episode_check=stress_check,
            benchmark_ok=benchmark_check.ok,
            benchmark_check=benchmark_check,
            additional_conditions=additional_conditions,
            tail_magnitude_check=_tail_magnitude_check(haircut_pnls, open_positions),
            # #658: era_positions above is already filtered to book.config_hash
            # (or NULL-legacy for a never-synced book) — that IS the era this
            # evidence belongs to, so the as-raced hash is that same value,
            # read from the stored column rather than recomputed from
            # book.config, so a future divergence between the two is
            # provenance, never silently papered over.
            as_raced_config_hash=book.config_hash,
            eligible=(
                len(closed) >= LIVE_GATE_TRADES
                and months >= LIVE_GATE_MONTHS
                and breaches == 0
                and expectancy_ok
                # #655: a materially weaker standard than ADR-0010 grants must
                # never render green — every additional condition must be
                # explicitly evaluated 'ok', not merely absent from the AND.
                # #215 computes two of the four; the still-pending baseline
                # and composition rows keep eligible un-claimable.
                and all(c.status == "ok" for c in additional_conditions)
                # ADR-0012: the tail-hedge sleeve is excluded from promotion
                # PERMANENTLY, not merely until the mechanical checks above
                # happen to clear — that must hold even once #215 finishes
                # ADR_0010_PENDING_CONDITIONS and other books start passing.
                and book.id not in _TAIL_HEDGE_BOOK_IDS
                # ADR-0009 (#993): B35 is a single-arm hypothesis test, same
                # permanent exclusion as the tail-hedge sleeve above but for
                # a different reason — see _SINGLE_ARM_HYPOTHESIS_BOOK_IDS.
                and book.id not in _SINGLE_ARM_HYPOTHESIS_BOOK_IDS
            ),
        )

        tail_hedge_metrics = None
        if book.id in _TAIL_HEDGE_BOOK_IDS:
            book_mtm_rows = mtm_rows_by_book.get(book.id, [])
            intervals = _position_intervals(positions, exit_date_by_position, today)
            stress_status, stress_payoff = _stress_episode_payoff(book_mtm_rows, intervals, stress_dates)
            tail_hedge_metrics = TailHedgeMetricsSchema(
                bleed_rate_pct_per_month=_bleed_rate_pct_per_month(book_mtm_rows, config.envelope.basis, now),
                stress_episode_payoff=stress_payoff,
                stress_episode_status=stress_status,
                portfolio_contribution=_portfolio_contribution(all_mtm_rows, book.id),
            )

        summaries.append(
            BookSummarySchema(
                id=book.id,
                name=book.name,
                status=book.status,
                engine_variant=config.variant or "?",
                underlying=config.underlying or "?",
                config_hash=book.config_hash,
                config_version=book.config_version,
                starting_capital=book.starting_capital,
                cash_balance=book.cash_balance,
                last_mtm=book.last_mtm,
                pnl=round(pnl, 2),
                closed_trades=len(closed),
                win_rate=round(win_rate, 4) if win_rate is not None else None,
                expectancy_after_haircut=gate.expectancy_after_haircut,
                expectancy_se=gate.expectancy_se,
                max_drawdown=_max_drawdown(closed_pnls),
                deployed_pct=round(deployed_pct, 2),
                open_positions=len(open_positions),
                max_positions=config.envelope.max_positions,
                # Fail-closed mirror of trading_control: a book without a row is halted
                control_state=controls.get(book.id, "HALT_ENTRIES"),  # type: ignore[arg-type]
                live_gate=gate,
                tail_hedge_metrics=tail_hedge_metrics,
            )
        )
    return summaries


def _is_stale(heartbeat_at: str | None, now: datetime) -> bool:
    """Fresh iff the heartbeat's MARKET date is on or after the last trading
    day as of *now* (#545 L3) — a task that only runs Mon-Fri, measured in
    trading days rather than a flat hour count, so a Friday-evening
    heartbeat stays fresh all weekend and only goes stale once Monday
    itself becomes the last trading day (roughly the run's usual gap plus
    the weekend it silently skips)."""
    if heartbeat_at is None:
        return True
    last_trading_day = snap_to_trading_day(now.astimezone(MARKET_TZ).date())
    try:
        return market_date_of(heartbeat_at) < last_trading_day
    except ValueError:
        return True


async def executor_status(session: AsyncSession, now: datetime | None = None) -> ExecutorStatusSchema:
    """Heartbeat + last reconciliation for the status strip (design §6.5)."""
    now = now or datetime.now(UTC)
    heartbeat_at: str | None = None
    age_hours: float | None = None
    broker_ok: bool | None = None
    entries: int | None = None
    closes: int | None = None

    path = heartbeat_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            heartbeat_at = data.get("at")
            broker_ok = data.get("broker_ok")
            entries = data.get("entries_placed")
            closes = data.get("closes_placed")
        except (json.JSONDecodeError, OSError):
            logger.warning("Unreadable heartbeat file at %s", path)
        if heartbeat_at:
            try:
                written = datetime.fromisoformat(heartbeat_at)
                if written.tzinfo is None:
                    written = written.replace(tzinfo=UTC)
                age_hours = round((now - written).total_seconds() / 3600.0, 2)
            except ValueError:
                heartbeat_at = None

    # Shared with /api/reconciliation/latest (#474, #478): the newest
    # UNRESOLVED drift run wins over a later CLEAN snapshot, so the strip
    # badge and the reconciliation panel can never disagree about "the
    # latest run".
    from backend.reconciliation import latest_reconciliation_run

    last_recon = await latest_reconciliation_run(session)
    # Digest delivery status (#277, audit H2): a total ntfy outage must be
    # visible SOMEWHERE — this is the somewhere.
    last_digest = (
        await session.execute(
            select(AuditEventModel).filter_by(event_type="DIGEST_COMPOSED").order_by(AuditEventModel.id.desc()).limit(1)
        )
    ).scalar_one_or_none()

    return ExecutorStatusSchema(
        # Missing or unparseable heartbeat reads as stale — silence is never
        # health. The verdict is trading-day-based (#545 L3, see _is_stale);
        # heartbeat_age_hours below is still the raw hour count for display.
        stale=_is_stale(heartbeat_at, now),
        heartbeat_at=heartbeat_at,
        heartbeat_age_hours=age_hours,
        broker_ok=broker_ok,
        entries_placed=entries,
        closes_placed=closes,
        last_reconciliation_at=last_recon.run_at if last_recon else None,
        last_reconciliation_result=last_recon.result if last_recon else None,
        last_reconciliation_resolved=(bool(last_recon.resolved_at) if last_recon else None),
        last_digest_at=last_digest.run_at if last_digest else None,
        last_digest_pushed=bool(last_digest.payload.get("pushed")) if last_digest else None,
        # None when the digest had no urgent lines to push (not a failure) —
        # preserve that tri-state rather than coercing to bool (#478).
        last_urgent_pushed=(last_digest.payload.get("urgent_pushed") if last_digest else None),
        trading_mode=_trading_mode(),
    )


def _trading_mode() -> str:
    """The process's real trading mode (#361) — sourced from the same value
    the mode isolation enforces on, never a config field."""
    from backend.database import TRADING_MODE

    return TRADING_MODE

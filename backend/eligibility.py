"""eligibility.py — the yes/no half of the opportunity engine (#190).

Everything that decides whether a playbook MAY trade tonight: the
portfolio-level gates, the per-playbook gates, the entry filters, and the
regime gate from the domain-rules playbook matrix. Spec generation (what
the trade IS) stays in opportunity.py; telemetry access lives in
telemetry.py. Each check returns a human-readable reason string or None —
the reason is the digest/console surface, so it explains, never codes.
"""

import re
from datetime import date

from backend.models import MarketStateSchema, PlaybookDefinitionSchema, PortfolioConfigSchema, PositionSchema
from backend.pricing import capital_at_risk
from backend.telemetry import telemetry_key, trend_label, underlying_price, underlying_sma20

# Income strategies that require minimum IVR
INCOME_STRATEGIES = {"IRON_CONDOR", "BROKEN_WING_BUTTERFLY"}

# Naked long options suppressed when IVR > 70 (show spreads only)
DEBIT_NAKED = {"LONG_STRADDLE", "LONG_STRANGLE"}

# Directional bias per strategy: +1 = bullish, -1 = bearish, 0 = neutral
DIRECTIONAL_BIAS = {
    "BULL_CALL_SPREAD": 1,
    "BEAR_PUT_SPREAD": -1,
    "BULL_PUT_SPREAD": 1,
    "BEAR_CALL_SPREAD": -1,
    "IRON_CONDOR": 0,
    "BROKEN_WING_BUTTERFLY": 0,
    "CALENDAR_SPREAD": 0,
    "LONG_STRADDLE": 0,
    "LONG_STRANGLE": 0,
}

# Regime → allowed strategies, from the domain-rules.md playbook matrix
# (PRIMARY + SECONDARY are allowed; AVOID is blocked). EVENT_CATALYST allows
# only the long-vol strategies, which ship disabled — so under every engine
# variant EVENT_CATALYST means Do Nothing. Before #136 this table existed
# only as prose and an acknowledgeable warning; nothing enforced it.
REGIME_ALLOWED_STRATEGIES: dict[str, frozenset[str]] = {
    # BWB (#132) sits with the income structures: neutral-to-bullish credit.
    # Calendars (#133) are neutral time spreads — best entered in calm tape
    # (low IV, long vega), so they ride the same two regimes.
    "CALM_BULL": frozenset(
        {"BULL_PUT_SPREAD", "BULL_CALL_SPREAD", "IRON_CONDOR", "BROKEN_WING_BUTTERFLY", "CALENDAR_SPREAD"}
    ),
    "HIGH_VOL_NEUTRAL": frozenset(
        {
            "IRON_CONDOR",
            "BULL_PUT_SPREAD",
            "BEAR_CALL_SPREAD",
            "BULL_CALL_SPREAD",
            "BEAR_PUT_SPREAD",
            "BROKEN_WING_BUTTERFLY",
            "CALENDAR_SPREAD",
        }
    ),
    "TRENDING_BEAR": frozenset({"BEAR_CALL_SPREAD", "BEAR_PUT_SPREAD"}),
    "EVENT_CATALYST": frozenset({"LONG_STRADDLE", "LONG_STRANGLE"}),
}


# -----------------------------------------------------------------------
# Catalyst and portfolio helpers (shared with spec generation)
# -----------------------------------------------------------------------


def catalyst_date(entry: str) -> date | None:
    """Extract the date from a catalyst entry. Entries may be bare ISO dates
    or prefixed ("FOMC:2026-09-16", "EARNINGS:... NVDA") — same contract as
    regime.parse_catalyst. Undated notes yield None. Assuming bare ISO here
    crashed the scan the moment the seeded calendar (#131) merged in."""
    match = re.search(r"\d{4}-\d{2}-\d{2}", entry)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(0))
    except ValueError:
        return None


def days_until(date_str: str, today: date | None = None) -> int:
    """Calendar days from today to a (possibly prefixed) catalyst entry.
    Undated entries read as long past — they never trip a date window."""
    target = catalyst_date(date_str)
    if target is None:
        return -9999
    return (target - (today or date.today())).days


def has_catalyst_within_14dte(catalyst_dates: list[str], today: date | None = None) -> bool:
    return any(0 <= days_until(d, today) <= 14 for d in catalyst_dates)


def capital_deployed(positions: list[PositionSchema]) -> float:
    """Total capital at risk across all open positions."""
    return sum(capital_at_risk(p.max_loss, p.contracts) for p in positions if p.status == "OPEN")


def open_positions(positions: list[PositionSchema]) -> list[PositionSchema]:
    return [p for p in positions if p.status == "OPEN"]


# -----------------------------------------------------------------------
# Gates and filters
# -----------------------------------------------------------------------


def run_portfolio_gates(
    open_pos: list[PositionSchema],
    portfolio_config: PortfolioConfigSchema,
) -> str | None:
    """
    Returns a block reason string if any portfolio-level gate fires.
    Portfolio gates suppress ALL candidates.
    """
    max_pos = portfolio_config.risk_profile.max_simultaneous_positions
    if len(open_pos) >= max_pos:
        return f"MAX POSITIONS: {len(open_pos)} open positions at limit of {max_pos}. Close an existing position before opening new entries."

    nav = portfolio_config.account.total_nav
    max_deployed_pct = portfolio_config.risk_profile.max_capital_deployed_pct
    deployed = capital_deployed(open_pos)
    if deployed >= (max_deployed_pct / 100.0) * nav:
        deployed_pct = deployed / nav * 100
        return f"MAX CAPITAL: ${deployed:.2f} ({deployed_pct:.1f}% of NAV) deployed, at or above {max_deployed_pct:.0f}% limit."

    return None


def check_per_playbook_gates(
    playbook: PlaybookDefinitionSchema,
    open_pos: list[PositionSchema],
    market_state: MarketStateSchema,
    *,
    enforce_ivr: bool = True,
    book_mode: bool = False,
) -> str | None:
    """
    Returns a suppression reason if a per-playbook gate fires, else None.
    Per-playbook gates suppress only that candidate.

    book_mode: lab books ladder multiple positions on ONE underlying by
    design — their concentration policy is the risk envelope
    (max_positions, max_same_strategy_expiry in book_gates.py), so the
    manual-portfolio concentration gates below would cap every book at 1–2
    positions and silently defeat the #136 cadence. They stay on for the
    manual console.
    """
    ticker = playbook.underlying_ticker
    ivr = (market_state.underlying_ivrs or {}).get(telemetry_key(ticker), 0.0)

    if not book_mode:
        # UNDERLYING CONCENTRATION: open position already exists on this underlying
        if any(p.underlying == ticker for p in open_pos):
            return f"UNDERLYING CONCENTRATION: open position already exists on {ticker}."

        # DIRECTIONAL CONCENTRATION: 2+ same-bias positions already open
        bias = DIRECTIONAL_BIAS.get(playbook.strategy_type, 0)
        if bias != 0:
            same_bias_count = sum(1 for p in open_pos if DIRECTIONAL_BIAS.get(p.strategy_type, 0) == bias)
            if same_bias_count >= 2:
                direction = "bullish" if bias > 0 else "bearish"
                return f"DIRECTIONAL CONCENTRATION: 2+ {direction} positions already open."

    # EARNINGS GATE: not modeled here (no earnings calendar) — skipped

    if enforce_ivr:
        # IVR GATE (INCOME): IVR < 40 suppresses Iron Condor
        if playbook.strategy_type in INCOME_STRATEGIES and ivr < 40.0:
            return (
                f"IVR GATE (INCOME): IVR={ivr:.0f} is below 40 — income strategies require elevated IV. "
                "Wait for IVR ≥ 40."
            )

        # IVR GATE (DEBIT): IVR > 70 suppresses naked long options
        if playbook.strategy_type in DEBIT_NAKED and ivr > 70.0:
            return (
                f"IVR GATE (DEBIT): IVR={ivr:.0f} exceeds 70 — buying naked vol is expensive at this IV level. "
                "Use a spread instead."
            )

    return None


def check_entry_filters(
    playbook: PlaybookDefinitionSchema,
    market_state: MarketStateSchema,
    today: date | None = None,
) -> str | None:
    """
    Returns suppression reason if entry filters are not satisfied.
    """
    f = playbook.entry_filters
    ticker = playbook.underlying_ticker
    ivr = (market_state.underlying_ivrs or {}).get(telemetry_key(ticker), 0.0)
    vix = market_state.vix_close or 0.0
    price = underlying_price(market_state, ticker)
    sma20 = underlying_sma20(market_state, ticker)
    catalysts = market_state.catalyst_dates or []

    # IVR range
    if not (f.min_ivr <= ivr <= f.max_ivr):
        return f"Entry filter: IVR={ivr:.0f} outside required range [{f.min_ivr:.0f}–{f.max_ivr:.0f}]."

    # VIX range
    vix_min, vix_max = f.vix_range
    if not (vix_min <= vix <= vix_max):
        return f"Entry filter: VIX={vix:.1f} outside required range [{vix_min:.0f}–{vix_max:.0f}]."

    # Trend requirement
    if f.required_trend != "ANY":
        trend = trend_label(price or 0.0, sma20)
        if trend != f.required_trend:
            return f"Entry filter: {ticker} trend is {trend}, playbook requires {f.required_trend}."

    # Catalyst block
    if f.block_catalyst_14dte and has_catalyst_within_14dte(catalysts, today):
        return "Entry filter: catalyst within 14 DTE — this playbook blocks new entries around events."

    # Catalyst requirement
    if f.require_catalyst_14dte and not has_catalyst_within_14dte(catalysts, today):
        return "Entry filter: no catalyst within 14 DTE — this playbook requires an upcoming event."

    return None


def check_regime_gate(playbook: PlaybookDefinitionSchema, market_state: MarketStateSchema) -> str | None:
    # current_regime is a Literal of the four regimes, so lookup cannot miss.
    allowed = REGIME_ALLOWED_STRATEGIES[market_state.current_regime]
    if playbook.strategy_type not in allowed:
        return (
            f"REGIME GATE: {playbook.strategy_type} is not in the {market_state.current_regime} "
            f"playbook matrix (allowed: {', '.join(sorted(allowed))})."
        )
    return None

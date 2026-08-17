"""
Layer C: Opportunity Engine

Scans active playbooks against Layer B telemetry, enforces exposure gates,
derives strike parameters, generates trade specs, and runs pre-output validation.
"""

import math
from datetime import date, timedelta

from backend.models import (
    CandidateCard,
    HardBlock,
    MarketStateSchema,
    OpportunityScanResult,
    PlaybookDefinitionSchema,
    PortfolioConfigSchema,
    PositionSchema,
    StrikeDerivedParams,
    TradeSpec,
    TradeSpecLeg,
    TradeSpecResult,
    TradeWarning,
)
from backend.observation import run_lifecycle_scan
from backend.pricing import calculate_position_metrics, capital_at_risk

# Strategies entered for a net credit; everything else is entered for a debit.
_CREDIT_STRATEGIES = ("IRON_CONDOR", "BULL_PUT_SPREAD", "BEAR_CALL_SPREAD")

# -----------------------------------------------------------------------
# Correlated index tickers (for capital concentration checks)
# -----------------------------------------------------------------------
_CORRELATED_INDICES = {"SPY", "QQQ", "IWM", "DIA", "SPX", "NDX", "RUT"}

# Income strategies that require minimum IVR
_INCOME_STRATEGIES = {"IRON_CONDOR"}

# Naked long options suppressed when IVR > 70 (show spreads only)
_DEBIT_NAKED = {"LONG_STRADDLE", "LONG_STRANGLE"}

# Directional bias per strategy: +1 = bullish, -1 = bearish, 0 = neutral
_DIRECTIONAL_BIAS = {
    "BULL_CALL_SPREAD": 1,
    "BEAR_PUT_SPREAD": -1,
    "BULL_PUT_SPREAD": 1,
    "BEAR_CALL_SPREAD": -1,
    "IRON_CONDOR": 0,
    "LONG_STRADDLE": 0,
    "LONG_STRANGLE": 0,
}


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------


def _days_until(date_str: str, today: date | None = None) -> int:
    """Calendar days from today to an ISO date string."""
    target = date.fromisoformat(date_str.split("T")[0])
    return (target - (today or date.today())).days


def _has_catalyst_within_14dte(catalyst_dates: list[str], today: date | None = None) -> bool:
    return any(0 <= _days_until(d, today) <= 14 for d in catalyst_dates)


def _capital_deployed(positions: list[PositionSchema]) -> float:
    """Total capital at risk across all open positions."""
    return sum(capital_at_risk(p.max_loss, p.contracts) for p in positions if p.status == "OPEN")


def _open_positions(positions: list[PositionSchema]) -> list[PositionSchema]:
    return [p for p in positions if p.status == "OPEN"]


def _spy_trend_label(spy_price: float, spy_sma20: float) -> str:
    if spy_sma20 == 0:
        return "ANY"
    diff_pct = (spy_price - spy_sma20) / spy_sma20 * 100
    if diff_pct > 0:
        return "ABOVE_SMA20"
    if diff_pct < 0:
        return "BELOW_SMA20"
    return "ANY"


def _one_sigma_move(spy_price: float, vix_close: float, dte: int) -> float:
    """Estimate 1-standard-deviation price move for given DTE using VIX as annualized IV."""
    iv = max(vix_close, 1.0) / 100.0
    return spy_price * iv * math.sqrt(dte / 252.0)


def _target_expiration(
    today: date,
    target_dte: int,
    require_after_catalyst: bool,
    catalyst_dates: list[str],
) -> tuple[date, int]:
    """
    Return (expiration_date, actual_dte).

    For event plays: earliest expiration that is at least 14 days after the
    nearest upcoming catalyst. For all others: today + target_dte, rounded to
    nearest Friday (options typically expire on Fridays).
    """
    if require_after_catalyst:
        upcoming = [d for d in catalyst_dates if _days_until(d, today) >= 0]
        if upcoming:
            nearest_catalyst = min(upcoming, key=lambda d: _days_until(d, today))
            catalyst_date = date.fromisoformat(nearest_catalyst.split("T")[0])
            min_exp = catalyst_date + timedelta(days=14)
            # Snap to next Friday on or after min_exp
            days_to_friday = (4 - min_exp.weekday()) % 7
            exp_date = min_exp + timedelta(days=days_to_friday)
        else:
            exp_date = today + timedelta(days=target_dte)
    else:
        base = today + timedelta(days=target_dte)
        days_to_friday = (4 - base.weekday()) % 7
        exp_date = base + timedelta(days=days_to_friday)

    actual_dte = (exp_date - today).days
    return exp_date, actual_dte


def _nearest_strike(price: float, interval: float = 5.0) -> float:
    """Round to nearest strike interval (default $5 for SPY)."""
    return round(price / interval) * interval


# -----------------------------------------------------------------------
# Strike derivation
# -----------------------------------------------------------------------


def _derive_strike_params(
    playbook: PlaybookDefinitionSchema,
    market_state: MarketStateSchema,
) -> StrikeDerivedParams:
    price = market_state.spy_price
    vix = market_state.vix_close or 20.0
    dte = playbook.execution_specs.target_dte
    sigma = _one_sigma_move(price, vix, dte)
    short_delta = playbook.execution_specs.short_leg_delta
    long_delta = playbook.execution_specs.long_leg_delta
    width = playbook.execution_specs.spread_width_dollars

    # Approximate strike from delta using 1σ move.
    # delta=0.50 → ATM; delta=0.16 → ~1σ OTM; delta=0.25 → ~0.7σ OTM
    def _otm_distance(delta: float) -> float:
        # Invert N(d1) ≈ delta: d1 = Φ⁻¹(delta)
        # Φ⁻¹(0.50) = 0, Φ⁻¹(0.16) ≈ -1.0, Φ⁻¹(0.25) ≈ -0.67
        # We use the normal quantile approximation
        if delta >= 0.5:
            return 0.0
        # Rational approximation of Φ⁻¹(delta)
        p = delta
        t = math.sqrt(-2 * math.log(p))
        c = [2.515517, 0.802853, 0.010328]
        d = [1.432788, 0.189269, 0.001308]
        z = t - (c[0] + c[1] * t + c[2] * t * t) / (1 + d[0] * t + d[1] * t * t + d[2] * t * t * t)
        # z is negative (below mean), distance = |z| * sigma
        return abs(z) * sigma

    note_parts: list[str] = [
        f"SPY @${price:.2f} | VIX={vix:.1f} | DTE={dte} | 1σ=${sigma:.2f}",
    ]

    if playbook.execution_specs.straddle_atm:
        note_parts.append(f"ATM strike → nearest ${5:.0f} to current price")
    else:
        otm = _otm_distance(short_delta)
        note_parts.append(f"Short leg Δ={short_delta} → ~${otm:.0f} OTM | Wing width=${width:.0f}")

    return StrikeDerivedParams(
        underlying=playbook.underlying_ticker,
        current_price=price,
        target_dte=dte,
        short_leg_delta=short_delta,
        long_leg_delta=long_delta,
        spread_width_dollars=width if not playbook.execution_specs.straddle_atm else None,
        one_sigma_move=round(sigma, 2),
        derivation_note=" | ".join(note_parts),
    )


# -----------------------------------------------------------------------
# Exposure gates
# -----------------------------------------------------------------------


def _run_portfolio_gates(
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
    deployed = _capital_deployed(open_pos)
    if deployed >= (max_deployed_pct / 100.0) * nav:
        deployed_pct = deployed / nav * 100
        return f"MAX CAPITAL: ${deployed:.2f} ({deployed_pct:.1f}% of NAV) deployed, at or above {max_deployed_pct:.0f}% limit."

    return None


def _check_per_playbook_gates(
    playbook: PlaybookDefinitionSchema,
    open_pos: list[PositionSchema],
    market_state: MarketStateSchema,
) -> str | None:
    """
    Returns a suppression reason if a per-playbook gate fires, else None.
    Per-playbook gates suppress only that candidate.
    """
    ticker = playbook.underlying_ticker
    ivr = (market_state.underlying_ivrs or {}).get(ticker, 0.0)

    # UNDERLYING CONCENTRATION: open position already exists on this underlying
    if any(p.underlying == ticker for p in open_pos):
        return f"UNDERLYING CONCENTRATION: open position already exists on {ticker}."

    # DIRECTIONAL CONCENTRATION: 2+ same-bias positions already open
    bias = _DIRECTIONAL_BIAS.get(playbook.strategy_type, 0)
    if bias != 0:
        same_bias_count = sum(1 for p in open_pos if _DIRECTIONAL_BIAS.get(p.strategy_type, 0) == bias)
        if same_bias_count >= 2:
            direction = "bullish" if bias > 0 else "bearish"
            return f"DIRECTIONAL CONCENTRATION: 2+ {direction} positions already open."

    # EARNINGS GATE: not modeled here (no earnings calendar) — skipped

    # IVR GATE (INCOME): IVR < 40 suppresses Iron Condor
    if playbook.strategy_type in _INCOME_STRATEGIES and ivr < 40.0:
        return (
            f"IVR GATE (INCOME): IVR={ivr:.0f} is below 40 — income strategies require elevated IV. Wait for IVR ≥ 40."
        )

    # IVR GATE (DEBIT): IVR > 70 suppresses naked long options
    if playbook.strategy_type in _DEBIT_NAKED and ivr > 70.0:
        return f"IVR GATE (DEBIT): IVR={ivr:.0f} exceeds 70 — buying naked vol is expensive at this IV level. Use a spread instead."

    return None


# -----------------------------------------------------------------------
# Entry filter check
# -----------------------------------------------------------------------


def _check_entry_filters(
    playbook: PlaybookDefinitionSchema,
    market_state: MarketStateSchema,
) -> str | None:
    """
    Returns suppression reason if entry filters are not satisfied.
    """
    f = playbook.entry_filters
    ticker = playbook.underlying_ticker
    ivr = (market_state.underlying_ivrs or {}).get(ticker, 0.0)
    vix = market_state.vix_close or 0.0
    spy_price = market_state.spy_price
    spy_sma20 = market_state.spy_sma20 or 0.0
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
        trend = _spy_trend_label(spy_price, spy_sma20)
        if trend != f.required_trend:
            return f"Entry filter: SPY trend is {trend}, playbook requires {f.required_trend}."

    # Catalyst block
    if f.block_catalyst_14dte and _has_catalyst_within_14dte(catalysts):
        return "Entry filter: catalyst within 14 DTE — this playbook blocks new entries around events."

    # Catalyst requirement
    if f.require_catalyst_14dte and not _has_catalyst_within_14dte(catalysts):
        return "Entry filter: no catalyst within 14 DTE — this playbook requires an upcoming event."

    return None


# -----------------------------------------------------------------------
# Public: scan all playbooks
# -----------------------------------------------------------------------


def scan_opportunities(
    playbooks: list[PlaybookDefinitionSchema],
    market_state: MarketStateSchema,
    positions: list[PositionSchema],
    portfolio_config: PortfolioConfigSchema,
    today: date | None = None,
) -> OpportunityScanResult:
    open_pos = _open_positions(positions)

    block = _run_portfolio_gates(open_pos, portfolio_config)
    if block:
        return OpportunityScanResult(portfolio_blocked=True, block_reason=block, candidates=[])

    candidates: list[CandidateCard] = []
    for pb in playbooks:
        # Disabled playbooks are skipped entirely — never shown as candidates
        if not pb.enabled:
            continue

        # Per-playbook gate check (runs before entry filters — gates are unconditional)
        gate_reason = _check_per_playbook_gates(pb, open_pos, market_state)
        if gate_reason:
            candidates.append(CandidateCard(playbook=pb, eligible=False, suppressed_reason=gate_reason))
            continue

        # Entry filter check
        filter_reason = _check_entry_filters(pb, market_state)
        if filter_reason:
            candidates.append(CandidateCard(playbook=pb, eligible=False, suppressed_reason=filter_reason))
            continue

        strike_params = _derive_strike_params(pb, market_state)
        candidates.append(CandidateCard(playbook=pb, eligible=True, strike_params=strike_params))

    # Spec: ineligible playbooks are hidden. Return only eligible ones (filter happens in API layer).
    return OpportunityScanResult(portfolio_blocked=False, candidates=candidates)


# -----------------------------------------------------------------------
# Public: generate trade spec for a specific playbook
# -----------------------------------------------------------------------


def generate_trade_spec(
    playbook: PlaybookDefinitionSchema,
    market_state: MarketStateSchema,
    positions: list[PositionSchema],
    portfolio_config: PortfolioConfigSchema,
    contracts: int = 1,
    today: date | None = None,
) -> TradeSpecResult:
    _today = today or date.today()

    if not playbook.enabled:
        return TradeSpecResult(
            hard_blocks=[
                HardBlock(
                    check="PLAYBOOK_DISABLED",
                    reason=f"Playbook {playbook.id!r} is disabled — enable it before generating a trade spec.",
                )
            ],
            warnings=[],
            spec=None,
        )

    price = market_state.spy_price
    vix = market_state.vix_close or 20.0
    specs = playbook.execution_specs
    exit_rules = playbook.exit_rules
    open_pos = _open_positions(positions)

    # ---- Compute expiration ----
    exp_date, dte = _target_expiration(
        _today,
        specs.target_dte,
        require_after_catalyst=playbook.entry_filters.require_catalyst_14dte,
        catalyst_dates=market_state.catalyst_dates or [],
    )

    # ---- Derive strikes ----
    strike_params = _derive_strike_params(playbook, market_state)
    sigma = strike_params.one_sigma_move or _one_sigma_move(price, vix, dte)

    def _otm_strike(delta: float, direction: int) -> float:
        """direction: +1 = call (above price), -1 = put (below price)."""
        if delta >= 0.5:
            return _nearest_strike(price)
        p = delta
        t = math.sqrt(-2 * math.log(p))
        c = [2.515517, 0.802853, 0.010328]
        d = [1.432788, 0.189269, 0.001308]
        z = t - (c[0] + c[1] * t + c[2] * t * t) / (1 + d[0] * t + d[1] * t * t + d[2] * t * t * t)
        otm = abs(z) * sigma
        return _nearest_strike(price + direction * otm)

    exp_str = exp_date.isoformat()
    legs: list[TradeSpecLeg] = []

    if playbook.strategy_type == "IRON_CONDOR":
        # Wing strikes honor the playbook width on the $1 grid (SPY/XSP list
        # $1 strikes near the money) — a $5 grid would silently widen $3
        # wings past the ADR-0006 per-trade cap (#94).
        short_call = _otm_strike(specs.short_leg_delta, +1)
        long_call = _nearest_strike(short_call + specs.spread_width_dollars, interval=1.0)
        short_put = _otm_strike(specs.short_leg_delta, -1)
        long_put = _nearest_strike(short_put - specs.spread_width_dollars, interval=1.0)
        legs = [
            TradeSpecLeg(
                action="SELL",
                option_type="PUT",
                strike=short_put,
                expiration_date=exp_str,
                quantity=contracts,
                delta_target=-specs.short_leg_delta,
            ),
            TradeSpecLeg(
                action="BUY",
                option_type="PUT",
                strike=long_put,
                expiration_date=exp_str,
                quantity=contracts,
                delta_target=None,
            ),
            TradeSpecLeg(
                action="SELL",
                option_type="CALL",
                strike=short_call,
                expiration_date=exp_str,
                quantity=contracts,
                delta_target=specs.short_leg_delta,
            ),
            TradeSpecLeg(
                action="BUY",
                option_type="CALL",
                strike=long_call,
                expiration_date=exp_str,
                quantity=contracts,
                delta_target=None,
            ),
        ]
        # Credit ≈ 1/3 spread width (conservative estimate without live chain)
        limit_price = round(specs.spread_width_dollars / 3.0, 2)

    elif playbook.strategy_type == "BULL_CALL_SPREAD":
        buy_strike = _otm_strike(specs.long_leg_delta, +1)  # ATM/near-ATM
        # Sell leg = buy + playbook width. The delta-derived sell leg produced
        # ~$30-wide spreads whose debit blew the per-trade cap (#94); width is
        # the sizing authority for autonomous entries.
        sell_strike = _nearest_strike(buy_strike + specs.spread_width_dollars, interval=1.0)
        legs = [
            TradeSpecLeg(
                action="BUY",
                option_type="CALL",
                strike=buy_strike,
                expiration_date=exp_str,
                quantity=contracts,
                delta_target=specs.long_leg_delta,
            ),
            TradeSpecLeg(
                action="SELL",
                option_type="CALL",
                strike=sell_strike,
                expiration_date=exp_str,
                quantity=contracts,
                delta_target=specs.short_leg_delta,
            ),
        ]
        spread = sell_strike - buy_strike
        limit_price = round(spread * 0.45, 2)  # ~45% of spread width (debit)

    elif playbook.strategy_type == "BEAR_PUT_SPREAD":
        buy_strike = _otm_strike(specs.long_leg_delta, -1)  # ATM/near-ATM put
        # Sell leg = buy − playbook width (see BULL_CALL_SPREAD note, #94)
        sell_strike = _nearest_strike(buy_strike - specs.spread_width_dollars, interval=1.0)
        legs = [
            TradeSpecLeg(
                action="BUY",
                option_type="PUT",
                strike=buy_strike,
                expiration_date=exp_str,
                quantity=contracts,
                delta_target=-specs.long_leg_delta,
            ),
            TradeSpecLeg(
                action="SELL",
                option_type="PUT",
                strike=sell_strike,
                expiration_date=exp_str,
                quantity=contracts,
                delta_target=-specs.short_leg_delta,
            ),
        ]
        spread = buy_strike - sell_strike
        limit_price = round(spread * 0.45, 2)

    elif playbook.strategy_type == "BULL_PUT_SPREAD":
        short_strike = _otm_strike(specs.short_leg_delta, -1)  # OTM put below price
        long_strike = _nearest_strike(short_strike - specs.spread_width_dollars, interval=1.0)
        legs = [
            TradeSpecLeg(
                action="SELL",
                option_type="PUT",
                strike=short_strike,
                expiration_date=exp_str,
                quantity=contracts,
                delta_target=-specs.short_leg_delta,
            ),
            TradeSpecLeg(
                action="BUY",
                option_type="PUT",
                strike=long_strike,
                expiration_date=exp_str,
                quantity=contracts,
                delta_target=None,
            ),
        ]
        spread = short_strike - long_strike
        # Credit ≈ 1/3 spread width (conservative estimate without live chain)
        limit_price = round(spread / 3.0, 2)

    elif playbook.strategy_type == "BEAR_CALL_SPREAD":
        short_strike = _otm_strike(specs.short_leg_delta, +1)  # OTM call above price
        long_strike = _nearest_strike(short_strike + specs.spread_width_dollars, interval=1.0)
        legs = [
            TradeSpecLeg(
                action="SELL",
                option_type="CALL",
                strike=short_strike,
                expiration_date=exp_str,
                quantity=contracts,
                delta_target=specs.short_leg_delta,
            ),
            TradeSpecLeg(
                action="BUY",
                option_type="CALL",
                strike=long_strike,
                expiration_date=exp_str,
                quantity=contracts,
                delta_target=None,
            ),
        ]
        spread = long_strike - short_strike
        limit_price = round(spread / 3.0, 2)

    elif playbook.strategy_type == "LONG_STRADDLE":
        atm = _nearest_strike(price)
        legs = [
            TradeSpecLeg(
                action="BUY",
                option_type="CALL",
                strike=atm,
                expiration_date=exp_str,
                quantity=contracts,
                delta_target=0.5,
            ),
            TradeSpecLeg(
                action="BUY",
                option_type="PUT",
                strike=atm,
                expiration_date=exp_str,
                quantity=contracts,
                delta_target=-0.5,
            ),
        ]
        # Estimate debit as σ-adjusted; straddle ≈ 0.8 * 1σ move (rough)
        limit_price = round(sigma * 0.8 * 2, 2)  # call + put

    elif playbook.strategy_type == "LONG_STRANGLE":
        call_strike = _otm_strike(specs.short_leg_delta, +1)
        put_strike = _otm_strike(specs.short_leg_delta, -1)
        legs = [
            TradeSpecLeg(
                action="BUY",
                option_type="CALL",
                strike=call_strike,
                expiration_date=exp_str,
                quantity=contracts,
                delta_target=specs.short_leg_delta,
            ),
            TradeSpecLeg(
                action="BUY",
                option_type="PUT",
                strike=put_strike,
                expiration_date=exp_str,
                quantity=contracts,
                delta_target=-specs.short_leg_delta,
            ),
        ]
        limit_price = round(sigma * 0.5 * 2, 2)  # strangle cheaper than straddle

    else:
        return TradeSpecResult(
            hard_blocks=[
                HardBlock(
                    check="UNKNOWN_STRATEGY",
                    reason=f"Strategy type {playbook.strategy_type} is not supported.",
                )
            ],
            warnings=[],
            spec=None,
        )

    # Trade economics — single source: backend/pricing.py
    premium_direction = "CREDIT" if playbook.strategy_type in _CREDIT_STRATEGIES else "DEBIT"
    metrics = calculate_position_metrics(
        strategy_type=playbook.strategy_type,
        legs=[
            {
                "option_type": leg.option_type,
                "direction": "LONG" if leg.action == "BUY" else "SHORT",
                "strike": leg.strike,
            }
            for leg in legs
        ],
        entry_premium=limit_price,
        premium_direction=premium_direction,
    )
    max_loss_per_share = metrics["max_loss"]
    unlimited_gain = playbook.strategy_type in ("LONG_STRADDLE", "LONG_STRANGLE")
    max_gain_per_share = None if unlimited_gain else metrics["max_profit"]
    break_evens = [b for b in (metrics["break_even_downside"], metrics["break_even_upside"]) if b is not None]
    if unlimited_gain:
        max_gain_note = "Unlimited"
    elif premium_direction == "CREDIT":
        max_gain_note = f"${max_gain_per_share * 100 * contracts:.2f} (premium collected)"
    else:
        max_gain_note = f"${max_gain_per_share * 100 * contracts:.2f}"

    max_loss_dollars = max_loss_per_share * 100 * contracts
    # For all strategies: profit/loss targets are percentages of the premium (debit paid or credit received).
    profit_target_dollars = limit_price * 100 * contracts * (exit_rules.profit_take_pct / 100.0)
    loss_limit_dollars = limit_price * 100 * contracts * (exit_rules.stop_loss_pct / 100.0)

    closing_instructions = (
        f"Immediately after fill: place GTC limit order to close all {contracts} contract(s) at "
        f"${profit_target_dollars / (100 * contracts):.2f}/share (profit target). "
        f"Also place GTC stop order to close at ${loss_limit_dollars / (100 * contracts):.2f}/share (loss limit)."
    )

    spec = TradeSpec(
        playbook_id=playbook.id,
        playbook_name=playbook.name,
        underlying=playbook.underlying_ticker,
        strategy_type=playbook.strategy_type,
        legs=legs,
        expiration_date=exp_str,
        dte_at_entry=dte,
        order_type="LIMIT",
        limit_price_per_share=limit_price,
        max_loss_dollars=max_loss_dollars,
        max_gain_dollars=(max_gain_per_share * 100 * contracts) if max_gain_per_share is not None else None,
        max_gain_note=max_gain_note,
        break_even_prices=[round(b, 2) for b in break_evens],
        profit_target_dollars=round(profit_target_dollars, 2),
        profit_target_pct=exit_rules.profit_take_pct,
        loss_limit_dollars=round(loss_limit_dollars, 2),
        loss_limit_pct=exit_rules.stop_loss_pct,
        closing_order_instructions=closing_instructions,
        derivation_params=strike_params,
    )

    # ---- Pre-output validation ----
    hard_blocks = _run_hard_blocks(spec, open_pos, portfolio_config, market_state, _today)
    warnings = _run_warnings(spec, open_pos, market_state, positions)

    return TradeSpecResult(
        hard_blocks=hard_blocks,
        warnings=warnings,
        spec=spec if not hard_blocks else None,
    )


# -----------------------------------------------------------------------
# Section 5.5 — Hard blocks and warnings
# -----------------------------------------------------------------------


def _run_hard_blocks(
    spec: TradeSpec,
    open_pos: list[PositionSchema],
    portfolio_config: PortfolioConfigSchema,
    market_state: MarketStateSchema,
    today: date,
) -> list[HardBlock]:
    blocks: list[HardBlock] = []

    # Unresolved P1 action — scan each open position individually
    p1_tickers: list[str] = []
    for pos in open_pos:
        scan = run_lifecycle_scan(
            pos,
            current_regime=market_state.current_regime,
            spy_price=market_state.spy_price,
            catalyst_dates=market_state.catalyst_dates or [],
            today=today,
        )
        if scan["priority"] == "P1 — CLOSE NOW":
            p1_tickers.append(pos.underlying)
    if p1_tickers:
        blocks.append(
            HardBlock(
                check="UNRESOLVED_P1",
                reason=f"P1 CLOSE NOW alert active on {', '.join(p1_tickers)}. Resolve all P1 actions before opening new positions.",
            )
        )

    # Capital exceeded
    nav = portfolio_config.account.total_nav
    deployed = _capital_deployed(open_pos)
    available_cash = nav - deployed
    if spec.max_loss_dollars > available_cash:
        blocks.append(
            HardBlock(
                check="CAPITAL_EXCEEDED",
                reason=f"Max loss ${spec.max_loss_dollars:.2f} exceeds available cash ${available_cash:.2f}.",
            )
        )

    # Max loss exceeded
    max_risk = portfolio_config.risk_profile.max_trade_risk_dollars
    if spec.max_loss_dollars > max_risk:
        blocks.append(
            HardBlock(
                check="MAX_LOSS_EXCEEDED",
                reason=f"Max loss ${spec.max_loss_dollars:.2f} exceeds per-trade risk limit of ${max_risk:.2f}.",
            )
        )

    # Expiration arithmetic
    exp_date = date.fromisoformat(spec.expiration_date)
    exp_dte = (exp_date - today).days
    if exp_dte < 0:
        blocks.append(
            HardBlock(
                check="EXPIRATION_ARITHMETIC",
                reason=f"Expiration date {spec.expiration_date} is in the past.",
            )
        )
    elif exp_dte < 14:
        blocks.append(
            HardBlock(
                check="EXPIRATION_ARITHMETIC",
                reason=f"Expiration {spec.expiration_date} is only {exp_dte} DTE — minimum 14 DTE required for new entries.",
            )
        )

    # Premium reasonableness
    underlying_price = spec.derivation_params.current_price
    if spec.limit_price_per_share <= 0:
        blocks.append(
            HardBlock(
                check="PREMIUM_UNREASONABLE",
                reason=f"Derived premium ${spec.limit_price_per_share:.2f}/share is zero or negative.",
            )
        )
    elif spec.limit_price_per_share > underlying_price:
        blocks.append(
            HardBlock(
                check="PREMIUM_UNREASONABLE",
                reason=f"Derived premium ${spec.limit_price_per_share:.2f}/share exceeds underlying price ${underlying_price:.2f}.",
            )
        )

    # Position count
    max_pos = portfolio_config.risk_profile.max_simultaneous_positions
    if len(open_pos) + 1 > max_pos:
        blocks.append(
            HardBlock(
                check="POSITION_COUNT",
                reason=f"Opening this trade would bring total positions to {len(open_pos) + 1}, above the {max_pos}-position limit.",
            )
        )

    # Strike sanity: buy leg of bull spread more than 10% OTM
    if spec.strategy_type == "BULL_CALL_SPREAD":
        buy_legs = [l for l in spec.legs if l.action == "BUY" and l.option_type == "CALL"]
        if buy_legs:
            buy_strike = buy_legs[0].strike
            ref_price = spec.derivation_params.current_price
            if buy_strike > ref_price * 1.10:
                blocks.append(
                    HardBlock(
                        check="STRIKE_SANITY",
                        reason=f"Bull call spread buy leg strike ${buy_strike:.0f} is more than 10% OTM from ${ref_price:.2f}.",
                    )
                )

    return blocks


def _run_warnings(
    spec: TradeSpec,
    open_pos: list[PositionSchema],
    market_state: MarketStateSchema,
    all_positions: list[PositionSchema],
) -> list[TradeWarning]:
    warnings: list[TradeWarning] = []
    regime = market_state.current_regime

    # Regime consistency
    bias = _DIRECTIONAL_BIAS.get(spec.strategy_type, 0)
    regime_conflict = (
        (bias > 0 and regime == "TRENDING_BEAR")
        or (bias < 0 and regime in ("CALM_BULL",))
        or (spec.strategy_type == "IRON_CONDOR" and regime == "EVENT_CATALYST")
    )
    if regime_conflict:
        warnings.append(
            TradeWarning(
                check="REGIME_CONSISTENCY",
                message=f"Trade direction ({spec.strategy_type}) may be inconsistent with current regime ({regime}). Confirm this is intentional.",
            )
        )

    # Duplicate underlying
    if any(p.underlying == spec.underlying and p.status == "OPEN" for p in all_positions):
        warnings.append(
            TradeWarning(
                check="DUPLICATE_UNDERLYING",
                message=f"An open position on {spec.underlying} already exists. Adding another increases concentration risk.",
            )
        )

    # Break-even realism (straddles/strangles)
    if spec.strategy_type in ("LONG_STRADDLE", "LONG_STRANGLE") and spec.derivation_params.one_sigma_move:
        sigma = spec.derivation_params.one_sigma_move
        current_price = spec.derivation_params.current_price
        if spec.break_even_prices:
            max_be_dist = max(abs(be - current_price) for be in spec.break_even_prices)
            if sigma > 0 and max_be_dist > 2 * sigma:
                warnings.append(
                    TradeWarning(
                        check="BREAKEVEN_REALISM",
                        message=f"Break-even requires a >{max_be_dist:.1f} move (>{max_be_dist / sigma:.1f}σ). This is an unusually large required move.",
                    )
                )

    # Strategy novelty: first time this strategy type is being used
    used_types = {p.strategy_type for p in all_positions}
    if spec.strategy_type not in used_types:
        warnings.append(
            TradeWarning(
                check="STRATEGY_NOVELTY",
                message=f"First time using {spec.strategy_type}. Strongly recommend paper mode for initial execution.",
            )
        )

    return warnings

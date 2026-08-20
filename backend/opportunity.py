"""
Layer C: Opportunity Engine — spec generation (#190).

Scans playbooks for eligible entries and generates trade specs with
pre-output validation. Eligibility (gates, filters, regime matrix) lives in
eligibility.py; telemetry access in telemetry.py; per-strategy legs in
strategy_builders.py. What remains here is the WHAT of a trade: expiration,
strikes, economics, hard blocks, warnings.
"""

import math
from datetime import date, timedelta

from backend.assignment_defense import entry_ex_div_block
from backend.dates import market_today
from backend.eligibility import (
    DIRECTIONAL_BIAS,
    capital_deployed,
    catalyst_date,
    check_entry_filters,
    check_per_playbook_gates,
    check_regime_gate,
    days_until,
    open_positions,
    relevant_catalysts,
    run_portfolio_gates,
    scoped_catalysts,
)
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
    TradeSpecResult,
    TradeWarning,
)
from backend.observation import run_lifecycle_scan
from backend.pricing import calculate_position_metrics
from backend.strategy_builders import STRATEGY_BUILDERS, BuildContext
from backend.telemetry import underlying_price

# Strategies entered for a net credit; everything else is entered for a debit.
_CREDIT_STRATEGIES = ("IRON_CONDOR", "BULL_PUT_SPREAD", "BEAR_CALL_SPREAD", "BROKEN_WING_BUTTERFLY")


def _one_sigma_move(spy_price: float, vix_close: float, dte: int) -> float:
    """Estimate 1-standard-deviation price move for given DTE using VIX as annualized IV."""
    iv = max(vix_close, 1.0) / 100.0
    return spy_price * iv * math.sqrt(dte / 252.0)


def _target_expiration(
    today: date,
    target_dte: int,
    require_after_catalyst: bool,
    catalyst_dates: list[str],
    event_buffer_days: int = 14,
) -> tuple[date, int]:
    """
    Return (expiration_date, actual_dte).

    For event plays: earliest expiration at least `event_buffer_days` after
    the nearest upcoming catalyst — 14 for long-vol plays that want time
    value left after the event; 6 for scoped short-premium crush plays
    (#317, #349): paired with a 5-DTE mandatory exit, the exit lands AFTER
    the event for every report weekday (a +3 buffer put Mon/Tue reports on
    the same-week Friday, so the old 7-DTE exit closed the condor ~4 days
    BEFORE the crush). The snap still usually lands before the underlying's
    next ex-div date (AAPL's trails earnings by ~10 days); a Monday report
    can push past it, and that quarter is then SKIPPED by the entry ex-div
    block rather than traded wrong. For all others: today + target_dte,
    rounded to nearest Friday.
    """
    if require_after_catalyst:
        upcoming = [d for d in catalyst_dates if days_until(d, today) >= 0]
        if upcoming:
            nearest_catalyst = min(upcoming, key=lambda d: days_until(d, today))
            nearest_date = catalyst_date(nearest_catalyst)
            assert nearest_date is not None  # undated entries filtered by days_until
            min_exp = nearest_date + timedelta(days=event_buffer_days)
            # Snap to next Friday on or after min_exp
            days_to_friday = (4 - min_exp.weekday()) % 7
            exp_date = min_exp + timedelta(days=days_to_friday)
        else:
            exp_date = today + timedelta(days=target_dte)
    else:
        base = today + timedelta(days=target_dte)
        days_to_friday = (4 - base.weekday()) % 7
        exp_date = base + timedelta(days=days_to_friday)

    # Holiday-aware snap (#282, audit H8): when the snapped Friday is a
    # market holiday (Good Friday), listed options expire the prior trading
    # day — a naive Friday yields unpriceable legs and a misleading audit.
    from backend.calendars import snap_to_trading_day

    exp_date = snap_to_trading_day(exp_date)

    actual_dte = (exp_date - today).days
    return exp_date, actual_dte


# Per-underlying strike spacing where the $1 default doesn't hold (#317):
# AAPL lists $2.5-spaced strikes at ~$230 — a $1-derived strike simply fails
# to quote and the candidate dies silently every night.
_STRIKE_INTERVALS: dict[str, float] = {"AAPL": 2.5}


def _nearest_strike(price: float, interval: float = 1.0) -> float:
    """Round to the nearest strike interval. Default $1 (#282, audit H8):
    every traded underlying (XSP/SPY/IWM/GLD/TLT) lists $1 strikes near the
    money, and the old $5 default made the short-delta knob (B14/B23/B24's
    whole question) sweep in $5 lumps. A strike that doesn't exist simply
    fails to quote and the candidate is skipped — coarseness bought nothing."""
    return round(price / interval) * interval


# -----------------------------------------------------------------------
# Strike derivation
# -----------------------------------------------------------------------


def _derive_strike_params(
    playbook: PlaybookDefinitionSchema,
    market_state: MarketStateSchema,
) -> StrikeDerivedParams | None:
    """Derive strike parameters, or None when the playbook's underlying has
    no price telemetry (#139) — callers suppress rather than derive off SPY."""
    price = underlying_price(market_state, playbook.underlying_ticker)
    if price is None:
        return None
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
        f"{playbook.underlying_ticker} @${price:.2f} | VIX={vix:.1f} | DTE={dte} | 1σ=${sigma:.2f}",
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
# Public: scan all playbooks
# -----------------------------------------------------------------------


def scan_opportunities(
    playbooks: list[PlaybookDefinitionSchema],
    market_state: MarketStateSchema,
    positions: list[PositionSchema],
    portfolio_config: PortfolioConfigSchema,
    today: date | None = None,
    enforce_regime: bool = True,
    enforce_ivr: bool = True,
    book_mode: bool = False,
) -> OpportunityScanResult:
    """Scan playbooks for eligible entries.

    enforce_regime / enforce_ivr exist for the experiment-control lab books
    (B12/B16, #136) — the manual console and every ordinary book run with both
    gates on. book_mode swaps the manual-portfolio concentration gates for the
    book envelope's own limits (see _check_per_playbook_gates).
    """
    open_pos = open_positions(positions)

    block = run_portfolio_gates(open_pos, portfolio_config)
    if block:
        return OpportunityScanResult(portfolio_blocked=True, block_reason=block, candidates=[])

    candidates: list[CandidateCard] = []
    for pb in playbooks:
        # Disabled playbooks are skipped entirely — never shown as candidates
        if not pb.enabled:
            continue

        # Regime gate (domain-rules playbook matrix) — unconditional unless
        # this scan belongs to the no-regime control book.
        if enforce_regime:
            regime_reason = check_regime_gate(pb, market_state)
            if regime_reason:
                candidates.append(CandidateCard(playbook=pb, eligible=False, suppressed_reason=regime_reason))
                continue

        # Per-playbook gate check (runs before entry filters — gates are unconditional)
        gate_reason = check_per_playbook_gates(pb, open_pos, market_state, enforce_ivr=enforce_ivr, book_mode=book_mode)
        if gate_reason:
            candidates.append(CandidateCard(playbook=pb, eligible=False, suppressed_reason=gate_reason))
            continue

        # Entry filter check
        filter_reason = check_entry_filters(pb, market_state, today)
        if filter_reason:
            candidates.append(CandidateCard(playbook=pb, eligible=False, suppressed_reason=filter_reason))
            continue

        strike_params = _derive_strike_params(pb, market_state)
        if strike_params is None:
            candidates.append(
                CandidateCard(
                    playbook=pb,
                    eligible=False,
                    suppressed_reason=f"TELEMETRY: no price history for {pb.underlying_ticker} — cannot derive strikes.",
                )
            )
            continue
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
    _today = today or market_today()  # #540: market clock, not the host's local date

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

    price = underlying_price(market_state, playbook.underlying_ticker)
    if price is None:
        return TradeSpecResult(
            hard_blocks=[
                HardBlock(
                    check="UNDERLYING_TELEMETRY",
                    reason=f"No price history for {playbook.underlying_ticker} — cannot derive strikes.",
                )
            ],
            warnings=[],
            spec=None,
        )
    vix = market_state.vix_close or 20.0
    specs = playbook.execution_specs
    exit_rules = playbook.exit_rules
    open_pos = open_positions(positions)

    # ---- Compute expiration ----
    # Scoped event plays (#317) snap around THIS underlying's own dates;
    # market-wide event plays consider global entries but never another
    # underlying's earnings.
    filters = playbook.entry_filters
    if filters.require_scoped_catalyst:
        expiry_catalysts = scoped_catalysts(market_state.catalyst_dates or [], playbook.underlying_ticker)
    else:
        expiry_catalysts = relevant_catalysts(market_state.catalyst_dates or [], playbook.underlying_ticker)
    exp_date, dte = _target_expiration(
        _today,
        specs.target_dte,
        require_after_catalyst=filters.require_catalyst_14dte or filters.require_scoped_catalyst,
        catalyst_dates=expiry_catalysts,
        # Crush plays hug the event; long-vol plays keep time value after it.
        event_buffer_days=6 if filters.require_scoped_catalyst else 14,
    )

    # ---- Derive strikes ---- (price checked above, so this cannot be None)
    strike_params = _derive_strike_params(playbook, market_state)
    assert strike_params is not None
    sigma = strike_params.one_sigma_move or _one_sigma_move(price, vix, dte)

    strike_interval = _STRIKE_INTERVALS.get(playbook.underlying_ticker, 1.0)

    def _otm_strike(delta: float, direction: int) -> float:
        """direction: +1 = call (above price), -1 = put (below price)."""
        if delta >= 0.5:
            return _nearest_strike(price, strike_interval)
        p = delta
        t = math.sqrt(-2 * math.log(p))
        c = [2.515517, 0.802853, 0.010328]
        d = [1.432788, 0.189269, 0.001308]
        z = t - (c[0] + c[1] * t + c[2] * t * t) / (1 + d[0] * t + d[1] * t * t + d[2] * t * t * t)
        otm = abs(z) * sigma
        return _nearest_strike(price + direction * otm, strike_interval)

    exp_str = exp_date.isoformat()

    # Per-strategy leg derivation lives in strategy_builders.py (#149) —
    # adding a strategy is a registry entry there plus a pricing branch,
    # not another elif here. A registry miss is the UNKNOWN_STRATEGY block.
    builder = STRATEGY_BUILDERS.get(playbook.strategy_type)
    if builder is None:
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
    legs, limit_price = builder(
        BuildContext(
            price=price,
            sigma=sigma,
            specs=specs,
            exp_str=exp_str,
            exp_date=exp_date,
            contracts=contracts,
            otm_strike=_otm_strike,
            # Builders round wings on their own interval (usually $1), but
            # never finer than the underlying's actual grid — AAPL's $2.5
            # spacing must survive a builder's interval=1.0 (#317).
            nearest_strike=lambda p, interval=1.0: _nearest_strike(p, max(interval, strike_interval)),
        )
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
        premium_direction=premium_direction,
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

    # Ex-dividend assignment defense (#130): a short call spanning an ex-date
    # on an American-style dividend payer is an early-assignment candidate.
    ex_div_reason = entry_ex_div_block(
        spec.underlying,
        has_short_call=any(leg.action == "SELL" and leg.option_type == "CALL" for leg in spec.legs),
        today=today,
        expiration=date.fromisoformat(spec.expiration_date),
    )
    if ex_div_reason:
        blocks.append(HardBlock(check="EX_DIV_ASSIGNMENT", reason=ex_div_reason))

    # Capital exceeded
    nav = portfolio_config.account.total_nav
    deployed = capital_deployed(open_pos)
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
    bias = DIRECTIONAL_BIAS.get(spec.strategy_type, 0)
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

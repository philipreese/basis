import datetime
import re
from typing import Any

from backend.assignment_defense import ASSIGNMENT_WINDOW_TRADING_DAYS, short_call_assignment_alert
from backend.models import (
    MarketStateSchema,
    PortfolioConfigSchema,
    PositionModel,
    PositionSchema,
    RollCandidateSchema,
    RollLegSchema,
    RollPositionRequest,
)
from backend.pricing import capital_at_risk

# Roll rules (spec/domain-rules.md → Exit rule engine): defensive only,
# net-credit only, max 2 rolls, down-and-out for puts / up-and-out for calls.
ROLLABLE_STRATEGIES = ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD")
MAX_ROLLS = 2
# A roll is considered once the buyback costs ≥150% of the credit collected —
# halfway to the 2× loss limit — or once the 21-DTE time rule is in play.
ROLL_PRESSURE_BUYBACK_RATIO = 1.5
ROLL_DTE_TRIGGER = 21
ROLL_EXTENSION_DAYS = 28  # suggested new expiration: one monthly cycle out


def calculate_dte(expiration_str: str, today: datetime.date) -> int:
    try:
        exp_date = datetime.date.fromisoformat(expiration_str)
        return (exp_date - today).days
    except Exception:
        return 999  # Safe default if parsing fails


def run_lifecycle_scan(
    position: PositionSchema,
    current_regime: str,
    spy_price: float,
    catalyst_dates: list[str],
    today: datetime.date | None = None,
    underlying_prices: dict[str, float] | None = None,
) -> dict[str, Any]:
    """
    Scans a single position to determine its priority level, action recommendation,
    and a mathematical explanation of the status.

    underlying_prices: per-underlying closes for non-SPY-scale positions
    (#139) — feeds the ex-dividend assignment defense for IWM/TLT (#130).
    """
    if today is None:
        today = datetime.date.today()

    # 0. Ex-dividend assignment defense (#130) — checked first: an imminent
    # early assignment would breach the No-Stock Mandate, which outranks
    # every profit/loss consideration.
    und_price = (
        spy_price if position.underlying in ("SPY", "XSP") else (underlying_prices or {}).get(position.underlying)
    )
    assignment_reason = short_call_assignment_alert(
        position.underlying,
        [
            {"direction": g.direction, "option_type": g.option_type, "strike": g.strike, "expiration": g.expiration}
            for g in position.legs
        ],
        und_price,
        today,
    )
    if assignment_reason:
        return {
            "priority": "P1 — CLOSE NOW",
            "action": "CLOSE NOW",
            "reason": assignment_reason,
            "math_detail": f"ITM short call within {ASSIGNMENT_WINDOW_TRADING_DAYS} trading days of an ex-dividend date",
        }

    strategy = position.strategy_type
    premium_dir = position.premium_direction
    entry_prem = position.entry_premium
    curr_val = position.current_value_per_share
    contracts = position.contracts

    # Exit thresholds come from the position's frozen playbook snapshot when
    # present (ADR-0003 — the entry-time rules govern the exit), falling back
    # to the universal defaults. Before #136 these were hardcoded, which made
    # per-playbook exit_rules decorative.
    exit_rules = position.playbook_snapshot.exit_rules if position.playbook_snapshot else None
    profit_take_frac = (exit_rules.profit_take_pct / 100.0) if exit_rules else (0.5 if premium_dir == "CREDIT" else 1.0)
    stop_loss_frac = (exit_rules.stop_loss_pct / 100.0) if exit_rules else (2.0 if premium_dir == "CREDIT" else 0.5)
    exit_dte = exit_rules.mandatory_exit_dte if exit_rules else 21

    # Expiration and DTE
    dte = calculate_dte(position.expiration_date, today)

    # 1. P1 — CLOSE NOW checks
    if premium_dir == "CREDIT":
        # Loss limit: credit trade loss >= stop_loss_pct of premium collected
        # Loss = current_value - entry_premium
        loss_per_share = curr_val - entry_prem
        loss_limit = stop_loss_frac * entry_prem
        if loss_per_share >= loss_limit:
            return {
                "priority": "P1 — CLOSE NOW",
                "action": "CLOSE NOW",
                "reason": f"Loss limit reached: position down ${loss_per_share * 100 * contracts:.2f} against a limit of ${loss_limit * 100 * contracts:.2f}.",
                "math_detail": f"Loss per share ${loss_per_share:.2f} >= {stop_loss_frac:.0%} of premium collected (${loss_limit:.2f})",
            }

        # Profit target: income trade at profit_take_pct of max profit
        # Profit = entry_premium - current_value
        profit_per_share = entry_prem - curr_val
        profit_target = profit_take_frac * entry_prem
        if profit_per_share >= profit_target:
            return {
                "priority": "P1 — CLOSE NOW",
                "action": "CLOSE NOW",
                "reason": f"Profit target reached: income trade profit of ${profit_per_share * 100 * contracts:.2f} meets {profit_take_frac:.0%} threshold of ${profit_target * 100 * contracts:.2f}.",
                "math_detail": f"Profit per share ${profit_per_share:.2f} >= {profit_take_frac:.0%} of entry premium (${profit_target:.2f})",
            }

    elif premium_dir == "DEBIT":
        # Profit target: debit trade at profit_take_pct gain
        # Gain = current_value - entry_premium
        gain_per_share = curr_val - entry_prem
        gain_target = profit_take_frac * entry_prem
        if gain_per_share >= gain_target:
            return {
                "priority": "P1 — CLOSE NOW",
                "action": "CLOSE NOW",
                "reason": f"Profit target reached: debit trade gain of ${gain_per_share * 100 * contracts:.2f} meets {profit_take_frac:.0%} threshold of ${gain_target * 100 * contracts:.2f}.",
                "math_detail": f"Gain per share ${gain_per_share:.2f} >= {profit_take_frac:.0%} of entry premium (${gain_target:.2f})",
            }

        # Loss limit: debit trade loss >= stop_loss_pct of premium paid
        loss_per_share = entry_prem - curr_val
        loss_limit = stop_loss_frac * entry_prem
        if loss_per_share >= loss_limit:
            return {
                "priority": "P1 — CLOSE NOW",
                "action": "CLOSE NOW",
                "reason": f"Loss limit reached: debit trade down ${loss_per_share * 100 * contracts:.2f} against a limit of ${loss_limit * 100 * contracts:.2f}.",
                "math_detail": f"Loss per share ${loss_per_share:.2f} >= {stop_loss_frac:.0%} of premium paid (${loss_limit:.2f})",
            }

    # 2. P2 — CLOSE SOON / REVIEW checks
    # Time rule: DTE <= playbook mandatory_exit_dte (default 21)
    if dte <= exit_dte:
        return {
            "priority": "P2 — CLOSE SOON",
            "action": "Review for potential close",
            "reason": f"Time limit warning: option has {dte} days to expiration (limit is {exit_dte} DTE).",
            "math_detail": f"DTE {dte} <= {exit_dte}",
        }

    # Regime conflict detection
    conflict = False
    conflict_desc = ""

    if current_regime == "TRENDING_BEAR":
        # Bullish positions in falling market
        if strategy in ("BULL_CALL_SPREAD", "BULL_PUT_SPREAD"):
            conflict = True
            conflict_desc = "Bullish vertical spread in trending bear market."
        elif strategy == "LONG_STRADDLE" or strategy == "LONG_STRANGLE":
            # Straddles are volatility trades, usually ok, but long calls are directional
            pass
        # Check if the position has a single long call leg
        elif (
            len(position.legs) == 1 and position.legs[0].option_type == "CALL" and position.legs[0].direction == "LONG"
        ):
            conflict = True
            conflict_desc = "Long call option in trending bear market."

    elif current_regime == "CALM_BULL":
        # Bearish positions in rising market
        if strategy in ("BEAR_PUT_SPREAD", "BEAR_CALL_SPREAD"):
            conflict = True
            conflict_desc = "Bearish vertical spread in calm bull market."
        elif len(position.legs) == 1 and position.legs[0].option_type == "PUT" and position.legs[0].direction == "LONG":
            conflict = True
            conflict_desc = "Long put option in calm bull market."

    elif current_regime == "HIGH_VOL_NEUTRAL":
        # Iron Condor short strikes breached by 2%
        if strategy == "IRON_CONDOR":
            short_put_strike = next(
                (l.strike for l in position.legs if l.option_type == "PUT" and l.direction == "SHORT"),
                None,
            )
            short_call_strike = next(
                (l.strike for l in position.legs if l.option_type == "CALL" and l.direction == "SHORT"),
                None,
            )

            if short_put_strike and short_call_strike:
                breached_put = spy_price <= (short_put_strike * 1.02)
                breached_call = spy_price >= (short_call_strike * 0.98)
                if breached_put:
                    conflict = True
                    conflict_desc = (
                        f"Iron Condor short put strike {short_put_strike} breached within 2% (SPY: {spy_price})."
                    )
                elif breached_call:
                    conflict = True
                    conflict_desc = (
                        f"Iron Condor short call strike {short_call_strike} breached within 2% (SPY: {spy_price})."
                    )

    elif current_regime == "EVENT_CATALYST" and premium_dir == "CREDIT":
        # Short premium position expiring around catalyst date (within 14
        # days). Entries may be prefixed ("FOMC:2026-09-16", #131) — extract
        # the date instead of assuming bare ISO, which silently never matched.
        for cat_str in catalyst_dates:
            try:
                match = re.search(r"\d{4}-\d{2}-\d{2}", cat_str)
                if match is None:
                    continue
                cat_date = datetime.date.fromisoformat(match.group(0))
                exp_date = datetime.date.fromisoformat(position.expiration_date)
                if abs((exp_date - cat_date).days) <= 14:
                    conflict = True
                    conflict_desc = f"Short premium position expiring on {position.expiration_date} is within 14 days of catalyst on {cat_str}."
                    break
            except Exception:
                pass

    if conflict:
        return {
            "priority": "P2 — REVIEW",
            "action": "Review for potential close",
            "reason": f"Regime conflict detected: {conflict_desc}",
            "math_detail": f"Regime: {current_regime}, Strategy: {strategy}",
        }

    # 3. P3 — MONITOR checks
    if premium_dir == "CREDIT":
        # Income trade approaching 35% of max profit
        profit_per_share = entry_prem - curr_val
        p3_target = 0.35 * entry_prem
        if profit_per_share >= p3_target:
            return {
                "priority": "P3 — MONITOR",
                "action": "Monitor position",
                "reason": f"Profit threshold approaching: income trade profit of ${profit_per_share * 100 * contracts:.2f} has reached 35% threshold of ${p3_target * 100 * contracts:.2f}.",
                "math_detail": f"Profit per share ${profit_per_share:.2f} >= 35% of premium collected (${p3_target:.2f})",
            }

    elif premium_dir == "DEBIT":
        # Debit trade approaching 35% loss
        # Loss = entry_premium - current_value
        loss_per_share = entry_prem - curr_val
        p3_target = 0.35 * entry_prem
        if loss_per_share >= p3_target:
            return {
                "priority": "P3 — MONITOR",
                "action": "Monitor position",
                "reason": f"Loss threshold approaching: debit trade loss of ${loss_per_share * 100 * contracts:.2f} has reached 35% threshold of ${p3_target * 100 * contracts:.2f}.",
                "math_detail": f"Loss per share ${loss_per_share:.2f} >= 35% of premium paid (${p3_target:.2f})",
            }

    # 4. OK
    return {
        "priority": "OK",
        "action": "Hold",
        "reason": "Position parameters within safe bounds.",
        "math_detail": f"DTE: {dte}, Price: ${curr_val:.2f} / share",
    }


def derive_roll_candidate(position: PositionSchema, today: datetime.date | None = None) -> RollCandidateSchema | None:
    """Assess whether a position is a defensive-roll candidate (domain-rules.md).

    Returns None for positions that are not roll instruments (debit structures,
    condors, closed positions) or not under pressure — rolling is defensive,
    never routine. Returns an ineligible candidate with the reason when the
    roll cap forces an exit instead.
    """
    today = today or datetime.date.today()
    if position.status != "OPEN" or position.premium_direction != "CREDIT":
        return None
    if position.strategy_type not in ROLLABLE_STRATEGIES:
        return None

    dte = calculate_dte(position.expiration_date, today)
    buyback_ratio = position.current_value_per_share / position.entry_premium if position.entry_premium > 0 else 0.0
    under_time_pressure = dte <= ROLL_DTE_TRIGGER
    under_loss_pressure = buyback_ratio >= ROLL_PRESSURE_BUYBACK_RATIO
    if not (under_time_pressure or under_loss_pressure):
        return None

    trigger = (
        f"buyback at {buyback_ratio:.0%} of credit collected"
        if under_loss_pressure
        else f"{dte} DTE reaches the 21-DTE time rule"
    )

    if position.rolls >= MAX_ROLLS:
        return RollCandidateSchema(
            eligible=False,
            reason=f"ROLL_CAP_REACHED: {position.rolls} rolls used — forced exit, no exceptions ({trigger})",
            rolls_used=position.rolls,
            rolls_max=MAX_ROLLS,
        )

    # Down-and-out for puts, up-and-out for calls: shift every leg by the
    # spread width (preserving the width) and extend one monthly cycle.
    strikes = [leg.strike for leg in position.legs]
    width = abs(max(strikes) - min(strikes))
    shift = -width if position.strategy_type == "BULL_PUT_SPREAD" else width
    try:
        new_expiration = (
            datetime.date.fromisoformat(position.expiration_date) + datetime.timedelta(days=ROLL_EXTENSION_DAYS)
        ).isoformat()
    except ValueError:
        return None
    suggested = [
        RollLegSchema(
            option_type=leg.option_type,
            direction=leg.direction,
            strike=leg.strike + shift,
            expiration=new_expiration,
        )
        for leg in position.legs
    ]
    return RollCandidateSchema(
        eligible=True,
        reason=f"Defensive roll available: {trigger}. Net credit required — take the loss if the roll needs a debit.",
        rolls_used=position.rolls,
        rolls_max=MAX_ROLLS,
        suggested_expiration=new_expiration,
        suggested_legs=suggested,
    )


def aggregate_portfolio_greeks(positions: list[PositionSchema]) -> dict[str, float]:
    """
    Computes Net Delta, Net Theta, Net Vega, and Net Gamma.
    Greeks contribution: direction_multiplier * leg_greek * contracts
    Note: Direction multiplier is +1 for LONG, -1 for SHORT.
    """
    net_delta = 0.0
    net_theta = 0.0
    net_vega = 0.0
    net_gamma = 0.0

    for pos in positions:
        if pos.status != "OPEN":
            continue
        contracts = pos.contracts
        for leg in pos.legs:
            mult = 1.0 if leg.direction == "LONG" else -1.0
            net_delta += mult * leg.delta * contracts
            net_theta += mult * leg.theta * contracts
            net_vega += mult * leg.vega * contracts
            net_gamma += mult * leg.gamma * contracts

    return {
        "net_delta": round(net_delta, 4),
        "net_theta": round(net_theta, 4),
        "net_vega": round(net_vega, 4),
        "net_gamma": round(net_gamma, 4),
    }


CORRELATED_INDICES = {"SPY", "QQQ", "IWM", "DIA", "SPX", "NDX", "RUT"}


def run_exposure_safeguards(positions: list[PositionSchema], config: PortfolioConfigSchema) -> list[dict[str, Any]]:
    """
    Checks the portfolio configuration limits against current open positions.
    Returns a list of safeguard violations.
    """
    warnings = []
    total_nav = config.account.total_nav
    open_positions = [p for p in positions if p.status == "OPEN"]

    # Position Count check
    max_positions = config.risk_profile.max_simultaneous_positions
    if len(open_positions) >= max_positions:
        warnings.append(
            {
                "type": "POSITION_COUNT",
                "severity": "WARNING",
                "message": f"Position limit reached or exceeded: {len(open_positions)} open positions against a limit of {max_positions}.",
            }
        )

    # Capital Deployed checks
    total_capital_deployed = 0.0
    underlying_capital: dict[str, float] = {}
    correlated_index_capital = 0.0

    for pos in open_positions:
        capital = capital_at_risk(pos.max_loss, pos.contracts)
        total_capital_deployed += capital

        underlying = pos.underlying.upper()
        underlying_capital[underlying] = underlying_capital.get(underlying, 0.0) + capital

        if underlying in CORRELATED_INDICES:
            correlated_index_capital += capital

    # Total Capital Deployed check
    capital_pct = (total_capital_deployed / total_nav) * 100 if total_nav > 0 else 0
    max_capital_pct = config.risk_profile.max_capital_deployed_pct
    if capital_pct >= max_capital_pct:
        warnings.append(
            {
                "type": "CAPITAL_DEPLOYED",
                "severity": "WARNING",
                "message": f"Capital deployment warning: {capital_pct:.1f}% of NAV deployed (${total_capital_deployed:.2f}) against a limit of {max_capital_pct:.1f}% (${max_capital_pct / 100 * total_nav:.2f}).",
            }
        )

    # Concentration check
    max_conc_pct = config.risk_profile.max_underlying_concentration_pct
    for und, cap in underlying_capital.items():
        conc_pct = (cap / total_nav) * 100 if total_nav > 0 else 0
        if conc_pct > max_conc_pct:
            warnings.append(
                {
                    "type": "UNDERLYING_CONCENTRATION",
                    "severity": "WARNING",
                    "message": f"Concentration warning: underlying {und} comprises {conc_pct:.1f}% of NAV (${cap:.2f}) which exceeds the limit of {max_conc_pct:.1f}% (${max_conc_pct / 100 * total_nav:.2f}).",
                }
            )

    # Correlated Index check
    max_idx_pct = config.risk_profile.max_correlated_index_pct
    index_pct = (correlated_index_capital / total_nav) * 100 if total_nav > 0 else 0
    if index_pct > max_idx_pct:
        warnings.append(
            {
                "type": "CORRELATED_INDEX_CONCENTRATION",
                "severity": "WARNING",
                "message": f"Correlated index concentration warning: index exposure comprises {index_pct:.1f}% of NAV (${correlated_index_capital:.2f}) which exceeds the limit of {max_idx_pct:.1f}% (${max_idx_pct / 100 * total_nav:.2f}).",
            }
        )

    return warnings


def compose_observation(
    config: PortfolioConfigSchema,
    positions: list[PositionSchema],
    state: MarketStateSchema,
) -> dict[str, Any]:
    """The portfolio-observation payload (#179): lifecycle scan per open
    position, aggregate greeks, exposure safeguards, and greek-limit checks.
    Pure composition over already-loaded data — the route loads and returns."""
    open_positions = [p for p in positions if p.status == "OPEN"]
    scanned_positions = []
    for pos in open_positions:
        scan_res = run_lifecycle_scan(
            pos,
            current_regime=state.current_regime,
            spy_price=state.spy_price,
            catalyst_dates=state.catalyst_dates,
        )
        scanned_positions.append(
            {
                "position_id": pos.id,
                "underlying": pos.underlying,
                "strategy_type": pos.strategy_type,
                "contracts": pos.contracts,
                "max_loss": pos.max_loss,
                "max_profit": pos.max_profit,
                "entry_premium": pos.entry_premium,
                "current_value_per_share": pos.current_value_per_share,
                "expiration_date": pos.expiration_date,
                "priority": scan_res["priority"],
                "action": scan_res["action"],
                "reason": scan_res["reason"],
                "math_detail": scan_res["math_detail"],
                "legs": [leg.model_dump() for leg in pos.legs],
                "roll": derive_roll_candidate(pos),
            }
        )

    greeks = aggregate_portfolio_greeks(positions)
    safeguards = run_exposure_safeguards(positions, config)
    limits = config.portfolio_greek_limits
    greek_warnings = []
    for greek, limit in (
        ("delta", limits.max_net_delta),
        ("vega", limits.max_net_vega),
        ("gamma", limits.max_net_gamma),
    ):
        value = abs(greeks[f"net_{greek}"])
        if value > limit:
            greek_warnings.append(
                {
                    "type": f"GREEK_LIMIT_{greek.upper()}",
                    "severity": "CRITICAL",
                    "message": f"Portfolio Net {greek.capitalize()} limit exceeded: absolute value {value} exceeds limit of {limit}.",
                }
            )

    return {
        "scanned_positions": scanned_positions,
        "greeks": greeks,
        "safeguards": safeguards + greek_warnings,
        "market_state": state,
    }


class RollError(ValueError):
    """A roll request violating the roll rules; str(err) is the API detail."""


def apply_roll(position: PositionModel, req: RollPositionRequest, today: datetime.date | None = None) -> None:
    """Validate and apply a defensive roll to an OPEN credit vertical
    (domain-rules.md): net-credit only, max 2 rolls, down-and-out for puts /
    up-and-out for calls. Mutates the position row in place; raises RollError
    (its message is the transport-facing detail) instead of touching HTTP."""
    today = today or datetime.date.today()
    if position.status != "OPEN":
        raise RollError("Position is not OPEN")
    if position.premium_direction != "CREDIT" or position.strategy_type not in ROLLABLE_STRATEGIES:
        raise RollError("NOT_ROLLABLE: only credit verticals roll; close instead")
    if position.rolls >= MAX_ROLLS:
        raise RollError(f"ROLL_CAP_REACHED: {position.rolls} rolls used — forced exit, no exceptions")

    net_credit = round(req.new_credit_per_share - req.close_cost_per_share, 4)
    if net_credit <= 0:
        raise RollError(f"DEBIT_ROLL_BLOCKED: roll nets {net_credit:+.2f}/share — take the loss instead")

    if req.new_expiration <= position.expiration_date:
        raise RollError("ROLL_DIRECTION: new expiration must be later (ISO dates)")
    try:
        datetime.date.fromisoformat(req.new_expiration)
    except ValueError as exc:
        raise RollError("ROLL_DIRECTION: new expiration must be an ISO date") from exc

    old_strikes = sorted(leg["strike"] for leg in position.legs)
    new_strikes = sorted(leg.strike for leg in req.new_legs)
    if position.strategy_type == "BULL_PUT_SPREAD":
        if not all(n < o for n, o in zip(new_strikes, old_strikes, strict=True)):
            raise RollError("ROLL_DIRECTION: puts roll DOWN — every new strike must be lower")
        if any(leg.option_type != "PUT" for leg in req.new_legs):
            raise RollError("ROLL_DIRECTION: a put spread rolls into puts")
    else:
        if not all(n > o for n, o in zip(new_strikes, old_strikes, strict=True)):
            raise RollError("ROLL_DIRECTION: calls roll UP — every new strike must be higher")
        if any(leg.option_type != "CALL" for leg in req.new_legs):
            raise RollError("ROLL_DIRECTION: a call spread rolls into calls")

    # Apply: same position row continues (the rolls counter lives on it).
    # entry_premium becomes the CUMULATIVE net credit collected per share, so
    # the 50%-profit / 2×-loss exit rules keep operating on real economics.
    cumulative_credit = round(position.entry_premium + net_credit, 4)
    width = round(abs(new_strikes[-1] - new_strikes[0]), 4)
    position.legs = [
        {
            "option_type": leg.option_type,
            "direction": leg.direction,
            "strike": leg.strike,
            "expiration": leg.expiration,
            "delta": 0.0,
            "theta": 0.0,
            "vega": 0.0,
            "gamma": 0.0,
        }
        for leg in req.new_legs
    ]
    position.expiration_date = req.new_expiration
    position.entry_premium = cumulative_credit
    position.current_value_per_share = req.new_credit_per_share
    position.max_profit = cumulative_credit
    position.max_loss = round(max(width - cumulative_credit, 0.01), 4)
    position.rolls += 1
    position.notes = (
        f"{position.notes}\nRolled {today.isoformat()}: closed @{req.close_cost_per_share:.2f}, "
        f"reopened @{req.new_credit_per_share:.2f} (net +{net_credit:.2f}), roll {position.rolls}/{MAX_ROLLS}"
    ).strip()

"""seeds.py — seed data and the experiment-matrix book allocation (#149).

Pure data, split out of database.py so schema/init logic stays readable.
SEED_POSITIONS is test-fixture-only (#53); everything else is what a fresh
database gets on first start. database.py re-exports these names, so
existing imports keep working.
"""

import hashlib
import json

# Seed Data from Section 9
SEED_PORTFOLIO_CONFIG = {
    "account": {
        "total_nav": 10000.0,
        "broker": "Charles Schwab",
        "account_type": "Roth IRA",
        "options_approval": "Level 3 — Spreads",
        "execution_mode": "PAPER",
    },
    "risk_profile": {
        "max_trade_risk_pct": 15.0,
        "max_trade_risk_dollars": 1500.0,
        "max_underlying_concentration_pct": 35.0,
        "max_correlated_index_pct": 50.0,
        "minimum_cash_reserve_pct": 15.0,
        "max_simultaneous_positions": 3,
        "max_capital_deployed_pct": 85.0,
    },
    "portfolio_greek_limits": {
        "max_net_delta": 50.0,
        "max_net_vega": 100.0,
        "max_net_gamma": 10.0,
    },
}

SEED_PLAYBOOKS = [
    {
        "id": "spy_iron_condor_v1",
        "version": "1.0",
        "name": "SPY Iron Condor — High-Vol Neutral",
        "underlying_ticker": "SPY",
        "strategy_type": "IRON_CONDOR",
        "execution_mode": "PAPER",
        "entry_filters": {
            "min_ivr": 50.0,
            "max_ivr": 100.0,
            "vix_range": [15.0, 35.0],
            "required_trend": "ANY",
            "block_catalyst_14dte": True,
            "require_catalyst_14dte": False,
        },
        "execution_specs": {
            "target_dte": 38,
            "short_leg_delta": 0.16,
            "long_leg_delta": 0.05,
            # $3 wings keep max loss under the ADR-0006 2.5%/trade cap (#94)
            "spread_width_dollars": 3.0,
            "straddle_atm": False,
        },
        "exit_rules": {
            "profit_take_pct": 50.0,
            "stop_loss_pct": 200.0,
            "mandatory_exit_dte": 21,
            "catalyst_exit_days_after": 5,
        },
    },
    {
        "id": "spy_broken_wing_butterfly_v1",
        "version": "1.0",
        "name": "SPY Broken-Wing Butterfly — Income",
        "underlying_ticker": "SPY",
        "strategy_type": "BROKEN_WING_BUTTERFLY",
        "execution_mode": "PAPER",
        # Ships disabled (#132): the BWB races ONLY in book B18, which
        # whitelists it and re-enables via playbook_overrides — keeping it
        # out of every other book's mix (one question per book, ADR-0009).
        "enabled": False,
        "entry_filters": {
            "min_ivr": 40.0,
            "max_ivr": 100.0,
            "vix_range": [15.0, 35.0],
            "required_trend": "ANY",
            "block_catalyst_14dte": True,
            "require_catalyst_14dte": False,
        },
        "execution_specs": {
            "target_dte": 38,
            "short_leg_delta": 0.30,
            "long_leg_delta": 0.05,
            # Narrow wing $3; the skip-strike lower wing is 2× ($6) — max
            # loss (wide − narrow − credit) stays under the 2.5%/trade cap
            "spread_width_dollars": 3.0,
            "straddle_atm": False,
        },
        "exit_rules": {
            "profit_take_pct": 50.0,
            "stop_loss_pct": 200.0,
            "mandatory_exit_dte": 21,
            "catalyst_exit_days_after": 5,
        },
    },
    {
        "id": "spy_calendar_spread_v1",
        "version": "1.0",
        "name": "SPY Calendar Spread — Long Vega",
        "underlying_ticker": "SPY",
        "strategy_type": "CALENDAR_SPREAD",
        "execution_mode": "PAPER",
        # Ships disabled (#133): races ONLY in book B21 (XSP — the short
        # front leg is cash-settled), which whitelists and re-enables it.
        "enabled": False,
        "entry_filters": {
            # Calendars buy vega — enter when IV is CHEAP, opposite of the
            # income playbooks. max_ivr is the load-bearing filter here.
            "min_ivr": 0.0,
            "max_ivr": 50.0,
            "vix_range": [10.0, 25.0],
            "required_trend": "ANY",
            "block_catalyst_14dte": True,
            "require_catalyst_14dte": False,
        },
        "execution_specs": {
            # target_dte is the FRONT leg; the back leg sits one monthly
            # cycle behind it (CALENDAR_BACK_LEG_DAYS in strategy_builders).
            "target_dte": 30,
            "short_leg_delta": 0.5,
            "long_leg_delta": 0.5,
            "spread_width_dollars": 0.0,  # unused: both legs share one strike
            "straddle_atm": False,
        },
        "exit_rules": {
            # Debit rules: take 30% of the debit as profit, stop at 50% loss,
            # and always exit before the front leg's final week.
            "profit_take_pct": 30.0,
            "stop_loss_pct": 50.0,
            "mandatory_exit_dte": 7,
            "catalyst_exit_days_after": 5,
        },
    },
    {
        "id": "spy_bull_call_spread_v1",
        "version": "1.0",
        "name": "SPY Bull Call Spread — Calm Bull",
        "underlying_ticker": "SPY",
        "strategy_type": "BULL_CALL_SPREAD",
        "execution_mode": "PAPER",
        "entry_filters": {
            "min_ivr": 20.0,
            "max_ivr": 60.0,
            "vix_range": [10.0, 25.0],
            "required_trend": "ABOVE_SMA20",
            "block_catalyst_14dte": True,
            "require_catalyst_14dte": False,
        },
        "execution_specs": {
            "target_dte": 45,
            "short_leg_delta": 0.25,
            "long_leg_delta": 0.50,
            # $5 wide caps the debit near the per-trade limit (#94)
            "spread_width_dollars": 5.0,
            "straddle_atm": False,
        },
        "exit_rules": {
            "profit_take_pct": 100.0,
            "stop_loss_pct": 50.0,
            "mandatory_exit_dte": 21,
            "catalyst_exit_days_after": 5,
        },
    },
    {
        "id": "spy_bear_put_spread_v1",
        "version": "1.0",
        "name": "SPY Bear Put Spread — Trending Bear",
        "underlying_ticker": "SPY",
        "strategy_type": "BEAR_PUT_SPREAD",
        "execution_mode": "PAPER",
        "entry_filters": {
            "min_ivr": 20.0,
            "max_ivr": 70.0,
            "vix_range": [15.0, 40.0],
            "required_trend": "BELOW_SMA20",
            "block_catalyst_14dte": True,
            "require_catalyst_14dte": False,
        },
        "execution_specs": {
            "target_dte": 45,
            "short_leg_delta": 0.25,
            "long_leg_delta": 0.50,
            # $5 wide caps the debit near the per-trade limit (#94)
            "spread_width_dollars": 5.0,
            "straddle_atm": False,
        },
        "exit_rules": {
            "profit_take_pct": 100.0,
            "stop_loss_pct": 50.0,
            "mandatory_exit_dte": 21,
            "catalyst_exit_days_after": 5,
        },
    },
    {
        "id": "spy_bull_put_spread_v1",
        "version": "1.0",
        "name": "SPY Bull Put Spread — Calm Bull Income",
        "underlying_ticker": "SPY",
        "strategy_type": "BULL_PUT_SPREAD",
        "execution_mode": "PAPER",
        "enabled": True,
        "entry_filters": {
            "min_ivr": 20.0,
            "max_ivr": 100.0,
            "vix_range": [10.0, 30.0],
            "required_trend": "ABOVE_SMA20",
            "block_catalyst_14dte": True,
            "require_catalyst_14dte": False,
        },
        "execution_specs": {
            "target_dte": 38,
            "short_leg_delta": 0.30,
            "long_leg_delta": 0.10,
            # $3 wings keep max loss under the ADR-0006 2.5%/trade cap (#94)
            "spread_width_dollars": 3.0,
            "straddle_atm": False,
        },
        "exit_rules": {
            "profit_take_pct": 50.0,
            "stop_loss_pct": 200.0,
            "mandatory_exit_dte": 21,
            "catalyst_exit_days_after": 5,
        },
    },
    {
        "id": "spy_bear_call_spread_v1",
        "version": "1.0",
        "name": "SPY Bear Call Spread — Trending Bear Income",
        "underlying_ticker": "SPY",
        "strategy_type": "BEAR_CALL_SPREAD",
        "execution_mode": "PAPER",
        "enabled": True,
        "entry_filters": {
            "min_ivr": 25.0,
            "max_ivr": 100.0,
            "vix_range": [15.0, 45.0],
            "required_trend": "BELOW_SMA20",
            "block_catalyst_14dte": True,
            "require_catalyst_14dte": False,
        },
        "execution_specs": {
            "target_dte": 38,
            "short_leg_delta": 0.30,
            "long_leg_delta": 0.10,
            # $3 wings keep max loss under the ADR-0006 2.5%/trade cap (#94)
            "spread_width_dollars": 3.0,
            "straddle_atm": False,
        },
        "exit_rules": {
            "profit_take_pct": 50.0,
            "stop_loss_pct": 200.0,
            "mandatory_exit_dte": 21,
            "catalyst_exit_days_after": 5,
        },
    },
    {
        "id": "spy_long_straddle_v1",
        "version": "1.0",
        "name": "SPY Long Straddle — Event Catalyst",
        "underlying_ticker": "SPY",
        "strategy_type": "LONG_STRADDLE",
        "execution_mode": "PAPER",
        # Disabled by default: buying vol into known catalysts fights pre-event
        # IV inflation and post-event crush. Kept for catalyst-study use only.
        "enabled": False,
        "entry_filters": {
            "min_ivr": 30.0,
            "max_ivr": 100.0,
            "vix_range": [0.0, 100.0],
            "required_trend": "ANY",
            "block_catalyst_14dte": False,
            "require_catalyst_14dte": True,
        },
        "execution_specs": {
            "target_dte": 38,
            "short_leg_delta": 0.50,
            "long_leg_delta": 0.50,
            "spread_width_dollars": 0.0,
            "straddle_atm": True,
        },
        "exit_rules": {
            "profit_take_pct": 100.0,
            "stop_loss_pct": 50.0,
            "mandatory_exit_dte": 21,
            "catalyst_exit_days_after": 5,
        },
    },
    {
        "id": "spy_long_strangle_v1",
        "version": "1.0",
        "name": "SPY Long Strangle — Event Catalyst (OTM)",
        "underlying_ticker": "SPY",
        "strategy_type": "LONG_STRANGLE",
        "execution_mode": "PAPER",
        # Disabled by default — same rationale as the long straddle above.
        "enabled": False,
        "entry_filters": {
            "min_ivr": 30.0,
            "max_ivr": 100.0,
            "vix_range": [15.0, 100.0],
            "required_trend": "ANY",
            "block_catalyst_14dte": False,
            "require_catalyst_14dte": True,
        },
        "execution_specs": {
            "target_dte": 38,
            "short_leg_delta": 0.25,
            "long_leg_delta": 0.25,
            "spread_width_dollars": 0.0,
            "straddle_atm": False,
        },
        "exit_rules": {
            "profit_take_pct": 100.0,
            "stop_loss_pct": 50.0,
            "mandatory_exit_dte": 21,
            "catalyst_exit_days_after": 5,
        },
    },
]

# Test-fixture data only — NOT seeded into real databases (#53). These June/July
# 2026 demo straddles are long expired; test fixtures import them to build
# in-memory databases with known positions.
SEED_POSITIONS = [
    {
        "id": "seed_pos_spy_straddle_jun18",
        "underlying": "SPY",
        "strategy_type": "LONG_STRADDLE",
        "execution_mode": "PAPER",
        "legs": [
            {
                "option_type": "CALL",
                "direction": "LONG",
                "strike": 759.0,
                "expiration": "2026-06-18",
                "delta": 0.5,
                "theta": -0.1,
                "vega": 0.2,
                "gamma": 0.05,
            },
            {
                "option_type": "PUT",
                "direction": "LONG",
                "strike": 759.0,
                "expiration": "2026-06-18",
                "delta": -0.5,
                "theta": -0.1,
                "vega": 0.2,
                "gamma": 0.05,
            },
        ],
        "entry_date": "2026-06-07",
        "expiration_date": "2026-06-18",
        "contracts": 1,
        "premium_direction": "DEBIT",
        "entry_premium": 16.61,
        "current_value_per_share": 16.61,
        "max_profit": 999999.0,
        "max_loss": 16.61,
        "profit_target_per_share": 33.22,
        "loss_limit_per_share": 8.31,
        "notes": "Learning exercise. Expiration BEFORE SpaceX IPO date. Treat as short-term straddle mechanics study. Do not extend or roll.",
        "rolls": 0,
        "status": "OPEN",
        "journal": {
            "core_thesis_rationale": "Short-term volatility study around SpaceX roadshow June 8. Not the primary IPO thesis trade.",
            "structural_invalidation": "SPY remains pinned within 1% of 759 through June 15.",
            "expected_underlying_move_pct": 2.2,
            "pre_trade_emotional_state": "Calm",
            "pre_trade_confidence_rating": 3,
        },
    },
    {
        "id": "seed_pos_spy_straddle_jul18",
        "underlying": "SPY",
        "strategy_type": "LONG_STRADDLE",
        "execution_mode": "PAPER",
        "legs": [
            {
                "option_type": "CALL",
                "direction": "LONG",
                "strike": 757.0,
                "expiration": "2026-07-18",
                "delta": 0.5,
                "theta": -0.05,
                "vega": 0.3,
                "gamma": 0.03,
            },
            {
                "option_type": "PUT",
                "direction": "LONG",
                "strike": 757.0,
                "expiration": "2026-07-18",
                "delta": -0.5,
                "theta": -0.05,
                "vega": 0.3,
                "gamma": 0.03,
            },
        ],
        "entry_date": "2026-06-07",
        "expiration_date": "2026-07-18",
        "contracts": 1,
        "premium_direction": "DEBIT",
        "entry_premium": 28.18,
        "current_value_per_share": 28.18,
        "max_profit": 999999.0,
        "max_loss": 28.18,
        "profit_target_per_share": 56.36,
        "loss_limit_per_share": 14.09,
        "break_even_upside": 785.18,
        "break_even_downside": 728.82,
        "notes": "Primary SpaceX IPO thesis trade. Roadshow June 8. IPO target late June. Close within 5 trading days after IPO fires regardless of profit target. Do not hold through IV crush.",
        "rolls": 0,
        "status": "OPEN",
        "journal": {
            "core_thesis_rationale": "Largest IPO in history creates market volatility regardless of direction. Vol expansion expected across roadshow and IPO window.",
            "structural_invalidation": "Implied volatility collapses before IPO date or SPY remains pinned through late June.",
            "expected_underlying_move_pct": 2.2,
            "pre_trade_emotional_state": "Calm",
            "pre_trade_confidence_rating": 4,
        },
    },
]


# The experiment matrix (ADR-0009, #136): every book asks ONE question
# against the shared baseline B01 (V0/XSP). B12 and B16 are controls — they
# exist to measure whether the regime and IVR gates earn their keep. B09/B10
# (IWM/GLD, #139) trade off per-underlying index_history telemetry;
# B18–B22 (BWB, V3, calendars, TLT) land with their own PRs.
LAB_BOOKS: list[dict] = [
    {"id": "B01", "name": "V0 on XSP", "config": {"engine_variant": "V0", "underlying": "XSP", "envelope": {}}},
    {"id": "B02", "name": "V1 on XSP", "config": {"engine_variant": "V1", "underlying": "XSP", "envelope": {}}},
    {"id": "B03", "name": "V2 on XSP", "config": {"engine_variant": "V2", "underlying": "XSP", "envelope": {}}},
    {"id": "B04", "name": "V0 on SPY", "config": {"engine_variant": "V0", "underlying": "SPY", "envelope": {}}},
    {"id": "B05", "name": "V1 on SPY", "config": {"engine_variant": "V1", "underlying": "SPY", "envelope": {}}},
    {"id": "B06", "name": "V2 on SPY", "config": {"engine_variant": "V2", "underlying": "SPY", "envelope": {}}},
    {
        "id": "B07",
        "name": "Short-DTE on XSP",
        "config": {
            "engine_variant": "V0",
            "underlying": "XSP",
            "envelope": {},
            "playbook_overrides": {"execution_specs.target_dte": 24},
        },
    },
    {
        "id": "B08",
        "name": "Short-DTE on SPY",
        "config": {
            "engine_variant": "V0",
            "underlying": "SPY",
            "envelope": {},
            "playbook_overrides": {"execution_specs.target_dte": 24},
        },
    },
    {
        "id": "B09",
        "name": "V0 on IWM",
        # Small-cap diversification (#139): IWM telemetry (price/SMA20) and
        # RV-rank pseudo-IVR come from index_history; regime gate stays on.
        "config": {"engine_variant": "V0", "underlying": "IWM", "envelope": {}},
    },
    {
        "id": "B10",
        "name": "GLD RV-gated",
        # Gold doesn't follow SPY-derived regimes — the RV-rank IVR gate and
        # entry filters are its selection discipline (#139).
        "config": {"engine_variant": "V0", "underlying": "GLD", "envelope": {}, "ignore_regime": True},
    },
    {
        "id": "B11",
        "name": "Condors only on XSP",
        "config": {
            "engine_variant": "V0",
            "underlying": "XSP",
            "envelope": {},
            "playbook_ids": ["spy_iron_condor_v1"],
        },
    },
    {
        "id": "B12",
        "name": "No regime gate on XSP (control)",
        "config": {"engine_variant": "V0", "underlying": "XSP", "envelope": {}, "ignore_regime": True},
    },
    {
        "id": "B13",
        "name": "$5 wings on XSP",
        "config": {
            "engine_variant": "V0",
            "underlying": "XSP",
            # $5-wide credit spreads risk ~$350-400/lot — impossible under the
            # default $250 cap (the book was a dead arm, #218). The raised cap
            # is a DELIBERATE CONFOUND: the question is "wider wings with the
            # risk budget they require", the only askable version.
            "envelope": {"max_loss_pct_per_trade": 4.5},
            "playbook_overrides": {"execution_specs.spread_width_dollars": 5.0},
        },
    },
    {
        "id": "B14",
        "name": "15-delta shorts on XSP",
        "config": {
            "engine_variant": "V0",
            "underlying": "XSP",
            "envelope": {},
            "playbook_overrides": {"execution_specs.short_leg_delta": 0.15},
        },
    },
    {
        "id": "B15",
        "name": "25% profit take on XSP",
        "config": {
            "engine_variant": "V0",
            "underlying": "XSP",
            "envelope": {},
            "playbook_overrides": {"exit_rules.profit_take_pct": 25.0},
        },
    },
    {
        "id": "B16",
        "name": "No IVR gate on XSP (control)",
        "config": {
            "engine_variant": "V0",
            "underlying": "XSP",
            "envelope": {},
            "ignore_ivr": True,
            "playbook_overrides": {"entry_filters.min_ivr": 0.0},
        },
    },
    {
        "id": "B18",
        "name": "Broken-wing butterfly on XSP",
        # The BWB arm (#132): whitelists the (globally disabled) BWB playbook
        # and re-enables it for this book only.
        "config": {
            "engine_variant": "V0",
            "underlying": "XSP",
            "envelope": {},
            "playbook_ids": ["spy_broken_wing_butterfly_v1"],
            "playbook_overrides": {"enabled": True},
        },
    },
    {
        "id": "B21",
        "name": "Calendar spreads on XSP",
        # The calendar arm (#133): whitelists the (globally disabled)
        # calendar playbook and re-enables it for this book only. An ATM XSP
        # calendar debit runs ~$300, so the per-trade cap rises to 4% —
        # still a tiny dollar risk against the $10K basis, and part of this
        # arm's config_hash fingerprint.
        "config": {
            "engine_variant": "V0",
            "underlying": "XSP",
            "envelope": {"max_loss_pct_per_trade": 4.0},
            "playbook_ids": ["spy_calendar_spread_v1"],
            "playbook_overrides": {"enabled": True},
        },
    },
    {
        "id": "B19",
        "name": "V3 on XSP",
        # Repaired-matrix regime engine (#134): same weights, fixed dimensions.
        "config": {"engine_variant": "V3", "underlying": "XSP", "envelope": {}},
    },
    {
        "id": "B20",
        "name": "V3 on SPY",
        "config": {"engine_variant": "V3", "underlying": "SPY", "envelope": {}},
    },
    {
        "id": "B22",
        "name": "TLT RV-gated",
        # Rate-vol diversifier (#135): SPY-derived regimes are blind to
        # bonds, so the RV-rank pseudo-IVR gate and entry filters are the
        # selection discipline (GLD pattern). TLT pays MONTHLY dividends —
        # every ~38-DTE window spans an ex-date, so the #130 defense keeps
        # this book put-side by construction.
        "config": {"engine_variant": "V0", "underlying": "TLT", "envelope": {}, "ignore_regime": True},
    },
    {
        "id": "B17",
        "name": "Hold to 7 DTE on XSP",
        # Safe ONLY on cash-settled XSP — holding SPY spreads near expiry
        # invites assignment into shares (No-Stock Mandate).
        "config": {
            "engine_variant": "V0",
            "underlying": "XSP",
            "envelope": {},
            "playbook_overrides": {"exit_rules.mandatory_exit_dte": 7},
        },
    },
    # Sweep completion (#219): 3 points per knob dimension so verdicts have a
    # direction (monotonicity), not just a pairwise difference. B23/B24 are
    # credit-spreads-only so the delta answer isn't muddled across playbooks
    # the way mix-wide B14's is (0.30→0.15 for spreads, 0.16→0.15 for condor).
    {
        "id": "B23",
        "name": "20-delta shorts, spreads only",
        "config": {
            "engine_variant": "V0",
            "underlying": "XSP",
            "envelope": {},
            "playbook_ids": ["spy_bull_put_spread_v1", "spy_bear_call_spread_v1"],
            "playbook_overrides": {"execution_specs.short_leg_delta": 0.20},
        },
    },
    {
        "id": "B24",
        "name": "40-delta shorts, spreads only",
        "config": {
            "engine_variant": "V0",
            "underlying": "XSP",
            "envelope": {},
            "playbook_ids": ["spy_bull_put_spread_v1", "spy_bear_call_spread_v1"],
            "playbook_overrides": {"execution_specs.short_leg_delta": 0.40},
        },
    },
    {
        "id": "B25",
        "name": "52-DTE on XSP",
        "config": {
            "engine_variant": "V0",
            "underlying": "XSP",
            "envelope": {},
            "playbook_overrides": {"execution_specs.target_dte": 52},
        },
    },
    {
        "id": "B26",
        "name": "75% profit take on XSP",
        "config": {
            "engine_variant": "V0",
            "underlying": "XSP",
            "envelope": {},
            "playbook_overrides": {"exit_rules.profit_take_pct": 75.0},
        },
    },
    {
        "id": "B27",
        "name": "$2 wings on XSP",
        "config": {
            "engine_variant": "V0",
            "underlying": "XSP",
            "envelope": {},
            "playbook_overrides": {"execution_specs.spread_width_dollars": 2.0},
        },
    },
    {
        "id": "B28",
        "name": "Regime-flip exit on XSP",
        # The exit-side question no entry gate can ask (#254): is closing
        # when the regime leaves the entry state better than riding to the
        # playbook exits?
        "config": {
            "engine_variant": "V0",
            "underlying": "XSP",
            "envelope": {},
            "exit_on_regime_flip": True,
        },
    },
]


def _config_hash(config: dict) -> str:
    """Stable fingerprint of a book config — the Live Gate attaches to
    (book_id, config_hash), the multi-book extension of ADR-0003."""
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:16]

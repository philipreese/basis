# OPTIONS PLAYBOOK AUTOMATION ENGINE & RESEARCH RUNTIME (V8.0)
## Codified Playbook Injections, Snapshot Immutability, and Multi-Benchmark Analytics
### Master Build Specification — Frozen for Construction — June 2026

---

## 1. Purpose and Scope

This document specifies the technical requirements for a daily decision-support web application and automated playbook execution engine tailored for a cash-settled Roth IRA account (no margin availability, no short stock).

### 1.1 Structural System Mandates

- **Prescriptive Automation via Playbooks:** The automation engine is strictly attached to codified user playbooks, not market predictions. The system automatically identifies when active market telemetry satisfies entry rules, instantly auto-generating matching order ticket specifications.
- **No Underlying Share Ownership:** The codebase contains no mechanisms for tracking stock assignment or covered call execution. Capital deployment is strictly restricted to defined-risk spreads and pure option premium structures.
- **Immutability of Historical Evidence:** Every trade log preserves an immutable deep-copy snapshot of the exact playbook ruleset active at the moment of trade execution to prevent data drift.
- **Common Sense First:** The system must never produce an output that is obviously wrong and then wait for the user to object. All validation runs before output is displayed. A system that says "you're right, I shouldn't have done that" after generating a bad output has failed.

### 1.2 What This System Is Not

- An autonomous trading bot — the user approves every trade
- A signal service or alpha generator — it works with user-supplied and API-fetched market data
- A backtesting engine — that is a separate project
- A financial advisor — it implements the user's own defined rules, not recommendations

### 1.3 Current Operational State

- **Brokerage:** Charles Schwab Roth IRA — funded, options approved Level 3 (spreads)
- **Platform:** Thinkorswim desktop — paper trading active
- **Capital:** $10,000 transferred and available
- **Execution:** Manual via Thinkorswim for now. Alpaca IRA API integration is a future layer — see Section 8.
- **System must initialize in Manual Sandbox Mode.** Live API endpoints remain decoupled behind environment variables (`process.env.ALPACA_LIVE_MODE = false`). The platform runs entirely on manual local logging during the capital migration window.

---

## 2. Core System Architecture

Three operating layers execute sequentially each evening:

```
┌──────────────────────────────────────────────────────────────┐
│                    LAYER A: OBSERVATION ENGINE               │
│     Active Position Tracker • Portfolio Greeks • Lifecycle   │
└───────────────────────────────────────┬──────────────────────┘
                                        │
                                        ▼
┌──────────────────────────────────────────────────────────────┐
│                 LAYER B: BACKGROUND CONTEXT LAYER            │
│      Market Telemetry • Trend Metrics • Regime Labels        │
└───────────────────────────────────────┬──────────────────────┘
                                        │
                                        ▼
┌──────────────────────────────────────────────────────────────┐
│                   LAYER C: OPPORTUNITY ENGINE                │
│     Playbook Scans • Automated Order Specification Cards     │
└──────────────────────────────────────────────────────────────┘
```

**Sequencing rule:** Layer A always runs first. If any position has a P1 action (CLOSE NOW), the system does not proceed to Layer C until that action is resolved. Position management takes absolute priority over new entry decisions.

---

## 3. Global Configuration & Position Schemas

### 3.1 Playbook Data Layout

All strategic setups are initialized as structural configurations. New playbooks are injected via data models — no application source code changes required.

```typescript
interface PlaybookDefinition {
  id: string;
  version: string;
  name: string;
  underlying_ticker: string;
  strategy_type: 
    | 'BULL_CALL_SPREAD' 
    | 'BEAR_PUT_SPREAD' 
    | 'IRON_CONDOR' 
    | 'LONG_STRADDLE' 
    | 'LONG_STRANGLE';
  execution_mode: 'LIVE' | 'PAPER';
  entry_filters: {
    min_ivr: number;
    max_ivr: number;
    vix_range: [number, number];
    required_trend: 'ABOVE_SMA20' | 'BELOW_SMA20' | 'ANY';
    block_catalyst_14dte: boolean;
    require_catalyst_14dte: boolean; // true for straddle/strangle playbooks
  };
  execution_specs: {
    target_dte: number;
    short_leg_delta: number;  // for spread strategies
    long_leg_delta: number;   // for spread strategies
    spread_width_dollars: number;
    straddle_atm: boolean;    // true = always buy ATM strike
  };
  exit_rules: {
    profit_take_pct: number;      // 50 for income, 100 for debit
    stop_loss_pct: number;        // 200 for income (2x), 50 for debit
    mandatory_exit_dte: number;   // 21 for all strategies
    catalyst_exit_days_after: number; // close within N trading days after catalyst fires
  };
}
```

### 3.2 Dynamic Portfolio & Risk Policy Configuration

User-configurable via admin settings panel. These thresholds drive automated warning logic across all layers.

```json
{
  "risk_profile": {
    "max_trade_risk_pct": 15.0,
    "max_trade_risk_dollars": 1500,
    "max_underlying_concentration_pct": 35.0,
    "max_correlated_index_pct": 50.0,
    "minimum_cash_reserve_pct": 15.0,
    "max_simultaneous_positions": 3,
    "max_capital_deployed_pct": 85.0
  },
  "portfolio_greek_limits": {
    "max_net_delta": 50,
    "max_net_vega": 100,
    "max_net_gamma": 10
  }
}
```

**Sizing rationale:** With a $10,000 account, a 15% max risk per trade ($1,500) is the workable ceiling for defined-risk spreads. The original $300 limit (3%) effectively locks the account out of meaningful positions at this capital level.

### 3.3 Position Data Schema

Option values stored as absolute raw numbers per-share. All `× 100 × contracts` multipliers occur strictly within presentation view components — never in storage or business logic.

```typescript
interface OptionLeg {
  option_type: 'CALL' | 'PUT';
  direction: 'LONG' | 'SHORT';
  strike: number;
  expiration: string;       // ISO date string: "2026-07-18"
  delta: number;
  theta: number;
  vega: number;
}

interface Position {
  id: string;
  underlying: string;
  playbook_id: string;
  playbook_version: string;
  playbook_snapshot: PlaybookDefinition; // Deep copy — immutable at entry
  legs: OptionLeg[];
  entry_date: string;
  expiration_date: string;
  entry_premium: number;              // Per-share, e.g. 16.61
  premium_direction: 'CREDIT' | 'DEBIT';
  current_value_per_share: number;   // Updated each evening manually
  contracts: number;
  max_profit: number;                // Per-share
  max_loss: number;                  // Per-share
  notes: string;
  rolls: number;                     // Max 2 before forced exit
  status: 'OPEN' | 'CLOSED' | 'EXPIRED';
}
```

---

## 4. Multi-Engine Pipeline Execution

### 4.1 LAYER A: Observation Engine (Portfolio Risk Dashboard)

Default view on every session open. No other navigation is accessible until Layer A is reviewed.

**Automated Lifecycle Scanning — Priority Levels:**

| Priority | Condition | Display |
|---|---|---|
| P1 — CLOSE NOW | Loss limit hit: credit trade loss ≥ 2× premium collected | Red, prominent, blocks Layer C |
| P1 — CLOSE NOW | Profit target hit: income trade at 50% max profit | Red, prominent, blocks Layer C |
| P1 — CLOSE NOW | Profit target hit: debit trade at 100% gain | Red, prominent, blocks Layer C |
| P2 — CLOSE SOON | DTE ≤ 21 | Orange warning |
| P2 — REVIEW | Regime conflict detected (see Section 4.1.1) | Orange warning |
| P3 — MONITOR | Income trade approaching 35% of max profit | Yellow alert |
| P3 — MONITOR | Debit trade approaching 35% loss | Yellow alert |
| OK | None of the above | Green |

**Language rules — non-negotiable:**
- Never say "consider closing." Say "CLOSE NOW" (P1) or "Review for potential close" (P2).
- Always show the math: "Loss limit reached: position down $X against a limit of $Y."
- Never suggest holding past an exit trigger "to see if it recovers."
- A P2 REVIEW means evaluate — not close automatically. Present the conflict, let the user decide.

**Portfolio Greeks Aggregation:**
Compute and display account-wide Net Delta (Δ), Net Theta (Θ), Net Vega, and Net Gamma in real time from all open positions. Flash high-visibility warning if any metric exceeds `portfolio_greek_limits` thresholds.

**Exposure Safeguards:**
- Flag if any single underlying exceeds `max_underlying_concentration_pct`
- Flag if total correlated index exposure exceeds `max_correlated_index_pct`
- Flag if total positions open ≥ `max_simultaneous_positions`
- Flag if capital deployed ≥ `max_capital_deployed_pct`

#### 4.1.1 Regime Conflict Definitions

| Conflict | Why It Matters |
|---|---|
| TRENDING_BEAR + LONG CALL position | Bullish position in falling market |
| TRENDING_BEAR + BULL_CALL_SPREAD | Bullish spread in falling market |
| CALM_BULL + BEAR_PUT_SPREAD | Bearish spread in rising market |
| HIGH_VOL_NEUTRAL + IRON_CONDOR short strikes breached by 2% | Range trade being violated |
| EVENT_CATALYST + any short premium position expiring around catalyst date | Selling vol into expected vol spike |

### 4.2 LAYER B: Background Context Layer (Market Telemetry)

Automated data collection on application load. Displayed in a visually subordinate status ribbon at the top of the layout — descriptive context only, no scoring, no predictive weights.

**Data collected:**
1. SPY closing price relative to 20-day SMA — label: ABOVE_STRONG / ABOVE_FLAT / AT / BELOW_FLAT / BELOW_FALLING
2. VIX closing level — label: VIX_LOW (<15) / VIX_NORMAL (15-20) / VIX_ELEVATED (20-30) / VIX_HIGH (>30)
3. IVR for each underlying in active playbooks — label: IVR_LOW (<30) / IVR_MODERATE (30-50) / IVR_ELEVATED (50-70) / IVR_HIGH (>70)
4. Catalyst calendar — FOMC dates, earnings within 14 DTE for any watched underlying

**Regime Classification (computed from Layer B inputs):**

Implement as a weighted scoring matrix. Store scores for all four regimes, display confidence breakdown.

| Signal Value | Adds Weight To | Subtracts From |
|---|---|---|
| ABOVE_STRONG | calm_bull +2 | trending_bear -2 |
| ABOVE_FLAT | calm_bull +1, high_vol_neutral +1 | trending_bear -1 |
| AT | high_vol_neutral +1 | — |
| BELOW_FLAT | trending_bear +1, high_vol_neutral +1 | calm_bull -1 |
| BELOW_FALLING | trending_bear +2 | calm_bull -2 |
| VIX_LOW | calm_bull +2 | high_vol_neutral -1, trending_bear -1 |
| VIX_NORMAL | calm_bull +1 | — |
| VIX_ELEVATED | high_vol_neutral +2, trending_bear +1 | calm_bull -1 |
| VIX_HIGH | trending_bear +2, high_vol_neutral +1 | calm_bull -2 |
| IVR_LOW | calm_bull +1 | high_vol_neutral -2 |
| IVR_MODERATE | calm_bull +1 | — |
| IVR_ELEVATED | high_vol_neutral +2 | event_catalyst +1 |
| IVR_HIGH | high_vol_neutral +1, trending_bear +1 | event_catalyst +1, calm_bull -1 |
| CATALYST_MAJOR | event_catalyst +3 | calm_bull -1 |
| CATALYST_MINOR | event_catalyst +1 | — |
| CATALYST_NONE | calm_bull +1 | event_catalyst -2 |
| DAY_UP_1PLUS | calm_bull +1 | trending_bear -1 |
| DAY_FLAT | calm_bull +1, high_vol_neutral +1 | — |
| DAY_DOWN_1PLUS | trending_bear +1, high_vol_neutral +1 | calm_bull -1 |
| DAY_DOWN_2PLUS | trending_bear +2, high_vol_neutral +1 | calm_bull -2 |

**Regime → Valid Strategy Menu:**

| Regime | PRIMARY | SECONDARY | AVOID |
|---|---|---|---|
| CALM_BULL | Bull Call Spread, Cash-Secured Put (0.25-0.30Δ) | Iron Condor | Straddles, Bear spreads |
| HIGH_VOL_NEUTRAL | Iron Condor, Cash-Secured Put (0.20Δ wider cushion) | Vertical Spread | Naked long options |
| TRENDING_BEAR | Bear Put Spread, Do Nothing | Deep OTM CSP (0.10-0.15Δ only on assets held 12mo+) | Iron Condors, Bull spreads |
| EVENT_CATALYST | Long Straddle ATM, Long Strangle OTM | Bull/Bear Vertical Spread (if directional) | Selling premium into the event |

### 4.3 LAYER C: Opportunity Engine (Automated Playbook Matcher)

Loops Layer B telemetry against all active playbook definitions. Outputs a Candidate Options Menu for eligible playbooks only. Ineligible playbooks are hidden — not shown as disabled.

**Candidate card format:**
```
[+] PLAYBOOK APPROVED: SPY 45-DTE Iron Condor — STATUS: ELIGIBLE
    Mode: PAPER | Sample: 0 trades | No historical data yet
    Automated Order Specification:
    -> Sell 1x SPY Put Spread (strikes derived from 0.16Δ short leg)
    -> Sell 1x SPY Call Spread (strikes derived from 0.16Δ short leg)
    -> Derived From: Target DTE=45 | Short Delta=0.16 | Wing Width=$5
```

Auto-generated strikes must display their exact derivation parameters beneath the order ticket. No black-box outputs.

**Position exposure gates — run before showing any candidate:**

| Gate | Rule |
|---|---|
| MAX POSITIONS | 3+ open: show no candidates. Display reason. |
| MAX CAPITAL | Capital deployed ≥ 85%: show no candidates. |
| DIRECTIONAL CONCENTRATION | 2+ positions same directional bias: suppress same-direction strategies as PRIMARY |
| UNDERLYING CONCENTRATION | Underlying already has open position: suppress new entries on that underlying |
| EARNINGS GATE | Earnings within 14 DTE: suppress all income strategies for that underlying |
| IVR GATE (INCOME) | IVR < 40: suppress CSP, CC, Iron Condor |
| IVR GATE (DEBIT) | IVR > 70: suppress naked long options, show spreads only |

---

## 5. Trade Specification & Intent Journaling

When the user selects an eligible candidate from Layer C, the system generates a staging card. The user must complete the intent journal before the order is saved.

### 5.1 Required Trade Specification Fields

Every generated spec must include all of the following — no exceptions:

| Field | Requirement |
|---|---|
| underlying | Ticker symbol |
| strategy_type | Full name |
| legs | Each leg: BUY/SELL, CALL/PUT, strike, expiration, quantity |
| expiration_date | Specific date, e.g. "July 18, 2026" |
| dte_at_entry | Integer days |
| order_type | LIMIT always — never market orders on options |
| limit_price | Midpoint of bid-ask. For spreads: net debit or net credit. |
| max_loss | Dollar amount, calculated explicitly |
| max_gain | Dollar amount, calculated explicitly |
| break_even | Underlying price(s) at breakeven, calculated explicitly |
| profit_target | Dollar amount and %, derived from playbook exit rules |
| loss_limit | Dollar amount and %, derived from playbook exit rules |
| closing_order_instructions | Exact GTC order language to place immediately after fill |

### 5.2 Strike Selection Rules

The system recommends a specific strike, not a range. If a required input is missing (e.g. user's price target for the sell leg of a spread), the system asks before generating the spec — never assumes.

| Strategy | Strike Rule |
|---|---|
| Cash-Secured Put | Delta closest to 0.30 |
| Iron Condor short strikes | Delta 0.16-0.20 on both sides |
| Iron Condor long strikes | $5-10 outside short strikes |
| Straddle | ATM — strike closest to current price |
| Strangle | 0.25-0.30 delta on both sides |
| Bull Call Spread — buy leg | ATM or first OTM above current price |
| Bull Call Spread — sell leg | User's stated price target — ask if not provided |
| Bear Put Spread — buy leg | ATM or first OTM below current price |
| Bear Put Spread — sell leg | User's stated downside target — ask if not provided |

### 5.3 Expiration Selection Rules

| Strategy Type | Rule |
|---|---|
| Income trades (CSP, Iron Condor) | 30-45 DTE. Select closest to 38 DTE. |
| Event trades (Straddle, Strangle) | Minimum 14 days AFTER known catalyst date |
| Directional spreads | 30-60 DTE. Select closest to 45 DTE. |
| Hard block | Never select expiration under 14 DTE for new entries |

### 5.4 Operational Intent Journal

Required before order is saved. Tracks subjective patterns and thesis invalidation.

```typescript
interface OperationalJournalEntry {
  core_thesis_rationale: string;
  structural_invalidation: string;  // Exact condition that proves setup false
  expected_underlying_move_pct: number;
  pre_trade_emotional_state: 'Calm' | 'Anxious' | 'Chasing' | 'Bored';
  pre_trade_confidence_rating: 1 | 2 | 3 | 4 | 5;
}
```

### 5.5 Common Sense Kill Switch (Pre-Output Validation)

This runs before any spec is displayed. Hard blocks cannot be bypassed by the user — the system does not ask "are you sure?" It simply does not generate the spec until the issue is resolved.

**Hard blocks:**
| Check | Block Condition |
|---|---|
| Unresolved P1 action | Any CLOSE NOW alert exists in Layer A |
| Capital exceeded | Spread collateral required > available liquid cash |
| Max loss exceeded | Position max loss > `max_trade_risk_dollars` |
| Strike sanity | Buy strike for a bull spread is more than 10% OTM |
| Expiration arithmetic | Expiration date is in the past, or under 14 DTE |
| Premium reasonableness | Suggested premium ≤ 0 or > underlying price |
| Position count | Trade would bring total open positions above 3 |

**Warnings (shown, require explicit confirmation to proceed):**
| Check | Warning Condition |
|---|---|
| Regime consistency | Trade direction inconsistent with current regime |
| Duplicate underlying | Open position already exists on this underlying |
| Break-even realism | Straddle break-even requires move > 2 standard deviations |
| Strategy novelty | First time this strategy type is being used — recommend paper mode |

---

## 6. Exit Rule Engine

Exit rules are non-negotiable. Defined at entry, enforced by Layer A every session. The system never suggests holding past a trigger "to see if it recovers."

### 6.1 Universal Exit Rules

| Rule | Specification | Example |
|---|---|---|
| Income — profit target | Close at 50% of premium collected | Collected $200. Close when buyback costs $100. |
| Income — loss limit | Close when loss = 2× premium collected | Collected $200. Close if buyback costs $400. |
| Debit — profit target | Close when position gained 100% of premium paid | Paid $1,661. Close when worth $3,322. |
| Debit — loss limit | Close when position lost 50% of premium paid | Paid $1,661. Close when worth $831. |
| Time rule | Close at 21 DTE if not at exit target | Gamma risk outweighs theta reward below 21 DTE. |
| Catalyst rule | Close within 5 trading days after catalyst fires | IV crush follows the event. Don't hold through it. |

### 6.2 Roll Rules

Rolling is a defensive action, not a way to avoid taking a loss.

- Roll only for net credit. If rolling requires a net debit, take the loss instead.
- Maximum 2 rolls per position. After 2 rolls, forced exit — no exceptions.
- Roll down for puts: lower strike AND later expiration. Never just roll out in freefall.
- Roll up for calls: higher strike AND later expiration for net credit if possible.

---

## 7. Retrospective Performance Ledger & Opportunity Auditing

### 7.1 Closure Post-Mortem Schema

Closing a position freezes the trade log into an immutable historical record.

```typescript
interface ClosurePostMortem {
  position_id: string;
  outcome: 'WIN' | 'LOSS' | 'BREAKEVEN';
  realized_pnl: number;
  actual_underlying_move_pct: number;
  exit_date: string;
  exit_trigger: 'PROFIT_TARGET' | 'LOSS_LIMIT' | 'TIME_RULE' | 'CATALYST_RULE' | 'MANUAL';
  lesson_tags: string[];  // e.g. ["#LateEntry", "#HeldTooLong", "#GoodThesis"]
  user_override_logged: boolean;  // True if trade was routed past a kill switch warning
}
```

### 7.2 Missed Trades Ledger

Logs every eligible playbook opportunity, whether taken or bypassed. Tracks the hypothetical outcome of bypassed trades to measure the value of human override.

```typescript
interface OpportunityRecord {
  playbook_id: string;
  playbook_version: string;
  generated_at: string;
  accepted: boolean;
  outcome_if_taken: number | null;  // Trailing paper return if bypassed
  bypass_reason: string | null;     // User-entered reason if rejected
}
```

### 7.3 Performance Diagnostics Dashboard

Historical snapshots grouped by Playbook ID and Version. Sample size displayed prominently alongside all metrics — never report percentages without the N.

| Metric | Active Options Portfolio | Passive SPY (Benchmark) | BXM Buy-Write Index | Cash / T-Bill |
|---|---|---|---|---|
| CAGR | — (0 trades) | live fetch | live fetch | live fetch |
| Return on Risk (avg/trade) | — | — | — | — |
| Max Drawdown | — | live fetch | — | — |
| Sharpe Ratio | — | — | — | — |
| Profit Factor | — | — | — | — |
| Win Rate | — | — | — | — |

Initialize all user metrics as empty. Do not populate with fictional sample data.

---

## 8. Future Architecture — Alpaca IRA API Integration

The manual position entry layer is designed to be replaced with an API call without restructuring any other layer.

When Alpaca IRA integration is activated:
- Position data fetched from Alpaca at session open instead of manual entry
- `current_value_per_share` fetched from live option chain data
- `TradeSpec` gains an `execute()` method routing multi-leg orders to Alpaca
- GTC closing orders placed programmatically immediately after fill confirmation
- Layer B market telemetry fetched from market data APIs instead of manual input
- Layer A common sense validation applies identically to API-executed trades
- Paper trading environment used for all testing before live execution

**Sequencing rule:** Do not activate live Alpaca integration until the manual version has run and the paper Alpaca integration has completed at least 5 full evening sessions. Automate what you understand. Do not automate what you don't.

---

## 9. Seed Data — Correct Initial State (June 7, 2026)

### 9.1 Portfolio Configuration

```json
{
  "account": {
    "total_nav": 10000,
    "broker": "Charles Schwab",
    "account_type": "Roth IRA",
    "options_approval": "Level 3 — Spreads",
    "execution_mode": "PAPER"
  },
  "risk_profile": {
    "max_trade_risk_pct": 15.0,
    "max_trade_risk_dollars": 1500,
    "max_underlying_concentration_pct": 35.0,
    "max_correlated_index_pct": 50.0,
    "minimum_cash_reserve_pct": 15.0,
    "max_simultaneous_positions": 3,
    "max_capital_deployed_pct": 85.0
  },
  "portfolio_greek_limits": {
    "max_net_delta": 50,
    "max_net_vega": 100,
    "max_net_gamma": 10
  }
}
```

### 9.2 Active Positions Seed Data

**Position 1 — SPY Straddle June 18 (Learning Exercise)**

```json
{
  "id": "seed_pos_spy_straddle_jun18",
  "underlying": "SPY",
  "strategy_type": "LONG_STRADDLE",
  "execution_mode": "PAPER",
  "legs": [
    { "option_type": "CALL", "direction": "LONG", "strike": 759, "expiration": "2026-06-18" },
    { "option_type": "PUT",  "direction": "LONG", "strike": 759, "expiration": "2026-06-18" }
  ],
  "entry_date": "2026-06-07",
  "expiration_date": "2026-06-18",
  "contracts": 1,
  "premium_direction": "DEBIT",
  "entry_premium": 16.61,
  "current_value_per_share": 16.61,
  "max_profit": 999999,
  "max_loss": 16.61,
  "profit_target_per_share": 33.22,
  "loss_limit_per_share": 8.31,
  "notes": "Learning exercise. Expiration BEFORE SpaceX IPO date. Treat as short-term straddle mechanics study. Do not extend or roll.",
  "journal": {
    "core_thesis_rationale": "Short-term volatility study around SpaceX roadshow June 8. Not the primary IPO thesis trade.",
    "structural_invalidation": "SPY remains pinned within 1% of 759 through June 15.",
    "expected_underlying_move_pct": 2.2,
    "pre_trade_emotional_state": "Calm",
    "pre_trade_confidence_rating": 3
  }
}
```

**Position 2 — SPY Straddle July 18 (Primary IPO Thesis Trade)**

```json
{
  "id": "seed_pos_spy_straddle_jul18",
  "underlying": "SPY",
  "strategy_type": "LONG_STRADDLE",
  "execution_mode": "PAPER",
  "legs": [
    { "option_type": "CALL", "direction": "LONG", "strike": 757, "expiration": "2026-07-18" },
    { "option_type": "PUT",  "direction": "LONG", "strike": 757, "expiration": "2026-07-18" }
  ],
  "entry_date": "2026-06-07",
  "expiration_date": "2026-07-18",
  "contracts": 1,
  "premium_direction": "DEBIT",
  "entry_premium": 28.18,
  "current_value_per_share": 28.18,
  "max_profit": 999999,
  "max_loss": 28.18,
  "profit_target_per_share": 56.36,
  "loss_limit_per_share": 14.09,
  "break_even_upside": 785.18,
  "break_even_downside": 728.82,
  "notes": "Primary SpaceX IPO thesis trade. Roadshow June 8. IPO target late June. Close within 5 trading days after IPO fires regardless of profit target. Do not hold through IV crush.",
  "journal": {
    "core_thesis_rationale": "Largest IPO in history creates market volatility regardless of direction. Vol expansion expected across roadshow and IPO window.",
    "structural_invalidation": "Implied volatility collapses before IPO date or SPY remains pinned through late June.",
    "expected_underlying_move_pct": 2.2,
    "pre_trade_emotional_state": "Calm",
    "pre_trade_confidence_rating": 4
  }
}
```

---

## 10. Build Sequence for Antigravity (agy)

Execute in strict order. Do not begin a sprint until the previous sprint's tests pass.

**Sprint 1 — Storage Core & State Machinery** ✅ COMPLETE
- Implement `PlaybookDefinition`, `Position`, `OptionLeg`, and `OperationalJournalEntry` data schemas
- Implement dynamic portfolio risk configuration state with admin settings panel
- Implement raw per-share pricing math module — all `× 100 × contracts` strictly in presentation layer
- Seed database with Section 9 data exactly as specified
- Unit test: all schema validations pass, seed data loads without errors

**Sprint 2 — Layer A: Observation Engine** ✅ COMPLETE
- Build position lifecycle scanner implementing all P1/P2/P3 priority rules from Section 4.1
- Build portfolio Greeks aggregator (Net Δ, Θ, Vega, Γ) from open position legs
- Build exposure safeguard checks (concentration, capital deployed, position count)
- Implement regime conflict detection per Section 4.1.1
- Implement all language rules — never "consider closing," always show math
- Unit test: P1 fires at exactly 2× loss and exactly 100%/50% profit thresholds. P2 fires at exactly 21 DTE. Edge cases: position at exactly 50% profit, DTE = 21 today.

**Sprint 3 — Layer B: Market Context & Regime Classification** ✅ COMPLETE
- Connect market data APIs for SPY SMA20, VIX, per-underlying IVR, catalyst calendar
- Implement regime scoring matrix per Section 4.2 weight table
- Display regime output with full score breakdown (all four scores visible, not just winner)
- Display as subordinate context ribbon — no predictive claims, no scoring language shown to user
- Unit test: given known signal inputs, verify regime output matches expected. Test all tie scenarios.

**Sprint 4 — Layer C: Opportunity Engine** ✅ COMPLETE
- Implement playbook eligibility scanner against Layer B telemetry
- Implement all position exposure gates per Section 4.3
- Build candidate card display with explicit strike derivation parameters
- Build full trade specification generator per Section 5.1 and 5.2
- Implement pre-output common sense validation (all hard blocks and warnings) per Section 5.5
- Hard blocks must be uncircumventable — no "proceed anyway" option
- Unit test: all kill switch conditions verified. Verify hard blocks cannot be bypassed. Verify warning conditions require explicit confirmation.

**Sprint 5 — Intent Journal, Post-Mortem & Ledger**
- Build trade staging card with mandatory `OperationalJournalEntry` before save
- Build closure post-mortem workflow per Section 7.1
- Build missed trades opportunity ledger per Section 7.2
- Build performance diagnostics dashboard per Section 7.3
- Initialize all performance metrics as empty — no fictional sample data
- Unit test: closing a position correctly freezes the record and routes to post-mortem. Override flag sets correctly when user bypasses a warning.

**Sprint 6 — UI Polish & Mobile**
- Mobile-first layout — this tool is used on a phone in the evening
- P1 actions must be immediately visually prominent on load — red, above the fold
- No chart rendering — data-first tool, charts live in Thinkorswim
- Dollar amounts: 2 decimal places. Percentages: 1 decimal place. DTE: integer. Dates: Month DD YYYY.
- Navigation locked until Layer A is reviewed each session

---

## 11. What NOT to Build

- No charting — Thinkorswim handles this
- No autonomous execution — user approves every trade
- No covered calls or share assignment tracking — no share ownership in this system
- No social features, sharing, or multi-user
- No strategy backtesting — separate project
- No LLM/AI integration in initial build — the system implements rules, not judgments
- No "proceed anyway" bypass on hard blocks
- No fictional historical data in the performance dashboard

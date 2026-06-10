# Data Models & Seed Data

> Part of the [modular specification](README.md). Schema *definitions* from §3 and §7 of the [archived v8 spec](archive/project_spec_v8.md); *instance* seed data from §9. These are the canonical shapes; the runtime equivalents live in [backend/models.py](../backend/models.py) (SQLAlchemy + Pydantic) and are surfaced to the frontend via [api.md](api.md).

**Storage convention:** Option values are stored as absolute raw numbers **per-share**. All `× 100 × contracts` multipliers occur strictly within presentation view components — never in storage or business logic.

---

## Schema definitions

### PlaybookDefinition
All strategic setups are initialized as structural configurations. New playbooks are injected via data models — no application source code changes required (see [ADR-0001](decisions.md#adr-0001--rules-engine-not-llm)).

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

### OptionLeg & Position

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
  playbook_snapshot: PlaybookDefinition; // Deep copy — immutable at entry (ADR-0003)
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

### OperationalJournalEntry
Required before an order is saved (enforced; incomplete journal → `422`).

```typescript
interface OperationalJournalEntry {
  core_thesis_rationale: string;
  structural_invalidation: string;  // Exact condition that proves setup false
  expected_underlying_move_pct: number;
  pre_trade_emotional_state: 'Calm' | 'Anxious' | 'Chasing' | 'Bored';
  pre_trade_confidence_rating: 1 | 2 | 3 | 4 | 5;
}
```

### ClosurePostMortem
Closing a position freezes the trade log into an immutable record.

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

### OpportunityRecord
Logs every eligible playbook opportunity, whether taken or bypassed — tracks the hypothetical outcome of bypassed trades to measure the value of human override.

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

### Portfolio & risk policy configuration
User-configurable via the admin settings panel. These thresholds drive automated warning logic across all layers.

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

---

## Seed data — correct initial state (June 7, 2026)

Loaded by [backend/database.py](../backend/database.py) at first startup.

### Portfolio configuration

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

### Active positions

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

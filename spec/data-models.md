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
    | 'BULL_PUT_SPREAD'
    | 'BEAR_CALL_SPREAD'
    | 'IRON_CONDOR'
    | 'LONG_STRADDLE'
    | 'LONG_STRANGLE';
  execution_mode: 'LIVE' | 'PAPER';
  enabled: boolean; // disabled playbooks are skipped by the Layer C scan and hard-blocked (PLAYBOOK_DISABLED) from spec generation
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

> **Note:** these are the current *seed* values. The autonomy-phase risk envelope decided in [ADR-0006](decisions.md#adr-0006--autonomy-roadmap-operator--executor-paper--executor-live) (≤2.5% per trade, ≤50% deployed, ≤4 concurrent positions) supersedes this rationale and replaces these seeds when Executor (Paper) is implemented ([#32](https://github.com/philipreese/basis/issues/32)).

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

### Seed playbooks

Seven SPY playbooks are seeded by [backend/database.py](../backend/database.py), one per strategy type: `spy_iron_condor_v1`, `spy_bull_call_spread_v1`, `spy_bear_put_spread_v1`, `spy_bull_put_spread_v1`, `spy_bear_call_spread_v1`, `spy_long_straddle_v1`, `spy_long_strangle_v1`. The long straddle/strangle ship with `enabled: false` (long-vol entries into known catalysts fight pre-event IV inflation and post-event crush; kept for catalyst study). Full parameter sets live in the seeding code — the database, not this file, is the runtime source.

### Demo positions (test fixtures only — not seeded)

> Since [#53](https://github.com/philipreese/basis/issues/53), real databases start with an empty position book. The two June/July 2026 demo straddles below survive only as `SEED_POSITIONS` test-fixture data in [backend/database.py](../backend/database.py).

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

## Executor (Paper) schema additions

> Specified contract-first for the Executor (Paper) build ([design/executor-paper.md](design/executor-paper.md) §4.2); implemented in [backend/models.py](../backend/models.py) (#61). Books exist only in this database — the broker knows nothing of them ([CONTEXT.md](../CONTEXT.md) → Book); `order_ref` is the redundant broker-side echo used by reconciliation and Flex audits.
>
> **Pre-launch schema policy (#94):** there are no migrations until the first real paper fill exists — the models are the schema, and a schema change means deleting the (worthless) pre-launch database. Migrations return the day the fills/gate/audit tables start accumulating Live Gate evidence, which can never be reset.

```
books               (id PK 'B01'..'B22', name, config JSON, config_version INT,
                     config_hash TEXT, starting_capital REAL, cash_balance REAL,
                     status, created_at)
orders              (id TEXT PK, book_id FK, position_id FK nullable,
                     order_ref TEXT UNIQUE, ib_order_id INT, ib_perm_id INT,
                     action OPEN|CLOSE|ROLL, combo_legs JSON, order_type,
                     limit_price, decision_midpoint,
                     status STAGED|SUBMITTED|PARTIAL|FILLED|CANCELLED|REJECTED,
                     submitted_at, completed_at)
fills               (exec_id TEXT PK, order_id FK, book_id, con_id INT, side,
                     quantity, price, commission, fill_time, raw JSON)   -- append-only
reconciliation_runs (id, run_at, broker_snapshot JSON, books_expected JSON,
                     result CLEAN|DRIFT, drift_details JSON, resolved_at, resolution)
gate_events         (id, book_id, run_at, gate, result PASS|BLOCK, context JSON)  -- append-only
audit_events        (id, run_at, book_id nullable, event_type, actor, payload JSON) -- append-only
trading_control     (scope PK: 'GLOBAL' | book_id, state ACTIVE|HALT_ENTRIES|FLATTEN_REQUESTED,
                     reason, actor, changed_at)
regime_readings     (date, book_id, engine_variant, regime, inputs JSON, scores JSON,
                     PK (date, book_id, engine_variant))
index_history       (date, symbol, close, PK (date, symbol))   -- VIX/VIX3M + ETF closes (SPY, IWM, GLD)
ALTER positions ADD book_id TEXT NOT NULL REFERENCES books(id)  -- + index
```

- `fills`, `gate_events`, and `audit_events` are **insert-only at the ORM layer** — no UPDATE/DELETE path, enforced with a test. They are the Live Gate's "zero breaches" and "expectancy after slippage" evidence; `decision_midpoint` vs fill price cannot be reconstructed later from any IBKR source.
- `exec_id` as the fills PK naturally dedupes IBKR's execution-correction semantics (corrections arrive as new suffixed execIds).
- **Per-underlying telemetry** (#139): `MarketStateSchema` carries `underlying_prices` / `underlying_sma20` dicts, computed by the executor from `index_history` at scan time (never persisted). Scan lookups go through a telemetry-proxy map (`XSP → SPY`); non-SPY underlyings (IWM, GLD) also get an RV20 percentile-rank pseudo-IVR published into `underlying_ivrs` so the IVR entry filters and gates work unchanged. An underlying with insufficient history is absent from the dicts, and its playbooks are suppressed — the scan never derives strikes off SPY telemetry for a different-scale asset.
- Book configs are versioned and hashed (edit ⇒ new `config_version` + hash); the Live Gate attaches to a `(book_id, config_hash)` pair — the multi-book extension of [ADR-0003](decisions.md#adr-0003--playbook-snapshot-immutability).
- **Book allocation** ([ADR-0009](decisions.md#adr-0009--accelerated-experiment-matrix), superseding the 2026-08-18 six-book plan): a 22-book experiment matrix, one question per book — B01–B06 core V0/V1/V2 × XSP/SPY grid plus 16 single-variable arms and controls. Book `config` gains optional keys `playbook_ids` (whitelist), `playbook_overrides` (dot-keyed field overrides, e.g. `"execution_specs.target_dte": 24`, revalidated through `PlaybookDefinitionSchema`), `ignore_regime`, and `ignore_ivr`; all participate in `config_hash`. Books whose machinery isn't built yet (B18–B22) seed with their enabling PRs; B09/B10 landed with the multi-underlying telemetry (#139).

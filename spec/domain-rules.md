# Domain Rules — Core Engine Logic

> Part of the [modular specification](README.md). This is the system's load-bearing logic, carried verbatim from §4–§7 of the [archived v8 spec](archive/project_spec_v8.md). Tables here are exact — treat changes as behavior changes and update the implementing module + tests in lockstep. Data shapes referenced below are defined in [data-models.md](data-models.md).

---

## Layer A — Position Lifecycle & Safeguards

Default view on every session open. No other navigation is accessible until Layer A is reviewed.

### Lifecycle scanning — priority levels

| Priority | Condition | Display |
|---|---|---|
| P1 — CLOSE NOW | Loss limit hit: credit trade loss ≥ 2× premium collected | Red, prominent, blocks Layer C |
| P1 — CLOSE NOW | Profit target hit: income trade at 50% max profit | Red, prominent, blocks Layer C |
| P1 — CLOSE NOW | Profit target hit: debit trade at 100% gain | Red, prominent, blocks Layer C |
| P2 — CLOSE SOON | DTE ≤ 21 | Orange warning |
| P2 — REVIEW | Regime conflict detected (see below) | Orange warning |
| P3 — MONITOR | Income trade approaching 35% of max profit | Yellow alert |
| P3 — MONITOR | Debit trade approaching 35% loss | Yellow alert |
| OK | None of the above | Green |

**Language rules — non-negotiable:**
- Never say "consider closing." Say "CLOSE NOW" (P1) or "Review for potential close" (P2).
- Always show the math: "Loss limit reached: position down $X against a limit of $Y."
- Never suggest holding past an exit trigger "to see if it recovers."
- A P2 REVIEW means evaluate — not close automatically. Present the conflict, let the user decide.

### Portfolio Greeks aggregation
Compute and display account-wide Net Delta (Δ), Net Theta (Θ), Net Vega, and Net Gamma in real time from all open positions. Flash a high-visibility warning if any metric exceeds `portfolio_greek_limits` thresholds.

### Exposure safeguards
- Flag if any single underlying exceeds `max_underlying_concentration_pct`
- Flag if total correlated index exposure exceeds `max_correlated_index_pct`
- Flag if total positions open ≥ `max_simultaneous_positions`
- Flag if capital deployed ≥ `max_capital_deployed_pct`

### Regime conflict definitions

| Conflict | Why It Matters |
|---|---|
| TRENDING_BEAR + LONG CALL position | Bullish position in falling market |
| TRENDING_BEAR + BULL_CALL_SPREAD | Bullish spread in falling market |
| CALM_BULL + BEAR_PUT_SPREAD | Bearish spread in rising market |
| HIGH_VOL_NEUTRAL + IRON_CONDOR short strikes breached by 2% | Range trade being violated |
| EVENT_CATALYST + any short premium position expiring around catalyst date | Selling vol into expected vol spike |

**Source of truth:** [backend/observation.py](../backend/observation.py).

---

## Regime classification (Layer B)

Automated data collection on application load, displayed in a subordinate status ribbon — descriptive context only, no scoring or predictive language shown to the user.

**Data collected:**
1. SPY closing price relative to 20-day SMA — label: ABOVE_STRONG / ABOVE_FLAT / AT / BELOW_FLAT / BELOW_FALLING
2. VIX closing level — label: VIX_LOW (<15) / VIX_NORMAL (15-20) / VIX_ELEVATED (20-30) / VIX_HIGH (>30)
3. IVR for each underlying in active playbooks — label: IVR_LOW (<30) / IVR_MODERATE (30-50) / IVR_ELEVATED (50-70) / IVR_HIGH (>70)
4. Catalyst calendar — FOMC dates, earnings within 14 DTE for any watched underlying

**Regime classification** is a weighted scoring matrix. Store scores for all four regimes; display the confidence breakdown (all four scores, not just the winner).

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

**Regime → valid strategy menu:**

| Regime | PRIMARY | SECONDARY | AVOID |
|---|---|---|---|
| CALM_BULL | Bull Put Spread (0.30Δ short), Bull Call Spread | Iron Condor | Straddles, Bear spreads |
| HIGH_VOL_NEUTRAL | Iron Condor, Cash-Secured Put (0.20Δ wider cushion) | Vertical Spread | Naked long options |
| TRENDING_BEAR | Bear Call Spread (0.30Δ short), Bear Put Spread, Do Nothing | Deep OTM CSP (0.10-0.15Δ only on assets held 12mo+) | Iron Condors, Bull spreads |
| EVENT_CATALYST | Long Straddle ATM, Long Strangle OTM | Bull/Bear Vertical Spread (if directional) | Selling premium into the event |

**Source of truth:** [backend/regime.py](../backend/regime.py), [backend/market_data.py](../backend/market_data.py).

---

## Playbook matching (Layer C)

Loops Layer B telemetry against all active playbook definitions. Outputs a candidate menu for eligible playbooks only. Ineligible playbooks are hidden — not shown as disabled. Auto-generated strikes must display their exact derivation parameters beneath the order ticket. No black-box outputs.

Playbook definitions carry an `enabled` flag. Disabled playbooks are skipped entirely by the Layer C scan (never shown, even as suppressed) and spec generation for them hard-blocks with `PLAYBOOK_DISABLED`. The seed long straddle/strangle playbooks ship disabled by default: buying volatility into a known catalyst fights pre-event IV inflation and post-event crush, so they are kept for catalyst-study use only.

**Candidate card format:**
```
[+] PLAYBOOK APPROVED: SPY 45-DTE Iron Condor — STATUS: ELIGIBLE
    Mode: PAPER | Sample: 0 trades | No historical data yet
    Automated Order Specification:
    -> Sell 1x SPY Put Spread (strikes derived from 0.16Δ short leg)
    -> Sell 1x SPY Call Spread (strikes derived from 0.16Δ short leg)
    -> Derived From: Target DTE=45 | Short Delta=0.16 | Wing Width=$5
```

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

**Source of truth:** [backend/opportunity.py](../backend/opportunity.py).

---

## Trade specification

When the user selects an eligible candidate, the system generates a staging card. The user must complete the [operational intent journal](#operational-intent-journal--post-mortem) before the order is saved.

### Required trade specification fields
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

### Strike selection rules
The system recommends a specific strike, not a range. If a required input is missing (e.g. the user's price target for the sell leg of a spread), the system asks before generating the spec — never assumes.

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
| Bull Put Spread — short leg | Delta closest to 0.30 below current price |
| Bull Put Spread — long leg | Spread width below short strike ($5 default) |
| Bear Call Spread — short leg | Delta closest to 0.30 above current price |
| Bear Call Spread — long leg | Spread width above short strike ($5 default) |

### Expiration selection rules

| Strategy Type | Rule |
|---|---|
| Income trades (CSP, Iron Condor) | 30-45 DTE. Select closest to 38 DTE. |
| Event trades (Straddle, Strangle) | Minimum 14 days AFTER known catalyst date |
| Directional spreads | 30-60 DTE. Select closest to 45 DTE. |
| Hard block | Never select expiration under 14 DTE for new entries |

---

## Validation — Common Sense Kill Switch

Runs before any spec is displayed. **Hard blocks cannot be bypassed** by the user — the system does not ask "are you sure?" It simply does not generate the spec until the issue is resolved. See [ADR-0001](decisions.md#adr-0001--rules-engine-not-llm) for the philosophy.

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
| Playbook disabled | The playbook's `enabled` flag is false (`PLAYBOOK_DISABLED`) |

**Warnings (shown, require explicit confirmation to proceed):**

| Check | Warning Condition |
|---|---|
| Regime consistency | Trade direction inconsistent with current regime |
| Duplicate underlying | Open position already exists on this underlying |
| Break-even realism | Straddle break-even requires move > 2 standard deviations |
| Strategy novelty | First time this strategy type is being used — recommend paper mode |

**Source of truth:** [backend/opportunity.py](../backend/opportunity.py).

---

## Exit rule engine

Exit rules are non-negotiable. Defined at entry, enforced by Layer A every session. The system never suggests holding past a trigger "to see if it recovers."

### Universal exit rules

| Rule | Specification | Example |
|---|---|---|
| Income — profit target | Close at 50% of premium collected | Collected $200. Close when buyback costs $100. |
| Income — loss limit | Close when loss = 2× premium collected | Collected $200. Close if buyback costs $400. |
| Debit — profit target | Close when position gained 100% of premium paid | Paid $1,661. Close when worth $3,322. |
| Debit — loss limit | Close when position lost 50% of premium paid | Paid $1,661. Close when worth $831. |
| Time rule | Close at 21 DTE if not at exit target | Gamma risk outweighs theta reward below 21 DTE. |
| Catalyst rule | Close within 5 trading days after catalyst fires | IV crush follows the event. Don't hold through it. |

### Roll rules
Rolling is a defensive action, not a way to avoid taking a loss.

- Roll only for net credit. If rolling requires a net debit, take the loss instead.
- Maximum 2 rolls per position. After 2 rolls, forced exit — no exceptions.
- Roll down for puts: lower strike AND later expiration. Never just roll out in freefall.
- Roll up for calls: higher strike AND later expiration for net credit if possible.

**Source of truth:** [backend/observation.py](../backend/observation.py) (enforcement), [backend/models.py](../backend/models.py) (`rolls` cap).

---

## Operational intent journal & post-mortem

### Intent journal (required before save)
Tracks subjective patterns and thesis invalidation. Enforced at position creation — a `POST /api/positions` with an incomplete journal returns `422`. Schema: [`OperationalJournalEntry`](data-models.md#operationaljournalentry).

### Closure post-mortem
Closing a position freezes the trade log into an immutable historical record (outcome, realized P&L, actual underlying move, exit trigger, lesson tags, override flag). Schema: [`ClosurePostMortem`](data-models.md#closurepostmortem). The system never reports percentages without the sample size N, and never populates the dashboard with fictional data.

**Source of truth:** [backend/main.py](../backend/main.py) (`create_position`, `close_position`), [backend/models.py](../backend/models.py).

# Domain Rules — Core Engine Logic

> Part of the [modular specification](README.md). This is the system's load-bearing logic, carried verbatim from §4–§7 of the [archived v8 spec](archive/project_spec_v8.md). Tables here are exact — treat changes as behavior changes and update the implementing module + tests in lockstep. Data shapes referenced below are defined in [data-models.md](data-models.md).

---

## Layer A — Position Lifecycle & Safeguards

Default view on every session open. No other navigation is accessible until Layer A is reviewed.

### Lifecycle scanning — priority levels

| Priority | Condition | Display |
|---|---|---|
| P1 — CLOSE NOW | Ex-div assignment risk: ITM short call on a dividend payer within 3 trading days of a projected ex-date (#130) — checked first; a No-Stock Mandate breach outranks P&L | Red, prominent, blocks Layer C |
| P1 — CLOSE NOW | Interest-carry assignment risk: short put priced 5% or more below strike (live underlying price, not entry-frozen delta), on any American-style underlying — dividend-paying or not (#736); no calendar exists for this trigger, unlike ex-div | Red, prominent, blocks Layer C |
| P1 — CLOSE NOW | Loss limit hit: credit trade loss ≥ `stop_loss_pct` (default 2×) of premium collected | Red, prominent, blocks Layer C |
| P1 — CLOSE NOW | Profit target hit: income trade at `profit_take_pct` (default 50%) of max profit | Red, prominent, blocks Layer C |
| P1 — CLOSE NOW | Profit target hit: debit trade at `profit_take_pct` (default 100%) gain | Red, prominent, blocks Layer C |
| P1 — CLOSE NOW | Loss limit hit: debit trade loss ≥ `stop_loss_pct` (default 50%) of premium paid | Red, prominent, blocks Layer C |
| P2 — CLOSE SOON | DTE ≤ `mandatory_exit_dte` (default 21) | Orange warning |
| | *Manual workbench only.* For executor books there is no reviewer, so "mandatory" means close: the executor treats this as a P1-equivalent trigger (`TIME_EXIT`) using the position's frozen playbook snapshot threshold (#260). | |
| P2 — REVIEW | Regime conflict detected (see below) | Orange warning |
| P3 — MONITOR | Income trade approaching 35% of max profit | Yellow alert |
| P3 — MONITOR | Debit trade approaching 35% loss | Yellow alert |
| OK | None of the above | Green |

**Language rules — non-negotiable:**
- Never say "consider closing." Say "CLOSE NOW" (P1) or "Review for potential close" (P2).
- Always show the math: "Loss limit reached: position down $X against a limit of $Y."
- Never suggest holding past an exit trigger "to see if it recovers."
- A P2 REVIEW means evaluate — not close automatically. Present the conflict, let the user decide.

Exit thresholds come from the position's **frozen playbook snapshot** (`exit_rules`, [ADR-0003](decisions.md#adr-0003--playbook-snapshot-immutability)) when present; the parenthesized defaults apply only to snapshot-less positions (legacy/manual entries). This is what lets experiment arms like B15 (25% profit take) and B17 (hold to 7 DTE) vary exits per book ([ADR-0009](decisions.md#adr-0009--accelerated-experiment-matrix)).

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
| EVENT_CATALYST + any short premium position expiring around catalyst date, OR the regime itself entered via term-structure backwardation/negative VRP with no catalyst date in scope (#770) | Selling vol into expected vol spike |

**Source of truth:** [backend/observation.py](../backend/observation.py).

---

## Regime classification (Layer B)

Automated data collection on application load, displayed in a subordinate status ribbon — descriptive context only, no scoring or predictive language shown to the user.

**Data collected:**
1. SPY closing price relative to 20-day SMA — label: ABOVE_STRONG / ABOVE_FLAT / AT / BELOW_FLAT / BELOW_FALLING
2. VIX closing level — label: VIX_LOW (<15) / VIX_NORMAL (15-20) / VIX_ELEVATED (20-30) / VIX_HIGH (>30)
3. IVR for each underlying in active playbooks — label: IVR_LOW (<30) / IVR_MODERATE (30-50) / IVR_ELEVATED (50-70) / IVR_HIGH (>70)
4. Catalyst calendar — `catalyst_dates` is auto-seeded nightly from the published FOMC and CPI schedules ([backend/catalyst_calendar.py](../backend/catalyst_calendar.py), #131) — the merge is additive (manual entries always survive), idempotent, and prunes entries 30 days past. FOMC and CPI both classify MAJOR; entries may be bare ISO dates or prefixed (`FOMC:2026-09-16`). The digest flags the seeded calendar when its coverage ends within 60 days. **Not implemented:** per-underlying *earnings* dates are neither auto-seeded nor watched — deliberately deferred, since every traded underlying is an index/ETF with no earnings; an earnings feed becomes relevant only if a single-name underlying is ever added. Election days are deliberately NOT catalysts (they are the ADR-0010 stress episode — see [backend/calendars.py](../backend/calendars.py)).

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

> **Regime-engine variants:** the scoring matrix above is variant **V0** in the Executor (Paper) regime race ([design/executor-paper.md](design/executor-paper.md) §5). Variants **V1** (term-structure), **V2** (VRP-conditioned), and **V3** (repaired matrix, #134 — same weights, dimensions fixed: VIX/VIX3M ratio buckets for absolute VIX, VIX 252-day percentile applied once for per-underlying IVR, SMA200 for SMA20, daily-return dimension dropped, catalyst window 5 trading days) race in books B02/B03/B05/B06/B19/B20. Under every non-V0 variant, EVENT_CATALYST means **Do Nothing** — the long straddle/strangle menu entries ship disabled, so no strategy is eligible in that regime.

This menu is **enforced as a hard gate** in the Layer C scan (#136): PRIMARY + SECONDARY strategies are allowed, everything else is suppressed with a `REGIME GATE` reason. The enforced sets (spread strategies only — CSP/CC are outside the No-Stock Mandate) are:

| Regime | Allowed strategy types |
|---|---|
| CALM_BULL | BULL_PUT_SPREAD, BULL_CALL_SPREAD, IRON_CONDOR, BROKEN_WING_BUTTERFLY, CALENDAR_SPREAD |
| HIGH_VOL_NEUTRAL | IRON_CONDOR, BROKEN_WING_BUTTERFLY, CALENDAR_SPREAD + all four verticals |
| TRENDING_BEAR | BEAR_CALL_SPREAD, BEAR_PUT_SPREAD |
| EVENT_CATALYST | LONG_STRADDLE, LONG_STRANGLE (ship disabled ⇒ Do Nothing) |

Only the no-regime-gate control book B12 scans with the gate off ([ADR-0009](decisions.md#adr-0009--accelerated-experiment-matrix)).

**Source of truth:** [backend/regime.py](../backend/regime.py), [backend/market_data.py](../backend/market_data.py); gate enforcement in [backend/opportunity.py](../backend/opportunity.py) (`REGIME_ALLOWED_STRATEGIES`).

---

## Playbook matching (Layer C)

Loops Layer B telemetry against all active playbook definitions. Outputs a candidate menu for eligible playbooks only. Price, SMA20, and IVR resolve per playbook underlying through a telemetry-proxy map (`XSP → SPY`); non-SPY-scale underlyings (IWM, GLD) read executor-computed values from `index_history`, with an RV20 percentile rank standing in for IVR (#139). An underlying without telemetry is suppressed (`TELEMETRY`) and spec generation for it hard-blocks (`UNDERLYING_TELEMETRY`) — strikes are never derived from a different asset's price. Ineligible playbooks are hidden — not shown as disabled. Auto-generated strikes must display their exact derivation parameters beneath the order ticket. No black-box outputs.

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

**Position exposure gates — run before showing any candidate. These are the manual portfolio scan's gates (`portfolio_config.risk_profile`, B00/manual console scope, `backend/eligibility.py` + `backend/opportunity.py`) — a DIFFERENT scope from the lab book envelope caps below and in [ADR-0006](decisions.md#adr-0006--autonomy-roadmap-operator--executor-paper--executor-live)/[ADR-0009](decisions.md#adr-0009--accelerated-experiment-matrix) (#773):**

| Gate | Rule |
|---|---|
| MAX POSITIONS | **Manual portfolio scan cap** (`risk_profile.max_simultaneous_positions`, seeded 3): 3+ open shows no candidates. Display reason. Distinct from a lab book's own `Envelope.max_positions` (`backend/book_gates.py`, seeded 8 per ADR-0009) — a book's own count never trips this gate; see "Executor book scans" below. |
| MAX CAPITAL | **Manual portfolio scan cap** (`risk_profile.max_capital_deployed_pct`, seeded 85%): capital deployed ≥ 85% shows no candidates. Distinct from the **lab book envelope cap** (`Envelope.max_deployed_pct`, seeded 50%, [ADR-0006](decisions.md#adr-0006--autonomy-roadmap-operator--executor-paper--executor-live)) — the manual scan's 85% is a looser, human-supervised ceiling; a lab book's autonomous 50% is stricter by design. |
| DIRECTIONAL CONCENTRATION | 2+ positions same directional bias: suppress same-direction strategies as PRIMARY |
| UNDERLYING CONCENTRATION | Underlying already has open position: suppress new entries on that underlying |
| EARNINGS GATE | Earnings within 14 DTE: suppress all income strategies for that underlying |
| IVR GATE (INCOME) | IVR < 40: suppress CSP, CC, Iron Condor |
| IVR GATE (DEBIT) | IVR > 70: suppress naked long options, show spreads only |

**Executor book scans** run in `book_mode`: the DIRECTIONAL and UNDERLYING CONCENTRATION gates are skipped, because a lab book ladders multiple positions on one underlying by design — its concentration policy is the **lab book envelope cap** (`max_positions`, `max_same_strategy_expiry`, `max_deployed_pct` in [backend/book_gates.py](../backend/book_gates.py)'s `Envelope`), not the manual portfolio scan's MAX POSITIONS/MAX CAPITAL rows above. The manual console keeps all gates. The IVR gates can be disabled per book (`ignore_ivr`) for the B16 control only ([ADR-0009](decisions.md#adr-0009--accelerated-experiment-matrix)).

**Vol-aware delta cap** (`delta_cap_vix`, B33's knob — #816, the #814 disposition): a book carrying this knob scans with the effective short-leg delta target of **credit-structure playbooks** (iron condor, bull put / bear call spread, broken-wing butterfly) capped at `min(target_delta, delta_cap_vix / VIX close)` — at high VIX the same premium lives further out, so entries move proportionally OTM. The cap applies **only to the short legs of credit structures** (`capped_playbooks` in [backend/opportunity.py](../backend/opportunity.py)): debit verticals' ~0.50Δ long legs and the long strangle's buy legs (which reuse the `short_leg_delta` field) are never capped. Fail closed: a knob-on book with no usable VIX close sits out that night (`ENTRIES_BLOCKED_NO_VIX` audit) — it never inherits the scan's knob-off-only VIX fallback. B33 carries no backtest validation (the #814 program closed on its own pre-registered power terms); it is judged on forward paper evidence by the normal ADR-0010 machinery.

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
| Iron Condor long strikes | Playbook `spread_width_dollars` outside short strikes ($3 seed), $1 strike grid |
| Straddle | ATM — strike closest to current price |
| Strangle | 0.25-0.30 delta on both sides |
| Bull Call Spread — buy leg | ATM or first OTM above current price |
| Bull Call Spread — sell leg | Buy strike + playbook width ($5 seed) — width is the sizing authority for autonomous entries (#94) |
| Bear Put Spread — buy leg | ATM or first OTM below current price |
| Bear Put Spread — sell leg | Buy strike − playbook width ($5 seed) (#94) |
| Bull Put Spread — short leg | Delta closest to 0.30 below current price |
| Bull Put Spread — long leg | Playbook width below short strike ($3 seed), $1 strike grid |
| Bear Call Spread — short leg | Delta closest to 0.30 above current price |
| Bear Call Spread — long leg | Playbook width above short strike ($3 seed), $1 strike grid |

> Seed widths were narrowed 2026-08-18 (#94) so max loss fits the ADR-0006 per-trade cap (2.5% of book basis): credit structures $3 wings, debit spreads $5.

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
| Strike sanity | Buy leg strike of any vertical spread (bull or bear, call or put) is more than 10% OTM from current price (`STRIKE_SANITY`, #743). Originally bull-only; extended to bears too — the check guards against a broken strike derivation, a concern that isn't directional |
| Expiration arithmetic | Expiration date is in the past, or under 14 DTE |
| Premium reasonableness | Suggested premium ≤ 0 or > underlying price |
| Position count | Trade would bring total open positions above 3 |
| Playbook disabled | The playbook's `enabled` flag is false (`PLAYBOOK_DISABLED`) |
| Ex-div assignment | Spec carries a SHORT call on an American-style dividend payer (SPY/IWM/TLT) whose expiration spans a projected ex-dividend date (`EX_DIV_ASSIGNMENT`, #130). XSP is immune (European, cash-settled); GLD pays no dividend |

Short-put interest-carry assignment risk (#736) has no entry-side hard block: unlike ex-div, there is no calendar to check a not-yet-opened position's expiration against, and the live underlying price the check reads isn't known until the position is live. It is Layer A-only — see the priority table above.

**Warnings (shown, require explicit confirmation to proceed):**

| Check | Warning Condition |
|---|---|
| Regime consistency | Trade direction inconsistent with current regime |
| Duplicate underlying | Open position already exists on this underlying |
| Break-even realism | Straddle break-even requires move > 2 standard deviations |
| Strategy novelty | First time this strategy type is being used — recommend paper mode |

**Source of truth:** [backend/opportunity.py](../backend/opportunity.py) (gates, strike derivation, validation); per-share trade economics (max loss/gain, break-evens) computed exclusively by [backend/pricing.py](../backend/pricing.py); ex-dividend calendar and assignment rules in [backend/assignment_defense.py](../backend/assignment_defense.py) — the calendar is a static, operator-maintained projection (SPY/IWM quarterly, TLT monthly), and the executor's nightly digest flags any calendar whose coverage ends within 60 days.

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

Layer A surfaces a defensive-roll candidate for credit verticals under pressure (buyback ≥150% of credit collected — halfway to the 2× loss limit — or ≤21 DTE), suggesting strikes shifted by one spread width in the rule's direction and an expiration one monthly cycle (28 days) later. `POST /api/positions/{id}/roll` enforces every rule at execution: debit rolls, third rolls, wrong-direction strikes, and earlier expirations are rejected with the reason. A roll continues the same position: `entry_premium` becomes the cumulative net credit collected, so the 50%-profit and 2×-loss exit rules keep operating on real economics.

**Source of truth:** [backend/observation.py](../backend/observation.py) (`derive_roll_candidate`), [backend/main.py](../backend/main.py) (`roll_position` enforcement), [backend/models.py](../backend/models.py) (`rolls` cap).

---

## Operational intent journal & post-mortem

### Intent journal (required before save)
Tracks subjective patterns and thesis invalidation. Enforced at position creation — a `POST /api/positions` with an incomplete journal returns `422`. Schema: [`OperationalJournalEntry`](data-models.md#operationaljournalentry).

### Closure post-mortem
Closing a position freezes the trade log into an immutable historical record (outcome, realized P&L, actual underlying move, exit trigger, lesson tags, override flag). Schema: [`ClosurePostMortem`](data-models.md#closurepostmortem). The system never reports percentages without the sample size N, and never populates the dashboard with fictional data.

**External closes settle at broker values.** When a position is closed without an executor order — expiry, exercise/assignment, or a manual broker-side close (an `EXTERNAL_CLOSE` reconciliation drift, [design/executor-paper.md](design/executor-paper.md) §4.4) — the post-mortem records the **broker's actual settlement value**, never the system's last marked value. Live Gate expectancy is built on real outcomes.

**Executor-placed fills settle at the fills ledger's real prices too (#666).** An executor entry or close order books its position/post-mortem/`cash_balance` movement from the actual per-leg fill prices on the `fills` table when they're available — never the order's `limit_price` alone, which is what was ASKED for, not what the market gave. Booking the limit unconditionally is a second, implicit slippage haircut layered under ADR-0007's explicit $5/contract one, understating book cash and reading realized P&L / expectancy systematically low. `limit_price` is used only as a fallback when no fills are yet on the ledger for a FILLED order (audited as `FILL_PRICE_UNAVAILABLE_LIMIT_FALLBACK`) — never silently. Measured realized slippage against the ADR-0007 haircut assumption is its own surface: `backend/analysis.py`'s `fill_quality_report` (Analysis tab's Fill Quality card), unaffected by this fix since it already reads fills directly.

**Expiries settle at computed intrinsic, not the last evening mark (#667).** `max(0, S−K)` for a CALL, `max(0, K−S)` for a PUT, per leg, from the underlying's `index_history` close on the expiration date — netted the same way the nightly live-quote reprice nets legs (LONG adds, SHORT subtracts; sign flips CREDIT vs DEBIT). A spread expiring worthless settles at exactly 0: its last-priced evening's mark still carries residual time value a contract with none left cannot actually have, which booked a small systematic error on every worthless expiry. Falls back to the last mark — audited (`EXPIRY_SETTLED_AT_MARK_FALLBACK`), never silently — only when the underlying's close isn't in `index_history` (an underlying outside the ten tracked symbols, e.g. AAPL, or a gap night with no fetch).

### Live Gate metrics (console)

The Books tab computes four ADR-0006 conditions per book, each with a real pass/fail value:

- **Trades:** ≥ 30 closed (CLOSED or EXPIRED) positions.
- **Duration:** ≥ 3 months since the book's `created_at`.
- **Zero breaches:** no `ENVELOPE_BREACH_POSTHOC` audit events for the book.
- **Expectancy after haircut:** mean realized P&L per closed trade minus a **$5/contract slippage haircut** ($0.05/share per combo round trip), minus **one standard error** of that same per-trade haircut P&L, must be ≥ 0 (ADR-0010 amendment, #656 — an interim floor, not the final threshold; see the ADR). SE is stdev (n-1 denominator) / √n from closed trades; n<2 or an undefined SE does not pass. The haircut exists because IBKR paper combo fills are optimistic ([ADR-0007](decisions.md#adr-0007--interactive-brokers-for-paper-and-live-execution)); raw paper expectancy is never trusted, and a bare point estimate against 0.0 is a coin flip at n≈30 for a true-zero-edge book.

ADR-0010 adds four further promotion conditions with no detection machinery yet (#215, #655): **stress episode observed** (a VIX close ≥ 25 or a ≥ 5% SPY close-to-close drawdown from the gate window's running peak, while the book held an open position — a calm window is an unfinished sample, whatever its length), **beats the SPY benchmark** (mechanical comparison of realized $10K-basis return against the SPY price return over the same window, `backend/benchmark.py`), **beats the same-engine baseline** (ADR-0009), and the **composition limit** (at most one single-knob amendment may be grafted onto a winning baseline, and only if that knob book beat its own same-engine baseline over the same window — anything more returns to paper for its own confirmation window). These four render on the checklist as `not_yet_evaluated` rows, not as silently-passing or silently-absent conditions: `eligible` is **un-claimable** while any of them is unevaluated, a deliberately stronger (never weaker) standard than the bare four-way AND of the original ADR-0006 conditions alone would give.

Max drawdown is peak-to-trough on the cumulative realized P&L of closed trades in entry-date order (there is no per-book equity-history table pre-launch, so open-position marks are excluded).

The checklist also carries **`as_raced_config_hash`** (#658): the `config_hash` whose evidence era (#534) the displayed trades/months/expectancy were actually accumulated under — not necessarily the book's current `config_hash`, if it has since resynced to a new (and, right after a resync, evidence-less) era. This is provenance for the composition limit above: a leaderboard of clean one-knob books does not by itself mean a config that grafts their knobs together ever raced at all. The mechanical promotion-time check this enables (a proposed live config's hash must equal an as-raced hash whose own gate conditions passed) lands with the future promotion workflow (~#215-adjacent); today this field only surfaces the provenance, rendered in the Books tab next to each book's gate cells.

The **empirical-null drill** (`backend/empirical_null_drill.py`, `pixi run empirical-null-drill`, #657) measures a selection null for this leaderboard: it pools every closed trade's haircut P&L across current-era, promotion-eligible books (B00 and the tail-hedge B32 excluded, ADR-0012) and bootstrap-resamples synthetic arms matching the real matrix's shape, reporting the null distribution's percentiles for max-per-arm expectancy (and expectancy − 1·SE) and where each real book falls against it. Ledger-only, read-only — no market simulation, no Gateway. It answers arm selection against multiplicity, not whether the strategy works at all; a positive max-per-book value in the null is expected, not a bug. The report is a measurement, not an ADR threshold on its own — promoting a measured percentile to the operative bar (superseding the ADR-0010 interim 1-SE floor) is its own deliberate amendment.

**Source of truth:** [backend/main.py](../backend/main.py) (`create_position`, `close_position`), [backend/models.py](../backend/models.py), [backend/console.py](../backend/console.py) (Live Gate metrics), [backend/empirical_null_drill.py](../backend/empirical_null_drill.py) (empirical-null drill).

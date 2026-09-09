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

A position with a genuine non-terminal CLOSE order already submitted/staged suppresses the P1/P2 action text as "already in flight" rather than re-demanding a close (#602). A resting take-profit order (the executor stages one, GTC, alongside every open) is a non-terminal CLOSE-action order too, but is never treated as in flight — it never suppresses a real P1/P2 action; it only labels an otherwise-quiet position "Take-profit resting @ \<limit\>" (#967).

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

A HEDGE-role playbook (`role`, `backend/states.py`, #967 — e.g. the XSP tail-hedge put) is exempt from the whole regime-conflict table: its positions are meant to look wrong-direction relative to the regime, and flagging that as a conflict would demand closing the insurance right when it's about to pay off. P1 loss-limit and P2 DTE checks still apply unchanged; absent `role` (every pre-#967 playbook) means DIRECTIONAL, not exempt.

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

> **Regime-engine variants:** the scoring matrix above is variant **V0** in the Executor (Paper) regime race ([design/executor-paper.md](design/executor-paper.md) §5). Variants **V1** (term-structure), **V2** (VRP-conditioned), and **V3** (repaired matrix, #134 — same weights, dimensions fixed: VIX/VIX3M ratio buckets for absolute VIX, VIX 252-day percentile applied once for per-underlying IVR, SMA200 for SMA20, daily-return dimension dropped, catalyst window 5 trading days) race in books B02/B03/B05/B06/B19/B20. Under every non-V0 variant, EVENT_CATALYST means **Do Nothing** — the long straddle/strangle menu entries ship disabled, so no strategy is eligible in that regime. **B35** (#993) is the one book intentionally reaching a long-vol strategy through the V0 regime table: it whitelists `xsp_long_straddle_catalyst_v1`, re-enabling the long straddle for that book alone. B12 and B32 are `ignore_regime` exceptions, so apart from those exceptions every other V0 book reads EVENT_CATALYST as **Do Nothing**.

This menu is **enforced as a hard gate** in the Layer C scan (#136): PRIMARY + SECONDARY strategies are allowed, everything else is suppressed with a `REGIME GATE` reason. The enforced sets (spread strategies only — CSP/CC are outside the No-Stock Mandate) are:

| Regime | Allowed strategy types |
|---|---|
| CALM_BULL | BULL_PUT_SPREAD, BULL_CALL_SPREAD, IRON_CONDOR, BROKEN_WING_BUTTERFLY, CALENDAR_SPREAD |
| HIGH_VOL_NEUTRAL | IRON_CONDOR, BROKEN_WING_BUTTERFLY, CALENDAR_SPREAD + all four verticals |
| TRENDING_BEAR | BEAR_CALL_SPREAD, BEAR_PUT_SPREAD |
| EVENT_CATALYST | LONG_STRADDLE, LONG_STRANGLE (ship disabled ⇒ Do Nothing) |

Five books scan with the gate off (`ignore_regime` in [backend/seeds.py](../backend/seeds.py)): the B12 control ([ADR-0009](decisions.md#adr-0009--accelerated-experiment-matrix)); the RV-gated diversifiers B10 (GLD), B22 (TLT) and B30 (AAPL earnings), whose selection discipline is the RV-rank IVR gate and entry filters because SPY-derived regimes are blind to their underlyings; and the B32 tail-hedge sleeve ([ADR-0012](decisions.md#adr-0012--tail-hedge-sleeve-is-judged-on-convexity-never-expectancy)), whose LONG_PUT sits in no regime's allowed set and trades only because the gate is off. B12 is the only one of the five that is a control for the gate itself.

**Source of truth:** [backend/regime.py](../backend/regime.py), [backend/market_data.py](../backend/market_data.py); gate enforcement in [backend/opportunity.py](../backend/opportunity.py) (`REGIME_ALLOWED_STRATEGIES`).

---

## Playbook matching (Layer C)

Loops Layer B telemetry against all active playbook definitions. Outputs a candidate menu for eligible playbooks only. Price, SMA20, and IVR resolve per playbook underlying through a telemetry-proxy map (`XSP → SPY`); non-SPY-scale underlyings (IWM, GLD, TLT, AAPL) read executor-computed price/SMA20 values from `index_history` at scan time (#139). **IVR source, every underlying:** there is no live IV-rank feed. IVR is the RV20 percentile rank (`regime_variants.rv_rank`) of the underlying's closes in `index_history`, computed nightly — for SPY (and XSP through the proxy) inside `refresh_market_state`, which stores it in `market_state.underlying_ivrs` after `persist_index_history` has written the night's close (#989); for the non-SPY-scale underlyings at scan time, published into the same map without overwriting stored entries. Nothing hand-types an IVR: a manual SPY value posted through the market-state API is replaced on the next refresh, and an underlying with fewer than 60 closes has no IVR at all and stays outside every IVR window (fail closed). An underlying without telemetry is suppressed (`TELEMETRY`) and spec generation for it hard-blocks (`UNDERLYING_TELEMETRY`) — strikes are never derived from a different asset's price. Ineligible playbooks are hidden — not shown as disabled. Auto-generated strikes must display their exact derivation parameters beneath the order ticket. No black-box outputs.

Playbook definitions carry an `enabled` flag. Disabled playbooks are skipped entirely by the Layer C scan (never shown, even as suppressed) and spec generation for them hard-blocks with `PLAYBOOK_DISABLED`. The seed long straddle/strangle playbooks ship disabled by default: buying volatility into a known catalyst fights pre-event IV inflation and post-event crush, so they are kept for catalyst-study use only — except `xsp_long_straddle_catalyst_v1`, which B35 (#993) whitelists and re-enables via `playbook_overrides` to measure the hypothesis directly instead of only studying it.

**Catalyst entry block** (`EntryFilters.catalyst_block_trading_days`, #990): a playbook refuses new entries whenever the nearest relevant catalyst (scope per #317 — market-wide, plus this underlying's own scoped events) sits within N **TRADING** days, N > 0, counting today as 0 and holiday-aware (`backend.calendars.trading_days_between`); 0 means no block. This is the BLOCK side and is measured in trading days because the pre-event vol premium the rule fights concentrates in the sessions before the event, not the calendar days; the REQUIRE side (`require_catalyst_14dte`, wanting an event inside the expiry cycle for long-vol plays) is a different question and stays a 14-**calendar**-day window. Every seed playbook that blocks at all ships `catalyst_block_trading_days: 3` (`DEFAULT_CATALYST_BLOCK_TRADING_DAYS`, `backend/models.py`), chosen from a corpus backtest ([ADR-0009's #990 amendment](decisions.md#adr-0009--accelerated-experiment-matrix)) after the prior 14-calendar-day default was measured shutting ~62% of the remaining 2026 trading nights for 32 of 34 books (#984). A `playbook_snapshot` frozen before #990 carries the legacy `block_catalyst_14dte` boolean instead; `EntryFilters`'s validator maps `True` to the default window and `False` to 0 on read, so old positions keep validating.

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

**Minimum-credit floor** (`min_credit_ratio`, B34's knob — #820, #818 backlog item 1): a book carrying this knob refuses a CREDIT entry whose `|net_mid|` is below `min_credit_ratio × width_bound` — the same-type strike span the #282 quote-sanity bound already computes in `_try_place_entry` ([backend/executor.py](../backend/executor.py)) — auditing `ENTRY_REFUSED_THIN_CREDIT` with the net mid, width, ratio, and dollar floor. Penny credits against dollar risk are the shape the 2020 bleed and the #814 round-3 fill analysis both showed. Scope: CREDIT structures only (`premium_direction`); debit entries pay their `|net_mid|` as max loss and are never checked. A zero width bound (calendars, straddles/strangles — no same-type multi-strike span) leaves the floor inert: there is no denominator. The floor is a **quality gate**, not a rejection — `ENTRY_REFUSED_THIN_CREDIT` is deliberately outside `anomaly._REJECTION_EVENTS`, so a strict knob refusing every night never trips `REPEATED_REJECTION`. It only ever refuses (no fallback fabricates a quote; unpriceable entries are already skipped upstream), and it takes no new clock or data inputs, so no sit-out arm exists. The backtest driver mirrors it at fill time: a knob-on book abandons a thin worst-side credit fill with reason `THIN_CREDIT`, counted alongside `NO_SNAPSHOT`/`SIGN_INVERTED`. B34 carries no backtest validation; forward paper evidence only, judged by the normal ADR-0010 machinery.

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

## Shared-account order contention (#853)

The 34 books share one broker account, so IBKR's account-wide order rules make books contend at the ORDER layer even though they are independent at the accounting layer (see [decisions.md](decisions.md)). IBKR refuses open orders on both sides of the same US Option contract — and a staged GTC profit-taker child is an open order on the opposite side of its entry's legs, so after the first contested entry is staged, every later candidate sharing a leg on the opposite effective side would be refused at preview.

Rules:

- **Pre-preview collision gate**: before previewing a candidate, its legs (OCC symbol + effective broker side) are checked against every resting order's legs (`STAGED`/`SUBMITTED`/`PARTIAL`, any book, any action — CLOSE orders invert the stored direction). On collision the candidate is **skipped**, with a typed `CROSS_BOOK_ORDER_COLLISION` audit event carrying the colliding `order_ref`; the broker never sees it and it never counts toward `REPEATED_REJECTION`. For any resting leg lacking an explicit `occ`, the OCC symbol is derived when the order's position or metadata names the underlying (#955); a leg whose underlying cannot be resolved either way is still skipped, now with a `logger.warning` naming the order_ref rather than silently.
- **Randomized nightly book order**: books are processed in a shuffled order each run (`BOOK_ORDER_SHUFFLED` audit event logs the seed and order), so no fixed-numbered book systematically wins contested legs; displacement is fair in expectation and countable per book from the typed events.
- **Classified preview refusals**: refusal reasons are classified — collision-class and riskless-combination-class refusals do not count toward the `REPEATED_REJECTION` halt; permissions-class refusals fire an immediate, non-latching `PERMISSIONS_REFUSED` finding (needs-human: retrying cannot fix a missing account permission, and halting all books is the wrong blast radius); infra-class refusals (#927 — whatIfOrder API error or timeout, IBKR's whatIf answer itself unusable) do not count toward `REPEATED_REJECTION` either, and instead accumulate toward their own same-night `PREVIEW_INFRA_FAILURE` rule (spec/supervision.md §6.2) — a gateway outage is not evidence the broker's rules are being modeled wrong. Unclassified reasons still pool into the halt count.

**Source of truth:** [backend/anomaly.py](../backend/anomaly.py) (collision check, refusal classification), [backend/executor.py](../backend/executor.py) (shuffle, gate wiring).

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
- **Duration:** ≥ 3 months since the book's evidence-era start (see the era clock below).
- **Zero breaches:** no `ENVELOPE_BREACH_POSTHOC` audit events for the book **since its last config change** — the count is era-scoped (#533), so a breach written under a retired config does not follow the book into its new era.

**One era clock (#534, operator ruling 2026-09-08 on #984).** The breach count, the Duration row, and the stress-episode and benchmark windows below all measure from the same instant: the book's current evidence era, which opens at the market date of its last `BOOK_CONFIG_SYNCED` audit event (a seed-sync that actually changed the config hash), else of its `created_at`. The checklist carries that date as `era_start` and the Books tab renders it beside the as-raced hash, so the reader can see the date every windowed row counts from. No row uses a different clock.
- **Expectancy after haircut:** mean realized P&L per closed trade minus a **$5/contract slippage haircut** ($0.05/share per combo round trip) minus that trade's **actual ledgered commissions** (`backend/console.py`'s gate-expectancy computation nets both out — the haircut proxies fill quality, commissions are the broker's real fees, and they're separate terms), minus **one standard error** of that same per-trade haircut-and-commission P&L, must be ≥ 0 (ADR-0010 amendment, #656 — an interim floor, not the final threshold; see the ADR). SE is stdev (n-1 denominator) / √n from closed trades; n<2 or an undefined SE does not pass. The haircut exists because IBKR paper combo fills are optimistic ([ADR-0007](decisions.md#adr-0007--interactive-brokers-for-paper-and-live-execution)); raw paper expectancy is never trusted, and a bare point estimate against 0.0 is a coin flip at n≈30 for a true-zero-edge book.

ADR-0010 adds four further promotion conditions (#655). Two are **computed** per book (#215), windowed to the book's **gate window** — its current evidence era (#534): the market date of the last `BOOK_CONFIG_SYNCED` for a synced book, else of `created_at`, through the evaluation date, the same clock the Duration row runs on, reading only era positions:

- **Stress episode observed** (`stress_episode_ok`, `stress_episode_check`), in the shape ratified in #738 — **episode × meaningful deployment**. An *episode* is an `index_history` date inside the window with a VIX close ≥ 25 or a SPY close-to-close drawdown ≥ 5% from the **window's** running peak (the peak restarts at the window start; a pre-window high cannot manufacture an in-window episode). The bare "a position was held through that session" overlap is surfaced (`episode_while_position_open`, rendered as "held, under-deployed" when it disagrees with the verdict) but is **not the bar** — held ≠ exposed. The row passes when on at least one episode date the book's dollars at risk **through that session** (Σ `max_loss` × contracts × 100 over positions held during the session: a position counts from the market date **after** its `entry_date` — entries are stamped by the 18:45 ET run, after the close that defines the episode, so a position opened on the episode evening was not exposed to it — through its post-mortem `exit_date`, else its expiration date if closed, else today) were **≥ 50%** (`STRESS_DEPLOYMENT_FRACTION`, pre-registered in the ADR-0010 amendment) of the book's **normal gate-window deployment** — the mean of that same daily figure over the window's **deployed** `index_history` dates (days with any position; flat days are not in the denominator, or a book in the market one day in four would pass at a fraction of its usual size) — and strictly positive. A fully-deployed book that stayed calm through the episode has passed a stress test; a near-flat book has not taken it; a calm window is an unfinished sample whatever its length. Supporting numbers (2 dp, the precision the verdict was taken at; the drawdown is floored so it can never render as 5.00% beside "no episode"): peak VIX close and deepest SPY drawdown in the window, episode-date count, deployment through the best-covered episode session vs the dollar bar (`required_deployment` = 50% of normal), and the book's **max adverse excursion** in `book_mtm_history` marks during the episode (last pre-episode mark inside the window − lowest episode mark; informational, never gating, composes with the #717 tail row). A window with no `index_history` rows at all is fail-closed for eligibility but renders as "no data", not as a tested-and-failed row.
- **Beats the SPY benchmark** (`benchmark_ok`, `benchmark_check`): the book's realized P&L on closed era trades, **net of the $5/contract haircut and ledgered commissions** (the identical per-trade figure the expectancy row, its SE, the tail row and the empirical-null drill judge — raw paper P&L is never trusted, and this row is no exception), as a return on its basis must **exceed** the SPY price return between the first and last SPY closes inside the window (`backend/benchmark.py`'s `spy_window_return`, the same definition the digest's benchmark line uses; dividends excluded). The comparison is realized-vs-marked: the book side excludes open-position marks while SPY's side is fully marked, so a book carrying unrealized gains is understated against SPY and one carrying unrealized losses is overstated — exact only when the book is flat at the window end. Fail-closed: no closed trades, or fewer than two SPY closes in the window, renders `fail` with the reason in the row's detail — never a silent pass (the console renders these input-missing cases as "no data").

The remaining two — **beats the same-engine baseline** (ADR-0009) and the **composition limit** (at most one single-knob amendment may be grafted onto a winning baseline, and only if that knob book beat its own same-engine baseline over the same window — anything more returns to paper for its own confirmation window) — still have no detection machinery and render as `not_yet_evaluated` rows, not as silently-passing or silently-absent conditions: `eligible` is **un-claimable** while either is unevaluated, even when every computed condition passes — a deliberately stronger (never weaker) standard than the bare AND of the evaluated conditions would give.

Max drawdown is peak-to-trough on the cumulative realized P&L of closed trades in entry-date order (there is no per-book equity-history table pre-launch, so open-position marks are excluded).

The checklist also carries **`as_raced_config_hash`** (#658): the `config_hash` whose evidence era (#534) the displayed trades/months/expectancy were actually accumulated under — not necessarily the book's current `config_hash`, if it has since resynced to a new (and, right after a resync, evidence-less) era. This is provenance for the composition limit above: a leaderboard of clean one-knob books does not by itself mean a config that grafts their knobs together ever raced at all. The mechanical promotion-time check this enables (a proposed live config's hash must equal an as-raced hash whose own gate conditions passed) lands with the future promotion workflow (~#215-adjacent); today this field only surfaces the provenance, rendered in the Books tab next to each book's gate cells.

The **empirical-null drill** (`backend/empirical_null_drill.py`, `pixi run empirical-null-drill`, #657) measures a selection null for this leaderboard: it pools every closed trade's haircut P&L across current-era, promotion-eligible books (B00 and the tail-hedge B32 excluded, ADR-0012) and bootstrap-resamples synthetic arms matching the real matrix's shape, reporting the null distribution's percentiles for max-per-arm expectancy (and expectancy − 1·SE) and where each real book falls against it. Ledger-only, read-only — no market simulation, no Gateway. It answers arm selection against multiplicity, not whether the strategy works at all; a positive max-per-book value in the null is expected, not a bug. The report is a measurement, not an ADR threshold on its own — promoting a measured percentile to the operative bar (superseding the ADR-0010 interim 1-SE floor) is its own deliberate amendment.

**Source of truth:** [backend/main.py](../backend/main.py) (`create_position`, `close_position`), [backend/models.py](../backend/models.py), [backend/console.py](../backend/console.py) (Live Gate metrics, stress-episode and benchmark rows), [backend/benchmark.py](../backend/benchmark.py) (`spy_window_return`), [backend/empirical_null_drill.py](../backend/empirical_null_drill.py) (empirical-null drill).

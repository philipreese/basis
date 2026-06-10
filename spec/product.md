# Product Spec — Vision, Scope & Requirements

> Part of the [modular specification](README.md). Source: §1 of the [archived v8 spec](archive/project_spec_v8.md).

## Vision

A daily decision-support web application and automated playbook execution engine tailored for a **cash-settled Roth IRA account** (no margin availability, no short stock). It tells the user, each evening, what to do about open positions and which codified playbooks the current market satisfies — then generates concrete order specifications for the user to place manually.

## Structural System Mandates

- **Prescriptive Automation via Playbooks:** The automation engine is strictly attached to codified user playbooks, not market predictions. The system automatically identifies when active market telemetry satisfies entry rules, instantly auto-generating matching order ticket specifications.
- **No Underlying Share Ownership:** The codebase contains no mechanisms for tracking stock assignment or covered call execution. Capital deployment is strictly restricted to defined-risk spreads and pure option premium structures.
- **Immutability of Historical Evidence:** Every trade log preserves an immutable deep-copy snapshot of the exact playbook ruleset active at the moment of trade execution to prevent data drift. See [ADR-0003](decisions.md#adr-0003--playbook-snapshot-immutability).
- **Common Sense First:** The system must never produce an output that is obviously wrong and then wait for the user to object. All validation runs before output is displayed. A system that says "you're right, I shouldn't have done that" after generating a bad output has failed.

## What This System Is NOT

- An autonomous trading bot — the user approves every trade
- A signal service or alpha generator — it works with user-supplied and API-fetched market data
- A backtesting engine — that is a separate project
- A financial advisor — it implements the user's own defined rules, not recommendations

### Explicit non-goals (do not build)

- No charting — Thinkorswim handles this
- No autonomous execution — user approves every trade
- No covered calls or share-assignment tracking — no share ownership in this system
- No social features, sharing, or multi-user
- No strategy backtesting — separate project
- No LLM/AI integration in the initial build — the system implements rules, not judgments (see [ADR-0001](decisions.md#adr-0001--rules-engine-not-llm))
- No "proceed anyway" bypass on hard blocks
- No fictional historical data in the performance dashboard

## Current Operational State

- **Brokerage:** Charles Schwab Roth IRA — funded, options approved Level 3 (spreads)
- **Platform:** Thinkorswim desktop — paper trading active
- **Capital:** $10,000 transferred and available
- **Execution:** Manual via Thinkorswim for now. Alpaca IRA API integration is a future layer — see [roadmap.md](roadmap.md) and [ADR-0002](decisions.md#adr-0002--manual-sandbox-first-alpaca-behind-env-vars).
- **System initializes in Manual Sandbox Mode.** Live API endpoints remain decoupled behind environment variables (`ALPACA_LIVE_MODE = false`). The platform runs entirely on manual local logging during the capital migration window.

## Functional Requirements (distilled)

The system runs a three-layer evening pipeline (see [architecture.md](architecture.md)). At a requirements level it must:

1. **Observe (Layer A):** Scan every open position and assign a lifecycle priority (P1 CLOSE NOW / P2 CLOSE SOON or REVIEW / P3 MONITOR / OK), aggregate portfolio Greeks against limits, and flag exposure-safeguard breaches. Position management takes absolute priority over new entries.
2. **Contextualize (Layer B):** Collect market telemetry (SPY vs SMA20, VIX, per-underlying IVR, catalyst calendar) and classify the market regime via a weighted scoring matrix, showing all four regime scores — not just the winner.
3. **Find opportunities (Layer C):** Match telemetry against active playbooks, apply exposure gates, and emit candidate cards with fully-derived strike parameters (no black-box outputs).
4. **Specify & journal (§5):** Generate a complete trade specification, run pre-output validation (uncircumventable hard blocks + acknowledgeable warnings), and require a complete operational intent journal before a position is saved.
5. **Enforce exits (§6):** Apply non-negotiable exit rules at every session; never suggest holding past a trigger.
6. **Retrospect (§7):** Freeze closed trades into immutable post-mortems, log every opportunity (taken or bypassed), and report per-playbook diagnostics with sample sizes — no fictional data.

Detailed rules for items 1–6 live in [domain-rules.md](domain-rules.md); the data shapes live in [data-models.md](data-models.md).

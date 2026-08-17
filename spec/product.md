# Product Spec — Vision, Scope & Requirements

> Part of the [modular specification](README.md). Source: §1 of the [archived v8 spec](archive/project_spec_v8.md).

## Vision

An evening options-trading system for a **Roth IRA** (defined-risk structures only, no stock ownership) that is advancing through three autonomy levels ([ADR-0006](decisions.md#adr-0006--autonomy-roadmap-operator--executor-paper--executor-live)): **Operator** — the pipeline runs on a schedule and tells the human what to do about open positions and which codified playbooks the market satisfies; **Executor (Paper)** — the system places its own orders in a paper account, racing configurations across virtual books; **Executor (Live)** — the system trades the real IRA once the Live Gate clears. At every level, position management precedes opportunity hunting, and every output is rule-derived and pre-validated.

## Structural System Mandates

- **Prescriptive Automation via Playbooks:** The automation engine is strictly attached to codified user playbooks, not market predictions. The system automatically identifies when active market telemetry satisfies entry rules, instantly auto-generating matching order ticket specifications.
- **No Underlying Share Ownership:** The codebase contains no mechanisms for tracking stock assignment or covered call execution. Capital deployment is strictly restricted to defined-risk spreads and pure option premium structures.
- **Immutability of Historical Evidence:** Every trade log preserves an immutable deep-copy snapshot of the exact playbook ruleset active at the moment of trade execution to prevent data drift. See [ADR-0003](decisions.md#adr-0003--playbook-snapshot-immutability).
- **Common Sense First:** The system must never produce an output that is obviously wrong and then wait for the user to object. All validation runs before output is displayed. A system that says "you're right, I shouldn't have done that" after generating a bad output has failed.

## What This System Is NOT

- A signal service or alpha generator — it works with user-supplied and API-fetched market data
- A backtesting engine — that is a separate project
- A financial advisor — it implements the user's own defined rules, not recommendations
- A discretionary trader — autonomy means executing the codified rules unattended, never improvising beyond them ([ADR-0001](decisions.md#adr-0001--rules-engine-not-llm) extends to order placement)

### Explicit non-goals (do not build)

- No charting — the brokerage platform handles this
- No live trading before the Live Gate clears ([ADR-0006](decisions.md#adr-0006--autonomy-roadmap-operator--executor-paper--executor-live))
- No covered calls, cash-secured puts, or share-assignment strategies — the No-Stock Mandate ([CONTEXT.md](../CONTEXT.md)) forbids holding the underlying at any point
- No social features, sharing, or multi-user
- No strategy backtesting — separate project
- No LLM/AI integration in the initial build — the system implements rules, not judgments (see [ADR-0001](decisions.md#adr-0001--rules-engine-not-llm))
- No "proceed anyway" bypass on hard blocks
- No fictional historical data in the performance dashboard

## Current Operational State

- **Capital:** $10,000 in a Charles Schwab Roth IRA (options approved for spreads). Transfers to an Interactive Brokers IRA-Margin account after the Live Gate clears ([ADR-0007](decisions.md#adr-0007--interactive-brokers-for-paper-and-live-execution)).
- **Autonomy level:** pre-Operator — the pipeline still runs manually from the UI; scheduled operation is tracked in [#23](https://github.com/philipreese/basis/issues/23), the Executor (Paper) build in [#32](https://github.com/philipreese/basis/issues/32).
- **Execution:** manual at the brokerage. No order-placement code exists yet; it arrives with Executor (Paper) behind the Trading Mode isolation design (ADR-0006).

## Functional Requirements (distilled)

The system runs a three-layer evening pipeline (see [architecture.md](architecture.md)). At a requirements level it must:

1. **Observe (Layer A):** Scan every open position and assign a lifecycle priority (P1 CLOSE NOW / P2 CLOSE SOON or REVIEW / P3 MONITOR / OK), aggregate portfolio Greeks against limits, and flag exposure-safeguard breaches. Position management takes absolute priority over new entries.
2. **Contextualize (Layer B):** Collect market telemetry (SPY vs SMA20, VIX, per-underlying IVR, catalyst calendar) and classify the market regime via a weighted scoring matrix, showing all four regime scores — not just the winner.
3. **Find opportunities (Layer C):** Match telemetry against active playbooks, apply exposure gates, and emit candidate cards with fully-derived strike parameters (no black-box outputs).
4. **Specify & journal (§5):** Generate a complete trade specification, run pre-output validation (uncircumventable hard blocks + acknowledgeable warnings), and require a complete operational intent journal before a position is saved.
5. **Enforce exits (§6):** Apply non-negotiable exit rules at every session; never suggest holding past a trigger.
6. **Retrospect (§7):** Freeze closed trades into immutable post-mortems, log every opportunity (taken or bypassed), and report per-playbook diagnostics with sample sizes — no fictional data.

Detailed rules for items 1–6 live in [domain-rules.md](domain-rules.md); the data shapes live in [data-models.md](data-models.md).

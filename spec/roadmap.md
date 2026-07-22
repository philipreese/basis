# Roadmap — Themes & Direction

> Part of the [modular specification](README.md). The granular backlog now lives in **GitHub issues** — the [project board](https://github.com/users/philipreese/projects/1) is the source of truth for what's open and what's done. This file keeps the high-level phases and the rationale for their ordering; it is **not** a task list. Each theme below is broken into one or more issues — see the board for status and detail.

## Near-term — UX & polish ✅ shipped
Design-token consistency, Fetch-Live feedback, inline telemetry validation, override-justification capture, and the Greek-limit CTA — plus an accessibility pass (modal autofocus, table semantics, severity by icon + text). Landed 2026-06-10 (commit `b6a96a7`); see the [CHANGELOG](../CHANGELOG.md). One near-term item remains open and is tracked as an issue: **session re-lock discoverability**.

## Playbook library composition ✅ shipped
The seed library (5 playbooks) had only one premium-selling playbook (Iron Condor, HIGH_VOL_NEUTRAL only) — the two directional regimes were covered only by lower-win-rate debit spreads, and the two long-vol event playbooks (straddle/strangle) fight pre-event IV inflation and post-event crush, making them the weakest structures for a mechanical system. Added `BULL_PUT_SPREAD` and `BEAR_CALL_SPREAD` credit-spread playbooks (one per directional regime) and an `enabled` flag; straddle/strangle now ship disabled by default (catalyst-study only). Landed 2026-07-22 (issue #20) — see [CHANGELOG](../CHANGELOG.md) and [gap-analysis.md](gap-analysis.md).

## Priority — scheduled scan + push-approval loop
**Revision 2026-07-22.** The stated goal of this project is decision-support *without requiring the user to be an options expert or to remember to start a session* — win more often than lose, compound steadily, minimize risk, via mechanical rule-following rather than personal judgment. Two consequences for sequencing:

- **SPY-only scope is intentional, not a gap** — at ~$10k and a 2-3 position cap, one liquid underlying with one regime model is the right amount of surface area. Breadth is not on the critical path.
- **The real bottleneck is that the user must manually initiate every evening session** (executive-dysfunction friction) — this pulls the read-only half of §8 (Alpaca position/quote sync) forward, ahead of the rest of mid-term, paired with a scheduled job that runs Layer A/B/C on its own and pushes a one-tap approve/reject decision when there's something to act on.
- **This does not relax "user approves every trade"** (see [product.md](product.md) non-goals) — every paper fill still requires an explicit tap from the user; the engine schedules and prepares, it does not execute unattended. This was confirmed explicitly rather than assumed, since it touches an ADR-level mandate.
- Also fetch positions/quotes at session open via the Alpaca paper API instead of manual entry — de-risks the eventual live integration and make the paper-session gate below cheaper to run.

## Mid-term — feature depth
Completes specified-but-partial areas. Worth building while the trade history is still small, so they're ready as N grows.
- **Roll workflow (§6.2)** — surface roll candidates in Layer A and enforce the roll rules. Today only the `rolls` cap is modeled.
- **First-class Opportunity Ledger UI (§7.2)** — filter/sort, outcome-if-taken deltas, visible bypass reasons.
- **Benchmark & risk-adjusted analytics (§7.3)** — fill the stubbed `BenchmarkData` (SPY / BXM / cash) and CAGR / Sharpe / max-drawdown, keeping the rule: never a % without its N, never fabricated data.

## Long-term — Alpaca IRA live execution (§8)
Live order placement behind the existing `ALPACA_LIVE_MODE` decoupling ([ADR-0002](decisions.md#adr-0002--manual-sandbox-first-alpaca-behind-env-vars)). **Gate:** do not enable live until the manual version has run *and* a paper Alpaca integration has completed ≥5 full evening sessions (the scheduled scan+approval loop above is what accumulates those sessions). Automate only what has been understood manually first. Manual per-trade approval remains required in live mode too, absent a separate, explicit decision to change that.

## Sequencing rationale
Near-term was independent and shippable immediately (done). Playbook composition was fixed first since it directly served the stated goal (mechanical positive-expectancy trades across every regime) and was small. The scheduled scan+push-approval loop is next because it removes the user as the required daily initiator — the single biggest gap between the built system and its intended use — while preserving the manual-approval mandate. Mid-term adds analytical value once the loop is running and trade history accumulates. Live execution is last by design — the manual layer was built to be swapped for an API call without restructuring the other layers.

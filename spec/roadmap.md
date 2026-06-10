# Roadmap — Themes & Direction

> Part of the [modular specification](README.md). The granular backlog now lives in **GitHub issues** — the [project board](https://github.com/users/philipreese/projects/1) is the source of truth for what's open and what's done. This file keeps the high-level phases and the rationale for their ordering; it is **not** a task list. Each theme below is broken into one or more issues — see the board for status and detail.

## Near-term — UX & polish ✅ shipped
Design-token consistency, Fetch-Live feedback, inline telemetry validation, override-justification capture, and the Greek-limit CTA — plus an accessibility pass (modal autofocus, table semantics, severity by icon + text). Landed 2026-06-10 (commit `b6a96a7`); see the [CHANGELOG](../CHANGELOG.md). One near-term item remains open and is tracked as an issue: **session re-lock discoverability**.

## Mid-term — feature depth
Completes specified-but-partial areas. Worth building while the trade history is still small, so they're ready as N grows.
- **Roll workflow (§6.2)** — surface roll candidates in Layer A and enforce the roll rules. Today only the `rolls` cap is modeled.
- **First-class Opportunity Ledger UI (§7.2)** — filter/sort, outcome-if-taken deltas, visible bypass reasons.
- **Benchmark & risk-adjusted analytics (§7.3)** — fill the stubbed `BenchmarkData` (SPY / BXM / cash) and CAGR / Sharpe / max-drawdown, keeping the rule: never a % without its N, never fabricated data.

## Long-term — Alpaca IRA execution (§8)
Order placement behind the existing `ALPACA_LIVE_MODE` decoupling ([ADR-0002](decisions.md#adr-0002--manual-sandbox-first-alpaca-behind-env-vars)). **Gate:** do not enable live until the manual version has run *and* a paper Alpaca integration has completed ≥5 full evening sessions. Automate only what has been understood manually first.

## Sequencing rationale
Near-term was independent and shippable immediately (done). Mid-term adds analytical value early so it's mature once enough trades exist. Execution is last by design — the manual layer was built to be swapped for an API call without restructuring the other layers.

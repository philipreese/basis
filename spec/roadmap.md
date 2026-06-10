# Roadmap — What's Missing & Next Steps

> Part of the [modular specification](README.md). Synthesizes the open items from [gap-analysis.md](gap-analysis.md) and [ux-review.md](ux-review.md) into prioritized next steps. Each item names the spec section it closes.

## Near-term — UX & polish
Low-risk, high-clarity wins. No new domain logic.

1. **Design-token cleanup** — convert `SafeguardsPanel`, `PerformanceDashboard`, `OpportunityLedger` to Catppuccin tokens so theming is consistent. *(ux-review #1; closes formatting/UI intent of §6 build sprint.)*
2. **Intermediate states** — loading skeletons for "Fetch Live"/refresh; inline field-validation guidance. *(ux-review #2.)*
3. **Override justification capture** — prompt for a reason before bypassing a suppressed playbook; persist via the existing ledger `bypass_reason`. *(ux-review #3; strengthens §7.2 audit value.)*
4. **Accessibility pass** — aria-labels on severity, table semantics, modal autofocus. *(ux-review #4.)*
5. **Greeks-limit CTA** — actionable link from a breached Greek limit toward the close flow. *(ux-review #3.)*

## Mid-term — feature depth
Completes specified-but-partial areas.

6. **Roll workflow (§6.2)** — surface roll candidates in Layer A and enforce the rules (net-credit only, max 2 rolls, roll down-and-out for puts / up-and-out for calls). Today only the `rolls` cap is modeled; there's no flow. *(gap-analysis §6.2 ◑.)*
7. **Opportunity-ledger UI (§7.2)** — make the ledger first-class: filter/sort, show `outcome_if_taken` deltas, and make bypass reasons visible — quantifying the value of human override as the spec intends. *(gap-analysis §7.2 ◑.)*
8. **Benchmark & risk-adjusted analytics (§7.3)** — fill the stubbed `BenchmarkData`: passive-SPY / BXM / cash benchmarks and CAGR / Sharpe / max-drawdown once sample size supports it. Keep the "never report % without N, never fabricate data" rule. *(gap-analysis §7.3 ◑.)*

## Long-term — Alpaca IRA execution (§8)
The deliberately-postponed layer. Governed by [ADR-0002](decisions.md#adr-0002--manual-sandbox-first-alpaca-behind-env-vars).

9. **Order execution integration** — give `TradeSpec` an `execute()` path that routes multi-leg orders to Alpaca, places GTC closing orders on fill, and pulls positions/quotes at session open instead of manual entry. The manual layer was built to be swapped without restructuring other layers.
   - **Gate:** do not enable live (`ALPACA_LIVE_MODE`) until the manual version has run *and* the paper Alpaca integration has completed ≥5 full evening sessions. Layer A validation applies identically to API-executed trades. *(gap-analysis §8 ✗.)*

## Sequencing rationale
Near-term items are independent and shippable immediately. Mid-term items add analytical value while the trade history is still small (so they're ready when N grows). Execution is last by design — automate only what has been understood manually first.

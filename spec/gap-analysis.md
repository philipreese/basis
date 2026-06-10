# Gap Analysis — Spec vs. Built

> Part of the [modular specification](README.md). How close the implementation is to the spec, section by section. Status flags trace to real files. Verified against `backend/` at the `docs/spec-modularization` branch point (post-Sprint 6).
>
> Legend: ✅ done · ◑ partial · ✗ missing/future

## Summary

The core engine is **fully built** through all six planned sprints: the three-layer pipeline, trade-spec generation with uncircumventable hard blocks, the mandatory intent journal, closure post-mortems, the opportunity ledger, and a mobile-first UI all exist and are tested. The material gaps are **deferred-by-design** (Alpaca order execution, §8) or **explicitly stubbed** (benchmark analytics, parts of §7.3). Nothing in the core spec is silently unimplemented.

## Section-by-section

| Spec area | Status | Evidence | Notes |
|---|---|---|---|
| §1 Vision / scope / non-goals | ✅ | whole codebase | Manual sandbox, no charting, no autonomous exec, no LLM — all honored. |
| §2 Three-layer pipeline + sequencing | ✅ | [observation.py](../backend/observation.py), [regime.py](../backend/regime.py), [opportunity.py](../backend/opportunity.py) | Layer A blocks Layer C on unresolved P1 (hard block `UNRESOLVED_P1`). |
| §3 Schemas (Playbook/Position/OptionLeg) | ✅ | [models.py](../backend/models.py) | Per-share storage convention upheld; `playbook_snapshot` deep-copied at entry. |
| §3.2 Risk/Greek config | ✅ | `PortfolioConfigSchema`, `GET/POST /api/portfolio/config` | Admin settings panel present. |
| §4.1 Layer A lifecycle (P1/P2/P3) | ✅ | `observation.py` (`run_lifecycle_scan`) | Priority thresholds + language rules implemented; unit-tested at exact boundaries. |
| §4.1 Portfolio Greeks aggregation | ✅ | `aggregate_portfolio_greeks` | Net Δ/Θ/Vega/Γ vs limits. |
| §4.1 Exposure safeguards | ✅ | `run_exposure_safeguards` | Concentration / capital / count checks. |
| §4.1.1 Regime conflict detection | ✅ | `observation.py` | Drives P2 REVIEW. |
| §4.2 Regime scoring matrix | ✅ | `regime.py` (`compute_regime`) | All four scores stored/returned; tie scenarios tested. |
| §4.2 Live telemetry fetch | ✅ | [market_data.py](../backend/market_data.py), `POST /api/market/fetch` | SPY bars + VIX from Alpaca; **graceful fallback** when creds absent. |
| §4.3 Layer C eligibility + exposure gates | ✅ | `opportunity.py` (`scan_opportunities`) | Suppressed candidates carry reasons. |
| §4.3 Strike derivation (shown, not black-box) | ✅ | `_derive_strikes_for_spec`, `StrikeDerivedParams` | VIX 1σ + Φ⁻¹ approximation; params surfaced. |
| §5.1–5.3 Trade spec fields / strike / expiry rules | ✅ | `generate_trade_spec` | LIMIT-only, explicit max loss/gain/break-even. |
| §5.4 Mandatory intent journal | ✅ | `POST /api/positions` returns **422** on incomplete journal | Enforced server-side, not just UI. |
| §5.5 Hard blocks (uncircumventable) | ✅ | `opportunity.py` | All 7 blocks present; no "proceed anyway". |
| §5.5 Warnings (require ack) | ✅ | `TradeSpecResult.warnings[]` | Per-warning confirmation in UI. |
| §6.1 Universal exit rules | ✅ | `observation.py` lifecycle thresholds | Profit/loss/time/catalyst triggers enforced each scan. |
| §6.2 Roll rules | ◑ | `rolls` field + max-2 cap in schema | Cap modeled; **no UI/flow to perform or suggest a roll** — exits are surfaced, rolls are not. |
| §7.1 Closure post-mortem (immutable) | ✅ | `POST /api/positions/{id}/close`, `ClosurePostMortemModel` | Freezes record, computes realized P&L + outcome. |
| §7.2 Opportunity / missed-trades ledger | ◑ | `GET/POST/PATCH /api/opportunity/ledger` | Backend complete (incl. `outcome_if_taken` backfill); UI is read-only and minimal. |
| §7.3 Per-playbook diagnostics | ✅ | `GET /api/performance/diagnostics` | Win rate, profit factor, avg return-on-risk by `(playbook_id, version)`. |
| §7.3 Benchmark / risk-adjusted metrics | ◑ | `BenchmarkData()` returned empty; no CAGR/Sharpe/MaxDrawdown | Columns specified but **stubbed** — correctly shows "N/A" rather than fictional data. |
| §8 Alpaca IRA **order execution** | ✗ | no `submit_order`/`TradingClient` anywhere in `backend/` | Deferred by [ADR-0002](decisions.md#adr-0002--manual-sandbox-first-alpaca-behind-env-vars). Market *data* is wired; order *placement* is not. |

## Where the build exceeds the spec
- **Live option-quote refresh** (`POST /api/positions/refresh`) — updates `current_value_per_share` from Alpaca options quotes; the spec assumed manual nightly entry.
- **Design system** — a Catppuccin token system + reusable UI primitives, beyond the spec's formatting rules (see [ux-review.md](ux-review.md)).

## Bottom line
The app **is what it set out to be**: a deterministic, manual-sandbox decision-support engine. Remaining work is depth (analytics, roll workflow, ledger UI) and the deliberately-postponed execution layer — captured in [roadmap.md](roadmap.md).

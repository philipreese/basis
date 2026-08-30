# Architecture — Three-Layer Pipeline

> Part of the [modular specification](README.md). Source: §2 and §4 of the [archived v8 spec](archive/project_spec_v8.md).

Three operating layers execute sequentially each evening. In the executor pipeline, Layer B refreshes market/regime state before Layer A runs, because Layer A's regime-flip exit reads that state; Layer C entries always run last:

```
┌──────────────────────────────────────────────────────────────┐
│                 LAYER B: BACKGROUND CONTEXT LAYER            │
│      Market Telemetry • Trend Metrics • Regime Labels        │
└───────────────────────────────────────┬──────────────────────┘
                                        │
                                        ▼
┌──────────────────────────────────────────────────────────────┐
│                    LAYER A: OBSERVATION ENGINE               │
│     Active Position Tracker • Portfolio Greeks • Lifecycle   │
└───────────────────────────────────────┬──────────────────────┘
                                        │
                                        ▼
┌──────────────────────────────────────────────────────────────┐
│                   LAYER C: OPPORTUNITY ENGINE                │
│     Playbook Scans • Automated Order Specification Cards     │
└──────────────────────────────────────────────────────────────┘
```

**Sequencing rule:** Layer C entries never precede Layer A closes. If any position has a P1 action (CLOSE NOW), the system does not proceed to Layer C until that action is resolved. Position management takes absolute priority over new entry decisions. (This diagram shows the executor's data-dependency order; see [README.md](README.md) for the full nightly pipeline sequence, including the order-sync/reconciliation stages that also precede Layer A.)

## Stack

Monorepo separating presentation from logic. See [ADR-0004](decisions.md#adr-0004--sqlite--fastapi--svelte-5-monorepo) for the rationale.

| Concern | Technology |
|---|---|
| Backend | Python 3.11+, FastAPI, Uvicorn |
| Persistence | SQLite via SQLAlchemy 2.0 + aiosqlite (async) |
| Validation/contracts | Pydantic 2 (OpenAPI exported to the frontend) |
| Frontend | Svelte 5 + TailwindCSS v4 (Vite) |
| Market data | IB Gateway (TWS API via `ib_async`, free delayed feed); degrades to stored state when unreachable |
| Tooling | Pixi (manages Python + Node); Pytest + Vitest |

Backend ↔ frontend communicate over typed REST + JSON. The backend exports `GET /openapi.json`; the frontend regenerates TypeScript types from it (`pixi run sync-types`). See [api.md](api.md) for the endpoint surface.

## Layer responsibilities

### Layer A — Observation Engine
Runs the position lifecycle scanner, the portfolio Greeks aggregator, exposure safeguards, and regime-conflict detection. The console's session-lock gate that once forced Layer A as the mandatory first view was retired (ADR-0005 status) once the executor started enforcing the sequencing rule itself; the console is now an ungated supervision surface (Overview · Scan · Books · Analysis · Settings). Full rules in [domain-rules.md → Layer A](domain-rules.md#layer-a--position-lifecycle--safeguards).
**Source of truth:** [backend/observation.py](../backend/observation.py).

### Layer B — Background Context Layer
Automated telemetry collection on load, displayed as a visually subordinate status ribbon (descriptive context only — no predictive language shown to the user). Computes the market regime from a weighted scoring matrix. Full rules in [domain-rules.md → Regime classification](domain-rules.md#regime-classification-layer-b).
**Source of truth:** [backend/regime.py](../backend/regime.py), [backend/market_data.py](../backend/market_data.py).

### Layer C — Opportunity Engine
Loops Layer B telemetry against all active playbook definitions, applies exposure gates, and outputs a candidate menu for eligible playbooks only (ineligible ones are hidden, not disabled). Selecting a candidate generates a trade spec subject to pre-output validation. Full rules in [domain-rules.md → Playbook matching](domain-rules.md#playbook-matching-layer-c) and [→ Trade specification](domain-rules.md#trade-specification).
**Source of truth:** [backend/opportunity.py](../backend/opportunity.py).

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

**The console is installable** (#1023): a web app manifest and a service worker make it an app on the operator's phone rather than a tab. The worker caches the SHELL only — `/api` is never intercepted, not cached, and has no offline fallback, because a cached HALT state or Live Gate row is not a degraded experience but a wrong one. Offline, a data fetch fails and the console shows its own error state; hashed `/assets/*` are cache-first (a URL's bytes never change) and the shell is network-first so a deploy is always picked up when online. Registration requires a SECURE CONTEXT, so a console reached over plain http stays an ordinary web page — a working console without offline support, never an error.

**The console is served BY the backend** (#1019): FastAPI mounts the built `frontend/dist` at `/`, so in production there is one process and one origin — no Vite dev server, no `/api` proxy, no host-check workaround for the tailnet. `backend/static_console.py` owns it: content-hashed `/assets/*` cache for a year, `index.html` and stable root files must revalidate (a cached shell points at asset hashes a deploy just deleted), an unknown `/api/*` still 404s as JSON rather than the SPA shell, and a path escaping `dist/` is refused. With no build on disk the mount declines and the API serves alone, which is exactly the dev flow: `pixi run server` + `pixi run client` is unchanged, and CORS config exists only for it.

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

# Options Playbook Automation Engine & Research Runtime

A daily decision-support web application and automated playbook execution engine tailored for cash-settled Roth IRA accounts, running entirely in a manual sandbox mode.

---

## Architecture Overview

The system is organized into a clean monorepo structure separating frontend presentation from backend logic:

```
options-playbook-automation/
├── backend/                  <-- Python FastAPI Backend
│   ├── main.py               <-- Endpoint APIs
│   ├── models.py             <-- SQLAlchemy and Pydantic Schemas
│   ├── database.py           <-- SQLite async connection and seeding
│   ├── pricing.py            <-- Raw per-share option math formulas
│   ├── observation.py        <-- Layer A: portfolio lifecycle scanner, Greeks, safeguards
│   ├── regime.py             <-- Layer B: regime scoring matrix
│   ├── market_data.py        <-- Alpaca API client for SPY/VIX fetching
│   ├── opportunity.py        <-- Layer C: playbook eligibility scanner, trade spec generator
│   └── tests/                <-- Pytest tests
│       ├── test_sprint1.py
│       ├── test_sprint2.py
│       ├── test_sprint3.py
│       ├── test_sprint4.py
│       └── test_sprint5.py
├── frontend/                 <-- Svelte 5 + TailwindCSS v4 Client
│   ├── src/
│   │   ├── App.svelte        <-- Orchestrator: global state, handlers, layout (Sprint 6: Re-lock, P1 above the fold)
│   │   └── lib/
│   │       ├── api.ts        <-- Type-safe HTTP Client
│   │       ├── api-types.ts  <-- Synced TypeScript schemas (generated)
│   │       ├── formatters.ts <-- Sprint 6: Centralized formatting helpers (currency, pct, DTE, dates)
│   │       ├── MarketContextRibbon.svelte  <-- Layer B regime ribbon
│   │       ├── GreeksPanel.svelte          <-- Portfolio net Greeks display
│   │       ├── SafeguardsPanel.svelte      <-- Exposure safeguard warnings
│   │       ├── PositionScanner.svelte      <-- Layer A position lifecycle cards (Sprint 5: P1 close button; Sprint 6: strict formatting)
│   │       ├── CandidateCards.svelte       <-- Layer C eligible/suppressed playbooks (Sprint 5: bypass logging; Sprint 6: strict formatting)
│   │       ├── TradeSpecCard.svelte        <-- Trade spec with mandatory intent journal form (Sprint 6: strict formatting)
│   │       ├── ClosePositionModal.svelte   <-- Sprint 5: close position capture form
│   │       ├── PostMortemCard.svelte       <-- Sprint 5: closed position post-mortem display (Sprint 6: strict formatting)
│   │       ├── OpportunityLedger.svelte    <-- Sprint 5: accepted/bypassed opportunity table (Sprint 6: strict formatting)
│   │       └── PerformanceDashboard.svelte <-- Sprint 5: per-playbook diagnostics (Sprint 6: strict formatting)
│   │   └── tests/
│   │       ├── api.test.ts
│   │       └── formatters.test.ts          <-- Sprint 6: Unit tests for formatting rules
│   └── tsconfig.json
├── pixi.toml                 <-- Monorepo Tasks & Environment Manager
├── pyproject.toml            <-- Python configurations
└── scripts/
    └── verify-project.ps1    <-- Code quality & verification script
```

---

## Getting Started

### Prerequisites

Ensure you have [Pixi](https://pixi.sh) installed. Pixi manages Node.js, Python, and all system tools automatically inside a virtual sandbox.

### Setup and Installation

Initialize the environment and download dependencies for both frontend and backend:
```bash
pixi run install-node-deps
```

### Dev Task Runners

Tasks are run inside the Pixi environment:

| Command | Action |
|---|---|
| `pixi run dev` | **Start Both Backend and Frontend concurrently** (Windows shell compatible) |
| `pixi run server` | Run backend FastAPI server only (`http://localhost:8000`) |
| `pixi run client` | Run Svelte Vite dev server only (`http://localhost:5173`) |
| `pixi run sync-types` | Synchronize Pydantic models to Svelte TypeScript files |
| `pixi run test` | Run backend Pytest unit tests |
| `powershell ./scripts/verify-project.ps1` | Run full pre-commit verification gates (Secrets scan, Node tests, Python tests) |

---

## Database and Seeding

On the initial backend start, a local SQLite database (`options_playbook.db`) is created in the root directory and seeded with:
- **Default Portfolio Configuration**: Schwab Roth IRA settings, maximum trade risk thresholds (15% / $1,500), and Greek limits.
- **Seed Positions**:
  1. SPY Long Straddle (June 18 Expiration) — short-term volatility study.
  2. SPY Long Straddle (July 18 Expiration) — SpaceX IPO thesis study.
- **Seed Playbooks** (Layer C): SPY Iron Condor, SPY Bull Call Spread, SPY Bear Put Spread, SPY Long Straddle, SPY Long Strangle.

---

## Multi-Engine Pipeline — Sprint 2: Layer A: Observation Engine

The system implements automated lifecycle scanning, portfolio Greeks aggregation, and exposure safeguards:
- **Lifecycle Scanner**: Evaluates open positions and assigns priority level (`P1 — CLOSE NOW`, `P2 — CLOSE SOON` / `P2 — REVIEW`, `P3 — MONITOR`, `OK`). Blocks access to playbook matches until Layer A is explicitly reviewed and acknowledged.
- **Greeks Aggregation**: Computes Net Delta, Net Theta, Net Vega, and Net Gamma dynamically from all open legs, adjusting for direction (LONG/SHORT) and contracts. Highlights limit overruns based on admin parameters.
- **Exposure Safeguards**: Automatically flags concentration risk (underlying concentration > 35%, index concentration > 50%), maximum position limits, and capital deployment overruns.

---

## Multi-Engine Pipeline — Sprint 3: Layer B: Market Context & Regime Classification

- **Regime Scoring Matrix** (`backend/regime.py`): Implements the full Section 4.2 weighted scoring matrix. Five telemetry dimensions are each classified into labelled signals, scored across four regimes, and the winner is selected with a risk-priority tie-breaker (`EVENT_CATALYST > TRENDING_BEAR > HIGH_VOL_NEUTRAL > CALM_BULL`).
- **Market Data Client** (`backend/market_data.py`): Isolated Alpaca HTTP client. Fetches SPY daily bars to compute closing price, 20-day SMA, and daily return. Fetches VIX closing price. Returns `None` gracefully if credentials are absent or the request fails — callers fall back to stored DB state.
- **API Updates**: `POST /api/market/state` now recomputes the regime from submitted telemetry inputs. `POST /api/market/fetch` triggers a live Alpaca pull and recomputes the regime.
- **Layer B Context Ribbon** (frontend): A compact, subordinate ribbon displaying the active regime badge (colour-coded), telemetry pills (SPY, SMA20, VIX, daily return), and a collapsible score breakdown for all four regimes.
- **Expanded Telemetry Form** (frontend): Six input fields (SPY price, SMA20, VIX, daily return, IVRs, catalysts) replace the old manual regime selector. A "Fetch Live Data" button pulls from Alpaca when API keys are present.

### Environment Variables (Sprint 3)

Add to `.env` to enable live Alpaca data fetching:
```
ALPACA_API_KEY_ID=your_key
ALPACA_SECRET_KEY=your_secret
```
Without these, the app operates fully in manual simulation mode — no functionality is lost.

---

## Multi-Engine Pipeline — Sprint 4: Layer C: Opportunity Engine

- **Playbook Eligibility Scanner** (`backend/opportunity.py`): Loops all active playbooks against current Layer B telemetry. Applies portfolio-level gates (max positions, max capital deployed), per-playbook suppression gates (underlying concentration, directional concentration, IVR gate for income/debit strategies), and per-playbook entry filters (IVR range, VIX range, SPY trend, catalyst calendar rules).
- **Strike Derivation**: Uses VIX-based 1σ move and rational Φ⁻¹ approximation to derive OTM strikes from target delta. All derivation inputs are recorded in `StrikeDerivedParams` for full traceability — no black-box outputs.
- **Trade Spec Generator**: Produces concrete order legs, limit price, max loss, break-even prices, profit target, loss limit, and GTC closing instructions for all five strategy types.
- **Pre-Output Validation**: Hard blocks (UNRESOLVED_P1, CAPITAL_EXCEEDED, MAX_LOSS_EXCEEDED, EXPIRATION_ARITHMETIC, PREMIUM_UNREASONABLE, POSITION_COUNT, STRIKE_SANITY) suppress the spec entirely. Warnings (REGIME_CONSISTENCY, DUPLICATE_UNDERLYING, BREAKEVEN_REALISM, STRATEGY_NOVELTY) require explicit per-warning confirmation before proceeding. Hard blocks cannot be bypassed.
- **API**: `GET /api/opportunity/scan` returns all candidates with suppression reasons. `POST /api/opportunity/spec/{playbook_id}` generates the full `TradeSpecResult`.
- **Layer C UI** (frontend): `CandidateCards.svelte` shows eligible playbooks with automated order spec; suppressed playbooks are shown in a collapsible panel with their suppression reason and an Override button. `TradeSpecCard.svelte` displays the full spec with per-warning acknowledgement gates and hard-block banners.

---

## Multi-Engine Pipeline — Sprint 5: Intent Journal, Post-Mortem & Ledger

- **Staging Intent Journal** (`backend/models.py`, `backend/main.py`): Requires logging a mandatory `OperationalJournalEntry` (thesis, invalidation, expected move, emotional state, confidence rating) prior to saving any position.
- **Closure Post-Mortem Workflow** (`POST /api/positions/{id}/close`): Atomic handler that records the exit trigger, actual move, lesson tags, and overrides, then freezes the position status to CLOSED.
- **Opportunity Ledger** (`GET/POST /api/opportunity/ledger`): Logs all eligible and bypassed opportunities for auditing and post-trade analysis.
- **Performance Diagnostics Dashboard** (`GET /api/performance/diagnostics`): Generates playbook-level performance statistics, including win rates, profit factors, and average return-on-risk metrics.

---

## Multi-Engine Pipeline — Sprint 6: UI Polish & Mobile Layout

- **Mobile-First Responsive Layout**: Refactored the UI grids, sizing, padding, and tap actions to accommodate evening phone usage.
- **P1 Critical Above-the-Fold Alerts**: Automatically aggregates P1 "CLOSE NOW" recommendation cards into a bright red alert panel at the very top of the page, ensuring immediate attention upon page load.
- **Strict Data Formatting**: Standardized and strictly formatted all numbers across the application using new centralized TypeScript helpers:
  - **Dollar Amounts**: Exactly 2 decimal places with localized currency formatting and minus sign placement (e.g. `-$1,234.56`).
  - **Percentages**: Exactly 1 decimal place (e.g. `12.3%`).
  - **DTE**: Formatted as integer Days to Expiration (e.g. `21 DTE`).
  - **Dates**: Formatted as `Month DD YYYY` (e.g. `June 18 2026`).
- **Session Re-Lock Control**: Added a "Re-lock Session" button in the header so users can manually toggle navigation back to the locked state after reviewing active positions.

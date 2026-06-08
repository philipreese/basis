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
│   ├── observation.py        <-- Portfolio lifecycle scanner, Greeks, safeguards
│   └── tests/                <-- Pytest tests
│       ├── test_sprint1.py
│       └── test_sprint2.py
├── frontend/                 <-- Svelte 5 + TailwindCSS v4 Client
│   ├── src/
│   │   ├── App.svelte        <-- Interactive dashboard and settings
│   │   └── lib/
│   │       ├── api.ts        <-- Type-safe HTTP Client
│   │       └── api-types.ts  <-- Synced TypeScript schemas
│   │   └── tests/
│   │       └── api.test.ts
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
- **Sprint 1 Active Seed Positions**:
  1. SPY Straddle (June 18 Expiration) study.
  2. SPY Straddle (July 18 Expiration) SpaceX IPO thesis study.

---

## Multi-Engine Pipeline — Sprint 2: Layer A: Observation Engine

The system implements automated lifecycle scanning, portfolio Greeks aggregation, and exposure safeguards:
- **Lifecycle Scanner**: Evaluates open positions and assigns priority level (`P1 — CLOSE NOW`, `P2 — CLOSE SOON` / `P2 — REVIEW`, `P3 — MONITOR`, `OK`). Blocks access to playbook matches until Layer A is explicitly reviewed and acknowledged.
- **Greeks Aggregation**: Computes Net Delta, Net Theta, Net Vega, and Net Gamma dynamically from all open legs, adjusting for direction (LONG/SHORT) and contracts. Highlights limit overruns based on admin parameters.
- **Exposure Safeguards**: Automatically flags concentration risk (underlying concentration > 35%, index concentration > 50%), maximum position limits, and capital deployment overruns.
- **Telemetry Controls**: Includes Svelte controls to dynamically adjust simulated regimes, index prices, and catalyst dates on the fly.

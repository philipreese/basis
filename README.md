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
│   └── tests/                <-- Pytest tests
├── frontend/                 <-- Svelte 5 + TailwindCSS v4 Client
│   ├── src/
│   │   ├── App.svelte        <-- Interactive dashboard and settings
│   │   └── lib/
│   │       ├── api.ts        <-- Type-safe HTTP Client
│   │       └── api-types.ts  <-- Synced TypeScript schemas
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

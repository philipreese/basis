# basis — Options Playbook Automation Engine

A system for defined-risk options trading that is graduating from decision-support to autonomy ([ADR-0006/0007](spec/decisions.md)). It has two modes of operation today:

- **Manual console**: an evening web app that scans open positions, classifies the market regime, matches codified playbooks, and generates order specifications.
- **Executor (Paper)**: an autonomous nightly pipeline that places real orders in an IBKR **paper** account across a matrix of virtual "lab books" racing strategy variants ([ADR-0009](spec/decisions.md)). Live money is gated behind the ADR-0006 Live Gate (≥30 closed paper trades per book, ≥3 months, zero envelope breaches, expectancy ≥ 0 after a slippage haircut).

Every trading rule is deterministic code — no LLM anywhere in the order path (ADR-0001).

> **Specification:** the full spec is modular and lives in [`spec/`](spec/README.md) — product, architecture, domain rules, API, data models, ADRs, standards. Domain vocabulary: [`CONTEXT.md`](CONTEXT.md). Executor design: [`spec/design/executor-paper.md`](spec/design/executor-paper.md); operational safety rules: [`spec/supervision.md`](spec/supervision.md).

---

## Architecture

```
basis/
├── backend/                       Python FastAPI backend
│   ├── main.py                    API endpoints
│   ├── models.py                  SQLAlchemy models + Pydantic schemas (the API contract)
│   ├── database.py                Async SQLite, schema bootstrap, seeding
│   ├── pricing.py                 Per-share option math (max loss/gain, break-evens, capital at risk)
│   ├── observation.py             Layer A: lifecycle scanner, Greeks, safeguards, roll candidates
│   ├── regime.py                  Layer B: regime scoring matrix (V0)
│   ├── regime_variants.py         Layer B variants V1/V2 raced by the lab books
│   ├── market_data.py             IB Gateway data client (SPY/VIX bars, option quotes)
│   ├── opportunity.py             Layer C: playbook eligibility, strike derivation, trade specs
│   ├── operator.py                Nightly operator (telemetry refresh, scans, ntfy digest)
│   ├── executor.py                Autonomous nightly trading pipeline (paper)
│   ├── broker.py                  IBKR order adapter (combo orders, orderRef discipline)
│   ├── trading_control.py         Kill switch (fail-closed, latched halts)
│   ├── book_gates.py              Per-book risk-envelope gates + capital encumbrance
│   ├── reconciliation.py          Nightly broker-vs-books comparison
│   ├── anomaly.py                 Auto-halt rules (rejections, duplicates, P&L shocks, breaches)
│   ├── digest.py                  Executor digest + urgent-push tiering
│   ├── flex_audit.py              Weekly IBKR Flex statement vs fills-ledger audit
│   ├── console.py                 Supervision console read models (books, Live Gate, status)
│   ├── performance.py             Sample-gated risk metrics (CAGR, Sharpe, drawdown) + SPY benchmark
│   └── tests/                     Pytest suite (80% branch-coverage gate)
├── frontend/                      Svelte 5 + Tailwind v4 client
│   ├── src/App.svelte             Orchestrator: tabs, session lock, global state
│   ├── src/lib/                   Components + API client
│   │   ├── api-types.ts           Generated OpenAPI types (pixi run sync-types)
│   │   └── api.ts                 openapi-fetch client — URLs, params, and types checked
│   │                              against the backend schema at compile time
│   ├── src/tests/                 Vitest unit tests
│   └── e2e/                       Playwright smoke pack (real stack, fresh temp DB)
├── scripts/                       Verification, scheduled-task registration, e2e backend
├── pixi.toml                      Monorepo tasks & environment manager
└── spec/                          Modular specification + ADRs
```

---

## Getting Started

Install [Pixi](https://pixi.sh) (it manages Python, Node.js, and all tooling), then:

```bash
pixi run install-node-deps
```

| Command | Action |
|---|---|
| `pixi run dev` | Start backend and frontend concurrently |
| `pixi run server` | Backend FastAPI only (`http://localhost:8000`) |
| `pixi run client` | Svelte Vite dev server only (`http://localhost:5173`) |
| `pixi run test` | Backend (pytest, 80% branch-coverage gate) + frontend (vitest) tests |
| `pixi run test-e2e` | Playwright smoke pack against the real stack (boot, session lock, close, HALT/RESUME, Books tab) |
| `pixi run lint` / `lint-fix` | Ruff lint + format check / autofix |
| `pixi run sync-types` | Regenerate `api-types.ts` from the running backend's OpenAPI schema |
| `pixi run operator` | Run the nightly operator once |
| `pixi run executor` | Run the executor pipeline once (needs IB Gateway) |
| `pixi run flex-audit` | Run the weekly Flex statement audit once |
| `powershell ./scripts/verify-project.ps1` | Full pre-commit verification (secrets scan, all tests) |

### Configuration (`.env`, all optional)

- **IB Gateway** (paper mode, free 15-min-delayed data): `IBKR_GATEWAY_HOST=127.0.0.1`, `IBKR_GATEWAY_PORT=4002`, `IBKR_CLIENT_ID=17`, `IBKR_SMOKE_CLIENT_ID=19`. Without a reachable Gateway the web app runs fully on stored/manual telemetry.
- **Push notifications**: `NTFY_TOPIC` (private [ntfy.sh](https://ntfy.sh) topic — treat as a secret), `NTFY_SERVER` (default `https://ntfy.sh`), `NTFY_COMMAND_TOPIC` (remote HALT channel — also a secret).
- **Executor**: `HALT_FILE` (sentinel path, default `HALT` in the repo root), `EXECUTOR_HEARTBEAT_FILE` (default `executor_heartbeat.json`).
- **Flex audit**: `IBKR_FLEX_TOKEN` (secret), `IBKR_FLEX_QUERY_ID`.
- **Misc**: `CORS_ORIGINS` (defaults to the local Vite dev server).

### Database

The schema is created directly from the SQLAlchemy models on startup — there are no migrations. Pre-launch policy: until the first real paper fill exists there is no data worth migrating, so a schema change means deleting `options_playbook.db` and restarting (the backend detects a stale schema and refuses to run with exactly that instruction; it never drops or alters data itself). Migrations return the day the fills/audit tables start holding Live Gate evidence, which can never be reset.

First start seeds: the default portfolio configuration, seven SPY playbooks (credit structures at $3 wings, debit spreads at $5; the long-vol event playbooks ship disabled), the ADR-0009 lab-book experiment matrix (17 books today; more arrive with their enabling features), and per-scope trading controls. Positions are never seeded — real databases start with an empty book.

---

## The Three Layers

- **Layer A — Observation** (`observation.py`): scans every open position into a priority (`P1 — CLOSE NOW` down to `OK`) with the exit-rule math shown, aggregates portfolio Greeks against limits, flags exposure safeguards (concentration, deployment), and surfaces defensive-roll candidates. The UI session-locks until Layer A is reviewed and acknowledged.
- **Layer B — Market Context** (`regime.py`, `regime_variants.py`, `market_data.py`): classifies the regime (`CALM_BULL`, `HIGH_VOL_NEUTRAL`, `TRENDING_BEAR`, `EVENT_CATALYST`) from SPY/VIX telemetry via a weighted scoring matrix; variants V1 (VIX term structure) and V2 (volatility risk premium) are raced against V0 by the lab books. Daily SPY/VIX/VIX3M closes persist to `index_history`.
- **Layer C — Opportunity** (`opportunity.py`): checks every enabled playbook against portfolio gates, suppression gates, and entry filters; derives strikes from target delta with full traceability; generates complete trade specs (legs, limit price, max loss, break-evens, exit rules). Hard blocks (e.g. `UNRESOLVED_P1`, `MAX_LOSS_EXCEEDED`) cannot be bypassed; warnings require explicit per-warning acknowledgement.

## Manual Console Workflows

- **Intent journal**: saving any position requires a mandatory journal entry (thesis, invalidation, expected move, emotional state, confidence).
- **Close + post-mortem**: closing freezes an immutable record (outcome, realized P&L, exit trigger, lesson tags, override flag).
- **Roll workflow**: credit verticals under pressure get a suggested down/up-and-out roll; execution enforces net-credit-only, max 2 rolls then forced exit, and direction rules.
- **Opportunity ledger**: every eligible or bypassed opportunity is logged; the view filters/sorts and summarizes the value of human overrides (counterfactual outcomes, with N).
- **Performance diagnostics**: per-playbook win rate, profit factor, avg return-on-risk, and sample-gated CAGR/Sharpe/max-drawdown (`null` below 10 trades or a 30-day span — never fabricated), benchmarked against SPY from stored index history.

---

## Nightly Operator

`backend/operator.py` runs the evening ritual unattended: reprices open positions from live quotes, refreshes SPY/VIX telemetry and all regime variants, runs the Layer A and Layer C scans, and pushes a digest via ntfy (priority escalates when a P1 or safeguard fires). Schedule with `scripts/register-operator-task.ps1` (weekdays 6:30 PM local, configurable).

## Executor (Paper)

`backend/executor.py` is the autonomous nightly trading pipeline against the IBKR paper account (a paper-only guard refuses non-demo accounts). Each run: broker session → order-state sync (yesterday's fills become positions, stale intents expire) → reconciliation (drift latches a global entry halt) → Layer A closes → Layer C entries per lab book with server-side GTC profit-takers → anomaly rules → heartbeat + digest.

Safety machinery, each with its own module and pinned tests:

- **Kill switch** (`trading_control.py`): `ACTIVE` / `HALT_ENTRIES` / `FLATTEN_REQUESTED` per scope; fail-closed defaults (missing or unreadable state reads as halted); halts latch until resumed from the console with a typed reason; a sentinel `HALT` file overrides everything; the remote ntfy channel accepts HALT only — RESUME over it is ignored and audited.
- **Lab books + gates** (`book_gates.py`): virtual $10K books — the ADR-0009 experiment matrix, one question per book — each enforcing the ADR-0006 envelope (2.5% max loss per trade, 50% max deployed, 8 positions) against its own virtual ledger, with capital encumbrance for pending orders and append-only gate/audit evidence.
- **Reconciliation** (`reconciliation.py`): broker vs. books compared nightly; discrepancies halt entries and are never auto-adjusted — silent adjustment would corrupt Live Gate evidence.
- **Anomaly rules** (`anomaly.py`): repeated rejections, duplicate orders, P&L shocks, and post-hoc envelope breaches auto-halt their scope.
- **Digest + dead-man watchdog** (`digest.py`, `scripts/watchdog.ps1`): fixed-order nightly digest (a halted system says so first, every night); interrupt-worthy events go out as a separate urgent push; an independent Scheduled Task (`scripts/register-watchdog-task.ps1`) alerts if the executor's heartbeat goes stale.
- **Weekly Flex audit** (`flex_audit.py`): cross-checks an IBKR Activity Flex statement against the incremental fills ledger; missing executions, absent orderRefs, and fill mismatches are reported, never auto-corrected. Schedule with `scripts/register-flex-audit-task.ps1`.
- **Supervision console** (`console.py` + `StatusStrip.svelte` / `BooksTab.svelte`): a status strip on every tab (PAPER badge, control state with HALT/RESUME + typed reason — the console is the *only* place RESUME exists — heartbeat staleness, last reconciliation) and a Books tab with per-book metrics, the Live Gate checklist, and a filterable audit trail.

---

## Testing

Three enforced layers, all in CI: pytest with an 80% branch-coverage gate (every fail-closed default, latch, and gate block has a failing test), vitest + svelte-check for the frontend (the API client is generated from the backend schema, so contract drift fails the type check), and a Playwright smoke pack that boots the real stack — FastAPI on a fresh temp database plus the built frontend — and drives the flows where breakage is dangerous: boot, session lock, close-position, the HALT/RESUME round-trip, and the Books tab.

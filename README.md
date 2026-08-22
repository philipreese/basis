# basis — autonomous options trading lab

A system for defined-risk options trading that is graduating from decision-support to autonomy ([ADR-0006/0007](spec/decisions.md)). It has two modes of operation today:

- **Supervision console**: a web app for watching and steering the executor — status strip, lab-book comparison with the Live Gate checklist, reconciliation resolution, audit trail, kill switches — plus a diagnostic playbook scan and position views (Overview · Scan · Books · Analysis · Settings; no session gating, #315).
- **Executor (Paper)**: an autonomous nightly pipeline that places real orders in an IBKR **paper** account across a matrix of virtual "lab books" racing strategy variants ([ADR-0009](spec/decisions.md)). Live money is gated behind the ADR-0006 Live Gate (≥30 closed paper trades per book, ≥3 months, zero envelope breaches, expectancy − 1 standard error ≥ 0 after a slippage haircut — an interim floor, ADR-0010) plus four further ADR-0010 promotion conditions still pending detection machinery (#215).

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
│   ├── regime_variants.py         Layer B engines V1–V6 (V1–V3 raced by books; V4–V6 observation-only)
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
│   ├── src/App.svelte             Orchestrator: tabs, global state
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
| `pixi run test-e2e` | Playwright smoke pack against the real stack (boot, navigation, close, HALT/RESUME, Books tab) |
| `pixi run lint` / `lint-fix` | Ruff lint + format check / autofix |
| `pixi run sync-types` | Regenerate `api-types.ts` from the running backend's OpenAPI schema |
| `pixi run executor` | Run the executor pipeline once (needs IB Gateway) |
| `pixi run fill-check` | Push a read-only summary of this morning's fills |
| `pixi run flex-audit` | Run the weekly Flex statement audit once |
| `pixi run restore-drill` | Sandboxed restore drill against a copied backup (`--against-production` for a live, read-only "what does the system think of the broker" check) |
| `pixi run empirical-null-drill` | Ledger-only bootstrap drill: measures the empirical-null distribution for the Live Gate leaderboard against the live database, read-only |
| `powershell ./scripts/verify-project.ps1` | Full pre-commit verification (secrets scan, all tests) |

### Configuration (`.env`, all optional)

- **IB Gateway** (paper mode, free 15-min-delayed data): `IBKR_GATEWAY_HOST=127.0.0.1`, `IBKR_GATEWAY_PORT=4002`, `IBKR_CLIENT_ID=17`, `IBKR_SMOKE_CLIENT_ID=19`. Without a reachable Gateway the web app runs fully on stored/manual telemetry.
- **Trading mode** (#204): `IBKR_TRADING_MODE` (`paper`|`live`, default `paper`) selects the mode and its own database file (`basis.db` vs `basis.live.db`; a legacy `options_playbook*.db` file is renamed automatically at startup). Every database is stamped with the mode that created it; a mode-mismatched open refuses at startup, and the paper executor pipeline refuses to run at all in live mode. Paper and live evidence never share a file — the paper lab is designed to keep running alongside live.
- **Push notifications**: `NTFY_TOPIC` (private [ntfy.sh](https://ntfy.sh) topic — treat as a secret), `NTFY_SERVER` (default `https://ntfy.sh`), `NTFY_COMMAND_TOPIC` (remote HALT channel — also a secret).
- **Executor**: `HALT_FILE` (sentinel path, default `HALT` in the repo root), `EXECUTOR_HEARTBEAT_FILE` (default `executor_heartbeat.json`).
- **Flex audit**: `IBKR_FLEX_TOKEN` (secret), `IBKR_FLEX_QUERY_ID`, `IBKR_FLEX_BASE` (override the Flex endpoint; rarely needed).
- **Ops**: `BASIS_LOG_DIR` (rotating entrypoint logs, default `logs/`), `BASIS_LOCK_DIR` (executor run-lock file, default repo root), `DATABASE_URL` (default `sqlite+aiosqlite:///basis.db`).
- **Misc**: `CORS_ORIGINS` (defaults to the local Vite dev server).

### Database

The schema is created directly from the SQLAlchemy models on startup. The database holds Live Gate evidence (real fills exist) and is never deleted: a model gaining a **nullable or defaulted** column is migrated additively in place (`ALTER TABLE ADD COLUMN`) at startup; a missing non-nullable column with no default fails loudly and demands a hand-written migration — the backend never drops or rewrites data itself. SQLite runs in WAL mode with a 5s busy timeout on every connection.

First start seeds: the default portfolio configuration, eleven playbooks — nine SPY (credit structures at $3 wings, debit spreads at $5; the long-vol event playbooks, broken-wing butterfly, and calendar spread ship disabled) plus the AAPL earnings-crush condor and the XSP tail-hedge put (both disabled, whitelisted only by B30/B32) — the complete ADR-0009 lab-book experiment matrix (32 books, B01–B32), and per-scope trading controls. Positions are never seeded — real databases start with an empty book.

---

## The Three Layers

- **Layer A — Observation** (`observation.py`): scans every open position into a priority (`P1 — CLOSE NOW` down to `OK`) with the exit-rule math shown, aggregates portfolio Greeks against limits, flags exposure safeguards (concentration, deployment), and surfaces defensive-roll candidates.
- **Layer B — Market Context** (`regime.py`, `regime_variants.py`, `market_data.py`): classifies the regime (`CALM_BULL`, `HIGH_VOL_NEUTRAL`, `TRENDING_BEAR`, `EVENT_CATALYST`). Seven engines read every night and persist to `regime_readings`: V0 (weighted scoring matrix), V1 (VIX term structure), V2 (volatility risk premium), and V3 (repaired scoring matrix) are raced by lab books; V4 (VIX9D/VIX short-end inversion), V5 (HYG/LQD credit ratio), and V6 (RSP/SPY breadth ratio) are observation-only — evidence first, a book only if earned. The digest carries a one-line regime consensus/split every night. Daily closes for ten symbols (VIX, VIX3M, VIX9D, SPY, IWM, GLD, TLT, HYG, LQD, RSP) persist to `index_history`, with a 1-year backfill on a symbol's first fetch.
- **Layer C — Opportunity** (`opportunity.py`): checks every enabled playbook against portfolio gates, suppression gates, and entry filters; derives strikes from target delta with full traceability; generates complete trade specs (legs, limit price, max loss, break-evens, exit rules). Hard blocks (e.g. `UNRESOLVED_P1`, `MAX_LOSS_EXCEEDED`) cannot be bypassed; warnings require explicit per-warning acknowledgement.

## Manual Console Workflows

- **Intent journal**: saving any position requires a mandatory journal entry (thesis, invalidation, expected move, emotional state, confidence).
- **Close + post-mortem**: closing freezes an immutable record (outcome, realized P&L, exit trigger, lesson tags, override flag).
- **Roll workflow**: credit verticals under pressure get a suggested down/up-and-out roll; execution enforces net-credit-only, max 2 rolls then forced exit, and direction rules.
- **Opportunity ledger**: every eligible or bypassed opportunity is logged; the view filters/sorts and summarizes the value of human overrides (counterfactual outcomes, with N).
- **Performance diagnostics**: per-playbook win rate, profit factor, avg return-on-risk, and sample-gated CAGR/Sharpe/max-drawdown (`null` below 10 trades or a 30-day span — never fabricated), benchmarked against SPY from stored index history.

---

## Nightly Operator

`backend/operator.py` is the evening-ritual library the executor pipeline runs each night: it reprices open positions from live quotes, refreshes SPY/VIX telemetry and all regime variants, runs the Layer A and Layer C scans, and composes the ntfy digest (priority escalates when a P1 or safeguard fires). It has no standalone entrypoint — `pixi run executor-nightly` (scheduled weekdays 6:45 PM local) is the sole scheduler.

## Executor (Paper)

`backend/executor.py` is the autonomous nightly trading pipeline against the IBKR paper account (a paper-only guard refuses non-demo accounts). Each run: broker session → order-state sync (yesterday's fills become positions, stale intents expire) → expiry settlement (positions past expiration cash-settle at their last mark and go `EXPIRED`; expired legs are reconciliation-neutral regardless of IB's purge timing) → reconciliation (drift latches a global entry halt) → Layer A closes → Layer C entries per lab book with server-side GTC profit-takers → anomaly rules → heartbeat + digest.

The exit side is fully self-recording: entries freeze the book-resolved playbook (id/version/snapshot) onto the position, so exits always run under the rules the trade was entered under; Layer A enforces the snapshot's `mandatory_exit_dte` as a hard time exit (and the B28 regime-flip exit); the GTC profit-taker child is tracked as its own order row, adopted by the position at fill, and cancelled before any manual close goes up; and every executor-side closure — profit-taker fill, Layer A close, expiry — writes a `ClosurePostMortem` row (trigger, realized P&L), the per-trade expectancy evidence the ADR-0006 Live Gate reads.

**Gateway lifecycle** (`gateway_lifecycle.py`): the nightly run starts IB Gateway on demand through IBC, polls the API port, runs the pipeline, and kills the Gateway process tree — no 24/7 session (resting GTC profit-takers live server-side at IBKR). On market holidays (`calendars.py`) the executor writes its heartbeat and exits without launching Gateway. After each trading-day run the database is copied to `DB_BACKUP_DIR` (default `~/OneDrive/basis-db-backups`, 7 rotations) — once real fills land the DB is Live Gate evidence, and a failed backup pushes an ntfy alert (`db_backup.py`). Every snapshot is verified before it's trusted (#649): opened and checked for a minimum set of tables (`books`, `orders`, `positions`, `audit_events`) that are non-empty whenever the source's are, and compared against the newest existing rotation's size — a snapshot under 25% of that size is quarantined under a `.suspect` suffix rather than risk overwriting a good backup. Either failure mode refuses the snapshot, deletes/quarantines it, and pushes an urgent `SCHEDULER_ALERT` instead of rotating in a silently-empty file. An order-path broker error aborts the rest of the submission phase — orders never fail soft. One-time setup: `scripts/setup-ibc.ps1` writes the bot's paper credentials into IBC's **local** `config.ini` (never this repo or `.env`); set `IBC_START_SCRIPT` in `.env`; schedule with `scripts/register-executor-task.ps1` (weekdays 6:45 PM local). Manual run: `pixi run executor-nightly`. A read-only morning fill check (`fill_check.py`, `scripts/register-fill-check-task.ps1`, 10:00 weekdays) pushes which resting orders filled at the open — notification only; the evening pipeline remains the sole database mutator.

**Operational hardening**: SQLite runs in WAL mode with a 5s busy timeout on every connection (console + scheduled entrypoints can overlap safely); every scheduled entrypoint logs to a rotating file under `logs/` (override with `BASIS_LOG_DIR`) in addition to the console; the register scripts set battery/wake flags so a laptop on battery still runs; and the fill check and Flex audit push a high-priority ntfy alert on any unexpected crash instead of exiting silently.

Safety machinery, each with its own module and pinned tests:

- **Kill switch** (`trading_control.py`): `ACTIVE` / `HALT_ENTRIES` / `FLATTEN_REQUESTED` per scope; fail-closed defaults (missing or unreadable state reads as halted); halts latch until resumed from the console with a typed reason; a sentinel `HALT` file overrides everything; the remote ntfy channel accepts HALT only — RESUME over it is ignored and audited.
- **Benchmark** (`benchmark.py`): the digest carries a $10K-in-SPY buy-and-hold line (price return, anchored on the first fill) — the null hypothesis every book must beat.
- **Lab books + gates** (`book_gates.py`): virtual $10K books — the ADR-0009 experiment matrix, one question per book — each enforcing the ADR-0006 envelope (2.5% max loss per trade, 50% max deployed, 8 positions) against its own virtual ledger, with capital encumbrance for pending orders and append-only gate/audit evidence.
- **Catalyst calendar** (`catalyst_calendar.py`): FOMC and CPI dates seed `catalyst_dates` automatically on every market refresh (additive and idempotent — manual entries survive, long-past ones prune), so the catalyst gates no longer depend on hand-typed dates. CPI classifies MAJOR alongside FOMC; the digest flags the calendar before seeded coverage lapses.
- **Ex-div assignment defense** (`assignment_defense.py`): short calls on American-style dividend payers (SPY/IWM/TLT) are hard-blocked at entry when the spec spans a projected ex-dividend date, and an ITM short call within 3 trading days of an ex-date is promoted to P1 CLOSE NOW — preventing the short-shares No-Stock breach that reconciliation could only detect. The static calendar's staleness is flagged in the nightly digest.
- **Reconciliation** (`reconciliation.py`): broker vs. books compared nightly; discrepancies halt entries and are never auto-adjusted — silent adjustment would corrupt Live Gate evidence.
- **Anomaly rules** (`anomaly.py`): repeated rejections, duplicate orders, P&L shocks, and post-hoc envelope breaches auto-halt their scope.
- **Digest + dead-man watchdog** (`digest.py`, `scripts/watchdog.ps1`): fixed-order nightly digest (a halted system says so first, every night); interrupt-worthy events go out as a separate urgent push; an independent Scheduled Task (`scripts/register-watchdog-task.ps1`) alerts if the executor's heartbeat goes stale.
- **Weekly Flex audit** (`flex_audit.py`): cross-checks an IBKR Activity Flex statement against the incremental fills ledger; missing executions, absent orderRefs, and fill mismatches are reported, never auto-corrected. Schedule with `scripts/register-flex-audit-task.ps1`. Open discrepancies (and a form to acknowledge one with a reason via `POST /api/resolution/flex-ack`) surface in the Books tab's Flex Audit panel — an acknowledged exec_id stops re-alerting on the next run without correcting the books.
- **Supervision console** (`console.py` + `StatusStrip.svelte` / `BooksTab.svelte`): a status strip on every tab (PAPER badge, control state with HALT/RESUME + typed reason — the console is the *only* place RESUME exists — heartbeat staleness, last reconciliation) and a Books tab with per-book metrics, the Live Gate checklist, a Flex-audit acknowledgment panel, and a filterable audit trail. The checklist also carries the four further ADR-0010 promotion conditions (stress episode, SPY benchmark, same-engine baseline, composition limit) as `not_yet_evaluated` rows until their detection machinery lands (#215, #655) — `eligible` is un-claimable while any is pending, never a silent pass — plus each book's `as_raced_config_hash` (#658): the config era its displayed evidence actually raced under, not necessarily the book's current config if it has since resynced, rendered next to the gate cells as provenance for the composition-limit condition above.

### Operations: restore drill

`backend/restore_drill.py` (`scripts/restore_drill.py`, `pixi run restore-drill`) automates the chaos drill that used to be a manual, rarely-run intention: it exercises the real reconcile/sync detection paths (RESTORE_GAP_UNKNOWN_HELD, GHOST_ORDER, drift classification, ORDER_LOST/REJECTED verdicts) against a REAL Gateway connection and reports what they'd find — read-only twice over, structurally, not by convention:

- **The broker** goes through `ReadOnlyBroker`, an adapter exposing only `reconcile`/`positions`/`open_orders`/`executions`. Every mutating `BrokerSession` method (`place_spread`, `close_spread`, `cancel_by_ref`, `cancel`, `preview_spread`, `wait_for_terminal`) raises `MutatingBrokerCallBlockedError` unconditionally, and any method the wrapper wasn't even taught about raises the same way — a mutating call reaching the wrapper is itself a drill finding, never silently routed through.
- **The database** is opened through a literal SQLite read-only URI connection (`mode=ro`); a stray write attempt raises at the driver, not at code review.

Default invocation copies the oldest `basis.YYYY-MM-DD.db` rotation from `DB_BACKUP_DIR` into a scratch directory and drills against the copy — production `basis.db` (and its `-wal`/`-shm` siblings) is never opened. `pixi run python scripts/restore_drill.py --against-production` runs the same recon-only analysis standalone, directly against the live database through the same read-only connection, as an operator "what does the system think of the broker right now" command. Reuses the gateway lifecycle and tenancy locks exactly as `fill_check.py` does (own `restore_drill` lock; defers if the executor, gateway, or fill-check lock is held), so it's safe to run any time, including unattended on a weekend.

Before the read-only analysis phase, the sandbox copy (only the copy — never `--against-production`) is migrated read-write by running the real `init_db()` against it in a fresh subprocess, mirroring real restore semantics: a restored backup gets migrated on the next process start, then the pipeline reconciles. This also exercises the migration path itself — additive `ALTER`s, table creation, the closure-post-mortem dupe quarantine, the test-pollution quarantine, seed/config sync — against genuinely old schemas, which the normal entrypoints never do (their databases are already current). The migration outcome (tables/columns added, quarantine and seed-sync rows) is its own drill-report section; a migration failure is reported as a run error and the drill exits before ever launching Gateway.

### Operations: empirical-null drill

`backend/empirical_null_drill.py` (`scripts/empirical_null_drill.py`, `pixi run empirical-null-drill`) measures a **selection null** for the Live Gate leaderboard, ledger-only — no market simulation, no Gateway. Every closed trade's haircut P&L (current evidence era only, #534; B00 and the permanently-promotion-excluded tail-hedge B32 excluded, ADR-0012) is pooled across books; for many iterations, synthetic arms matching the real matrix's shape (same arm count, same per-arm trade count as each real book) are resampled from that pool with replacement — destroying any arm-specific edge while preserving whatever structural premium the pool carries as a whole. The report gives the null distribution's percentiles for max-per-arm expectancy (and max-per-arm expectancy − 1·SE) and where each real book's current value falls against it.

This answers "is the best book distinguishable from a random draw of the system's own trades" (arm selection against multiplicity) — **not** "does the strategy work at all" (a no-edge-at-all null is v2 territory: a block bootstrap by date, or shuffled regime signals through the pipeline, would tighten the independence assumption this v1 makes). A positive max-per-book value in the null distribution is expected under this construction, not a broken drill. The database is opened through the same read-only `mode=ro` connection `restore_drill.py` uses; the report itself is a measurement, not yet an ADR threshold — promoting a measured percentile to the operative Live Gate bar (superseding the interim 1-SE floor, ADR-0010) is its own deliberate amendment, not an automatic consequence of running this drill.

---

## Testing

Three enforced layers, all in CI: pytest with an 80% branch-coverage gate (every fail-closed default, latch, and gate block has a failing test), vitest + svelte-check for the frontend (the API client is generated from the backend schema, so contract drift fails the type check), and a Playwright smoke pack that boots the real stack — FastAPI on a fresh temp database plus the built frontend — and drives the flows where breakage is dangerous: boot, navigation, close-position, the HALT/RESUME round-trip, and the Books tab.

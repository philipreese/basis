# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Sprint 6: Shared Component Library** (`frontend/src/lib/ui/`): 9 reusable Svelte 5 primitives — `Badge`, `Button`, `MetricCard`, `Alert`, `FormField`, `Collapsible`, `DataTable`, `Modal`, `Tooltip` — eliminating duplicated markup across all feature components.
- **Sprint 6: Design Token Centralization** (`frontend/src/index.css`): Rewrote CSS with Tailwind v4 `@theme` block. Semantic `--c-*` custom properties for consistent light/dark theming. Added `.glow-indigo` and `.glow-violet` to the glow set.
- **Sprint 6: Navigation Unit Tests** (`frontend/src/tests/navigation.test.ts`): 10 tests covering tab state transitions and session-lock gating.

### Changed
- **Sprint 6: UX Clarity** (`frontend/src/App.svelte`): Session lock banner explains the review requirement and shows a 3-step workflow breadcrumb. Opportunities pre-scan state is descriptive. Loading skeleton replaces spinner text. Empty states for post-mortems. First-time settings callout. Mobile tab labels aligned with desktop ("Positions", "Performance").
- **Sprint 6: CSS Bug Fixes**: Fixed dynamic Tailwind class names in `MarketContextRibbon.svelte`, non-standard color values in `CandidateCards.svelte` and `PositionScanner.svelte`.
- **Sprint 6: Component Refactors**: All feature components use shared `ui/` primitives — `Alert`, `FormField`, `Button`, `Badge`, `Collapsible`, `Tooltip` on Greek labels.

### Added (Sprint 6 Responsiveness & Polish)
- **Sprint 6: Snackbar notifications** (`frontend/src/lib/ui/Snackbar.svelte`, `snackbar.svelte.ts`): Fixed-position toast system replaces inline `errorMsg`/`successMsg` alerts. Toasts slide in via Svelte `fly` transition, auto-dismiss, and support success/error/info levels without shifting page layout.
- **Sprint 6: Interactive hover animations** (`frontend/src/lib/ui/Button.svelte`, `frontend/src/index.css`): Buttons scale up on hover and compress on click (`hover:scale-[1.02]`, `active:scale-[0.98]`). Global `cursor-pointer` rule covers all non-disabled buttons. New `.carbon-card-interactive` utility for hover-grow cards with mauve glow shadow.
- **Sprint 6: Fixed desktop status bar** (`frontend/src/App.svelte`): VS Code-style status bar is now `position: fixed` at the bottom of the viewport on desktop.

### Changed (Sprint 6 Responsiveness & Polish)
- **Sprint 6: Typography legibility pass** — Removed all sub-12px inline sizes (`text-[10px]`, `text-[11px]`, `text-[9px]`) across every component. Minimum body text is now `text-xs` (12px). Description and reason copy bumped to `text-sm` (14px) in `PositionScanner`, `TradeSpecCard`, `CandidateCards`, `SafeguardsPanel`, `Alert`, and the session-lock banner. `DataTable` table font bumped to `text-sm`.

### Added (Sprint 6 UI Polish, continued)
- **Sprint 6: UI Polish & Mobile Layout** (`frontend/src/App.svelte`, `frontend/src/lib/PositionScanner.svelte`, `frontend/src/lib/CandidateCards.svelte`, `frontend/src/lib/TradeSpecCard.svelte`, `frontend/src/lib/PostMortemCard.svelte`, `frontend/src/lib/OpportunityLedger.svelte`, `frontend/src/lib/MarketContextRibbon.svelte`):
  - Designed mobile-first responsive layout tailored for evening phone usage (improved grids, padding, tap-friendly sizes).
  - Implemented prominent above-the-fold red banner on load showing P1 "CLOSE NOW" alerts.
  - Added "Re-lock Session" button in header to easily lock navigation back down.
  - Created centralized formatting utility functions in `frontend/src/lib/formatters.ts` to strictly enforce:
    - Dollar amounts: 2 decimal places with currency symbol, properly handling negative values (e.g. `-$12.34`).
    - Percentages: 1 decimal place (e.g. `12.3%`).
    - DTE: integer format (e.g. `21 DTE`).
    - Dates: `Month DD YYYY` format (e.g., `June 18 2026`).
  - Added unit tests for formatting utilities in `frontend/src/tests/formatters.test.ts`.
- **Live Options Pricing Refresh** (`backend/market_data.py`, `backend/main.py`, `frontend/src/App.svelte`): Added `format_occ_symbol` and `fetch_options_latest_quotes` helpers to fetch live option quotes from Alpaca Options Market Data API (`/v1beta1/options/quotes/latest`). Created `POST /api/positions/refresh` API endpoint to automatically update `current_value_per_share` of all open positions based on current mid-market pricing. Wired frontend to refresh position pricing on load and when live market data is fetched. Added unit and integration tests covering the new helpers and API route.

### Fixed
- **Floating-point noise in telemetry form** (`frontend/src/App.svelte`): Rounded the SMA20 and Daily Return values in the manual telemetry input form to prevent display of raw floating-point noise.
- **Close P&L floating-point imprecision** (`backend/main.py`): Rounded `realized_pnl` to 2 decimal places in the close position endpoint to eliminate floating point noise in the raw API response.

## [0.5.0] - 2026-06-09

### Added
- **Sprint 5: Intent Journal** (`backend/models.py`, `backend/main.py`): `OperationalJournalEntrySchema` is now mandatory on `POST /api/positions`; missing or partial journal returns 422. Positions store `warnings_acknowledged: List[str]` to track which warnings the user confirmed before saving.
- **Sprint 5: Close Position workflow** (`POST /api/positions/{id}/close`): Accepts `ClosePositionRequest` (current value, exit trigger, actual move %, lesson tags). Computes realized P&L (DEBIT: current−entry; CREDIT: entry−current), derives WIN/LOSS/BREAKEVEN outcome, sets `user_override_logged` from `warnings_acknowledged`, freezes position to CLOSED, and creates a `ClosurePostMortemModel` record — all in one atomic transaction.
- **Sprint 5: Post-mortem retrieval** (`GET /api/positions/post-mortems`, `GET /api/positions/{id}/post-mortem`): List all post-mortems or fetch by position ID; route ordering ensures `/post-mortems` resolves before `/{id}`.
- **Sprint 5: Opportunity Ledger** (`GET/POST /api/opportunity/ledger`, `PATCH /api/opportunity/ledger/{id}`): Logs every accepted and bypassed trade opportunity. PATCH endpoint updates `outcome_if_taken` for after-the-fact analysis.
- **Sprint 5: Performance Diagnostics** (`GET /api/performance/diagnostics`): Returns `PerformanceDiagnosticsSchema` with per-playbook win rate, profit factor, avg return-on-risk grouped by `(playbook_id, playbook_version)`. CAGR/Sharpe/max-drawdown stub as "N/A (insufficient data)". Benchmarks section is stubbed. Initializes empty — no fictional data.
- **Sprint 5 ORM models**: `ClosurePostMortemModel` (table `closure_post_mortems`) and `OpportunityRecordModel` (table `opportunity_records`).
- **Sprint 5 Pydantic schemas**: `ClosePositionRequest`, `ClosurePostMortemSchema`, `OpportunityRecordSchema`, `UpdateOutcomeRequest`, `PlaybookMetrics`, `BenchmarkData`, `PerformanceDiagnosticsSchema`.
- **Sprint 5 frontend** (`TradeSpecCard.svelte`): "Save Trade Spec & Log Intent Journal" now reveals a mandatory 5-field intent journal form (thesis, invalidation, expected move, emotional state, confidence rating); "Confirm & Save Position" button only enabled when all fields are valid. On save: creates the position via API and logs an accepted opportunity record.
- **Sprint 5 frontend** (`PositionScanner.svelte`): P1-priority cards now show a "Close Position Now →" button; triggers `ClosePositionModal` overlay.
- **Sprint 5 frontend** (`CandidateCards.svelte`): Override button on suppressed playbooks now logs a bypassed `OpportunityRecord` (with suppression reason) before generating the trade spec.
- **Sprint 5 frontend** (`ClosePositionModal.svelte`, `PostMortemCard.svelte`, `OpportunityLedger.svelte`, `PerformanceDashboard.svelte`): New display components for the full post-trade workflow.
- **Sprint 5 frontend** (`App.svelte`): Integrates all Sprint 5 state (post-mortems, opportunity records, diagnostics), modal close flow, and position-saved callback.
- **Sprint 5 Tests** (`backend/tests/test_sprint5.py`): 28 tests covering journal enforcement (422 variants), close position P&L logic (WIN/LOSS/BREAKEVEN/double-close/404), post-mortem retrieval, opportunity ledger CRUD, and diagnostics computation. Total test count: 170.

### Fixed
- **Pre-sprint-5 bug fixes** (`backend/opportunity.py`, `backend/database.py`): Five correctness issues fixed — dead `_spy_trend_label` branch, `run_lifecycle_scan` hardcoded spy_price/regime in `_run_hard_blocks`, PREMIUM_UNREASONABLE using BUY-leg strike instead of market price, Iron Condor profit/loss targets using max_loss instead of limit price, and `_needs_migration` early-return skipping Sprint 4 check.

## [0.4.0] - 2026-06-09

### Added
- **Layer C Opportunity Engine** (`backend/opportunity.py`): Full Section 4.3/5.1/5.2/5.5 implementation. Scans all active playbooks against current market telemetry; applies portfolio-level gates (MAX_POSITIONS, MAX_CAPITAL), per-playbook suppression gates (UNDERLYING_CONCENTRATION, DIRECTIONAL_CONCENTRATION, IVR_GATE_INCOME, IVR_GATE_DEBIT), and entry filters (IVR range, VIX range, trend, catalyst). Returns `OpportunityScanResult` with eligible candidate cards and derived strike parameters.
- **Trade Spec Generator** (`backend/opportunity.py`): `generate_trade_spec()` derives concrete legs, limit price, max loss, break-even prices, profit target, loss limit, and GTC closing instructions for Iron Condor, Bull Call Spread, Bear Put Spread, Long Straddle, and Long Strangle strategies. Uses VIX-based 1σ move and rational Φ⁻¹ approximation for strike derivation; all derivation inputs recorded in `StrikeDerivedParams` for full traceability.
- **Trade Spec Validation** (`backend/opportunity.py`): `_run_hard_blocks()` checks UNRESOLVED_P1 (per-position lifecycle scan), CAPITAL_EXCEEDED, MAX_LOSS_EXCEEDED, EXPIRATION_ARITHMETIC (< 14 DTE), PREMIUM_UNREASONABLE, POSITION_COUNT, and STRIKE_SANITY. `_run_warnings()` checks REGIME_CONSISTENCY, DUPLICATE_UNDERLYING, BREAKEVEN_REALISM (> 2σ), and STRATEGY_NOVELTY. Hard blocks set `spec=None`; warnings require explicit UI confirmation before proceeding.
- **Playbook Seeding** (`backend/database.py`): Five default playbooks seeded on `init_db()`: SPY Iron Condor, Bull Call Spread, Bear Put Spread, Long Straddle, Long Strangle.
- **Sprint 4 API models** (`backend/models.py`): `StrikeDerivedParams`, `CandidateCard`, `OpportunityScanResult`, `TradeSpecLeg`, `TradeSpec`, `HardBlock`, `TradeWarning`, `TradeSpecResult`.
- **`GET /api/opportunity/scan`**: Returns eligible candidate cards with automated strike derivation notes for all non-suppressed playbooks.
- **`POST /api/opportunity/spec/{playbook_id}`**: Generates a full `TradeSpecResult` with hard-block/warning validation for the given playbook.
- **`CandidateCards.svelte`** (frontend): Displays eligible playbooks with automated order specification and per-card "Generate Trade Spec →" button; shows portfolio-level suppression banner when blocked.
- **`TradeSpecCard.svelte`** (frontend): Full trade specification display with per-warning "Acknowledged" button, hard-block suppression (no bypass), P&L grid, order legs, break-evens, derivation parameters, and GTC closing instructions. Proceed button gated on all warnings confirmed.
- **App.svelte component extraction**: Split `MarketContextRibbon.svelte`, `GreeksPanel.svelte`, `SafeguardsPanel.svelte`, and `PositionScanner.svelte` out of App.svelte; App.svelte reduced from ~727 to ~530 lines as a thin orchestrator.
- **Sprint 4 Tests** (`backend/tests/test_sprint4.py`): 60 tests covering all gates, entry filters, hard blocks, warnings, strike derivation, spec generation, and API integration. Total test count: 142 across all four test files.
- **OpenAPI type sync**: Regenerated `frontend/src/lib/api-types.ts` to include all Sprint 4 schemas.

## [0.3.1] - 2026-06-09

### Fixed
- **Live market fetch** (`backend/market_data.py`): Alpaca API returns `null` bars when no date range is given and today's session is incomplete; added `start = today - 60 days` to guarantee historical bars are returned.
- **Alpaca feed** (`backend/market_data.py`): Changed feed from `sip` to `iex` for free-tier account compatibility; also fixed `payload.get("bars") or []` to safely handle `null` in the response.
- **Credential loading** (`backend/main.py`): Load `.env` with an explicit absolute path and `override=True` so credentials are always available regardless of working directory or pre-set shell environment variables.
- **Lazy credential reads** (`backend/market_data.py`): Read Alpaca credentials via `os.environ.get()` at call time instead of capturing module-level constants at import, preventing stale empty values.

### Added
- **`python-dotenv` dependency**: Added to `pixi.toml` and `pyproject.toml`.
- **`svelte.config.js`** (`frontend/`): Added to fix `svelte-check` failing to load config due to a Vite CJS/ESM incompatibility in `@sveltejs/load-config`.
- **Dev tooling tasks** (`pixi.toml`): Added `check-frontend` (svelte-check + TypeScript), `check-backend` (compileall), and `check` (both) tasks.
- **`PYTHONUNBUFFERED=1`** (`pixi.toml`): Set on the `server` task for reliable stdout output from the uvicorn worker process.

### Changed
- **Verification Script** (`scripts/verify-project.ps1`): Overhauled to auto-detect project type, validate conventional commits, scan for hardcoded secrets, and verify documentation sync.
- **Workspace Config**: Replaced `AGENTS.md` with `CLAUDE.md` (project-level Claude Code instructions), added `.claudeignore` to reduce token overhead, and updated `.gitignore` for AI agent artifacts.
- **SQLAlchemy engine** (`backend/database.py`): Removed `echo=True` to suppress verbose SQL INFO logs.

## [0.3.0] - 2026-06-08

### Added
- **Regime Scoring Matrix** (`backend/regime.py`): Implement the full Section 4.2 weighted scoring matrix classifying SPY/SMA20 trend, VIX level, per-underlying IVR, catalyst calendar, and daily return into four market regimes. Tie-breaking follows a risk-priority hierarchy: `EVENT_CATALYST > TRENDING_BEAR > HIGH_VOL_NEUTRAL > CALM_BULL`.
- **Market Data Client** (`backend/market_data.py`): Isolated Alpaca API client that fetches SPY daily bars (price, SMA20, daily return) and VIX closing price, with graceful `None` fallback when credentials are absent or requests fail.
- **Extended Market State**: `MarketStateSchema` and `MarketStateModel` now store `spy_sma20`, `vix_close`, `underlying_ivrs`, `spy_daily_return`, and `regime_scores` alongside the existing fields.
- **`POST /api/market/state`**: Now recomputes the regime server-side from the supplied telemetry inputs — `current_regime` in the request body is ignored and always recalculated.
- **`POST /api/market/fetch`**: New endpoint that triggers a live Alpaca data pull, updates the stored telemetry, and recomputes the active regime. Returns `503` when credentials are not configured.
- **Layer B Context Ribbon** (frontend): Subordinate ribbon below the header showing the active regime badge, live telemetry pills (SPY price, SMA20, VIX, daily return), and a collapsible score breakdown panel for all four regimes.
- **Expanded Telemetry Form** (frontend): Replaced the manual regime dropdown with six input fields (SPY price, SMA20, VIX, daily return %, IVRs, catalyst dates) and a "Fetch Live Data" button wired to the new endpoint.
- **Sprint 3 Tests** (`backend/tests/test_sprint3.py`): 57 tests covering all five classification functions and their boundary conditions, known scenario matrix outputs, all tie-breaking combinations, mocked Alpaca fetch calls, and API integration tests for the new endpoints.

## [0.2.0] - 2026-06-08

### Added
- **Observation Engine**: Implement position lifecycle scanner, portfolio Greeks aggregator, and exposure safeguards.
- **Simulated Telemetry**: Build mock market environment state APIs and UI controls to adjust simulated regimes, SPY index prices, and catalyst calendars.
- **Session Lock**: Lock navigation and settings access until the portfolio risk telemetry has been reviewed and acknowledged for the active session.
- **Tests**: Add 13 backend unit and integration tests covering priority transitions, DTE decay, short strike breaches, and safeguards.

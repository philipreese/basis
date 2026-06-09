# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

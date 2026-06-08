# Changelog

All notable changes to this project will be documented in this file.

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

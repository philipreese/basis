# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.34.0](https://github.com/philipreese/basis/compare/v0.33.6...v0.34.0) (2026-08-18)


### Features

* **gateway:** Add the IBC Gateway lifecycle, holiday guard, and 162 policy ([#157](https://github.com/philipreese/basis/issues/157)) ([4a25682](https://github.com/philipreese/basis/commit/4a2568266165f11cb31ecf30bf2cc6e02acc16b8))

## [0.33.6](https://github.com/philipreese/basis/compare/v0.33.5...v0.33.6) (2026-08-18)


### Code Refactoring

* **api:** Extract domain composition out of the fat main.py routes ([#181](https://github.com/philipreese/basis/issues/181)) ([eddf809](https://github.com/philipreese/basis/commit/eddf809a7940e9aab5c5bdd9336b475906b0c946))

## [0.33.5](https://github.com/philipreese/basis/compare/v0.33.4...v0.33.5) (2026-08-18)


### Code Refactoring

* **digest:** Type the blocked-entry seam as BlockedEntry data ([#177](https://github.com/philipreese/basis/issues/177)) ([6485b45](https://github.com/philipreese/basis/commit/6485b454817c624544dd85d14c38c8ce78494e22))

## [0.33.4](https://github.com/philipreese/basis/compare/v0.33.3...v0.33.4) (2026-08-18)


### Code Refactoring

* **executor:** Replace occ_by_leg tuples with a ComboLeg dataclass ([#173](https://github.com/philipreese/basis/issues/173)) ([569f70f](https://github.com/philipreese/basis/commit/569f70f6f1cbf292185fb52108f6e886fb6b55ce))

## [0.33.3](https://github.com/philipreese/basis/compare/v0.33.2...v0.33.3) (2026-08-18)


### Code Refactoring

* **models:** Declare the strategy vocabulary once as StrategyType ([#171](https://github.com/philipreese/basis/issues/171)) ([a36be22](https://github.com/philipreese/basis/commit/a36be222a564ad6853ccd050944d62ba6f404b1a))

## [0.33.2](https://github.com/philipreese/basis/compare/v0.33.1...v0.33.2) (2026-08-18)


### Code Refactoring

* **books:** Resolve book config through a typed BookConfig ([#168](https://github.com/philipreese/basis/issues/168)) ([671ccb0](https://github.com/philipreese/basis/commit/671ccb0769043a765761030fd325c51dcc65fd29))

## [0.33.1](https://github.com/philipreese/basis/compare/v0.33.0...v0.33.1) (2026-08-18)


### Miscellaneous

* **agents:** Configure repo for Pocock engineering skills ([#165](https://github.com/philipreese/basis/issues/165)) ([69b3a81](https://github.com/philipreese/basis/commit/69b3a81c6296f381707f95b07cab7b90cc1c6c7e))

## [0.33.0](https://github.com/philipreese/basis/compare/v0.32.1...v0.33.0) (2026-08-18)


### Features

* **digest:** Keep the nightly digest legible at 22 books ([#162](https://github.com/philipreese/basis/issues/162)) ([ac10e25](https://github.com/philipreese/basis/commit/ac10e251d5b3f5019bca970ebaaa4bd4c80e0fb4))

## [0.32.1](https://github.com/philipreese/basis/compare/v0.32.0...v0.32.1) (2026-08-18)


### Bug Fixes

* **calendars:** Correct verified ex-div and CPI dates ([#159](https://github.com/philipreese/basis/issues/159)) ([c3c457a](https://github.com/philipreese/basis/commit/c3c457a2f36a304fb3c8fda340dc0f4af9e7e9e5))

## [0.32.0](https://github.com/philipreese/basis/compare/v0.31.0...v0.32.0) (2026-08-18)


### Features

* **matrix:** Seed the TLT book B22 and complete the 22-book matrix ([#155](https://github.com/philipreese/basis/issues/155)) ([321da86](https://github.com/philipreese/basis/commit/321da86dce563022fb7ca29dc9c9197641b78497))

## [0.31.0](https://github.com/philipreese/basis/compare/v0.30.1...v0.31.0) (2026-08-18)


### Features

* **strategy:** Add the calendar spread and seed book B21 ([#153](https://github.com/philipreese/basis/issues/153)) ([af704ea](https://github.com/philipreese/basis/commit/af704ea82e91d27207b7bbd631e258d6079dc20e))

## [0.30.1](https://github.com/philipreese/basis/compare/v0.30.0...v0.30.1) (2026-08-18)


### Code Refactoring

* **backend:** Extract strategy builders and seed data into modules ([#151](https://github.com/philipreese/basis/issues/151)) ([7bf9e5c](https://github.com/philipreese/basis/commit/7bf9e5cb7d9a70f4ca10bcc5ec42cfc049111bac))

## [0.30.0](https://github.com/philipreese/basis/compare/v0.29.0...v0.30.0) (2026-08-18)


### Features

* **strategy:** Add the broken-wing butterfly and seed book B18 ([#148](https://github.com/philipreese/basis/issues/148)) ([61f35cd](https://github.com/philipreese/basis/commit/61f35cd7357c9f775517fbb581ad855153a17b5a))

## [0.29.0](https://github.com/philipreese/basis/compare/v0.28.0...v0.29.0) (2026-08-18)


### Features

* **regime:** Add the V3 repaired-matrix variant and seed books B19/B20 ([#146](https://github.com/philipreese/basis/issues/146)) ([42204bc](https://github.com/philipreese/basis/commit/42204bcab8eecf231dfca3b053de4045248e938c))

## [0.28.0](https://github.com/philipreese/basis/compare/v0.27.0...v0.28.0) (2026-08-18)


### Features

* **catalyst:** Seed FOMC/CPI dates into the catalyst calendar ([#144](https://github.com/philipreese/basis/issues/144)) ([9618ee8](https://github.com/philipreese/basis/commit/9618ee8f533044672fde590e77550f3eaa18d777))

## [0.27.0](https://github.com/philipreese/basis/compare/v0.26.0...v0.27.0) (2026-08-18)


### Features

* **defense:** Prevent ex-dividend early assignment on short calls ([#142](https://github.com/philipreese/basis/issues/142)) ([abca3b1](https://github.com/philipreese/basis/commit/abca3b12093bcc35c8bd62f2cd27c79fe60e3bfb))

## [0.26.0](https://github.com/philipreese/basis/compare/v0.25.0...v0.26.0) (2026-08-18)


### Features

* **telemetry:** Add per-underlying telemetry and seed the B09/B10 books ([#140](https://github.com/philipreese/basis/issues/140)) ([253aa23](https://github.com/philipreese/basis/commit/253aa2302c05bbf25f3490cefdc7f2ec4e8a34bf))

## [0.25.0](https://github.com/philipreese/basis/compare/v0.24.2...v0.25.0) (2026-08-18)


### Features

* **matrix:** Enforce the regime gate and seed the ADR-0009 experiment matrix ([#137](https://github.com/philipreese/basis/issues/137)) ([feffde2](https://github.com/philipreese/basis/commit/feffde21572b8b0342173d78cf202ec166eb50aa))

## [0.24.2](https://github.com/philipreese/basis/compare/v0.24.1...v0.24.2) (2026-08-17)


### Documentation

* **readme:** Restructure around the current system instead of sprint history ([#125](https://github.com/philipreese/basis/issues/125)) ([62548ad](https://github.com/philipreese/basis/commit/62548adbbe5bac85c6a9efe8779dc6e08613e771))

## [0.24.1](https://github.com/philipreese/basis/compare/v0.24.0...v0.24.1) (2026-08-17)


### Miscellaneous

* **ci:** Cache the Playwright browser keyed on the locked version ([#127](https://github.com/philipreese/basis/issues/127)) ([d15dcaa](https://github.com/philipreese/basis/commit/d15dcaae862389619c7c151f50528db9f56a44f9))

## [0.24.0](https://github.com/philipreese/basis/compare/v0.23.0...v0.24.0) (2026-08-17)


### Features

* **performance:** Compute sample-gated CAGR, Sharpe, max drawdown, and SPY benchmark ([#122](https://github.com/philipreese/basis/issues/122)) ([769494c](https://github.com/philipreese/basis/commit/769494c6d9e911610ccdbff93934dce6deffe297))

## [0.23.0](https://github.com/philipreese/basis/compare/v0.22.0...v0.23.0) (2026-08-17)


### Features

* **ledger:** Add filtering, sorting, and override-value summary to the Opportunity Ledger ([#119](https://github.com/philipreese/basis/issues/119)) ([823a706](https://github.com/philipreese/basis/commit/823a70605a7d45361bfe11708556fa11e718db01))

## [0.22.0](https://github.com/philipreese/basis/compare/v0.21.0...v0.22.0) (2026-08-17)


### Features

* **positions:** Add roll workflow with net-credit and roll-cap enforcement ([#118](https://github.com/philipreese/basis/issues/118)) ([33fea45](https://github.com/philipreese/basis/commit/33fea457cd5150c601b309520bbbcb9da87bcca6))

## [0.21.0](https://github.com/philipreese/basis/compare/v0.20.4...v0.21.0) (2026-08-17)


### Features

* **audit:** Add weekly Flex Query audit of the fills ledger ([#116](https://github.com/philipreese/basis/issues/116)) ([7b8c72f](https://github.com/philipreese/basis/commit/7b8c72ffd68c1618e3da5f5e9d98344be4c8bff4))

## [0.20.4](https://github.com/philipreese/basis/compare/v0.20.3...v0.20.4) (2026-08-17)


### Tests

* **e2e:** Add Playwright smoke pack for critical console flows ([#114](https://github.com/philipreese/basis/issues/114)) ([e4cd6b1](https://github.com/philipreese/basis/commit/e4cd6b165ef5d832a13b7c5f55915c1f39e38ded))

## [0.20.3](https://github.com/philipreese/basis/compare/v0.20.2...v0.20.3) (2026-08-17)


### Bug Fixes

* **frontend:** Enable close-position confirm by validating numeric bindings as numbers ([#112](https://github.com/philipreese/basis/issues/112)) ([e75d5cb](https://github.com/philipreese/basis/commit/e75d5cb6e3be095d55080798c2618c8dbc83dd14))

## [0.20.2](https://github.com/philipreese/basis/compare/v0.20.1...v0.20.2) (2026-08-17)


### Code Refactoring

* **api:** Add response_model to the portfolio observation endpoint ([#109](https://github.com/philipreese/basis/issues/109)) ([7b24711](https://github.com/philipreese/basis/commit/7b24711ca67a609699b63dfb610adc26a6b3a7ec))

## [0.20.1](https://github.com/philipreese/basis/compare/v0.20.0...v0.20.1) (2026-08-17)


### Code Refactoring

* **frontend:** Generate the API client from the backend OpenAPI schema ([#106](https://github.com/philipreese/basis/issues/106)) ([7a03606](https://github.com/philipreese/basis/commit/7a03606dd7bbf085b07fa1fbb290d2a7a1739a4c))

## [0.20.0](https://github.com/philipreese/basis/compare/v0.19.0...v0.20.0) (2026-08-17)


### Features

* **console:** Add status strip, Books tab, and audit view ([#103](https://github.com/philipreese/basis/issues/103)) ([9d53933](https://github.com/philipreese/basis/commit/9d539330bce3719610fca7d605135a304d3c8f25))

## [0.19.0](https://github.com/philipreese/basis/compare/v0.18.0...v0.19.0) (2026-08-17)


### Features

* **executor:** Add evening digest with urgent-push tiering and dead-man watchdog ([#101](https://github.com/philipreese/basis/issues/101)) ([520a770](https://github.com/philipreese/basis/commit/520a770ca7e410bd30a5ea473a7a489eae614d11))

## [0.18.0](https://github.com/philipreese/basis/compare/v0.17.1...v0.18.0) (2026-08-17)


### Features

* **executor:** Wire deterministic anomaly auto-halt rules into the pipeline ([#99](https://github.com/philipreese/basis/issues/99)) ([6e31ab2](https://github.com/philipreese/basis/commit/6e31ab25e5d81a99da4f8e7436bca1a6a6bd2220))

## [0.17.1](https://github.com/philipreese/basis/compare/v0.17.0...v0.17.1) (2026-08-17)


### Bug Fixes

* **executor:** Fit seed spreads inside the risk envelope and drop pre-launch migrations ([#97](https://github.com/philipreese/basis/issues/97)) ([0379bb3](https://github.com/philipreese/basis/commit/0379bb33ad2bba86a08a83b132cc023651921a85))

## [0.17.0](https://github.com/philipreese/basis/compare/v0.16.0...v0.17.0) (2026-08-17)


### Features

* **executor:** Add nightly executor pipeline orchestrating reconcile, closes, and entries ([#95](https://github.com/philipreese/basis/issues/95)) ([b69c1c7](https://github.com/philipreese/basis/commit/b69c1c7648469c0dbe73c511c44ad4bfd8af0c9c))

## [0.16.0](https://github.com/philipreese/basis/compare/v0.15.0...v0.16.0) (2026-08-17)


### Features

* **executor:** Add V1 and V2 regime variants with nightly readings and lab books ([#92](https://github.com/philipreese/basis/issues/92)) ([e8db9b7](https://github.com/philipreese/basis/commit/e8db9b7d56dc8c4e33de9d95b328be42c7be9ede))

## [0.15.0](https://github.com/philipreese/basis/compare/v0.14.0...v0.15.0) (2026-08-17)


### Features

* **executor:** Add per-book risk gates with capital encumbrance and netting block ([#90](https://github.com/philipreese/basis/issues/90)) ([1d5d9c8](https://github.com/philipreese/basis/commit/1d5d9c896c428b1483d39fcbbd14d5afd3edbbb8))

## [0.14.0](https://github.com/philipreese/basis/compare/v0.13.0...v0.14.0) (2026-08-17)


### Features

* **executor:** Add reconciliation engine with drift classification and fill backfill ([#88](https://github.com/philipreese/basis/issues/88)) ([88eac0c](https://github.com/philipreese/basis/commit/88eac0cb758b3a8f338126e3e92765a12d9becac))

## [0.13.0](https://github.com/philipreese/basis/compare/v0.12.0...v0.13.0) (2026-08-17)


### Features

* **executor:** Add latched kill switch with fail-closed defaults and HALT-only remote ([#86](https://github.com/philipreese/basis/issues/86)) ([d586242](https://github.com/philipreese/basis/commit/d586242f21f82da5715bd3a4ef6e4f193629810c))

## [0.12.0](https://github.com/philipreese/basis/compare/v0.11.0...v0.12.0) (2026-08-17)


### Features

* **executor:** Add BrokerSession adapter with combo orders and orderRef idempotency ([#84](https://github.com/philipreese/basis/issues/84)) ([cb90217](https://github.com/philipreese/basis/commit/cb90217c2b4a7f2085e987231f80aef266e9b3d3))

## [0.11.0](https://github.com/philipreese/basis/compare/v0.10.0...v0.11.0) (2026-08-17)


### Features

* **executor:** Add IBKR paper smoke-test script for combo order mechanics ([#81](https://github.com/philipreese/basis/issues/81)) ([892a850](https://github.com/philipreese/basis/commit/892a8507561fede39786f6831e7a3203a58112d1))

## [0.10.0](https://github.com/philipreese/basis/compare/v0.9.0...v0.10.0) (2026-08-17)


### Features

* **executor:** Persist nightly VIX and VIX3M closes to index_history ([#79](https://github.com/philipreese/basis/issues/79)) ([a8629e3](https://github.com/philipreese/basis/commit/a8629e361171566d4e7e9fdcf00213f00906435d))

## [0.9.0](https://github.com/philipreese/basis/compare/v0.8.2...v0.9.0) (2026-08-17)


### Features

* **executor:** Add multi-book schema with append-only audit tables ([#77](https://github.com/philipreese/basis/issues/77)) ([8c80507](https://github.com/philipreese/basis/commit/8c80507cd30b861d2b68bbeef240b6ca5f2dfe6a))

## [0.8.2](https://github.com/philipreese/basis/compare/v0.8.1...v0.8.2) (2026-08-17)


### Documentation

* **spec:** Add supervision concern, kill-switch ADR, and record executor decisions ([#75](https://github.com/philipreese/basis/issues/75)) ([d3f6f1c](https://github.com/philipreese/basis/commit/d3f6f1c1c21a4c93cd88ecdc3dca6fae470b9ddd))

## [0.8.1](https://github.com/philipreese/basis/compare/v0.8.0...v0.8.1) (2026-08-17)


### Documentation

* **spec:** Add Executor-Paper design document ([#58](https://github.com/philipreese/basis/issues/58)) ([4456687](https://github.com/philipreese/basis/commit/445668775a8375d7ea082281fd8d082058ce3cf2))

## [0.8.0](https://github.com/philipreese/basis/compare/v0.7.8...v0.8.0) (2026-08-17)


### Features

* **data:** Replace Alpaca market data with IBKR delayed feed ([#51](https://github.com/philipreese/basis/issues/51)) ([eb64fef](https://github.com/philipreese/basis/commit/eb64fef1f53b2c1ddf3ab801f1686444256a8e7c))
* **operator:** Add nightly evening pipeline with ntfy push digest ([#49](https://github.com/philipreese/basis/issues/49)) ([796eb10](https://github.com/philipreese/basis/commit/796eb10be8847ad19630609165b254bff3149931))


### Bug Fixes

* **ui:** Allow closing any open position; stop seeding demo positions ([#54](https://github.com/philipreese/basis/issues/54)) ([540a9a4](https://github.com/philipreese/basis/commit/540a9a44e267b134c9ae0fcc06286fb859b3c125))


### Continuous Integration

* Merge Release PRs with --squash to match repo merge settings ([#56](https://github.com/philipreese/basis/issues/56)) ([5da896b](https://github.com/philipreese/basis/commit/5da896b325c70fb6a45b9901ca16c32fb88694b8))

## [0.7.8](https://github.com/philipreese/basis/compare/v0.7.7...v0.7.8) (2026-08-17)


### Bug Fixes

* **backend:** Restrict CORS to the local dev origin ([f6c3cc1](https://github.com/philipreese/basis/commit/f6c3cc140cef7ee91dd4378c19e19b26cc933d0e))

## [0.7.7](https://github.com/philipreese/basis/compare/v0.7.6...v0.7.7) (2026-08-17)


### Bug Fixes

* **backend:** Unify capital-at-risk calculation across gates and safeguards ([aafc1ba](https://github.com/philipreese/basis/commit/aafc1ba09eb73e81e850c1ac426b875bfcad2df2))

## [0.7.6](https://github.com/philipreese/basis/compare/v0.7.5...v0.7.6) (2026-08-17)


### Miscellaneous

* Add ruff and pytest-cov gates; fix pixi test task chaining ([e5660c0](https://github.com/philipreese/basis/commit/e5660c0fc284a38b0a483324024f46598008b109))

## [0.7.5](https://github.com/philipreese/basis/compare/v0.7.4...v0.7.5) (2026-08-17)


### Code Refactoring

* **backend:** Make pricing.py the single source of trade economics ([2cf37f6](https://github.com/philipreese/basis/commit/2cf37f6f52dfdf00b36e4d9aecee5ef99b1daba7))

## [0.7.4](https://github.com/philipreese/basis/compare/v0.7.3...v0.7.4) (2026-08-17)


### Bug Fixes

* **db:** Replace drop_all migration heuristic with Alembic migrations ([fd39483](https://github.com/philipreese/basis/commit/fd3948307fc183e89d0ec5070ea18f86c80617e0))

## [0.7.3](https://github.com/philipreese/basis/compare/v0.7.2...v0.7.3) (2026-08-17)


### Documentation

* Fix spec drift, retire snapshot spec files, README truthfulness pass ([c19e329](https://github.com/philipreese/basis/commit/c19e329e018f3d6a1a7d9f74efc81ccd1b43e54b))

## [0.7.2](https://github.com/philipreese/basis/compare/v0.7.1...v0.7.2) (2026-08-17)


### Documentation

* Consolidate standards into AGENTS.md; hand CHANGELOG to release-please ([0ac4769](https://github.com/philipreese/basis/commit/0ac4769e1e7c1f8dfbfddd1101b821deaa73e0b1))

## [0.7.1](https://github.com/philipreese/basis/compare/v0.7.0...v0.7.1) (2026-08-17)


### Documentation

* **spec:** Capture autonomy roadmap decisions (ADR-0006, ADR-0007) ([f2a9696](https://github.com/philipreese/basis/commit/f2a9696eb3e72c89cd255addbd406ecf5c90b5ca))

## [0.7.0](https://github.com/philipreese/alpaca-agent-bot/compare/v0.6.1...v0.7.0) (2026-07-22)


### Features

* **playbooks:** Add credit-spread playbooks and playbook enabled flag ([266f9ec](https://github.com/philipreese/alpaca-agent-bot/commit/266f9ec1bf579fca0c94ff74021f4226b41d937d)), closes [#20](https://github.com/philipreese/alpaca-agent-bot/issues/20)

## [0.6.1](https://github.com/philipreese/alpaca-agent-bot/compare/v0.6.0...v0.6.1) (2026-06-14)


### Bug Fixes

* **ci:** Use immediate gh pr merge instead of auto-merge ([114425a](https://github.com/philipreese/alpaca-agent-bot/commit/114425ae4a9c4fdc5f7d216536d184475b10af75))


### Documentation

* **spec:** Remove branch protection step — not available on free private repos ([43f9160](https://github.com/philipreese/alpaca-agent-bot/commit/43f91605d2f837ac45a42bfd64f6732f3ec5e65f))


### Continuous Integration

* Set up GitHub Actions CI and release-please automation ([a49af49](https://github.com/philipreese/alpaca-agent-bot/commit/a49af49d3c8b02b43aaeb4a47148de2b5c3ea710))


### Miscellaneous

* **ci:** Regenerate pixi.lock with linux-64 platform ([d96988b](https://github.com/philipreese/alpaca-agent-bot/commit/d96988ba6909e682382b3948f05ac97ec118c91b))

## [0.6.0] - 2026-06-11

### Added
- **Shared Component Library** (`frontend/src/lib/ui/`): 9 reusable Svelte 5 primitives — `Badge`, `Button`, `MetricCard`, `Alert`, `FormField`, `Collapsible`, `DataTable`, `Modal`, `Tooltip` — eliminating duplicated markup across all feature components.
- **Design Token Centralization** (`frontend/src/index.css`): Rewrote CSS with Tailwind v4 `@theme` block. Semantic `--c-*` custom properties for consistent light/dark theming. Added `.glow-indigo` and `.glow-violet` to the glow set.
- **Snackbar notifications** (`frontend/src/lib/ui/Snackbar.svelte`, `snackbar.svelte.ts`): Fixed-position toast system replaces inline `errorMsg`/`successMsg` alerts. Toasts slide in via Svelte `fly` transition, auto-dismiss, and support success/error/info levels without shifting page layout.
- **Interactive hover animations** (`frontend/src/lib/ui/Button.svelte`, `frontend/src/index.css`): Buttons scale up on hover and compress on click (`hover:scale-[1.02]`, `active:scale-[0.98]`). Global `cursor-pointer` rule covers all non-disabled buttons. New `.carbon-card-interactive` utility for hover-grow cards with mauve glow shadow.
- **Fixed desktop status bar** (`frontend/src/App.svelte`): VS Code-style status bar is now `position: fixed` at the bottom of the viewport on desktop.
- **Mobile-first responsive layout** (`App.svelte`, `PositionScanner`, `CandidateCards`, `TradeSpecCard`, `PostMortemCard`, `OpportunityLedger`, `MarketContextRibbon`): Redesigned grids, padding, and tap targets for evening phone usage. Prominent above-the-fold red banner for P1 "CLOSE NOW" alerts. Re-lock Session button in header. Centralized formatting utilities (`formatters.ts`) for dollars, percentages, DTE, and dates; unit tests in `formatters.test.ts`.
- **Live Options Pricing Refresh** (`backend/market_data.py`, `backend/main.py`, `frontend/src/App.svelte`): `format_occ_symbol` and `fetch_options_latest_quotes` fetch live mid-market quotes from Alpaca Options Market Data API. `POST /api/positions/refresh` updates `current_value_per_share` for all open positions; frontend refreshes on load and after a live fetch. Covered by unit and integration tests.
- **Navigation Unit Tests** (`frontend/src/tests/navigation.test.ts`): 10 tests covering tab state transitions and session-lock gating.
- **Session re-lock discoverability** (`App.svelte`): Tooltip on the Re-lock button explains its effect; Enter key acknowledges and unlocks the session while the lock banner is visible.

### Changed
- **UX Clarity** (`App.svelte`): Session lock banner explains the review requirement with a 3-step breadcrumb. Opportunities pre-scan state is descriptive. Loading skeleton replaces spinner text. Empty states for post-mortems and first-time settings callout. Mobile tab labels aligned with desktop.
- **Design-token consistency**: Converted `SafeguardsPanel`, `PerformanceDashboard`, and `OpportunityLedger` from hardcoded Tailwind slate/rose classes to Catppuccin tokens (`--ctp-*`) and shared `ui/` primitives so they theme correctly in light/dark mode.
- **Component refactors**: All feature components use shared `ui/` primitives — `Alert`, `FormField`, `Button`, `Badge`, `Collapsible`, `Tooltip` on Greek labels.
- **CSS bug fixes**: Fixed dynamic Tailwind class names in `MarketContextRibbon.svelte`, non-standard color values in `CandidateCards.svelte` and `PositionScanner.svelte`.
- **Typography legibility pass**: Removed all sub-12px inline sizes across every component. Minimum body text is `text-xs` (12px); description and reason copy bumped to `text-sm` (14px) in `PositionScanner`, `TradeSpecCard`, `CandidateCards`, `SafeguardsPanel`, `Alert`, and the session-lock banner.
- **Fetch Live feedback** (`App.svelte`): Market Telemetry form shows a "Pulling SPY & VIX from Alpaca…" indicator and disables inputs while a live fetch is in flight.
- **Inline telemetry validation** (`App.svelte`, `FormField`): IVRs and Catalyst Dates fields validate format as you type; "Apply Telemetry" is disabled until inputs parse. Added optional `error` prop to `FormField`.
- **Override justification** (`CandidateCards`): Overriding a suppressed playbook now requires a written reason, recorded to the opportunity ledger's `bypass_reason`.
- **Greek-limit CTA** (`GreeksPanel`): Exceeded Greek limit now shows an alert with a "Review positions →" action that scrolls to the position scanner.

### Fixed
- **Floating-point noise in telemetry form** (`App.svelte`): Rounded SMA20 and Daily Return values to prevent display of raw floating-point noise.
- **Close P&L floating-point imprecision** (`backend/main.py`): Rounded `realized_pnl` to 2 decimal places in the close position endpoint.

### Accessibility
- **svelte-check 0 warnings** (`ui/Tooltip.svelte`, `ui/Collapsible.svelte`): Added `role="group"` to the Tooltip wrapper span; used `untrack()` in Collapsible to silence the `state_referenced_locally` hint.
- **Modal** (`ui/Modal.svelte`): Autofocuses the first field on open; added `tabindex` and keyboard handling.
- **Tables**: Added `scope="col"` to headers in `DataTable`, `PerformanceDashboard`, and `OpportunityLedger`.
- **Severity**: Safeguard alerts convey severity by icon + text (via `Alert`), not color alone.

### Documentation
- **Modular specification** (`spec/`): Split the monolithic `spec/project_spec.md` into concern-based files indexed by `spec/README.md` — `product.md`, `architecture.md`, `domain-rules.md`, `data-models.md`, `api.md`, `decisions.md`, and `standards.md`. Original preserved at `spec/archive/project_spec_v8.md`.
- **Analysis docs** (`spec/`): Added `gap-analysis.md`, `ux-review.md`, and `roadmap.md`.
- **Issue-driven workflow**: Documented the full GitHub CLI loop in `spec/standards.md`, `CLAUDE.md`, and `GEMINI.md`. Board Auto-add and Item-closed→Done workflows wired up.

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

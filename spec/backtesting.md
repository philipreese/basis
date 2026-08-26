# Backtesting — historical replay as an elimination tool

The backtester (`backend/backtest/`, #796) replays the production decision pipeline against a historical options corpus. It exists to **retire** books, playbooks, and knobs — never to promote them or raise confidence in them. Every rule below is bound by [ADR-0015](decisions.md) (direction rule, structural separation, unconditional run log, data honesty); this file describes the mechanism, not the rationale — read the ADR first.

## The corpus and its trust tiers (#793)

- **Chains** (`chain_store.py`): optionsDX end-of-day bid/ask chains ingested into a standalone SQLite file, stored RAW — crossed/locked quotes kept exactly as shipped and filtered to unquoted at load time, with the drop count reported on every snapshot. **SPX is the primary series (2010–2023)**; XSP is served as an SPX ÷10 derived view flagged `derived_from_spx` (a declared assumption — XSP is not in the corpus). **SPY chains before 2015-01-01 are refused for verdict-grade use** (`DegradedCorpusError`): 4–11% crossed quotes and real zero-bid holes in the tradeable band make them a degraded tier. A missing day returns no snapshot — never interpolated; **a gap in the corpus is a no-entry day**, not a synthesized one.
- **Closes** (`closes_store.py`): per-symbol `date,close` CSVs (#794) served as trailing slices. Every read is bounded by an explicit `through` date, so replay code structurally cannot look ahead.

## The replay architecture, in brief

`driver.py`'s `run_replay` walks each trading day and calls the **real production functions verbatim** — `scan_opportunities`, `generate_trade_spec`, `compute_regime`, the variant classifiers, `run_lifecycle_scan`, `resolve_book_config`, and `evaluate_book_gates` against an in-memory SQLite session on the production schema. Only the ~90-line nightly orchestration is replicated, each replica citing the production file:line it mirrors. Every decision call passes `today=` explicitly, and the whole loop runs inside `clock_guard.poisoned_clock()`: any defaulted wall-clock read (`market_today()`) raises `ReplayClockError` instead of silently reading the present. Entries decided on day T fill on the next trading day; expiries settle intrinsically off the underlying close (`settlement.py`, SPX/XSP last-trading-day dating rule).

A position whose legs span more than one expiration (a B21 calendar spread — `pos.expiration_date` holds only the FRONT leg's date) is refused for BOTH automated settlement and staged closing, mirroring production's `_mismatched_leg_expirations` guard (#691/#708/#761) via the same shared predicate, imported verbatim rather than reimplemented (#807). The position is left OPEN — occupying its book's `max_positions` envelope slot and accruing `stale_marks` for the rest of the run, once the front leg drops out of the chain — and the run's `multi_expiry_unsupported` counter (a position-DAY count: it re-fires every remaining trading day the position stays open) and `EXPIRY_SETTLEMENT_BLOCKED_MULTI_EXPIRATION`/`CLOSE_SKIPPED_MISMATCHED_EXPIRATION` events record it — `report.py` calls a nonzero count out loudly: **any run carrying it is not verdict-grade for the affected book.**

## The fill model's declared assumptions

`backend/backtest/fills.py`'s module docstring is the **source of truth** for the declared assumption set (ADR-0015 §4): worst-side fills with no additional slippage haircut, the flat per-leg-contract commission constant, nearest-listed strike snapping with away-from-the-money tie-breaks, no substitute expirations, never partial-fill, mid-based marks that keep the prior mark rather than invent a price. The driver and settlement docstrings declare the orchestration-level assumptions (pseudo-IVR in place of a live IVR feed, seeded catalyst calendar, AM-settlement dating). These docstrings are stamped verbatim into every logged run, so each historical run carries what was assumed **at the time it ran**.

## The run log contract (ADR-0015 §3, #792)

`runlog.py` writes a **separate SQLite file** (`backtest.db`, caller-supplied path — never the production data directory) with `backtest_runs`, `backtest_trades`, and `backtest_verdicts`. The contract, enforced structurally:

- **Every run is logged**, win or lose, with its subject, config hash (`seeds._config_hash` over the exact books/playbooks/portfolio tested), date range, and stamped assumption set. A run that crashes still stands in the log, unfinished — the denominator counts attempts.
- **`what_changed` is required non-empty** — the free-text reason for this run relative to the prior run on the same subject is the denominator's meaning; `open_run` refuses an empty one ("first run on this subject" is valid).
- **Verdicts are RETIRE-only.** `backtest_verdicts` carries `CHECK(verdict = 'RETIRE')` — the schema is structurally incapable of expressing promotion or confidence.
- **The verdict embeds its own denominator.** `record_retirement` computes `prior_variant_count` at verdict time — the count of runs on the same subject up to and including the retired run ("this was variant N") — and never accepts it from the caller. Orphan verdicts (no matching run/subject) are refused.
- **Reports lead with log position.** `report.py`'s header opens with "run N; M prior runs on subject X" before any outcome number — a result without its position in the log is not evidence.
- **The two denominators partition, and must stay partitioned** (#792 item 3). The backtest run log counts backtest iterations per subject; the paper selection-null's trial count ([backend/empirical_null_drill.py](../backend/empirical_null_drill.py), #717) counts arms that produced closed paper trades — with **no book-status filter**, so an arm that traded paper stays in the paper N even after retirement. An arm retired by backtest before ever trading paper belongs only to this run log's denominator. Never add a status filter to `load_haircut_pnls_by_book`, and never count paper-naive backtest kills in the paper null; when a production retirement mechanism exists, this rule gets a tripwire test (the #674 pattern).

## Fail-closed preconditions

`run_replay` refuses to start (never warns) when: the date range falls outside `CALENDAR_COVERAGE_START/END` (#795 — outside it the calendars silently report no holidays/catalysts, wrong in the flattering direction); the closes store lacks SPY or VIX; the chain DB has no rows for a book's underlying. Mid-run, a missing settlement close raises — a settlement value is money, never guessed — and a variant that cannot read its regime sits the day out (`INSUFFICIENT_DATA` propagates as-is; it is the production behavior).

## CLI

`pixi run backtest run --start YYYY-MM-DD --end YYYY-MM-DD --subject <s> --what-changed <text> [--books B01,B18] --chains <db> --closes <dir> --runlog <db>` replays the seeded lab configuration, logs the run, and prints the report. `pixi run backtest retire --runlog <db> --run <n> --subject <s> --rationale <text>` records the only verdict that exists, printing it with its computed denominator. Retirement verdicts are recorded in `backtest.db` and acted on by the operator — there is no automated wiring into the control plane.

Source of truth: [`backend/backtest/`](../backend/backtest/) — `chain_store.py`, `closes_store.py`, `driver.py`, `fills.py`, `settlement.py`, `clock_guard.py`, `runlog.py`, `report.py`, `__main__.py`; tests in `backend/tests/test_backtest_*.py`.

# Architecture Decision Records

> Part of the [modular specification](README.md). Each record captures a load-bearing choice already evident in the spec and code, in short MADR form (Context / Decision / Consequences). New decisions append a new `## ADR-NNNN` section; superseded ones are marked, not deleted.

---

## ADR-0001 — Rules engine, not LLM

**Status:** Accepted

**Context.** The system generates trade specifications and blocks bad trades. A generative/LLM approach would be probabilistic and could "explain away" a bad output after the fact.

**Decision.** Implement the engine as deterministic rules over codified playbook data. No LLM/AI in the initial build. New strategies are injected as `PlaybookDefinition` data, not code. All validation runs *before* output is shown ("Common Sense First").

**Consequences.** Outputs are reproducible and auditable; strike derivations must display their parameters (no black boxes). Hard blocks are uncircumventable by design. The cost is no natural-language flexibility — every behavior must be expressed as an explicit rule in [domain-rules.md](domain-rules.md).

---

## ADR-0002 — Manual sandbox first; Alpaca behind env vars

**Status:** Superseded by [ADR-0006](#adr-0006--autonomy-roadmap-operator--executor-paper--executor-live) and [ADR-0007](#adr-0007--interactive-brokers-for-paper-and-live-execution)

**Context.** The account is real ($10k Roth IRA) and execution currently happens manually in Thinkorswim. Automating order placement prematurely is the highest-risk path.

**Decision.** The system initializes in Manual Sandbox Mode. Live integration stays decoupled behind environment variables (`ALPACA_LIVE_MODE = false`). Alpaca is wired for *market data only* today; order *execution* is a future layer. Do not activate live execution until the manual version has run and a paper Alpaca integration has completed ≥5 full evening sessions.

**Consequences.** The manual position-entry layer must be replaceable by an API call without restructuring other layers. Market-data calls degrade gracefully when credentials are absent. The execution-integration path this ADR anticipated is now defined by ADR-0006/0007.

---

## ADR-0003 — Playbook snapshot immutability

**Status:** Accepted

**Context.** Playbooks evolve (versioned). If a position referenced a *mutable* playbook, editing the playbook later would silently rewrite the rules a past trade was taken under — data drift that destroys the post-mortem audit trail.

**Decision.** At entry, every `Position` stores a deep-copy `playbook_snapshot` of the exact ruleset active at execution. Closing a position freezes an immutable `ClosurePostMortem`.

**Consequences.** Historical evidence is trustworthy; diagnostics group by `(playbook_id, playbook_version)`. The cost is storage duplication and the need to treat snapshots as read-only.

---

## ADR-0004 — SQLite + FastAPI + Svelte 5 monorepo

**Status:** Accepted

**Context.** Single-user, single-machine, evening-cadence tool. Needs typed contracts and low operational overhead.

**Decision.** Monorepo: Python/FastAPI backend with async SQLAlchemy over SQLite; Svelte 5 + Tailwind v4 frontend. Pydantic drives the OpenAPI contract; the frontend regenerates TypeScript types from it. Pixi manages both toolchains.

**Consequences.** Zero external DB to operate; one source of truth for types (`sync-types`). SQLite caps concurrency, which is irrelevant for a single user. See [architecture.md](architecture.md).

---

## ADR-0005 — Session-lock gating

**Status:** Retired (2026-08-20, #315) — the lock guarded against a human staging trades before reviewing Layer A; under ADR-0006 the executor stages entries itself and enforces the sequencing rule in code (Layer A closes run before Layer C entries every night), so the UX gate protected nothing and cost a click. The console is now an ungated supervision surface.

**Context.** Position management must take absolute priority over hunting new trades; the spec forbids proceeding to Layer C while a P1 (CLOSE NOW) is unresolved.

**Decision.** The UI locks navigation to Opportunities/Performance/Settings until the user reviews Layer A and acknowledges. A manual re-lock control resets the gate each session.

**Consequences.** Enforces the sequencing rule at the UX level, not just the engine level. The cost is an extra click each session; the re-lock control's discoverability friction was addressed in 0.6.0 (tooltip + Enter-to-acknowledge).

---

## ADR-0006 — Autonomy roadmap: Operator → Executor (Paper) → Executor (Live)

**Status:** Accepted — supersedes [ADR-0002](#adr-0002--manual-sandbox-first-alpaca-behind-env-vars)

**Context.** The project's goal changed (2026-08-17 session): from evening decision-support with manual execution to autonomous trading of the IRA. ADR-0002's manual-only stance and its Alpaca-based integration path no longer describe the destination.

**Decision.** Autonomy advances through three strictly ordered levels (terms in [CONTEXT.md](../CONTEXT.md)): **Operator** (agent runs the evening pipeline on a schedule, human places orders), **Executor (Paper)** (agent places orders in a paper account, fully autonomous within the hard blocks), **Executor (Live)** (agent trades the real IRA, approval-per-trade at first). Promotion from Paper to Live requires the **Live Gate**: ≥30 closed paper trades AND ≥3 calendar months AND zero hard-block/gate breaches AND expectancy ≥ 0 after a slippage haircut (paper fills run optimistic). The **risk envelope**: $10,000 account basis, ≤50% capital deployed, ≤2.5% max loss per trade, ≤4 concurrent positions with at most 2 sharing strategy-type-and-expiry. Paper runs a **multi-book lab**: up to 10 virtual $10K books in one paper account, each an experiment arm (playbook mixes, regime-engine variants, SPY vs XSP); the Live Gate attaches to the specific book configuration going live. The **No-Stock Mandate** ([CONTEXT.md](../CONTEXT.md)) is enforced structurally where possible (cash-settled European-style underlyings, e.g. XSP) and by a same-day assignment-response rule as defense-in-depth. **Trading-mode isolation:** separate PAPER/LIVE database files; mode fixed at process start, stamped inside the DB, refused on mismatch; live DB backed up before any migration.

**Consequences.** The execution module is deterministic code — [ADR-0001](#adr-0001--rules-engine-not-llm) extends to order placement (no LLM in the order path). The UI evolves from evening workbench to supervision console (digest, book comparison, approval queue, kill switch). The `ALPACA_LIVE_MODE` prose flag is replaced by the real Trading Mode mechanism. product.md's "not an autonomous bot" non-goals are retired.

**Amendments.** (2026-08-19, #220/#133) Two experiment arms deliberately override the 2.5%/trade envelope as *documented confounds*: B13 ($5 wings) races at 4.5% and B21 (calendars) at 4.0% — without the raise those structures cannot enter at all, so the arm would measure nothing. Each override lives in the book's config, participates in its `config_hash`, and is judged against its own envelope; the baseline books' 2.5% is untouched. (2026-08-19, #222) `max_positions` runs at 8 per ADR-0009. (2026-08-20, #204) **Trading-mode isolation implemented**: `IBKR_TRADING_MODE` selects the mode (default `paper`); each mode has its own database file (`basis.db` / `basis.live.db` since #313; the legacy `options_playbook*.db` names are renamed automatically at startup), every database is stamped with the mode that created it (`db_meta`), a process in one mode refuses a database stamped with the other, the file is backed up before any schema migration, and the paper executor pipeline refuses outright to run in live mode. Decided the same day: **the paper lab keeps running alongside live** once live exists — paper evidence keeps accumulating and feeds the live strategy, which the isolation makes safe. Mechanically feasible because live and paper are different IBKR usernames (separate Gateway sessions on separate ports); the live build will add its own IBC config and schedule.

---

## ADR-0007 — Interactive Brokers for paper and live execution

**Status:** Accepted

**Context.** Requirements: paper trading must run on the same broker API that will trade live; defined-risk multi-leg spreads must be permitted in an IRA; no recurring fees. Research (2026-08-17): Alpaca retail IRAs cap options at Level 2 — spreads prohibited; Schwab's Trader API has no paper environment at all (paperMoney is platform-only); tastytrade's cert sandbox uses toy fills and resets daily.

**Decision.** Interactive Brokers for both paper and live. IRA Margin account type (permits defined-risk spreads under limited-margin rules); `ib_async` against IB Gateway; the linked paper account uses the identical API. Market data: IBKR's free 15-min-delayed OPRA option chains (adequate for evening scans; real-time OPRA optional at ~$1.50/mo if ever needed). Live path: after the Live Gate clears, partial ACATS transfer of the Schwab Roth IRA into the IBKR IRA (partial to avoid Schwab's $50 full-transfer fee).

**Consequences.** Alpaca's role shrinks to nothing (repo renamed `basis`); `market_data.py` is replaced or kept only as a fallback data source. The nightly scheduler must manage the IB Gateway process lifecycle. IBKR paper fills are slightly optimistic on spreads — the Live Gate's expectancy criterion applies a haircut rather than trusting raw paper results.

---

## ADR-0008 — Kill-switch semantics: latched halts, human-only flatten, asymmetric remote

**Status:** Accepted

**Context.** Executor (Paper) places orders with no human in the loop ([ADR-0006](#adr-0006--autonomy-roadmap-operator--executor-paper--executor-live)). It needs an operational stop mechanism distinct from the per-trade validity hard blocks — one that works when the bug is in the system itself, and that a phone can trigger from anywhere. The remote channel available without recurring cost (ntfy topic) is a bearer secret, not an authenticated channel.

**Decision.** Trading control is a persisted state (`GLOBAL` + per-book) with three values: ACTIVE, HALT_ENTRIES, FLATTEN_REQUESTED. (1) **Halts latch** — clearing requires the console, a typed reason, and an audit event; resume is never automatic and never remote. (2) **Automatic responses stop at HALT_ENTRIES** — flatten is human-initiated only, because a system that force-liquidates on a data glitch does more damage than one that stops and waits, and the nightly cadence plus defined-risk structure means positions are never unattended intraday. (3) **The remote channel is asymmetric**: the ntfy command topic accepts HALT only; RESUME over it is ignored and logged. A leaked topic can therefore only move the system toward safety. (4) **Fail-closed**: missing/unreadable/unrecognized control state is treated as HALT_ENTRIES, each default pinned by a failing test. (5) **Rolls count as entries under a halt** — a roll opens a new short position; a halted book takes the plain exit instead. (6) **Flatten uses a deterministic marketable-limit ladder** (mid, then thirds toward natural at 5-minute steps, natural at step four) — market orders on options stay banned. (7) Enforcement is a single synchronous `check_trading_control` read immediately before `placeOrder`, logged per submission.

**Consequences.** Full state tables and anomaly rules live in [supervision.md](supervision.md). The console is the only resume surface, so a broken console blocks resumption (accepted; the sentinel file covers the opposite failure). Executor (Live) will likely harden the remote channel with ntfy auth tokens or a self-hosted instance — supplementing the asymmetry, not replacing it.

**Amendment** (2026-08-20, #280/#281). As implemented, the escalation ladder runs on the **nightly cadence** — one rung per evening at +15% concession, capped at 5 rungs before escalating to a human (`CLOSE_LADDER_EXHAUSTED`, urgent) — not the intraday 5-minute ladder described in (6), which presupposed an attended flatten. FLATTEN_REQUESTED is now implemented on the same nightly ladder ([ADR-0011](#adr-0011--flatten_requested-closes-everything-in-scope-at-the-next-run)); an operator needing an immediate intraday flatten uses the broker directly.

---

## ADR-0009 — Accelerated experiment matrix

**Status:** Accepted

**Context.** The Live Gate (ADR-0006) requires ≥30 closed trades per book config. The original six-book lab (V0/V1/V2 × XSP/SPY) with `max_positions: 4` and ~45-DTE entries would take roughly 5–6 months to accumulate that sample. The operator wants gate eligibility in ~2–3 months, more underlyings, and answers to the strategy questions (wing width, short delta, profit-take level, exit DTE, gate value) *measured* rather than assumed — the $1M paper account makes parallel experiments free. Implementing the control books also exposed three latent defects: the regime→strategy matrix existed only as prose (nothing enforced it), the manual-portfolio concentration gates capped every book at 1–2 positions, and playbook `exit_rules` were decorative (thresholds were hardcoded, and the debit loss limit was missing entirely).

**Decision.** Widen the lab to a 22-book matrix where **every book asks exactly one question** against the shared baseline B01 (V0/XSP): B01–B06 core variant×underlying grid; B07/B08 short-DTE (24); B09 IWM, B10 GLD (multi-underlying); B11 condors-only; B12 no-regime-gate control; B13 $5 wings; B14 15Δ shorts; B15 25% profit take; B16 no-IVR control; B17 hold-to-7-DTE (XSP only — cash-settled, assignment-safe); B18 broken-wing butterfly; B19/B20 V3 variant; B21 calendars; B22 TLT. Mechanics: book configs gain `playbook_ids` (whitelist), `playbook_overrides` (dot-keyed field overrides, revalidated through the schema), `ignore_regime`, and `ignore_ivr` — all feed the book's `config_hash`, so every arm is fingerprinted (ADR-0003 pattern). `max_positions` rises 4 → 8 (8 × ~$250 max loss = 20% deployed, still far under the 50% cap — the old 4 bound trade-count, not risk). The regime matrix becomes an enforced hard gate; executor scans run `book_mode` (envelope gates are the concentration authority); exits come from the frozen playbook snapshot. **Selection discipline is unchanged:** an arm graduates only by clearing the full Live Gate on its own ≥30 trades, beating the SPY benchmark, and beating its same-engine baseline book. Gate standards never loosen — only the cadence accelerates.

**Consequences.** More books share one paper account, so cross-book netting and per-book virtual ledgers (book_gates.py) carry more load. Controls B12/B16 deliberately trade *without* protective gates — their losses are the measurement, bounded by the per-book envelope. Books whose question needs unbuilt machinery (B09/B10 multi-underlying telemetry, B18 BWB, B19/B20 V3, B21 calendars, B22 TLT after ex-div handling) seed with their enabling PRs, not before. The digest and console must stay legible at the full matrix. Sample-splitting is the accepted cost of parallelism: arms that answer their question early can be retired by halting the book — the ledger and gate events survive retirement.

**Amendment** (2026-08-19, #222/#254). The matrix grew to **28 books**: B23/B24 (20Δ/40Δ, credit-spreads-only) complete a 3-point short-delta sweep with B14 and baseline; B25 (52-DTE) completes the DTE sweep with B07/B08; B26 (75% profit take) completes the PT sweep with B15; B27 ($2 wings) completes the width sweep with B13 — every knob now reads for *monotonicity* at n≈30, which is legible long before pairwise significance. B28 races the regime-flip exit (`exit_on_regime_flip`, the exit-side question no entry gate can ask). B13 was also repaired the same day: at 2.5%/trade its $5 wings could never enter (a dead arm) — see the ADR-0006 amendment. (2026-08-20, #316) **B29** races the ensemble-consensus gate (`require_consensus: 3`): entries only when ≥3 of the raced engines V0–V3 read the same regime as the book's own — engine disagreement becomes abstention, asking whether sitting out mixed signals pays. (2026-08-20, #317) **B30** is the first single-name arm: the AAPL earnings-crush condor, gated on **underlying-scoped catalysts** (`EARNINGS:AAPL:date`, typed in quarterly by the operator). Scoped entries are invisible to market regime engines and to every other underlying's catalyst filters — one stock's earnings never blackouts the index books. RV-gated like GLD/TLT; $5 wings force a 4.5% per-trade envelope (documented confound, B13/B21 pattern); AAPL joins the ex-div assignment-defense calendar. 30 books.

---

## ADR-0010 — Live Gate promotion procedure: stress exposure and composition limits

**Status:** Accepted

**Context.** The Live Gate (ADR-0006) and the graduation rule (ADR-0009: clear the gate, beat the SPY benchmark, beat the same-engine baseline) are necessary but incomplete as a *promotion procedure*. Two gaps: (1) short-premium payoffs are skewed — a calm 3-month window contains no tail event, so gate-passing expectancy can be pure luck; the sample isn't finished until it includes stress. (2) The single-knob books (B13–B17) invite composing "winning" knobs onto the winner — a configuration assembled from the luckiest arm of each comparison that never itself ran, i.e. selection bias by construction. These rules are registered *before* the first fill exists (2026-08-19, hours ahead of the first armed run) precisely so they cannot be bent around a leaderboard. The 2026-11-03 midterm elections fall inside the first eligible window and may satisfy the stress condition naturally; if the quarter stays calm, the clock extends — that is the condition working, not a delay.

**Decision.** Promotion to Executor (Live) additionally requires:

1. **Stress episode observed.** Within the candidate book's gate window, at least one of (measured from `index_history` closes): a VIX close ≥ 25, or a SPY close-to-close drawdown ≥ 5% from the window's running peak — while the book held at least one open position. A gate window with no stress episode is an unfinished sample regardless of duration.
2. **Benchmark comparison is mechanical.** "Beats the SPY benchmark" (ADR-0009) means the book's realized-P&L return on its $10K basis over its gate window exceeds the SPY price return over the same window (`benchmark.py`, dividends excluded — a margin that flatters the book, accepted as conservative-against-promotion is impossible both ways and the haircut already leans against paper optimism).
3. **Composition limit.** The promoted configuration is a baseline book as-raced. At most ONE single-knob amendment may be grafted on, and only if that knob book's haircut expectancy exceeded its same-engine baseline's over the same window. Any further composition produces a new configuration that must return to paper for its own confirmation window (≥ 30 trades, stress episode included) before going live.
4. **The human decides; the checklist gates.** All conditions render on the console Live Gate checklist. A condition failing is a hard block on promotion; all conditions passing is necessary, never sufficient — the operator still signs off.

**Consequences.** The console checklist (backend/console.py) must grow rows for the stress-episode and benchmark conditions — until it does, this ADR is prose, and prose enforces nothing (tracked as a follow-up issue). Promotion may take longer than 3 calendar months; that is intended. Retired or losing arms stay in the ledger — negative results are results.

---

## ADR-0011 — FLATTEN_REQUESTED closes everything in scope at the next run

**Status:** Accepted

**Context.** ADR-0008 defined three kill-switch states, but FLATTEN_REQUESTED had zero implementation behind it — a defined panic button wired to nothing (audit finding, The Basis Audit 2026-08-19). The alternatives were to retire the state or implement it.

**Decision.** Implemented, not retired. When a scope (GLOBAL or a book) is in FLATTEN_REQUESTED, the nightly Layer A pass closes every OPEN position in that scope (`P1_FLATTEN`), bypassing the lifecycle scan's verdicts. It is a **limit-order flatten on the nightly cadence**, not a market order: the standard escalation ladder, stale-mark guard, ladder cap, and TP-cancel discipline all apply unchanged. Entries in the scope stay blocked (any non-ACTIVE state fails the choke point). Post-mortems record `MANUAL` — a human requested it. Resuming from FLATTEN_REQUESTED remains console-only with a typed reason.

**Consequences.** A flatten completes over one or more evenings depending on fills, with urgent-tier alerts if the ladder exhausts or marks go stale — an operator needing an *immediate* intraday flatten still uses the broker directly, and reconciliation will then see the closes as external. The remote ntfy channel still cannot request a flatten (HALT only) — a leaked topic must not be able to force liquidation timing.

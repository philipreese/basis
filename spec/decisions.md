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

**Decision.** Autonomy advances through three strictly ordered levels (terms in [CONTEXT.md](../CONTEXT.md)): **Operator** (agent runs the evening pipeline on a schedule, human places orders), **Executor (Paper)** (agent places orders in a paper account, fully autonomous within the hard blocks), **Executor (Live)** (agent trades the real IRA, approval-per-trade at first). Promotion from Paper to Live requires the **Live Gate**: ≥30 closed paper trades AND ≥3 calendar months AND zero hard-block/gate breaches AND expectancy ≥ 0 after a slippage haircut (paper fills run optimistic). The **risk envelope** — the **lab book envelope cap** (`Envelope.max_deployed_pct`, `backend/book_gates.py`; a DIFFERENT scope and number from [domain-rules.md](domain-rules.md#playbook-matching-layer-c)'s Layer C manual-portfolio-scan cap, #773): $10,000 account basis, ≤50% capital deployed, ≤2.5% max loss per trade, ≤4 concurrent positions with at most 2 sharing strategy-type-and-expiry (raised to 8 positions by [ADR-0009](#adr-0009--accelerated-experiment-matrix)). Paper runs a **multi-book lab**: up to 10 virtual $10K books in one paper account, each an experiment arm (playbook mixes, regime-engine variants, SPY vs XSP); the Live Gate attaches to the specific book configuration going live. The **No-Stock Mandate** ([CONTEXT.md](../CONTEXT.md)) is enforced structurally where possible (cash-settled European-style underlyings, e.g. XSP) and by a same-day assignment-response rule as defense-in-depth. **Trading-mode isolation:** separate PAPER/LIVE database files; mode fixed at process start, stamped inside the DB, refused on mismatch; live DB backed up before any migration.

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

**Amendment** (2026-08-19, #222/#254). The matrix grew to **28 books**: B23/B24 (20Δ/40Δ, credit-spreads-only) complete a 3-point short-delta sweep with B14 and baseline; B25 (52-DTE) completes the DTE sweep with B07/B08; B26 (75% profit take) completes the PT sweep with B15; B27 ($2 wings) completes the width sweep with B13 — every knob now reads for *monotonicity* at n≈30, which is legible long before pairwise significance. B28 races the regime-flip exit (`exit_on_regime_flip`, the exit-side question no entry gate can ask). B13 was also repaired the same day: at 2.5%/trade its $5 wings could never enter (a dead arm) — see the ADR-0006 amendment. (2026-08-20, #316) **B29** races the ensemble-consensus gate (`require_consensus: 3`): entries only when ≥3 of the raced engines V0–V3 read the same regime as the book's own — engine disagreement becomes abstention, asking whether sitting out mixed signals pays. (2026-08-20, #317) **B30** is the first single-name arm: the AAPL earnings-crush condor, gated on **underlying-scoped catalysts** (`EARNINGS:AAPL:date`, typed in quarterly by the operator). Scoped entries are invisible to market regime engines and to every other underlying's catalyst filters — one stock's earnings never blackouts the index books. RV-gated like GLD/TLT; $5 wings force a 4.5% per-trade envelope (documented confound, B13/B21 pattern); AAPL joins the ex-div assignment-defense calendar. (2026-08-20, #318) **B31** races the roll (`roll_time_exits`): a LOSING position leaving on the mandatory time exit gets a roll-out entry (same strikes, next cycle from its own frozen `target_dte`) staged alongside the close; winners just close, the chain caps at 2 rolls, and the roll entry runs the complete normal entry path (quotes, sanity, gates, encumbrance, profit-taker) so it can never sneak past a gate an ordinary entry would hit — if blocked, the arm degrades to a plain time exit, audited. Close and roll-entry rest as independent orders; lineage lives in the new position's journal (`rolled_from`). (2026-08-20, #319) **B32** is the tail-hedge sleeve — one far-OTM XSP `LONG_PUT` (new strategy type), rolled by the 30-DTE time exit + next-night re-entry; excluded from promotion and judged per **ADR-0012** (bleed rate + stress-episode payoff, never expectancy). 4% envelope confound; one position slot. 32 books.

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

**Amendment** (2026-08-22, #656, external review finding, operator-ratified). `expectancy_ok` was a point estimate against a hard 0.0 at n≈30: a true-zero-edge book passes roughly half the time on that alone, and with high-20s correlated promotion arms sharing underlyings and dates, several false greens can light together in a good quarter and read as consensus — multiplicity, not evidence. The bar is now **expectancy after the $5/contract haircut minus one standard error ≥ 0** (`expectancy_se` = stdev of per-trade haircut P&L, n-1 denominator, divided by √n; n<2 or an undefined SE is not passable). This scales the bar with the sample instead of holding it at a constant, and is surfaced on the checklist/leaderboard as an interval (`expectancy ± SE`).

The 1-SE multiplier is an **interim, admittedly arbitrary floor** — chosen because it scales with evidence rather than remaining a constant, not because 1 standard error is independently justified. It does **not** address multiplicity across correlated arms: passing this bar says a book's own sample clears one SE of its own noise, nothing about how many other correlated arms were tried. It is **superseded** by a threshold derived from the empirical-null drill's measured distribution (#657) once that exists — the final threshold must sit above the null distribution's max-per-book value and will be set in its own ADR amendment citing the measurement (run count, percentile). Until that amendment lands, expectancy − 1·SE ≥ 0 is the operative bar, not the final one. **Distributional annotation (2026-08-25, from the Deflated Sharpe Ratio / Bakshi–Kapadia reading):** the floor is a noise filter, not a skew correction — `mean − stdev/√n` is an exact-variance statistic under any skewness (unlike a Sharpe ratio, whose estimator variance inflates under negative skew per Bailey & López de Prado 2014), but the bar is a function of x̄ and s JOINTLY, and Cov(x̄, s²) = μ₃/n couples them: under left skew the two estimation errors move together in the permissive direction, inflating the bar's false-pass rate even in samples that DO contain tail events (Edgeworth: ≈20% vs a nominal 16% at skew −2, N=30). Separately, at N≈30–60 a sample that has not yet contained a near-max loss understates σ̂ and overstates the mean simultaneously (the peso problem). This ADR's **stress-episode condition** guards the peso-problem channel; it does not remove the skew-coupling one. The working mitigation for the latter is that the #657 drill computes the SAME mean−SE statistic on synthetic arms drawn from the real, left-skewed pooled P&L — a threshold derived from that measured distribution inherits the skew distortion empirically instead of assuming it away (caveat: if the pool itself contains no tail event, the null is understated by the same peso mechanism). If a skew-aware bound is ever wanted, the correct mechanism is a one-sided percentile bootstrap lower bound reusing the #657 drill's resampling machinery — NOT a bootstrap SE (which converges to the same σ/√n) — and adopting it requires moving `run_bootstrap`'s synthetic-arm metric to the same percentile definition, or the drill and the bar stop measuring the same quantity (value-provenance discipline, AGENTS.md).

---

## ADR-0011 — FLATTEN_REQUESTED closes everything in scope at the next run

**Status:** Accepted

**Context.** ADR-0008 defined three kill-switch states, but FLATTEN_REQUESTED had zero implementation behind it — a defined panic button wired to nothing (audit finding, The Basis Audit 2026-08-19). The alternatives were to retire the state or implement it.

**Decision.** Implemented, not retired. When a scope (GLOBAL or a book) is in FLATTEN_REQUESTED, the nightly Layer A pass closes every OPEN position in that scope (`P1_FLATTEN`), bypassing the lifecycle scan's verdicts. It is a **limit-order flatten on the nightly cadence**, not a market order: the standard escalation ladder, stale-mark guard, ladder cap, and TP-cancel discipline all apply unchanged. Entries in the scope stay blocked (any non-ACTIVE state fails the choke point). Post-mortems record `MANUAL` — a human requested it. Resuming from FLATTEN_REQUESTED remains console-only with a typed reason.

**Consequences.** A flatten completes over one or more evenings depending on fills, with urgent-tier alerts if the ladder exhausts or marks go stale — an operator needing an *immediate* intraday flatten still uses the broker directly, and reconciliation will then see the closes as external. The remote ntfy channel still cannot request a flatten (HALT only) — a leaked topic must not be able to force liquidation timing.

---

## ADR-0012 — Tail-hedge sleeve is judged on convexity, never expectancy

**Status:** Accepted

**Context.** A tail hedge (B32: one far-OTM XSP put, rolled monthly-ish) is EXPECTED to lose money most months — theta bleed is its premium, not its failure. Every evaluation instrument the lab has (Live Gate expectancy, win rate, the leaderboard) would call it broken by design, and promoting it alone to live money would be nonsense.

**Decision.** B32 is **excluded from promotion permanently** — the Live Gate never applies to it, and no checklist ever lists it as a candidate. Its metrics are: (1) **bleed rate** — average monthly cost as a % of the sleeve basis, from `book_mtm_history`; (2) **stress-episode payoff** — the sleeve's P&L during ADR-0010 stress episodes (VIX close ≥25 or ≥5% SPY drawdown), the only periods it exists for; (3) **portfolio contribution** — whether lab-total max drawdown improves with the sleeve included. Verdict horizon: no judgment before the first stress episode; a year of bleed with no episode is an *unpriced* insurance policy, not a failed one. The playbook deliberately has no vol/trend entry gates — insurance that only buys when vol is cheap lapses exactly when cover matters — and its stop-loss is set so only the time exit or the +400% profit-take (the put selling for 5× its cost) ever close it.

**Consequences.** The console's per-book Live Gate row for B32 reads permanently ineligible; that is correct, not a bug. Computing metric (2) mechanically depends on stress-episode detection (#215); until that lands, `book_mtm_history` preserves everything needed retroactively — nothing is lost by waiting. The sleeve costs real expected value ($10K basis, ~$250-390/roll) to buy information about crisis behavior; that tuition is the point (same reasoning as the lab itself).

**Amendment** (2026-08-20, #351). "Coverage is continuous by construction" was false as originally shipped: with `max_positions: 1`, the slot guard counts the resting 30-DTE close order against the cap, so the replacement put could not stage until the close *filled* — at least one uninsured session per monthly roll, a week on a laddering close, indefinite after `CLOSE_LADDER_EXHAUSTED`. The envelope is now **two slots**: the replacement stages the same night the close stages, and the brief overlap (an occasional one-night double bleed, ~$3-4 extra) is the premium paid for actually-continuous coverage. Bleed-rate arithmetic (metric 1) is unaffected in steady state — there is still one put except on roll nights.

**Amendment** (2026-08-20, #411). Two slots alone did NOT keep steady state at one put: the playbook is always-on, so the night after the first put filled a second lot passed every gate (`book_mode` skips concentration; `max_same_strategy_expiry` defaults to 2) and the sleeve settled at TWO lots — double bleed, and the roll-night slot full again, reproducing the original one-slot bug at n=2. The book now sets `dedup_playbook_entries: true`: a new same-playbook entry is allowed only when every open lot is already inside its exit window (DTE ≤ `mandatory_exit_dte`) — i.e. exactly the roll-night overlap the second slot exists for.

**Amendment** (2026-08-24, #772). The console previously rendered B32's row with the same win-rate/expectancy cells as every promotable book — negative-by-design bleed numbers that misread as failure, exactly the gap this ADR's "Consequences" section flagged. `console.book_summaries` now computes all three metrics for `_TAIL_HEDGE_BOOK_IDS` (currently just B32): bleed rate from `book_mtm_history`'s whole-history delta over elapsed months against the sleeve basis; stress-episode payoff using a minimal inline stress-episode detector (ADR-0010's own VIX≥25-or-≥5%-SPY-drawdown definition, read from `index_history`) — reusable once #215's fuller detection machinery lands, superseding this one rather than duplicating it; and portfolio contribution as the lab-wide max-drawdown delta with vs without the sleeve's marks. `BookSummarySchema.tail_hedge_metrics` carries these; the frontend (`BooksTab.svelte`) renders them in place of win-rate/expectancy for any book that has them. The Live Gate row's `eligible` flag is now explicitly forced `False` for `_TAIL_HEDGE_BOOK_IDS` regardless of the mechanical checks, so B32 cannot render eligible once #215 finishes the ADR-0010 pending conditions and other books start clearing them.

---

## ADR-0013 — seeds.py is the single source of truth for book configs; no out-of-band DB edits

**Status:** Accepted

**Context.** #436 made `database.init_db` converge any stored `BookModel.config` whose hash differs from `backend/seeds.py` back to the seed, on every process start — otherwise a seeds.py fix (e.g. #351's two-slot tail-hedge envelope) would silently never reach an existing database. That sync is unconditional: it has no way to tell "seeds.py changed, propagate it" apart from "an operator hand-edited `books.config` directly and meant it to stick." Audit II R3 (#482) found this makes any future direct DB edit a footgun — it works once, until the next process start reverts it, with only a `BOOK_CONFIG_SYNCED` audit row (previously a bare hash change) as the trace.

**Decision.** `seeds.py` is the **only** source of truth for book configs. Editing `books.config` directly in the database is prohibited and futile — the next process start reverts it. Any config change goes through a `seeds.py` PR, which the sync then propagates to every existing database automatically (the intended #436 behavior). No opt-out flag exists on purpose: a per-book "don't sync me" toggle would itself become a second, undocumented source of truth to keep straight against seeds.py.

**Consequences.** `BOOK_CONFIG_SYNCED` audit payloads now carry a key-level `diff` (`database._config_diff`) between the stored and seed config, so an unexpected revert is diagnosable from the audit row alone. A sync firing on a book that already has trade history triggers a best-effort ntfy alert (`database.init_db`) — a brand-new book has no history yet to distinguish "expected seed rollout" from "someone hand-edited a live book," so the alert is scoped to the case that actually matters. The B13 fix (seed updated in the same PR as the DB) is unaffected: hashes already match, so the sync never fires for it.

**Amendment (Audit II R4, #533/#534).** A seed sync starts a new **evidence era**, and evidence never crosses eras: orders stamp `config_hash` at stage time and positions inherit it at fill (#534); the Live Gate aggregates in `console.book_summaries` count only current-era trades, with the months clock restarting at the last `BOOK_CONFIG_SYNCED`; and `anomaly.check_envelope_breach` judges each position against the era that decided it — a seeds.py envelope reduction is a new era's rule, not a gate bypass by the old era's positions (#533), so it can never poison the zero-breaches criterion. Practical consequence for experiment design: editing a live book's config resets its Live Gate progress by construction — prefer a fresh book for a new configuration when the old era's evidence should keep accruing.

**Amendment (#760, resolving #737).** The unconditional-sync rule has a single carve-out: a book whose `live_authority` is LIVE is never synced — see ADR-0014's amendment for the full refuse/halt/alert handling of a live book's config-hash divergence.

---

## ADR-0014 — The demotion gate: automated live→paper revocation is pre-registered before any book goes live

**Status:** Accepted (pre-registration only — enforcement machinery is a separate, later issue)

**Context.** Weekend external panel review (Gemini/ChatGPT, operator-ratified, 2026-08-23/24). ADR-0010 built a ruthless *promotion* procedure — stress exposure, mechanical benchmark comparison, a composition limit against selection bias — but nothing symmetrical governs staying live. Every rule this project has that constrains a human's discretion under pressure (ADR-0008's kill-switch semantics, ADR-0010's promotion checklist itself) was written before the pressure existed. Demotion rules are the same category of rule, and they do not yet exist. Writing them while watching a live book bleed real money is a negotiation with the loss already in front of the writer, not a rule — this ADR is registered now, months before the Live Gate can even be cleared once (ADR-0006, ADR-0010), precisely so that timing is impossible.

**Decision.**

1. **Promotion and demotion are symmetrical authority, not a one-way ratchet.** In the domain model, a book *earns permission to advance* (ADR-0010's promotion procedure) and *continued permission has its own explicit, pre-stated conditions* — going live is not a terminal state that promotion alone can grant forever. The two sides need not share thresholds (a book may be promoted on evidence stronger than what would demote it, or vice versa) — "symmetrical" means both directions are equally rule-governed and equally pre-registered, not that the numbers match.

2. **Automated demotion triggers — candidates for the future enforcement issue, refined against existing envelope machinery rather than invented fresh:**
   - A rolling drawdown envelope breach measured at LIVE scale (not the paper $10K basis substitute ADR-0006/§Risk Envelope describes for paper — live capital is the real account).
   - Live expectancy − 1·SE < 0 over a defined rolling window (the same statistic ADR-0010's promotion bar already computes; demotion is its live-side, downward-facing mirror).
   - An envelope breach of any kind (mirrors the promotion gate's zero-breaches criterion — a breach that blocks promotion getting a pass once live would make the promotion criterion decorative).
   - Regime-telemetry breakdown (the regime engine itself failing/going stale — trading live blind is worse than trading paper blind).

   A trigger firing **automatically revokes live authority** — the book re-enters paper quarantine — and fires an **urgent** operator notification (`digest.py`'s `URGENT_EVENT_TYPES`, same tier as `RESTORE_GAP_UNKNOWN_HELD`/`ENVELOPE_BREACH_POSTHOC`). **No operator confirmation is required to demote** — this is the fail-closed direction, mirroring ADR-0008's kill-switch asymmetry (halting requires no permission; only resuming does). Re-promotion is the ADR-0010 procedure run again from scratch — an operator action, never automatic.

3. **The demotion policy is immutable once any book is live under it.** A policy change (a threshold amendment, a new trigger, a removed one) creates a new **policy version** that applies only to *subsequent* promotion grants — it never mutates the policy an already-live book is currently being judged against. This is deliberate insulation against the same failure ADR-0010's pre-registration exists to prevent, mirrored: a human tempted to loosen a demotion trigger while a live book is actively approaching it would be rewriting the rule around the outcome, exactly as forbidden as writing the rule after the fact. (An operator can still demote a book manually at any time regardless of policy version — this immutability governs the *automated policy*, never a human's authority to pull the plug sooner.)

4. **Every promotion grant records the policy version it was granted under.** Extends the #658 as-raced-config-hash provenance pattern (`domain-rules.md`'s `as_raced_config_hash`) to the demotion side: a live grant's provenance must be able to answer "which demotion policy governs this book" without ambiguity, the same way `as_raced_config_hash` answers "which config era earned this checklist." `BookModel.demotion_policy_version` (schema field reserved this issue, #713) is where that provenance lives once the promotion workflow exists to write it.

**Consequences.** This ADR is prose plus three reserved, unenforced schema fields (`BookModel.live_authority`, `demotion_policy_version`, `promoted_at`) — until the enforcement issue lands, it constrains nothing mechanically, exactly like ADR-0010 read for the ~3 days between its own registration and the console checklist rows that gave it teeth. That gap is accepted here for the same reason: no book can reach Live today (ADR-0006's autonomy roadmap has not reached Executor (Live)), so there is no live book for the absence of enforcement to endanger — but the RULE exists now, unnegotiable by construction, before the first candidate for it exists. The enforcement issue (detection machinery for each trigger, the automated revocation action itself, console surfacing of `live_authority`/policy version) is explicitly out of scope here and must be filed separately before any book is promoted.

**Amendment (operator-ratified, #760, resolving #737).** ADR-0013 and this ADR were written six ADRs apart and were never reconciled: ADR-0013's config-hash sync is unconditional, and nothing in it or in `database.init_db` special-cases a book whose `live_authority` is LIVE — a seeds.py PR aimed at an unrelated book, or even a genuine bug fix to the live book's own config, could silently change what a live book trades under a Live Gate grant issued for a different configuration, the moment the process next restarts.

A live grant attaches to the book's `as_raced_config_hash` (point 4 above). **Any** divergence from that hash while the book is live — a seeds.py sync, a hand edit, a migration side-effect, the mechanism doesn't matter — is a guarded event, handled uniformly:

1. The change is **refused** for the live book. ADR-0013's sync must not apply to a `live_authority=LIVE` book — that is the sync's one carve-out (see the corresponding note added to ADR-0013 below).
2. The book enters a **book-scoped entries-halt**.
3. An **urgent** operator alert fires, naming the divergence (the book, the promoted hash, the hash the config now resolves to).

Resolution is binary and explicit, an operator action either way — never automatic, never silent:

- **Revert** the seed/config change so the live book's hash matches its promoted `as_raced_config_hash` again, clearing the halt; or
- **Demote** the book (full ADR-0014 consequences — live authority revoked, re-promotion is the ADR-0010 procedure run again from scratch) and let the new config race from paper, as any new configuration must.

**Rationale.** An unconditional sync reaching a live book would let a routine config tweak silently reset rolling demotion evidence mid-grant — exactly the "rough patch" negotiation ADR-0014's own registration exists to foreclose, just wearing a code change instead of a moment of operator discretion under pressure. Refusing and halting is the fail-closed choice for the *new* exposure a diverged config would represent, without forcing a three-month re-promotion window onto what might be a completely innocent, unrelated edit — the operator's revert-or-demote choice is exactly that: a choice, made with full information, not a default. And this does not violate point 3's immutability rule: that rule forbids rewriting the demotion *policy* a live book is judged against; it does not, and was never meant to, forbid an operator's present-tense authority to revert a change or exercise a demotion they could already make manually at any time. Divergence-handling is a new guarded *event*, not a new *policy version*.

Enforcement (the hash-comparison check, the refusal path in `database.init_db`/`seeds.py`, the halt and alert wiring) lands with ADR-0014's enforcement issue, same as points 1–4 above — this amendment is the spec decision, not the implementation.

---

## ADR-0015 — Backtest direction rule: history can retire a book, never promote one

**Status:** Accepted (pre-registration only — no backtester exists yet; this ADR binds it whenever it arrives)

**Context.** Operator-ratified, weekend review. A backtester is genuinely wanted — cross-regime evidence that a 3-month paper window structurally cannot provide, since ADR-0010's own stress-episode condition exists to patch exactly that gap for the SINGLE window a book happens to run in. It is also the single most dangerous addition this system could make: every pre-registration ADR here (ADR-0010's promotion procedure, ADR-0014's demotion gate) is binding *because* the evidence that satisfies it arrives forward in time, at a pace the operator cannot influence or replay. A backtest inverts that — it is evidence computed instantly, against a fixed history, that CAN be rerun with a tweaked rule until it says what the operator wants. Registering the rules now, before a backtester exists to strain them, is the same discipline ADR-0010 applied to promotion itself: a constraint written before the temptation exists is a rule; the same constraint written after is a negotiation.

**Decision.**

1. **Direction rule.** A backtest can **retire** a book (or a playbook, or a knob) — disqualifying evidence is admissible immediately, no confirmation window required, since a backtest that shows a rule losing money is *at minimum* as credible as the same rule passing paper by chance. A backtest can **never promote** a book and can **never raise confidence** in one already accruing paper evidence — a good backtest result is not evidence of anything except that the backtest was tuned (deliberately or not) to produce it. History disqualifies; paper earns. This is not symmetry — it is the asymmetry ADR-0010's whole apparatus (stress exposure, composition limits, the empirical-null drill, #657) exists to protect, applied one layer earlier.

2. **Structural separation.** Backtest results live **entirely outside the evidence ledger**. They must never feed `console.book_summaries`, must never appear as a Live Gate checklist row or condition, and must never enter the "Why should I believe this?" verdict function's evidence set (#716) — that function is defined over the append-only ledger of REAL trades precisely so its answer can't be gamed by a favorable replay of the past. A backtest's disqualifying verdict (point 1) acts on a book/playbook/knob's *eligibility to keep running* — a control-plane action (retire, same category as an operator manually retiring a losing arm today) — never by writing a number into the evidence the ledger reports.

3. **Every run logged, unconditionally — the iteration count is the denominator.** Every backtest run, win or lose, promoted-nothing or retired-something, is logged: the exact config hash tested, the run date, and what changed since the prior run on the same subject. This is the number Bailey & López de Prado ("Pseudo-Mathematics and Financial Charlatanism," 2014) name as the one nobody reports — the count of configurations tried before the one that "worked" appeared, without which a single good backtest is unfalsifiable. A backtest result presented without its position in this log (how many prior variants were tried on the same subject before this one) is not evidence, full stop — same posture as #717's null-drill trial count, one layer earlier in the pipeline.

4. **Data honesty.** An options backtest requires historical bid/ask chains, not closes — the gap between a mid-priced fill and a realistic (bid-side sell, ask-side buy) fill is most of a short-premium strategy's apparent edge, and pricing off closes alone manufactures an edge that was never tradable. Any gap in that data filled by an assumption (an interpolated quote, a synthetic bid/ask spread, a missing strike) must be **declared in the run log**, not silently smoothed over — an undeclared assumption is exactly the kind of "it worked in the backtest" claim this ADR exists to make unfalsifiable-by-default rather than falsifiable-on-inspection.

**Consequences.** This ADR constrains a tool that does not exist yet — like ADR-0010 read before the console checklist rows existed, it is prose until a backtester's implementation issue lands, at which point building the run log, the bid/ask data pipeline, and the retirement action are that issue's scope, not this one's. No book, playbook, or knob can be retired by a backtest today because no backtest can run today; the rule exists so that when one can, "the backtest says it works" is structurally incapable of becoming a promotion argument, and "the backtest says it fails" is admissible on day one with no argument required. Building the tool without first reading this ADR is the failure mode being pre-empted.

---

## ADR-0016 — Books are independent at the accounting layer, contended at the broker order layer

**Status:** Accepted (2026-08-27, incident-driven — #853)

**Context.** The 34 books were designed as independent experiments: separate cash, separate envelopes, separate playbook arms. That independence is real at the accounting layer and was silently assumed to extend to execution. It does not: the books share one IBKR account, and IBKR applies order-level rules account-wide — "no open orders on both sides of the same US Option contract" and a riskless-combination cap among them. The 2026-08-27 run proved it: a dozen baseline books converged on the same bull put spread on the same strikes; after the first entry (and its GTC profit-taker child, an open order on the opposite side of those legs) was staged, IBKR refused every later identical candidate at preview, and the undifferentiated rejection count latched a false global halt.

**Decision.** The limitation is accepted, not engineered away (a per-book sub-account structure is not worth its operational cost at this scale). Consequences are managed instead: a pre-preview collision gate skips contested candidates with a typed audit event; nightly book processing order is randomized (logged seed) so contention is fair in expectation; preview-refusal reasons are classified so broker-structural refusals never masquerade as a broken-pipeline rejection stream. See `spec/domain-rules.md` → "Shared-account order contention".

**Consequences.** On convergence nights, only the first book (in that night's shuffled order) gets a contested spread; displaced books record `CROSS_BOOK_ORDER_COLLISION` events, which are the denominator for any future analysis of displacement cost. Any cross-book comparison of arms that can converge on the same strikes must account for execution contention — two books running "the same" playbook are not statistically independent samples on nights they contest legs.

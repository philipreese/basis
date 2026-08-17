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

**Consequences.** The manual position-entry layer must be replaceable by an API call without restructuring other layers. Market-data calls degrade gracefully when credentials are absent. See [roadmap.md](roadmap.md) for the execution-integration path.

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

**Status:** Accepted

**Context.** Position management must take absolute priority over hunting new trades; the spec forbids proceeding to Layer C while a P1 (CLOSE NOW) is unresolved.

**Decision.** The UI locks navigation to Opportunities/Performance/Settings until the user reviews Layer A and acknowledges. A manual re-lock control resets the gate each session.

**Consequences.** Enforces the sequencing rule at the UX level, not just the engine level. The cost is an extra click each session and some discoverability friction on the re-lock control — flagged in [ux-review.md](ux-review.md).

---

## ADR-0006 — Autonomy roadmap: Operator → Executor (Paper) → Executor (Live)

**Status:** Accepted — supersedes [ADR-0002](#adr-0002--manual-sandbox-first-alpaca-behind-env-vars)

**Context.** The project's goal changed (2026-08-17 session): from evening decision-support with manual execution to autonomous trading of the IRA. ADR-0002's manual-only stance and its Alpaca-based integration path no longer describe the destination.

**Decision.** Autonomy advances through three strictly ordered levels (terms in [CONTEXT.md](../CONTEXT.md)): **Operator** (agent runs the evening pipeline on a schedule, human places orders), **Executor (Paper)** (agent places orders in a paper account, fully autonomous within the hard blocks), **Executor (Live)** (agent trades the real IRA, approval-per-trade at first). Promotion from Paper to Live requires the **Live Gate**: ≥30 closed paper trades AND ≥3 calendar months AND zero hard-block/gate breaches AND expectancy ≥ 0 after a slippage haircut (paper fills run optimistic). The **risk envelope**: $10,000 account basis, ≤50% capital deployed, ≤2.5% max loss per trade, ≤4 concurrent positions with at most 2 sharing strategy-type-and-expiry. Paper runs a **multi-book lab**: up to 10 virtual $10K books in one paper account, each an experiment arm (playbook mixes, regime-engine variants, SPY vs XSP); the Live Gate attaches to the specific book configuration going live. The **No-Stock Mandate** ([CONTEXT.md](../CONTEXT.md)) is enforced structurally where possible (cash-settled European-style underlyings, e.g. XSP) and by a same-day assignment-response rule as defense-in-depth. **Trading-mode isolation:** separate PAPER/LIVE database files; mode fixed at process start, stamped inside the DB, refused on mismatch; live DB backed up before any migration.

**Consequences.** The execution module is deterministic code — [ADR-0001](#adr-0001--rules-engine-not-llm) extends to order placement (no LLM in the order path). The UI evolves from evening workbench to supervision console (digest, book comparison, approval queue, kill switch). The `ALPACA_LIVE_MODE` prose flag is replaced by the real Trading Mode mechanism. product.md's "not an autonomous bot" non-goals are retired.

---

## ADR-0007 — Interactive Brokers for paper and live execution

**Status:** Accepted

**Context.** Requirements: paper trading must run on the same broker API that will trade live; defined-risk multi-leg spreads must be permitted in an IRA; no recurring fees. Research (2026-08-17): Alpaca retail IRAs cap options at Level 2 — spreads prohibited; Schwab's Trader API has no paper environment at all (paperMoney is platform-only); tastytrade's cert sandbox uses toy fills and resets daily.

**Decision.** Interactive Brokers for both paper and live. IRA Margin account type (permits defined-risk spreads under limited-margin rules); `ib_async` against IB Gateway; the linked paper account uses the identical API. Market data: IBKR's free 15-min-delayed OPRA option chains (adequate for evening scans; real-time OPRA optional at ~$1.50/mo if ever needed). Live path: after the Live Gate clears, partial ACATS transfer of the Schwab Roth IRA into the IBKR IRA (partial to avoid Schwab's $50 full-transfer fee).

**Consequences.** Alpaca's role shrinks to nothing (repo renamed `basis`); `market_data.py` is replaced or kept only as a fallback data source. The nightly scheduler must manage the IB Gateway process lifecycle. IBKR paper fills are slightly optimistic on spreads — the Live Gate's expectancy criterion applies a haircut rather than trusting raw paper results.

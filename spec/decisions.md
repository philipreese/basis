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

**Status:** Accepted

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

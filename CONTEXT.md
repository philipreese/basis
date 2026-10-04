# Domain Glossary

Canonical terms for this project. Definitions only — no implementation detail. Deeper domain rules live in [`spec/domain-rules.md`](spec/domain-rules.md); decisions in [`spec/decisions.md`](spec/decisions.md).

## Autonomy Level

How much of the trading loop the agent performs. Three levels, strictly ordered; the project moves through them in sequence.

- **Operator** — The agent runs the evening pipeline (telemetry fetch, lifecycle scan, opportunity scan) on a schedule and delivers finished trade specs and a portfolio digest. Every order is placed manually by the human at their brokerage.
- **Executor (Paper)** — The agent places orders itself against a paper account. No real money at risk.
- **Executor (Live)** — The agent places orders against the real (IRA) account. Entered only after the paper track record justifies it.

## Trading Mode

Whether an action, position, or record belongs to the paper world or the real-money world: **PAPER** or **LIVE**. The two worlds must never share state — a record's trading mode is fixed at creation and paper and live data are stored separately. (The existing `execution_mode` field is a descriptive label today; Trading Mode is the term for the enforced concept.)

## Live Gate

The falsifiable criteria that must all hold before the project moves from Executor (Paper) to Executor (Live): at least 30 closed paper trades, at least 3 calendar months of paper operation, zero hard-block or gate breaches, and expectancy after slippage/commission assumptions minus one standard error at or above zero (a stricter, sample-scaled bar than the original bare-zero point estimate — [ADR-0010's amendment](spec/decisions.md#adr-0010--live-gate-promotion-procedure-stress-exposure-and-composition-limits), full definition in [domain-rules.md](spec/domain-rules.md#live-gate-metrics-console)). ADR-0010 adds promotion conditions on top: a **stress episode** (a VIX close ≥ 25 or a ≥ 5% SPY drawdown inside the book's gate window) on a day the book was **meaningfully deployed** — at least half its normal dollars at risk on the days it is deployed, counting only positions entered before that session, because a position merely held is not a position exposed — and beating the SPY price return over the same window on haircut-net realized P&L, both computed on the console; the same-engine-baseline and composition-limit conditions are still pending and keep eligibility un-claimable. "It feels ready" is not a criterion. Since 2026-10-03 the full gate is **permission to scale up**, judged on real-money results: before it, a book can go live in **stage 1**, a small capped stake with an automatic drawdown halt, on a lighter bar ([ADR-0006's staged amendment](spec/decisions.md#adr-0006--autonomy-roadmap-operator--executor-paper--executor-live)).

## Risk Envelope

The set of account-relative limits the agent may never exceed: maximum capital deployed, maximum loss per trade, maximum concurrent positions, maximum per-underlying exposure. Expressed as percentages of the account so they survive account growth. Paper trading runs against a pretend account sized to match the real one, regardless of what the paper broker grants by default.

## Stake

The real money a book trades in stage 1 of going live: capped at 10% of the account, an amount the operator has decided in advance they can lose entirely (ADR-0006). A staked book's Risk Envelope is measured against the stake, not the account, and a loss of 30% of the stake demotes it automatically (ADR-0014). Set per book as `stage1_stake`.

## No-Stock Mandate

The account never holds shares **by accident**. Options never leave shares behind: assignment risk on American-style options is either eliminated (cash-settled, European-style underlyings such as XSP) or neutralized the same day by the assignment-response rule, and a share position arising from an option is always an incident. Shares are held only **on purpose**, by a strategy designed to hold them, within that strategy's own risk limits; covered calls and cash-secured puts remain out of scope.

**Simplicity guard:** a deliberate-holdings strategy must stay simpler than the options machinery it sits beside (monthly-ish, unleveraged, a handful of orders). One that needs daily trading or leverage to work has broken the spirit of the rule.

(Reworded 2026-10-03, #1055: the mandate exists to prevent accidental assignment and to keep the system simple, not to forbid a deliberate ETF holding. Since #1061, reconciliation tells the two apart by quantity: a book whose config designates a symbol (`share_symbols`) records its holding, and the broker's share count per symbol must equal the designated books' recorded total. Any other share, or any shortfall, is still an incident. No book is designated yet, so today **every** share position is still an incident.)

## Brokerage (of record)

Where the human's real money lives. Currently **Charles Schwab** (Roth IRA). Decided: **Interactive Brokers** is the execution broker for both paper and live (ADR-0007); the Schwab IRA transfers to an IBKR IRA-Margin account when stage 1 of going live begins (ADR-0006). The spec's older references to Thinkorswim describe the same Schwab account's trading platform.

## Book

One of a set of virtual $10,000 paper-trading envelopes living inside the single IBKR paper account. Each book is an independent experiment arm — its own playbook mix, risk-envelope settings, regime-engine variant, or underlying (e.g. SPY vs XSP) — with positions attributed per book in our database, not at the broker. Books exist to race configurations; the Live Gate attaches to one specific book's configuration, and the others are exploration. Current book count and matrix: `backend/seeds.py` is the single source of truth ([ADR-0013](spec/decisions.md#adr-0013--seedspy-is-the-single-source-of-truth-for-book-configs-no-out-of-band-db-edits)); the matrix's design rationale is [ADR-0009](spec/decisions.md#adr-0009--accelerated-experiment-matrix) and its amendments.

## Live Authority

Whether a book currently holds permission to trade real money: **PAPER** (the default — every book today), **LIVE** (permission granted by the Live Gate promotion procedure, ADR-0010), or **REVOKED** (permission automatically withdrawn by the demotion gate, ADR-0014). Symmetrical with promotion by design — earning Live Authority and keeping it are both governed by explicit, pre-stated rules, never by discretion in the moment. Losing it never requires operator confirmation (fail-closed, same asymmetry as the kill-switch, ADR-0008); regaining it always does, via the full promotion procedure run again.

## Policy Version

The identifier a book's Live Authority grant is stamped with, naming which version of the demotion policy (ADR-0014) governs whether that grant can be automatically revoked. Immutable per grant once live: amending the demotion policy creates a new Policy Version that binds only *future* promotions, never rewrites the policy an already-live book is being judged against. Extends the same as-raced-provenance pattern `as_raced_config_hash` already applies to a book's config era.

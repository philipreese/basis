# Executor (Paper) — Design

> Part of the [modular specification](../README.md). This document specifies the Executor (Paper) build: the system that places orders autonomously against the IBKR paper account, per [ADR-0006](../decisions.md#adr-0006--autonomy-roadmap-operator--executor-paper--executor-live) and [ADR-0007](../decisions.md#adr-0007--interactive-brokers-for-paper-and-live-execution). Synthesized 2026-08-17 from four research lanes (execution, regime, books, supervision), including adversarial verification; claims that failed or weakened under verification are flagged inline.

---

## 1. Overview & scope

### What Executor (Paper) builds

Executor (Paper) advances the system from Operator (agent proposes, human places orders) to autonomous order placement in the IBKR paper account, within the existing hard blocks and risk envelope. Concretely:

1. **A broker adapter** (`backend/broker.py`) that constructs and places multi-leg spread orders as native IBKR combos, attaches GTC profit-target orders, waits for fills, and reconciles state across crashes — with idempotency guarantees the current codebase has no need for and therefore lacks.
2. **Gateway lifecycle automation**: the nightly run starts IB Gateway on demand via IBC, does its work, and shuts it down. No 24/7 session.
3. **The multi-book lab**: up to 10 virtual $10K books inside the one paper account ([CONTEXT.md](../../CONTEXT.md) → Book), with client-side attribution, per-book risk gates, nightly broker-vs-DB reconciliation, and append-only audit records sufficient to make the Live Gate criteria computable queries rather than claims.
4. **Regime-engine variants to race**: the current scoring matrix as control (V0) plus two evidence-first challengers (V1 term-structure, V2 VRP-conditioned) and an optional repaired-matrix arm (V3), each fully specified below.
5. **Safety layer**: a latched kill switch (global and per-book), deterministic anomaly auto-halts, fail-closed defaults, an evolved nightly digest, immediate-push events, and a dead-man's watchdog.
6. **Console evolution**: status strip, Books comparison tab, and audit view — additive to the existing 4-tab Svelte app (`frontend/src/App.svelte`).

### What it defers

- **Executor (Live)** entirely: approval-per-trade queue UI, ACATS transfer, live-mode hardening of the remote command channel.
- **Threshold tuning within a variant** (e.g. racing VRP cutoffs): second-generation experiment after the variant-level race resolves; 10 books is not enough arms for both.
- **Intraday anything.** The system remains nightly-batch. Positions are protected between runs by structure (defined-risk spreads) and server-side resting GTC profit targets — not by intraday monitoring.
- **Fill-quality conclusions from paper.** IBKR paper combo fills are optimistic ([ADR-0007](../decisions.md#adr-0007--interactive-brokers-for-paper-and-live-execution)); the XSP-vs-SPY race design must not read paper fill quality as evidence. The Live Gate's slippage haircut absorbs this.

### Design invariants carried forward

- Deterministic rules engine, no LLM anywhere in the order path (ADR-0001, extended by ADR-0006).
- Layer ordering: reconciliation → Layer A (position management) → Layer C (opportunity hunting). Reconciliation is position management and gates everything downstream (ADR-0005 pattern).
- Trading-mode isolation: separate PAPER DB file, mode stamped in the DB, refused on mismatch (ADR-0006).
- No-Stock Mandate: any stock position at the broker is a P1 incident, never a strategy (CONTEXT.md).

---

## 2. Broker adapter

### 2.1 Interface

New module `backend/broker.py`. A **session-scoped** adapter — one connected `ib_async` session per nightly run — not the connect-per-call pattern of `backend/market_data.py` (`_run_ib`), because order placement, fill waiting, and reconciliation need a persistent `Trade` object stream. It reuses `_gateway_config` and `parse_occ_symbol` from `market_data.py` (which already qualifies Option contracts via `qualifyContractsAsync` at lines 225–257).

```python
class BrokerSession:  # context manager; sync facade over a dedicated ib_async thread
    def open(self) -> None            # connect (port 4002, IBKR_CLIENT_ID=17), reqMarketDataType(3), verify managed account
    def close(self) -> None

    def reconcile(self, refs: list[str]) -> ReconcileReport
        # reqAllOpenOrdersAsync + reqCompletedOrdersAsync(apiOnly=True)
        # + reqExecutionsAsync(ExecutionFilter(time=last_run_ts)), matched by orderRef.
        # Returns per-ref state {UNKNOWN, OPEN, FILLED, CANCELLED}.
        # MUST be called before any place_* method (enforced with an internal flag).

    def preview_spread(self, spread: SpreadOrder) -> MarginPreview
        # whatIfOrder on the BAG: init_margin_change, maint_margin_change,
        # commission_min/max (nullable — DBL_MAX sentinel means unavailable), warning_text.

    def place_spread(self, spread: SpreadOrder, ref: str,
                     profit_target_price: float | None) -> PlacedOrder
    def close_spread(self, position_legs, ref: str, limit_price: float,
                     urgency: Urgency) -> PlacedOrder
    def wait_for_terminal(self, order_id: int, timeout_s: float) -> FillResult
    def cancel(self, order_id: int) -> None
    def positions(self) -> list[LegPosition]      # aggregate consistency check only
    def open_orders(self) -> list[OpenOrderInfo]
```

`SpreadOrder` is a frozen dataclass: `legs: [(occ_symbol, action, ratio)]`, `quantity` (combo units), `net_limit_price` (signed; negative = net credit under the BUY-action convention), `underlying`, `exchange`.

**Error policy diverges from `market_data.py` on purpose**: the data layer degrades to `None`/`{}` on failure; the order path never does. `place_spread` and friends raise typed `BrokerError` subclasses. A silent degradation in the order path is how phantom or duplicate orders happen.

### 2.2 Order construction

Multi-leg spreads are ONE order: a `Contract(secType='BAG', symbol=<underlying>, currency='USD', exchange='SMART')` with `ComboLeg(conId, ratio=1, action, exchange='SMART')` children, submitted as a single `LimitOrder` at the net price. Qualify each Option leg first to obtain conIds. This works identically for 2-leg verticals and 4-leg condors ([IBKR spread docs](https://interactivebrokers.github.io/tws-api/spread_contracts.html); [IBKR Python complex-orders lesson](https://www.interactivebrokers.com/campus/trading-lessons/python-complex-orders/)).

Load-bearing facts, with verification status:

- **XSP combos**: BAG symbol = `'XSP'`, SMART-routed. Smart-routed US option-vs-option combos are **guaranteed** (execute as a unit, no legging risk) per IBKR KB 1323 — confirmed under adversarial check. Note the guarantee attaches to *Smart-routed US option-vs-option* combos generally, not to "single underlying" per se; XSP spreads qualify either way. The alphabetical-symbol gotcha ([ib_async #119](https://github.com/ib-api-reloaded/ib_async/discussions/119)) applies only to stock/stock combos.
- **Order types on BAG**: plain `LimitOrder` only — entry `tif='DAY'`, profit-target child `tif='GTC'`. Algo types (Adaptive, MidPrice) on combos: MidPrice is documented as US-stock-only; **Adaptive-on-BAG unsupported is inferred, not documented verbatim** — the smoke test (§2.6) verifies it, but the design doesn't depend on it since we use plain limits regardless.
- **Profit target attachment**: entry `LimitOrder(transmit=False)`, child `LimitOrder(parentId=entry.orderId, tif='GTC', transmit=True)`. IB links them; the child sits at IB in **PreSubmitted** state and is released on parent fill (visible and cancelable before activation — a precision note from verification, not a behavior change). GTC orders rest **server-side** and keep working while Gateway is down ([IBKR order submission docs](https://interactivebrokers.github.io/tws-api/order_submission.html)) — this is what makes the start-on-demand lifecycle (§3) safe. Caveat for the spec: GTC targets execute only during regular options trading hours.
- **No resting stops on combos.** Combo stops are simulated orders triggering off synthetic combo quotes, which for option spreads are wide, jumpy, and often crossed; IB's own disclosures warn simulated orders may misfire. The loss exit is instead a deterministic rule in the nightly Layer A pass: `close_spread` with a marketable limit when the loss threshold is breached. Defined-risk structure already caps true max loss, so an intraday stop buys little for an evening system. (Verified; the two originally cited forum threads were unreachable, but IB primary docs and independent practitioner guidance corroborate.)
- **Margin preview**: `whatIfOrder` works on BAGs in paper and returns `initMarginChange`/`maintMarginChange` plus commission min/max. Treat it as a pre-trade sanity gate (margin change ≈ width − credit for a defined-risk spread), **not** an accounting source: results can be wrong when `warningText` is non-empty ([ib_insync #380](https://github.com/erdewit/ib_insync/issues/380)), and commission fields may be DBL_MAX sentinels. The adapter rejects the preview if `warning_text` is non-empty.

### 2.3 Fills

`placeOrder` returns a live-updating `Trade`. Status walks PendingSubmit → PreSubmitted → Submitted → Filled/Cancelled; `trade.fills` accumulates `Fill(execution, commissionReport)` entries. Combos report **per-leg executions** plus combined status on the BAG order. Reconcile at the **BAG `orderStatus` level** (`filled`/`remaining` in combo units) — never by counting leg executions. Partial fills happen in whole combo units on guaranteed combos (legs stay balanced). Capture fills via `trade.fillEvent` handlers **plus** an end-of-run `ib.fills()` sweep, deduplicated on `execId` — because `reqExecutions` only returns current-day executions ([IBKR executions docs](https://interactivebrokers.github.io/tws-api/executions_commissions.html)); a fill not written to the DB the night it happens is only recoverable via Flex Query (§4.5).

### 2.4 Idempotency

TWS API has no server-side deduplicated idempotency key. The contract is therefore:

1. **Intent row before placeOrder.** Write an `orders` row (status `STAGED` → `PENDING_SUBMIT`) with a generated `order_ref` *before* calling `placeOrder`.
2. **`order.orderRef` = that ref**, on every order. Format: `basis:{book_id}:{order_id}:{action}` (e.g. `basis:B03:o_7f3a:open`; profit-taker child gets `:tp` suffix). orderRef round-trips on `openOrder` and `execDetails` and survives into Flex exports — it is the only per-order book discriminator visible at the broker.
3. **Persist `permId` immediately on submit ack.** `permId` (not `orderId`) is the durable cross-session key; `orderId` is per-clientId-session.
4. **Reconcile-by-orderRef is the first act of every run**, before any submission: `reqAllOpenOrders` + `reqCompletedOrders(apiOnly=True)` + `reqExecutions(ExecutionFilter(time=last_run_ts))`. If a ref already exists at the broker as open/filled, mark the intent accordingly and do NOT resubmit. A crash between DB-write and submit-ack resolves here: ref absent at IB ⇒ safe to resubmit or expire the intent per policy.
5. **clientId discipline**: fixed `IBKR_CLIENT_ID=17` for the executor. Orders from a previous session of the same clientId are not auto-repopulated into `openTrades()` ([ib_insync #416](https://github.com/erdewit/ib_insync/issues/416)) — hence the explicit reconcile calls. clientId 0 (master) only if reconciliation needs cross-client visibility.

**Required test** (per the fail-that-runs rule): simulate crash-after-placeOrder and assert no second submission on the next run.

### 2.5 Unfilled entries

Entry orders never rest overnight: any entry still working at session end is cancelled (anomaly rule UNFILLED_ENTRY, §6.3). GTC belongs to closing orders only.

### 2.6 Smoke test (prerequisite to trusting any of this)

A one-off paper script converting the medium-confidence claims to ground truth:

- (a) place a 2-leg XSP vertical BAG at a non-marketable price → confirm Submitted → cancel;
- (b) place a marketable one with attached GTC profit-taker → confirm per-leg executions + combo orderStatus → confirm child releases on fill;
- (c) `whatIfOrder` the same BAG → check margin change ≈ width − credit, record whether commission fields are populated or sentinels;
- (d) kill the process mid-run → confirm reconcile finds the order by orderRef (and specifically whether `reqCompletedOrders` returns prior-session fills, or whether reconciliation must lean on `reqExecutions` with a time filter);
- (e) confirm orderRef is echoed on **every leg execution** of a combo fill;
- (f) confirm XSP combo net-price tick increments (does IB reject $0.01 net prices?).

---

## 3. Gateway lifecycle & operational reliability

### 3.1 Start-on-demand, not 24/7

The nightly job launches Gateway, uses it, and shuts it down:

1. Scheduled Task fires (`scripts/register-executor-task.ps1`; the standalone operator task it grew from was retired once the executor pipeline subsumed it).
2. IBC (`IbcAlpha`) `StartGateway.bat` with `config.ini`: `IbLoginId`/`IbPassword`/`TradingMode=paper` ([IBC userguide](https://github.com/IbcAlpha/IBC/blob/master/userguide.md)). Paper-only usernames have no 2FA, so login is fully scriptable (verify once against this specific account — see §8).
3. Poll port 4002 until the API handshake succeeds (bounded retries, then abort with an executor-failure push).
4. Run the pipeline (reconcile → Layer A → Layer C → digest).
5. IBC stop script.

This sidesteps the whole always-on failure surface: the weekly forced re-login, the built-in Auto-Restart token bug on paper accounts ([IBC #345](https://github.com/ibcalpha/ibc/issues/345)), and session drift. Keep Gateway's built-in Auto-Restart **off** in this model. Resting GTC profit targets are held at IB's servers and keep working while Gateway is down — `backend/market_data.py` line 16 already anticipated exactly this design ("process lifecycle management arrives with the Executor build").

### 3.2 Error 162 / competing sessions

A paper session shares market-data entitlements with its associated live username. If that username logs in anywhere else — IBKR Mobile, Client Portal quotes, TWS — the API paper session's data farms disconnect and requests fail with error 162, sometimes silently ([IBKR error handling](https://interactivebrokers.github.io/tws-api/error_handling.html); [QuantRocket catalogue of 162 variants](https://support.quantrocket.com/t/error-code-162-tws-session-is-connected-from-a-different-ip-address/595)). The repo's `reqMarketDataType(3)` (delayed, `market_data.py` line 34) avoids *subscription* 162s but not *competing-session* ones.

Mitigations, in order of value:

1. **Dedicated second paper username for the bot** (IBKR allows additional paper users), so human logins never collide with the bot's session.
2. Never log the bot's username in elsewhere during the run window (moot if #1 is done).
3. In-run policy: 162 on the **data** path is retry-once-then-fail-soft to stored data (consistent with `market_data.py`'s existing degradation and the digest's existing "⚠ Live telemetry unavailable" flag); 162 on the **order** path aborts the submission phase — never fail-soft where orders are concerned.

### 3.3 Refuse-to-start preconditions

Executor preconditions, all pre-existing mechanisms restated as startup checks: trading-mode stamp mismatch (ADR-0006); pending un-run migration without backup; portfolio config missing (`operator.py` already raises). Plus a calendar guard: on market holidays the executor writes its heartbeat and exits without trading (silent non-operation is only acceptable when announced by the heartbeat).

---

## 4. Multi-book architecture

### 4.1 Principles

- **The DB is truth for attribution; the broker is truth for quantities.** Books do not exist at IBKR ([CONTEXT.md](../../CONTEXT.md) → Book). There is no broker-side sub-account mechanism available (Model Portfolios/partitions are FA-account features; clientId-per-book is strictly worse). `book_id` is assigned in the DB **before** placement; `orderRef` is the redundant broker-side echo used by reconciliation and Flex audits to independently verify attribution — never the primary partition.
- **Per-book P&L comes exclusively from attributed fills** (price, qty, commission per `execId`), never from broker P&L fields — the broker nets per conId and knows nothing about books.
- **Shared-capital isolation is arithmetic.** Every gate in [domain-rules.md](../domain-rules.md) (≤2.5% max loss/trade, ≤50% deployed, ≤4 positions) evaluates against the book's virtual ledger (starting 10,000, adjusted by attributed fills + commissions). The paper account's $1M never enters gate math — this is the Risk Envelope's "pretend account" rule, ten times over. The only account-level checks are a sanity margin ceiling that should never bind, and the No-Stock scan, which is account-global by definition.

### 4.2 Schema additions

The existing schema has no book concept and no order/fill chain at all — `PositionModel` (`backend/models.py`) records already-filled positions. New tables:

```
books               (id PK 'B01'..'B10', name, config JSON, config_version INT,
                     config_hash TEXT, starting_capital REAL, cash_balance REAL,
                     status, created_at)
orders              (id TEXT PK, book_id FK, position_id FK nullable,
                     order_ref TEXT UNIQUE, ib_order_id INT, ib_perm_id INT,
                     action OPEN|CLOSE|ROLL, combo_legs JSON, order_type,
                     limit_price, decision_midpoint,
                     status STAGED|SUBMITTED|PARTIAL|FILLED|CANCELLED|REJECTED,
                     submitted_at, completed_at)
fills               (exec_id TEXT PK, order_id FK, book_id, con_id INT, side,
                     quantity, price, commission, fill_time, raw JSON)   -- append-only
reconciliation_runs (id, run_at, broker_snapshot JSON, books_expected JSON,
                     result CLEAN|DRIFT, drift_details JSON, resolved_at, resolution)
gate_events         (id, book_id, run_at, gate, result PASS|BLOCK, context JSON)  -- append-only
audit_events        (id, run_at, book_id nullable, event_type, actor, payload JSON) -- append-only
trading_control     (scope PK: 'GLOBAL' | book_id, state ACTIVE|HALT_ENTRIES|FLATTEN_REQUESTED,
                     reason, actor, changed_at)
regime_readings     (date, book_id, engine_variant, regime, inputs JSON, scores JSON,
                     PK (date, book_id, engine_variant))
index_history       (date, symbol, close, PK (date, symbol))   -- VIX, VIX3M closes
ALTER positions ADD book_id TEXT NOT NULL REFERENCES books(id)  -- + index
```

Notes: `exec_id` as fills PK naturally dedupes IBKR's execution-correction semantics (corrections arrive as new suffixed execIds). `fills`, `gate_events`, and `audit_events` are insert-only at the ORM layer — no UPDATE/DELETE path, enforced with a test, because they are the Live Gate's "zero breaches" and "expectancy after slippage" evidence (`decision_midpoint` vs fill price per order cannot be reconstructed later from any IBKR source). Book configs are versioned and hashed (edit ⇒ new `config_version` + hash); the Live Gate attaches to a `(book_id, config_hash)` pair — the multi-book extension of ADR-0003's snapshot immutability.

### 4.3 Per-book gates and the placement flow

Per-book flow, transactional: per-book opportunity scan with that book's config → candidate → per-book gates against the virtual ledger → **reserve capital** (pending-order encumbrance of max-loss/collateral, released on cancel/reject) → stage `orders` row with generated orderRef → `check_trading_control(book_id)` (§6.1) → place via adapter → fill handler writes `fills` keyed by execId → position row with book_id → book `cash_balance` adjusted from actual fill + commission. The encumbrance step exists because without it two same-evening candidates in one book could both pass the deployed-capital gate.

**Cross-book netting gate**: hard-block opposite-direction exposure on the same conId across books. The broker nets per conId, so opposite-direction sharing nets to flat at the broker — making broker-side state ambiguous and exercise/expiry unattributable. Same-direction sharing is fine (reconciliation sums leg quantities across books before comparing). To avoid spurious blocks when books collide on the same contract, the pipeline places closes before opens. Exposure is not just OPEN positions (#665): a STAGED/SUBMITTED/PARTIAL OPEN order — the same run's earlier book, or a resting order from a prior night not yet filled/synced — is real committed exposure the broker will hold once it fills, and blocks an opposite-direction candidate exactly like a position would. In-flight CLOSE orders are deliberately excluded from the aggregation: their `combo_legs` mirror the position being closed (SELL-the-bag reverses the order's execution side, never the stored LONG/SHORT direction field), so their exposure is already counted via the OPEN position itself — counting the close order too would be redundant, and would misread "closing a LONG" as new SHORT exposure if that convention ever inverted.

Every gate evaluation — pass or block — is written to `gate_events`, making the Live Gate's "zero breaches" a table scan.

### 4.4 Reconciliation (first step of every run, ahead of Layer A)

1. Snapshot broker state: `ib.positions()`, `ib.fills()` (today), `reqAllOpenOrders`.
2. Ingest any fills whose orderRef parses but are missing from `fills` (missed-event backfill, dedupe on execId).
3. Compare broker net per conId vs sum-over-books of open-position leg quantities.
4. Classify drift:
   - **ORPHAN** — untagged broker position (manual order, assignment). Any STK conId is simultaneously a No-Stock P1.
   - **EXTERNAL_CLOSE** — book says open, broker flat (expiry/exercise/manual close). Needs human resolution.
   - **PARTIAL_DRIFT** — quantity mismatch.
5. Any non-clean result sets global `reconciled=false`, which hard-blocks **all new-entry orders across all books** until a human resolves it — the multi-book analogue of the unresolved-P1 block. **Never auto-adjust book ledgers to match the broker**: silent adjustment corrupts the Live Gate evidence. Flag and halt.

### 4.5 Weekly Flex audit

`reqExecutions` is current-day-only, so the nightly ledger must be captured incrementally; a weekly Activity Flex Query (includes orderRef and commissions over arbitrary date ranges) audits the incremental ledger end-to-end. Verify once that the paper account's Flex exports carry orderRef identically to live (expected yes).

---

## 5. Regime-engine variants to race

Background from the regime lane, all claims surviving adversarial check: the VIX term-structure slope (VIX/VIX3M) is the best-evidenced cheap timing signal for short premium ([Simon & Campasano 2014](https://www.quantseeker.com/p/timing-volatility-with-the-vix-term); Johnson 2017, JFQA — slope predicts variance-asset returns *to the exclusion of the rest of the term structure*, i.e. subsuming the level); the realized-vs-implied spread (VRP) is second ([Carr & Wu 2009](https://quantpedia.com/strategies/volatility-risk-premium-effect)); absolute VIX level alone is ambiguous — it cannot distinguish rich-premium decay from dangerous fresh-spike tape (verification nuance: the level is not zero-information, which argues for *conditioning* it, not deleting it — V3 does exactly that); trend filters have evidence at the 200-day horizon, not the 20-day horizon the current matrix uses ([Faber 2007](https://mebfaber.com/wp-content/uploads/2016/05/SSRN-id962461.pdf)); and the current matrix's SMA20 + daily-return dimensions double-count short-term price noise while the IVR dimension's weight silently scales with watchlist size (`backend/regime.py:233` per-underlying loop — a multiplier absent from the spec's matrix table).

All variants emit the existing four regimes (CALM_BULL / HIGH_VOL_NEUTRAL / TRENDING_BEAR / EVENT_CATALYST) so the playbook-matching layer is untouched. Under V1/V2, EVENT_CATALYST effectively means **Do Nothing** (the long-straddle menu entry ships disabled); `domain-rules.md`'s regime→menu table must say so explicitly for those variants.

### V0 — Control: current matrix

`backend/regime.py` exactly as-is: 5-dimension additive integer scoring, tie-break hierarchy, silent IVR=25.0 fallback (confirmed at lines 229–231; it biases toward CALM_BULL — that bias is part of what's being raced, so it stays in the control unchanged). Inputs: SPY close, SPY SMA20, VIX close, per-underlying IVR dict, SPY daily return, catalyst list. The weights are hand-tuned priors with no evidence base (confirmed: no citation or calibration anywhere in spec/), which is exactly why V0 races as control rather than being assumed correct.

### V1 — Term-structure two-gate

Inputs: VIX close, VIX3M close, SPY close, SPY SMA200, catalyst list. Compute `R = VIX/VIX3M`, `T = SPY_close > SMA200`.

| Condition | Regime | Menu note |
|---|---|---|
| `R >= 1.00` (backwardation) OR MAJOR catalyst (FOMC/CPI) within **3 trading days** | EVENT_CATALYST | No new short premium (Do Nothing) |
| `R < 1.00` and `T` false | TRENDING_BEAR | Bear call spreads or Do Nothing |
| `0.95 <= R < 1.00` and `T` true | HIGH_VOL_NEUTRAL | Condors / delta-neutral, reduced size |
| `R < 0.95` and `T` true | CALM_BULL | Bull put spreads primary |

Hysteresis: once EVENT_CATALYST fires on backwardation, require `R < 0.97` for two consecutive closes before downgrading — sell the relief, not the panic. Every input is one delayed index quote or one SPY bar series; ~10 lines of arithmetic.

The 3-day catalyst window is deliberate: the pre-FOMC vol premium concentrates in roughly the final ~24 hours (Lucca & Moench, NY Fed SR512 — verification tightened this from "2–3 days"), so the current matrix's 14-day window is far too generous; 3 trading days is already conservative.

### V2 — VRP-conditioned

Inputs: VIX close, VIX3M close, SPY daily bars (≥200), catalyst list. Compute `RV20` = annualized 20-day close-to-close stdev of SPY log returns (vol points), `VRP = VIX − RV20`, `R`, `T` as in V1.

| Condition | Regime |
|---|---|
| `R >= 1.00` OR `VRP <= 0` (seller's edge absent) OR major catalyst within 3 trading days | EVENT_CATALYST |
| `VRP >= 2.0` and `T` true and `R < 1.00` | CALM_BULL |
| `VRP >= 2.0` and `T` false and `R < 1.00` | HIGH_VOL_NEUTRAL |
| `0 < VRP < 2.0` | TRENDING_BEAR (thin edge: debit spreads or Do Nothing) |

The 2.0-vol-point threshold is a literature-informed ballpark (long-run median VRP), logged as a tunable, not a truth.

### V3 (optional, spare-book only) — Matrix repaired

Keeps the 4-regime scoring-matrix shape and weights but fixes the dimensions, isolating whether the matrix's problem is dimensions or weights: (1) absolute-VIX dimension → VIX/VIX3M buckets (`<0.90` / `0.90–1.00` / `>=1.00`); (2) per-underlying IVR → VIX 252-day-percentile buckets (`<30` / `30–70` / `>70`); (3) SMA20 → SMA200, and **drop the daily-return dimension** (removes the double-counted short-term input); (4) catalyst window 14 → 5 trading days.

On the IVR replacement: verification corrected one sub-claim — IBKR *can* return a year of daily 30-day IV history in one `reqHistoricalData(whatToShow='OPTION_IMPLIED_VOLATILITY')` call, so "you must self-accumulate IV" was overstated. But whether those bars work under a **delayed-data** subscription is unverified (162 failures reported in ib_insync #458), and the VIX-percentile substitute is free, instantly computable, and strictly simpler for SPY/XSP — so the recommendation stands on simplicity, not impossibility.

### Race design

- Assign V0, V1, V2 to three books with **identical playbook mixes and risk envelopes** so the regime engine is the only independent variable.
- Nightly, compute and persist **all** variants' outputs and inputs to `regime_readings` keyed `(date, book_id, engine_variant)` — regime *disagreement*, not trade P&L, is the informative early signal; 30 trades per book takes months.
- Data prerequisite: start persisting nightly VIX and VIX3M closes to `index_history` immediately (backfill via IBKR historical index bars — delayed subscriptions do serve index history). Both are CBOE cash indices requestable via `Index('VIX3M','CBOE')` exactly like VIX; end-of-day closes are the right inputs for a nightly system, so the 15-min delay is immaterial. One 5-minute empirical check that VIX3M serves without an extra subscription line is required before committing V1/V2 (§8).

---

## 6. Safety: kill switch, anomaly halts, digest/push events

### 6.1 Kill switch

Distinct from the existing Common Sense Kill Switch (per-trade validity hard blocks in [domain-rules.md](../domain-rules.md) §Validation) — this is an operational **state**, checked pre-order regardless of trade validity. Industry practice separates "closing only" from "flatten all," with flatten reserved for emergencies, and halts **latch** (exchange kill switches require manual re-enable).

**States** (in `trading_control`, one GLOBAL row + one per book):

- **ACTIVE** — normal operation.
- **HALT_ENTRIES** — blocks all new-entry orders in scope. Layer C still computes and records "would have traded" (experiment record survives the halt). **Layer A exit management continues** — exits are risk-reducing and must never be blocked; this constraint also keeps the remote-HALT abuse case purely denial-of-trading (verification correction folded in).
- **FLATTEN_REQUESTED** — cancel all working entry orders, then submit closing orders (limit at mid, deterministic marketable-limit escalation ladder — exact ladder is an open question, §8) for every open position in scope. **Human-initiated only, never automatic**: automatic responses stop at HALT_ENTRIES, because a system that force-liquidates on a data glitch does more damage than one that stops and waits, and nightly cadence means positions are never unattended intraday anyway.

**Enforcement point**: a single `check_trading_control(book_id)` read executed synchronously inside the order-submission function immediately before `placeOrder` — not cached at session start, not only in the scan layer. Every submission logs the control-state value it read (to `audit_events`). One choke point all orders pass through, 15c3-5-style.

**Fail-closed defaults, enumerated** (each needs a failing test, not prose):

- `trading_control` row missing, unreadable, or unrecognized value → treat as HALT_ENTRIES.
- DB connection failure anywhere in the executor → abort before the order path.
- Trading-mode stamp mismatch → refuse to start (ADR-0006).
- Executor crash → inherently safe (no daemon, no orders) — but see dead-man's check below.
- HALT states **latch**: clearing requires the console, a typed reason, and writes an audit event with actor + timestamp. Resume is never automatic and never remote.

**Control surfaces**:

1. Console status strip toggle — primary, and the **only** place RESUME exists.
2. ntfy command topic (separate from the digest topic), polled at session start and before the order phase. Accepts exactly one command: HALT (optionally book-scoped). RESUME over ntfy is ignored and logged. Rationale (verified against ntfy's own docs: "the topic is essentially a password"): topics are bearer-token-grade secrets, so the remote channel may only move the system toward safety — worst-case abuse of a leaked topic is denial of new trades, the fail-safe direction. (One verification correction: the exchange-reinstatement-asymmetry citation didn't hold; the asymmetric design stands on the ntfy threat model alone. Hardening option for Live: ntfy access tokens or self-hosted ACLs, supplementing — not replacing — the asymmetry.)
3. A plain sentinel file (e.g. `HALT` in the data dir) as the zero-dependency override for when the DB or UI is itself broken.

### 6.2 Anomaly auto-halts — global (all deterministic, machine-checkable IDs used verbatim in `audit_events`, digests, and tests)

| ID | Trigger | Response |
|---|---|---|
| RECONCILIATION_DRIFT | Post-session broker-vs-books bijection fails (symbol, legs, quantity exact) | Global HALT_ENTRIES + immediate push |
| UNEXPECTED_INSTRUMENT | Any non-option position with qty ≠ 0 (No-Stock Mandate) | Global HALT_ENTRIES + immediate push + scripted same-day assignment response: next session opens with a pre-built closing order for the stock as its **only** permitted action (ADR-0006 defense-in-depth) |
| REPEATED_REJECTION | ≥2 rejections in one session, or ≥3 across trailing 3 sessions | Global HALT_ENTRIES — repeated rejection means our model of the broker's rules is wrong; retrying is how bots dig holes |
| DUPLICATE_ORDER | An order matching (book, legs, expiry, strikes, direction) already submitted this session | Block that order + global HALT_ENTRIES — logic bug, not market condition |

### 6.3 Anomaly auto-halts — scoped

| ID | Trigger | Response |
|---|---|---|
| STALE_DATA | `telemetry_live=False` (already computed in `operator.py`) or quote older than last close + 2h | Block new entries this session; exits still run on stored values, flagged (as today). A book with unpriceable legs escalates from digest-flag to entries-blocked for that book |
| PNL_SHOCK | Book day MTM move > 15% of book basis ($1,500) | HALT_ENTRIES that book — a 4-position defined-risk book respecting 2.5% max loss cannot legitimately lose ~>10% in a day; beyond that is a pricing-data or attribution bug. Threshold is envelope-derived, not data-derived; re-derive once real fills exist |
| ENVELOPE_BREACH_POSTHOC | Reconciled state shows >4 positions, >50% deployed, or a position with max loss >2.5% of basis | HALT_ENTRIES that book — these are pre-blocked by gates, so post-hoc detection proves a code defect; the halt is about the bug, not the risk |
| UNFILLED_ENTRY | Entry order still working at session end | Cancel it (not a halt) — entries never rest overnight |

### 6.4 Digest evolution & push policy

The attachment point exists: `compose_digest()`/`send_ntfy()` in `backend/operator.py` already escalate ntfy priority on P1s/safeguards and flag degraded data. New sections, in order:

1. **Control-state banner** — first line whenever not ACTIVE ("⛔ HALTED (reason) since date"). A halted system must say so every night, or silence becomes indistinguishable from health.
2. **Fills** — per order: book, spec summary, limit vs fill price, slippage in dollars.
3. **Rejections/unfilled** — anything not filled by session end, and why.
4. **Books** — one line per active book: day P&L, cumulative P&L, positions n/4, deployed %, trades toward Live Gate (n/30).
5. **Gate hits** — which gates/hard blocks suppressed candidates tonight (Live-Gate-relevant events).
6. **Anomalies** — reconciliation result, explicitly "reconciliation clean" when clean; absence of the line must not be interpretable as success.
7. Existing lifecycle/candidate content.

**Immediate push** (separate ntfy priority `urgent`) vs nightly batch: push immediately anything that changed the control state or needs human action before the next evening — any auto-halt (with rule ID and values), No-Stock violation, reconciliation drift, order rejection, executor run failure/exception, fill violating its limit. Batch everything else — normal fills, P&L, gate hits, unfilled cancellations, all-quiet. Push fatigue is itself a safety failure: it trains the operator to ignore the channel.

**Dead-man's check**: the executor's last step writes a heartbeat; the digest push doubles as the visible heartbeat; an independent watchdog (second trivial Scheduled Task, or a free healthchecks.io ping) pushes "executor did not report by 22:00" if absent. The nightly system's worst failure mode is silent non-operation — positions aging past 21 DTE with nobody watching — and the executor cannot report its own death.

### 6.5 Console

- **Status strip** (all tabs, additive to existing header): PAPER/LIVE badge, control-state indicator per scope with HALT toggle + reason field, last-executor-run timestamp with staleness coloring (green <24h, red beyond), last-reconciliation result.
- **Books tab** (5th tab, existing pattern in `App.svelte`): one row per book — config fingerprint (playbook mix / regime variant / underlying), N closed trades, win rate with N displayed, expectancy after slippage haircut, max drawdown, deployment %, positions n/4, and a Live Gate checklist column (≥30 trades / ≥3 months / zero breaches / expectancy ≥0, each ✓ or current value). Row click filters ledger and audit views to that book.
- **Audit view** (extends the existing ledger tab): renders `audit_events` — order lifecycle transitions (spec → gates with outcomes → control-state read → submitted → ack → fill/reject → reconciled), control-state changes with actor + reason, anomaly evaluations that fired. Filter by book/date/event-type; each order links to its playbook snapshot and gate record (ADR-0003 pattern).

---

## 7. Implementation plan

Ordered; sizes S (≤half a session), M (1–2 sessions), L (multi-session). Hard sequencing constraint: **the kill switch, fail-closed defaults, and reconciliation land in the same PR set as the first order-placing code** — an executor that can trade before it can be halted, or before it can prove the broker agrees with its books, has no business submitting order one.

| # | Issue | Size | Depends on |
|---|---|---|---|
| 1 | Spec updates first: new `spec/supervision.md` concern file (indexed from spec/README.md), new ADR for kill-switch semantics (halt-vs-flatten, fail-closed, asymmetric remote), anomaly-rule tables into domain-rules.md style, regime→menu note for V1/V2 EVENT_CATALYST = Do Nothing, data-models.md for §4.2 | M | — |
| 2 | Schema migration: `books`, `orders`, `fills`, `reconciliation_runs`, `gate_events`, `audit_events`, `trading_control`, `regime_readings`, `index_history`, `positions.book_id`; append-only enforcement + tests | M | 1 |
| 3 | `index_history` ingestion: nightly VIX/VIX3M close persist + IBKR historical backfill; includes the VIX3M-availability empirical check | S | 2 |
| 4 | Paper smoke-test script (§2.6, checks a–f) — converts every medium-confidence broker claim to ground truth; run against a **dedicated second paper username** created for the bot | S | — |
| 5 | Broker adapter `backend/broker.py`: session, BAG construction, place/close/cancel/wait, whatIf preview, reconcile-by-orderRef, typed errors; crash-after-placeOrder idempotency test | L | 2, 4 |
| 6 | Kill switch: `trading_control`, `check_trading_control` at the placeOrder choke point, fail-closed tests, sentinel file, ntfy HALT-only command topic | M | 2 |
| 7 | Reconciliation engine: snapshot, fill backfill, drift classification (ORPHAN / EXTERNAL_CLOSE / PARTIAL_DRIFT), global `reconciled=false` block, No-Stock P1 wiring | M | 2, 5 |
| 8 | Per-book gate evaluation: virtual ledgers, transactional encumbrance, cross-book netting gate, closes-before-opens ordering, `gate_events` logging | M | 2 |
| 9 | Gateway lifecycle: IBC install/config (paper mode), start-on-demand scripts, port-4002 poll, Scheduled Task, holiday guard, 162 policy | M | 4 |
| 10 | Regime variants V1 + V2 as pluggable engines; nightly multi-variant `regime_readings` persistence; book↔variant assignment in book config | M | 3 |
| 11 | Nightly executor pipeline orchestration: reconcile → Layer A (incl. loss-exit rule via `close_spread`) → Layer C per book → entry placement with GTC profit-takers → unfilled-entry cancellation → heartbeat | L | 5–10 |
| 12 | Anomaly rules (§6.2/§6.3) wired into the pipeline with rule-ID vocabulary, tests per rule | M | 11 |
| 13 | Digest evolution + immediate-push tiering + dead-man watchdog task | M | 11 |
| 14 | Console: status strip + Books tab + audit view | L | 2, 6 |
| 15 | Weekly Flex Query audit job (verify paper orderRef in exports) | S | 7 |
| 16 | Optional: V3 repaired-matrix variant, if a spare book exists after V0/V1/V2 + underlying arms are allocated | S | 10 |

Issues 5–7 merge together (or in one tightly sequenced PR train) per the sequencing constraint. Issues 3, 4, and 9 are independent of each other and parallelizable.

---

## 8. Open questions needing the user's decision

**Policy decisions — all five resolved 2026-08-18** (recorded in [ADR-0008](../decisions.md#adr-0008--kill-switch-semantics-latched-halts-human-only-flatten-asymmetric-remote), [supervision.md](../supervision.md), [domain-rules.md](../domain-rules.md), and [data-models.md](../data-models.md)):

1. **FLATTEN escalation ladder** — ✅ limit at mid, then step one third of the mid-to-natural distance every 5 minutes, natural at the final step. Exact ladder in [supervision.md](../supervision.md).
2. **Rolls under HALT_ENTRIES** — ✅ rolls count as entries (blocked under halt; the halted book takes the plain exit instead).
3. **EXTERNAL_CLOSE valuation** — ✅ broker settlement value, never last-marked value ([domain-rules.md](../domain-rules.md#closure-post-mortem)).
4. **Intent-expiry policy** — ✅ expire stale staged intents; the evening's prices are stale by the next session. Implemented in the adapter (#64).
5. **Book allocation** — ✅ six books now: V0/V1/V2 × XSP/SPY with identical playbook mixes and envelopes; four books reserved for second-generation experiments ([data-models.md](../data-models.md#executor-paper-schema-additions)).

**Empirical checks (owned by the smoke test / early issues, listed so they aren't lost):**

6. VIX3M quote + history under the delayed subscription (issue 3).
7. GTC profit-taker child on an XSP BAG persisting and releasing correctly at CBOE in paper (issue 4).
8. `whatIfOrder` commission fields for XSP BAGs: populated or DBL_MAX sentinels (determines `MarginPreview` nullability, issue 4).
9. XSP combo net-price tick increments (limit-price rounding in `place_spread`, issue 4).
10. IBC login on the bot's paper username: confirm no 2FA dialog (issue 9).
11. `reqCompletedOrders` behavior for prior-session same-clientId fills (issue 4d; determines how hard reconciliation leans on time-filtered `reqExecutions`).
12. Flex exports from the paper account carry orderRef (issue 15).

**Deferred by design (recorded so deferral is a decision, not an omission):**

13. ntfy command-topic authentication for Executor (Live) — topic-as-secret is acceptable for Paper given HALT-only asymmetry; Live likely wants a self-hosted instance or auth tokens.
14. PNL_SHOCK threshold re-derivation from real paper fills, incl. whether XSP books need a different threshold given wider delayed-quote spreads.
15. Where the ADR-0006 approval queue (Executor Live) lives in the console — constrains status-strip/tab layout but is out of scope for Paper.
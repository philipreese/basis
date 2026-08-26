# Supervision & Trading Control

> Part of the [modular specification](README.md). Specifies the operational safety layer for the Executor levels ([ADR-0006](decisions.md#adr-0006--autonomy-roadmap-operator--executor-paper--executor-live)): the kill switch, anomaly auto-halts, notification policy, and the dead-man watchdog. Design rationale in [design/executor-paper.md](design/executor-paper.md) §6; kill-switch semantics decided in [ADR-0008](decisions.md#adr-0008--kill-switch-semantics). Tables here are exact — the rule IDs are used verbatim in `audit_events`, digests, and tests.
>
> **Implementation status:** specified ahead of code, contract-first. Implementation tracked in [#65](https://github.com/philipreese/basis/issues/65) (kill switch), [#71](https://github.com/philipreese/basis/issues/71) (anomaly rules), [#72](https://github.com/philipreese/basis/issues/72) (digest/watchdog).

---

## Trading control states

Distinct from the Common Sense Kill Switch (per-trade validity hard blocks in [domain-rules.md](domain-rules.md#validation--common-sense-kill-switch)) — this is an operational **state**, checked before every order regardless of trade validity. Stored in `trading_control` ([data-models.md](data-models.md#executor-paper-schema-additions)): one `GLOBAL` row plus one row per book.

| State | Effect |
|---|---|
| ACTIVE | Normal operation |
| HALT_ENTRIES | Blocks all new-entry orders in scope. **Rolls count as entries** — a halted book takes the plain exit instead. Layer C still computes and records "would have traded" (the experiment record survives the halt). Layer A exit management continues — exits are risk-reducing and are never blocked |
| FLATTEN_REQUESTED | Cancel all working entry orders, then submit closing orders for every open position in scope. **Human-initiated only, never automatic** — automatic responses stop at HALT_ENTRIES |

### Flatten escalation ladder

Closing orders under FLATTEN_REQUESTED (and any manual flatten) use a deterministic marketable-limit ladder — market orders on options remain banned ([domain-rules.md](domain-rules.md#trade-specification)):

1. Limit at the combo midpoint; wait 5 minutes.
2. Move the limit one third of the mid-to-natural distance toward natural; wait 5 minutes.
3. Move another third; wait 5 minutes.
4. Limit at the natural price (bid for closing a credit structure's buyback direction as applicable); rests until filled or session end.

Every step is logged to `audit_events`.

### Enforcement point

A single `check_trading_control(book_id)` read executed synchronously inside the order-submission function immediately before `placeOrder` — not cached at session start, not only in the scan layer. Every submission logs the control-state value it read. One choke point all orders pass through.

### Fail-closed defaults

Each of these requires a failing test, not prose:

- `trading_control` row missing, unreadable, or holding an unrecognized value → treat as HALT_ENTRIES.
- DB connection failure anywhere in the executor → abort before the order path.
- Trading-mode stamp mismatch → refuse to start ([ADR-0006](decisions.md#adr-0006--autonomy-roadmap-operator--executor-paper--executor-live)).
- HALT states **latch**: clearing requires the console, a typed reason, and writes an audit event with actor + timestamp. Resume is never automatic and never remote.

### Control surfaces

1. **Console status strip** — primary control, and the **only** place RESUME exists.
2. **ntfy command topic** (separate from the digest topic), polled at session start and before the order phase. Accepts exactly one command: HALT (optionally book-scoped). RESUME over ntfy is ignored and logged. Rationale: an ntfy topic is a bearer-token-grade secret, so the remote channel may only move the system toward safety — worst-case abuse of a leaked topic is denial of new trades.
3. **Sentinel file** (`HALT` in the data directory) — zero-dependency override for when the DB or UI is itself broken.

---

## Anomaly auto-halts — global

| ID | Trigger | Response |
|---|---|---|
| RECONCILIATION_DRIFT | Post-session broker-vs-books bijection fails (symbol, legs, quantity exact) | Global HALT_ENTRIES + immediate push |
| UNEXPECTED_INSTRUMENT | Any non-option position with qty ≠ 0 (No-Stock Mandate, [CONTEXT.md](../CONTEXT.md)) | Global HALT_ENTRIES + immediate push + scripted same-day assignment response: next session opens with a pre-built closing order for the stock as its **only** permitted action |
| REPEATED_REJECTION | ≥2 order rejections in one session, or ≥3 across trailing 3 sessions | Global HALT_ENTRIES — repeated rejection means the system's model of the broker's rules is wrong; retrying digs holes |
| DUPLICATE_ORDER | An order matching (book, legs, expiry, strikes, direction) already submitted this session | Block that order + global HALT_ENTRIES — logic bug, not market condition |

## Anomaly auto-halts — scoped

| ID | Trigger | Response |
|---|---|---|
| STALE_DATA | `telemetry_live=False`, or quote older than last close + 2h | Block new entries this session; exits still run on stored values, flagged. A book with unpriceable legs escalates from digest-flag to entries-blocked for that book |
| PNL_SHOCK | Book day MTM move > 15% of book basis ($1,500) | HALT_ENTRIES that book — a 4-position defined-risk book respecting 2.5% max loss cannot legitimately lose that much in a day; beyond it is a pricing-data or attribution bug. Threshold is envelope-derived; re-derive once real fills exist |
| ENVELOPE_BREACH_POSTHOC | Reconciled state shows >4 positions, >50% deployed, or a position with max loss >2.5% of basis | HALT_ENTRIES that book — these are pre-blocked by gates, so post-hoc detection proves a code defect |
| UNFILLED_ENTRY | Entry order still working at session end | Cancel it (not a halt) — entries never rest overnight; GTC belongs to closing orders only |

---

## Digest & push policy

The nightly digest (`compose_digest`/`send_ntfy` in [backend/operator.py](../backend/operator.py)) gains sections, in order:

1. **Control-state banner** — first line whenever not ACTIVE ("⛔ HALTED (reason) since date"). A halted system must say so every night, or silence becomes indistinguishable from health.
2. **Fills** — per order: book, spec summary, limit vs fill price, slippage in dollars.
3. **Rejections/unfilled** — anything not filled by session end, and why.
4. **Books** — one line per active book: day P&L, cumulative P&L, positions n/4, deployed %, trades toward Live Gate (n/30).
5. **Gate hits** — which gates/hard blocks suppressed candidates tonight.
6. **Anomalies** — reconciliation result, explicitly "reconciliation clean" when clean; absence of the line must not be interpretable as success.
7. Existing lifecycle/candidate content.

**Broker-unavailable line** ([#823](https://github.com/philipreese/basis/issues/823)): when the broker session fails to open, the executor captures every API error ib_async surfaced during the connect attempt (`IB.errorEvent`) and writes them as `api_errors` in the `EXECUTOR_BROKER_UNAVAILABLE` audit payload — the terminal exception can be an anonymous `TimeoutError` while the real cause arrived only as an error event. A captured code classified as needs-a-human (`NEEDS_HUMAN_BROKER_ERRORS` in [backend/broker.py](../backend/broker.py); seeded with 10141, the paper-trading disclaimer — never connectivity-lost codes a retry can clear) replaces the generic "IB Gateway unreachable" digest line with the specific operator instruction ("⛔ ACTION NEEDED: ..."); unclassified failures keep the generic line and append the captured codes/messages so no cause is ever swallowed.

**Immediate push** (ntfy priority `urgent`, distinct from the nightly digest): anything that changed the control state or needs human action before the next evening — any auto-halt (with rule ID and values), No-Stock violation, reconciliation drift, order rejection, executor run failure/exception, fill violating its limit. Everything else batches into the digest — normal fills, P&L, gate hits, unfilled cancellations, all-quiet. Push fatigue is itself a safety failure: it trains the operator to ignore the channel.

---

## Dead-man watchdog

The executor's last step writes a heartbeat; the digest push doubles as the visible heartbeat. An independent watchdog (a second trivial Scheduled Task, or a free healthchecks.io ping) pushes "executor did not report by 22:00" if the heartbeat is absent. The nightly system's worst failure mode is silent non-operation — positions aging past 21 DTE with nobody watching — and the executor cannot report its own death. On market holidays the executor writes its heartbeat and exits without trading: silent non-operation is only acceptable when announced by the heartbeat.

---

## Afternoon preflight rehearsal

The 18:45 run is the system's only full exercise of its broker machinery — one reveal per day, so a connect-path failure (the 10141 disclaimer wall), a preview crash, or a drift halt discovered at 18:45 costs the whole evening. The preflight ([backend/preflight.py](../backend/preflight.py), `pixi run preflight`, `scripts/register-preflight-task.ps1`, 14:00 weekdays, [#827](https://github.com/philipreese/basis/issues/827)) walks those same paths hours earlier, while the operator is awake to act.

Its place in the reporting model: a **rehearsal, report-only, never a mutator**.

- It rehearses, in order, each step independently guarded so an exception becomes a finding rather than a crash: Gateway launch + broker session open (the executor's exact path, including the connect retry and the `NEEDS_HUMAN_BROKER_ERRORS` classification — a classified code reports the same specific operator instruction the digest would); the broker-vs-books comparison via `reconciliation.compare_books` (the pure half of `run_reconciliation`, so preflight and the evening run can never disagree about what counts as drift); a `preview_spread` probe on a live-priced XSP put vertical (whatIfOrder only — a `PreviewRejectedError` is a reported finding, distinct from the machinery crashing); and control/heartbeat state (any non-ACTIVE scope, the HALT sentinel, heartbeat staleness).
- It reports through one ntfy push on the digest topic — ASCII-only title, "basis preflight: all clear" or "basis preflight: N problem(s)" — each finding carrying its plain-language action, plus a `PREFLIGHT_RUN` audit event with the findings as payload. That audit event is the **only** database write preflight may make.
- It never: places or cancels orders, mutates books/positions/control state, writes `reconciliation_runs` rows, latches halts, or writes the executor heartbeat — the dead-man watchdog must never be pacified by a rehearsal.
- Tenancy: it takes its own `preflight` Gateway-tenant lock (`run_lock.GATEWAY_TENANT_LOCKS`); if the executor or any other tenant is live it pushes "executor running, preflight skipped" and exits cleanly. Its teardown re-checks tenancy immediately before the kill and defers to any tenant that went live while it ran; the kill itself only ever targets the process tree preflight's own launch produced (`stop_gateway_tree_only`), never the executor/fill-check/restore-drill teardowns' system-wide ibgateway sweep — a rehearsal must never be the reason a real tenant's Gateway dies ([#838](https://github.com/philipreese/basis/issues/838)).
- The nightly launch is the symmetric half: if another tenant is still active when the executor is about to launch its own Gateway, it waits up to 5 minutes (polling every 15s) rather than colliding into a second Gateway/clientId; if the tenant hasn't cleared by then it aborts the run with an audited `EXECUTOR_ABORTED_TENANT_ACTIVE` event and an urgent ntfy push instead of running half-collided ([#838](https://github.com/philipreese/basis/issues/838)).

---

## Console

The supervision console ([#73](https://github.com/philipreese/basis/issues/73)) surfaces all of the above in the web UI:

- **Status strip** (all tabs): PAPER badge, per-scope control state with HALT toggle + typed reason, RESUME (the only surface where it exists), executor heartbeat age (green <24h, red beyond), last reconciliation result. Backed by `GET/POST /api/trading-control` and `GET /api/executor/status`.
- **Books tab**: per-book row — config fingerprint, closed trades, win rate (with N), expectancy after slippage haircut, max drawdown, deployment %, positions n/max, and the Live Gate checklist (metric definitions in [domain-rules.md](domain-rules.md#live-gate-metrics-console)). Backed by `GET /api/books`.
- **Audit trail** (Books tab, below the table): renders `audit_events` newest-first, filterable by book/date/event-type; a book row click scopes the trail. Backed by `GET /api/audit-events`.

---

**Source of truth:** [backend/trading_control.py](../backend/trading_control.py) (kill switch), [backend/anomaly.py](../backend/anomaly.py) (anomaly rules), [backend/digest.py](../backend/digest.py) (digest + urgent tiering), [scripts/watchdog.ps1](../scripts/watchdog.ps1) (dead-man watchdog), [backend/console.py](../backend/console.py) + [frontend/src/lib/StatusStrip.svelte](../frontend/src/lib/StatusStrip.svelte) / [BooksTab.svelte](../frontend/src/lib/BooksTab.svelte) (console).

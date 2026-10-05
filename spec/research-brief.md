# Research brief and the operator picks book

> Part of the [modular specification](README.md). The design of [#1131](https://github.com/philipreese/basis/issues/1131): LLM-in-the-loop stock research with capped real-money picks. The scope is frozen at what the issue lists; later changes become new issues filed after real use. Decision record: [ADR-0018](decisions.md#adr-0018--research-brief-snapshot-then-analyze-two-ledgers-and-a-manual-picks-book).

## What it is

An AI does the upfront research and the operator makes the decisions. Each brief shortlists a few US small- and mid-cap stocks, each with a one-paragraph thesis, its key risks and one line saying what would prove it wrong. The operator reads the brief on a phone, marks each candidate PICK or PASS, and buys the picks **by hand** in the live account. Nothing in this design places an order. Autonomous execution is a possible later stage, and only if the record earns it.

Two records run side by side from the first brief:

| Ledger | Money | What goes in | The question it answers |
|---|---|---|---|
| **AI shortlist** | Paper | Every candidate, equal-weighted (one unit each), entered at the snapshot's frozen close | Does the AI beat the benchmarks on its own? That decides whether autonomy is ever an option |
| **Operator picks** (book **P01**) | Real, hard-capped | The candidates the operator picked, at the prices actually paid | Does a human filter add value over the shortlist? |

Both are append-only and timestamped before the outcomes are known.

## Cadence

- **Nightly (cheap).** For each held pick, check its thesis against that night's frozen snapshot. Surface 0–2 new candidates, and only when a new filing or earnings report warrants one. Most nights the brief reads "nothing new, theses intact".
- **Monthly.** A full screen of the universe for new candidates.

## Snapshot, then analyze

The AI never reads the live web. A plain scheduled script with no AI (`pixi run research-snapshot`, [backend/research_snapshot.py](../backend/research_snapshot.py)) freezes every input into one timestamped folder **outside the repo** (default `%USERPROFILE%\basis-data\research\snapshots\`, or `BASIS_RESEARCH_SNAPSHOT_DIR`). It records the folder's content hash and `as_of` date in the live database. The AI analysis then reads **only** that folder, with web access off. So a brief written late can never see anything after the snapshot's date. A missed slot runs at the next opportunity and is dated when it actually ran.

What a snapshot contains (sources are documented in the script's module docstring):

| File | Contents |
|---|---|
| `universe.json` | The screened pool: US-listed common stock, market cap in the small/mid band, a price floor, a one-day dollar-volume liquidity floor, and an SEC registrant. Source: Nasdaq's public stock-screener download (NASDAQ, NYSE and NYSE American listings), mapped to CIKs through SEC's `company_tickers.json`. Names with no CIK are listed as excluded. |
| `prices.json` | Every universe member's last sale from the screen. For the focus set (held picks, every company with a filing in the window, and SPY), six months of daily closes. |
| `filings.json`, `filings/` | Per company, the latest 8-K, 10-Q and 10-K in the lookback window (nightly 4 days, monthly 35), accepted before the run began. Each comes with a text excerpt: an 8-K's EX-99 press release, or the MD&A section of a 10-Q or 10-K. |
| `held.json` | The picks book's current holdings, so a held pick stays covered after it leaves the screen. |
| `manifest.json` | id, kind, as_of, status, reasons, counts, the screen thresholds, and a sha256 for every file. The snapshot's `content_hash` is the sha256 of that file map. |

**Fail loud.** Any input that could not be fetched makes the snapshot `INCOMPLETE`, with the reasons recorded in its manifest and its database row. A screener response under 3,000 rows is treated as truncated. The partial folder is still written, because it is evidence of what the run saw. It is never presented as complete, and `research.record_brief` refuses to write a brief from it. A brief also re-hashes the folder before it reads a price, so an altered snapshot is refused too.

SEC fair access: at most 8 requests a second, with a User-Agent naming the contact address in `BASIS_SEC_CONTACT` (`.env`, never in the repo).

**Intended schedule.** Phase 2 registers this; nothing is scheduled yet. The nightly run goes after 17:30 ET and avoids 18:30–19:30, the evening executor's window. The monthly kind runs on the first trading evening of each month.

## The operator picks book (P01)

The operator's picks are shares in the **same live IBKR account** that B36 trades. Reconciliation compares the broker's share count against what the books expect, and treats any difference as a No-Stock P1 that halts every book. Without P01, a single hand-bought pick would read as unexplained drift and freeze B36. P01 exists to prevent that.

- **A manual book.** Its status is `MANUAL` (`states.BOOK_MANUAL_STATUS`), seeded from `seeds.MANUAL_BOOKS`, which works like B00 for options. It is in neither the ACTIVE set nor the managed set. So no Layer C scan, rebalance, mark, flatten or distribution credit ever acts on it. `console.book_summaries` and the empirical null drill exclude it, because it is never evidence. The system takes no autonomous action on it; the operator places every trade.
- **Holdings come from a fill ledger, not `share_holdings`.** The operator records each hand-placed execution against its PICK (`POST /api/research/picks/{id}/fills`). P01's holdings are the signed net of those rows: BUY adds, SELL subtracts. `reconciliation._expected_share_quantities` adds them, per symbol, to the designated books' holdings. A recorded buy therefore reconciles clean. B36's holding of the same symbol stays in B36's own `share_holdings` row, and the two are summed only for the comparison. Because picks never land in `share_holdings`, a GLOBAL flatten can never sell them, and B36's rebalance never sees them.
- **Fail closed.** A hand buy that was never recorded adds nothing to the expectation, so it raises the ordinary drift halt (ORPHAN, or SHARE_DRIFT when a holding exists). So does a hand sale that was never recorded. Fill rows count only while P01 exists with status MANUAL. The fix for pick drift is to record the missing fill; `/api/resolution/share-holding` refuses P01, which designates no `share_symbols`.
- **Unknown-ref executions.** A hand trade carries no `basis:` orderRef. When the fill is recorded with its IBKR `exec_id`, reconciliation treats that execution as known. Without the `exec_id`, the night's digest lists it among the unknown-ref executions. That list is a note, not a halt.
- **The hard cap is private.** It lives in `BASIS_MANUAL_CAP_P01` in `.env.live` and never in the repo. It uses a different prefix from `BASIS_LIVE_STAKE_*`, which would arm the stage-1 machinery. The cap counts the net shares held at their average cost, commissions included. A PICK is refused when the cap is unset or malformed, or when no room is left. A fill is **never** refused for the cap, because the trade has already happened and refusing the record would only turn it into drift. An over-cap buy is recorded, P01 is halted, and an urgent `PICKS_CAP_BREACH` event is written. The cap is not raised mid-year because of early wins.
- **Seeded in the live database only, and halted** (the B35/B36 precedent). The paper database never gets P01, because a halted book nobody can use would be a permanent halt line in the paper digest and preflight. A PICK is refused until the operator RESUMEs P01 from the console. A PASS is always recorded. Fills on an existing pick are always recorded.
- **Live database only.** Every research write refuses unless the process runs in live mode. Serve the endpoints from the live console (`pixi run live-console`). A pick fill recorded in the paper database would make paper reconciliation expect shares the paper account never holds.
- **Cash is shared.** P01's buys spend the same account cash B36's month-end rebalance needs. Keep the cap small enough to leave B36 its room.

## Hold period and exits

Each pick is held **3–6 months**. The exact horizon is pre-registered before the first brief, and the pre-registration is hashed. The only early exit is the candidate's own pre-registered "what would prove this wrong" line coming true. The shortlist's paper positions follow the same rule, so the two ledgers stay comparable.

## Benchmarks

Each book is judged against three yardsticks, all entered on the same dates and at the same snapshot closes:

- **SPY**, the S&P 500.
- **An equal-weight portfolio of the same screened universe**, read from the snapshot's frozen pool.
- **A random same-pool null:** random picks drawn from that snapshot's universe, the same number as the brief's candidates. This answers whether either book beats luck.

Entering at the snapshot's close is slightly optimistic, because a real buy happens the next session. The same convention applies to every benchmark, so the comparison stays fair. The operator's picks are measured at the prices actually paid.

No backtest of AI picks on historical data is ever run, because the model may already know the outcomes ([#1082](https://github.com/philipreese/basis/issues/1082) check 13). The test is forward only.

## Scaling

After about 12 months, three questions decide the next step:

1. Does the AI shortlist beat the benchmarks? This decides whether autonomy is ever on the table.
2. Do the operator's picks beat the shortlist? This decides whether the human filter earns its place.
3. Do both beat the random null? This rules out luck.

Any scale-up goes through the staged-live machinery of [ADR-0006](decisions.md#adr-0006--autonomy-roadmap-operator--executor-paper--executor-live).

## Delivery phases

| Phase | Scope |
|---|---|
| 1 (shipped) | This design; the append-only research tables; P01 and its reconciliation attribution; the snapshot script |
| 2 | The scheduled snapshot task, the brief runner (pinned model, frozen prompt), and how the brief reaches the phone and how picks are marked |
| 3 | Scoring: shortlist and pick exits, and the three benchmarks |

**Source of truth:** [backend/research.py](../backend/research.py) (ledgers, attribution, cap), [backend/research_snapshot.py](../backend/research_snapshot.py) (snapshot), [backend/reconciliation.py](../backend/reconciliation.py) (`_expected_share_quantities`), [backend/seeds.py](../backend/seeds.py) (`MANUAL_BOOKS`), [backend/states.py](../backend/states.py) (vocabularies).

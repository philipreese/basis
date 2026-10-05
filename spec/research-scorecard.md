# Research scorecard — every money-making idea tested so far

> Part of the [modular specification](README.md).

This is the index to the lab's research: every idea that has been sanity-checked, backtested, or studied against the historical corpus, in one table per category. Each row names the idea, its current verdict, one plain-English reason, and a link to the full writeup (a GitHub issue or comment — the analysis itself, with its pre-registration, caveats and corrections, lives there, not here).

**A backtest can retire an idea, never promote one** ([ADR-0015](decisions.md#adr-0015--backtest-direction-rule-history-can-retire-a-book-never-promote-one)). History is admissible evidence that something doesn't work, immediately; it is never evidence that something does. So the best verdict a backtest alone can produce is **paper candidate** — not disproven, worth watching live. An idea only reaches **adopted** when the operator decides, on top of a backtest's permission, to run it on paper money, and a paper book only reaches live money through its own separate gate (the Live Gate, [ADR-0006](decisions.md#adr-0006--autonomy-roadmap-operator--executor-paper--executor-live)/[ADR-0010](decisions.md#adr-0010--live-gate-promotion-procedure-stress-exposure-and-composition-limits)), tracked elsewhere, not on this page.

## Summary

| Verdict | Count |
|---|---|
| killed | 20 |
| inconclusive | 6 |
| weak survivor | 2 |
| paper candidate | 1 |
| adopted | 3 |
| blocked on data | 1 |
| in progress | 5 |
| **Total** | **38** |

## Options

| Idea | Verdict | Why | Link |
|---|---|---|---|
| The baseline S&P options trade (B01's iron condor mix) | killed | Before any trading cost, the base trade earns almost nothing per trade; commissions and bid/ask crossing alone are far bigger than that edge. | [#1056 (issue body)](https://github.com/philipreese/basis/issues/1056) |
| Fewer legs, wider spreads, further-dated (options packaging study) | adopted | Every tight, standard-width structure has the same near-zero edge regardless of leg count; only a much wider, longer-dated put spread or condor cleared its own pre-registered bar — narrowly, and only one of two tested survived. It now runs as a dedicated paper book. | [comment](https://github.com/philipreese/basis/issues/1056#issuecomment-5976014618) |
| Every reasonable packaging lever at once (wing width, DTE, delta, exit rule) | killed | None of the picks chosen on the first half of history held up on the second half — one bad year erased several good ones, and the result can't be told apart from zero. | [comment](https://github.com/philipreese/basis/issues/1056#issuecomment-5982150649) |
| 27 single-knob, regime-variant and structure-swap options books | killed | Each knob moves only a few dollars a trade against a much larger cost gap in a base trade with roughly zero edge before costs; retired together as a group. | [decisions.md § ADR-0009 amendment](decisions.md#adr-0009--accelerated-experiment-matrix) |

## ETF / trend (B36, B38)

| Idea | Verdict | Why | Link |
|---|---|---|---|
| Fractional-share sizing via the broker API | killed | The broker's API flatly rejects fractional equity orders with no account-setting workaround found; the book sticks to whole shares with a cash remainder. | [comment](https://github.com/philipreese/basis/issues/1054#issuecomment-5981719742) |
| Splitting the monthly rebalance into tranches | weak survivor | At today's small stage-1 size, commission drag wipes out the benefit; at roughly ten times that size, two tranches clears the bar (four doesn't) — worth adopting only once the book is much bigger. | [comment](https://github.com/philipreese/basis/issues/1054#issuecomment-5983193326) |
| Factor-tilt levers for B36 (momentum, quality, low-volatility, inverse-vol weighting, concentrate-the-winners) | killed | Two tilts lose outright, one wins on return but with a much deeper drawdown (disqualifying on its own), one is a marginal loss, and the one borderline win doesn't repeat in a longer second window. | [comment](https://github.com/philipreese/basis/issues/1054#issuecomment-5986501950) |
| A Bitcoin ETF slot for B36 | adopted | A half-weight Bitcoin slot, held only while the same trend rule says "on," cleared its bar in two overlapping windows with a real, mechanism-driven edge; the operator chose the more conservative half-weight version over the full-weight one the study also flagged. A Bitcoin-plus-Ethereum version was tested too and didn't add anything. | [comment](https://github.com/philipreese/basis/issues/1054#issuecomment-5987400278) |
| Running B36 on margin leverage | inconclusive | Leverage doesn't meaningfully hurt B36's own risk-adjusted return, but it also never catches up to simply holding more unlevered stock, and amplifies losses in a downturn without buying any extra protection. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5995059080) |
| Running a B36-style book in a taxable account | killed | The rebalance frequency sits right at the edge of wash-sale rules, and modeled tax drag costs a real, ongoing chunk of annual return with no offsetting benefit for this specific book. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5995059080) |
| Turn-of-month calendar effect (hold the market a few days around month-end) | adopted | Beat a matched random-day comparison by a wide margin, survived realistic cost stress, and even beat buy-and-hold on risk-adjusted return while invested only a fraction of the time; now runs as its own paper book. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5982800478) |
| Pre-holiday calendar effect | paper candidate | Also clears its cost-sensitivity tests, with a smaller time-in-market footprint than turn-of-month; not yet built into its own paper book. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5982800478) |
| Closed-end fund discount buying | killed | An early pass that looked promising was driven by a handful of funds with corrupted price data; with those fixed, the result lands right back at a coin-flip against its own benchmark. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5982800478) |

## Stock anomalies

| Idea | Verdict | Why | Link |
|---|---|---|---|
| Wide-universe momentum / trend (industries, dual momentum, cross-asset) | inconclusive | Each variant either trades a better return for a much deeper drawdown than a simple 60/40 mix, or loses on risk-adjusted return while being much gentler in drawdowns — none cleanly beats the benchmark on both counts at once. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5981640674) |
| Short-term mean reversion (buying recent dips) | weak survivor | Clears its own low bar (beating sitting in cash) but never comes close to just holding the market, and several variants lost more than buy-and-hold during real crashes — a genuine falling-knife risk, not a free lunch. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5981712745) |
| Long-only merger arbitrage | blocked on data | Historical prices for delisted tickers are solvable affordably, but a structured, free record of each deal's terms and outcome isn't — and the one free source tried systematically loses exactly the completed deals the strategy is paid to catch. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5982948117) |
| S&P 500 index-addition effect | killed | Buying on the announcement is statistically indistinguishable from zero (and mostly one outlier stock); holding through the actual addition is negative after costs — this effect has essentially disappeared since the 2010s. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5982948117) |
| VIX term structure via ETFs | killed | Loses to simply holding the stock market on both risk-adjusted return and worst drawdown, held out. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5984779628) |
| The overnight-only effect (hold stocks overnight, cash during the day) | killed | Earns less per unit of risk than just holding the whole time, and at realistic trade sizes commissions alone eat the entire effect. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5984898503) |
| Insider buying (Form 4 filings) | killed | All three signal variants (officer purchases, insider clusters, "opportunistic" insiders) lose to a liquidity-and-date-matched benchmark after costs, held out — insiders buy stocks that go up in general, and none of the signals beats that baseline. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5988248904) |
| AI/text-scored earnings-release sentiment | killed | Both a modern sentiment model and a classic finance word-list dictionary underperform picking earnings releases at random, held out — text sentiment actively hurt here rather than helping. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5988725855) |
| Pre-FOMC announcement drift | inconclusive | Not cleanly killed, but doesn't beat a time-matched random-day comparison by a meaningful margin since 2010 either; the narrowest version is killed outright. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5984612724) |
| Treasury auction cycle | killed | Every held-out variant and tenor is either a loser or statistically indistinguishable from a matched random-day comparison. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5984612724) |

## Kalshi / prediction markets

| Idea | Verdict | Why | Link |
|---|---|---|---|
| Kalshi daily-high-temperature weather markets | killed | Every strategy tested (favorite-buying, longshot-fading, forecast-model edge) loses money or is statistically indistinguishable from zero, in both cities tested, held out. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5983396451) |
| Kalshi S&P 500 daily-range markets (buy the center / buy the tails) | killed | Both framings lose held out; the tail-buying version loses almost everything risked, the same favorite-longshot overpricing pattern found across every market family tested on this issue. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5984348088) |
| Kalshi NFL moneyline mispricing | inconclusive | Too few held-out trades to say anything either way — underpowered, not evidence of no edge. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5984121677) |
| Kalshi MLB moneyline favorite-longshot bias | killed | Tested with real statistical power; Kalshi's own baseball pricing is calibrated closely enough that fading favorites doesn't pay for itself after fees. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5984121677) |
| Kalshi crypto hourly-range markets (buy the calm/favorite bucket) | killed | A real, consistent loser on both Bitcoin and Ethereum under two different ways of picking "the favorite" — the market routinely overprices a confident-looking outcome that isn't actually that likely. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5984121677) |
| Systematic NO-selling (fading longshots) on Kalshi | inconclusive | The crypto and weather legs are killed or too thin to test; the S&P leg has a real-looking positive result built entirely on zero observed losses in a payoff shape that needs far more loss-free trades before that can be trusted. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5984951702) |
| Kalshi economic-release markets vs. consensus (CPI, unemployment, payrolls) | killed | Every strategy loses money held out, decisively — Kalshi's own price is simply a better predictor of these releases than the comparison model used here, matching outside academic findings. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5986741404) |
| Kalshi market-making economics (historical maker P&L on S&P ranges) | inconclusive | The pooled, full-crediting result is weakly positive but not statistically significant; whether a new, small market-maker without queue priority would actually capture a representative share of that edge can't be answered from historical trade data alone. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5987290391) |
| Kalshi market-making, read-only forward simulation | in progress | Running now as a paper simulator; see "Live and running" below. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5994451739) |
| Kalshi–Polymarket arbitrage (sanity check 17) | in progress | Being researched now. | — |
| Sportsbook promo extraction and "+EV" betting as a Georgia resident (sanity check 18) | killed | Georgia has no legal sportsbook to matched-bet against (the 2026 legalization bills both died); the accessible alternatives (DFS pick'em apps, Kalshi/Robinhood sports contracts) either already failed elsewhere in this research or offer only a one-time, non-repeatable sign-up bonus, not an ongoing edge. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5995956077) |

## Account and wrapper

| Idea | Verdict | Why | Link |
|---|---|---|---|
| What a taxable account would unlock beyond the Roth IRA | killed | Of roughly 25 ideas and variants tested, at most one died specifically because the Roth forbids something; everything else died on lack of edge, decay, data quality or statistical power. A new taxable account isn't justified until a specific idea actually needs one. | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5995059080) |

## Small-player edges

| Idea | Verdict | Why | Link |
|---|---|---|---|
| SPAC trust arbitrage (sanity check 19) | in progress | Being researched now. | — |
| Small-cap post-earnings-announcement drift (sanity check 20) | in progress | Being researched now. | — |
| Spin-off underperformance/outperformance (sanity check 21) | in progress | Being researched now. | — |

## Live and running

Four things are running on paper or in a read-only simulation today, each waiting on its own kind of evidence before anything changes:

- **B36, the monthly ETF trend book**, now including the half-weight Bitcoin slot — waiting on enough monthly decisions, spanning a real stress episode, to clear its own yardstick (a 60/40 comparison, not the options lab's 30-trade gate).
- **B38, the turn-of-month calendar book** — waiting on the same kind of live track record; it has no yardstick of its own yet, so it's excluded from any promotion step until one is designed.
- **The Kalshi market-making read-only paper simulator** — polls Kalshi's public market data only, places no real orders, and is waiting out a pre-registered minimum run length before any kill/continue/survive verdict is read as final.
- **The sanity check 13 forward ledger** — an append-only log built to score new earnings releases as they happen (never backfilled against history, to avoid leaking outcomes into the model). It sits idle, waiting on a future check finding a text-sentiment effect worth confirming forward before anything is scored into it.

Also active: **B37, the wide, far-dated condor paper arm** — the one options packaging that cleared its own bar, running forward as a single-arm hypothesis book, excluded from promotion by design.

## How to add a row

New checks get their own GitHub issue, not a comment buried in an existing thread. Once a check has a verdict, add one row to the relevant table above (or a new category if none fits) linking the issue or the comment that carries the result.

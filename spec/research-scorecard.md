# Research scorecard — every money-making idea tested so far

> Part of the [modular specification](README.md).

This is the index to the lab's research: every idea that has been sanity-checked, backtested, or studied against the historical corpus, in one table per category, each row naming the idea, its current verdict, one plain-English reason, and a link to the full writeup (a GitHub issue or comment — the analysis itself, with its pre-registration, caveats and corrections, lives there, not here). **A backtest can retire an idea, never promote one** ([ADR-0015](decisions.md#adr-0015--backtest-direction-rule-history-can-retire-a-book-never-promote-one)): history is admissible evidence that something doesn't work, immediately, but never evidence that something does — so the best verdict a backtest alone can produce is **paper candidate**, not disproven and worth watching forward. An idea only reaches **adopted** when the operator decides, on top of a backtest's permission, to run it on paper money.

## Summary

| Verdict | Count |
|---|---|
| killed | 30 |
| inconclusive | 10 |
| weak survivor | 5 |
| paper candidate | 1 |
| adopted | 3 |
| blocked on data | 2 |
| in progress | 2 |
| **Total** | **53** |

## Options

| Idea | Verdict | Why | Revisit when | Link |
|---|---|---|---|---|
| The baseline S&P options trade (B01, a regime-switched mix of five playbooks) | killed | Before any trading cost, the base trade earns almost nothing per trade; commissions and bid/ask crossing alone are far bigger than that edge. | — | [#1056 (issue body)](https://github.com/philipreese/basis/issues/1056) |
| Fewer legs, wider spreads, further-dated (options packaging study) | adopted | Every tight, standard-width structure has the same near-zero edge regardless of leg count. Only a much wider, longer-dated condor cleared its own pre-registered bar, and only narrowly — it misses once the standard error is clustered by year; the matching wide put spread missed its own bar narrowly too. The condor version now runs as a dedicated paper book. | — | [comment](https://github.com/philipreese/basis/issues/1056#issuecomment-5976014618) |
| Every reasonable packaging lever at once (wing width, DTE, delta, exit rule) | killed | None of the picks chosen on the first half of history held up on the second half — one bad year erased several good ones, and the result can't be told apart from zero. | — | [comment](https://github.com/philipreese/basis/issues/1056#issuecomment-5982150649) |
| 27 single-knob, regime-variant and structure-swap options books | killed | Each knob moves only a few dollars a trade against a much larger cost gap in a base trade with roughly zero edge before costs; retired together as a group. | — | [decisions.md § ADR-0009 amendment](decisions.md#adr-0009--accelerated-experiment-matrix) |
| Entry-timing rules that sit out bad days (vol-expansion gates, macro-conditioned IVR, calendar throttles) | killed | After a correctly-specified random-blocking comparison and a multiplicity correction, no tested rule beat blind day-blocking — any apparent protection was an artifact of how the comparison was first set up, not a real timing edge. | — | [issue (withdrawn, disposition in comments)](https://github.com/philipreese/basis/issues/812) |

## ETF / trend (B36, B38)

| Idea | Verdict | Why | Revisit when | Link |
|---|---|---|---|---|
| Fractional-share sizing via the broker API | killed | The broker's API flatly rejects fractional equity orders with no account-setting workaround found; the book sticks to whole shares with a cash remainder. | — | [comment](https://github.com/philipreese/basis/issues/1054#issuecomment-5981719742) |
| Volatility targeting (standalone, scale stock exposure to recent volatility) | weak survivor | Barely clears its own pre-registered kill line over a century of data, but loses decisively to a plain 60/40 mix that needs no signal at all — most of its apparent benefit is just holding less stock on average, not real timing skill. | — | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5981410445) |
| Volatility targeting as an overlay on the B36 trend book | killed | Makes the trend book's held-out risk-adjusted return worse, not better — the trend book already de-risks ahead of real crashes via its own signal, so the overlay mostly just adds cost. | — | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5981410445) |
| Splitting the monthly rebalance into tranches | weak survivor | At today's small stage-1 size, commission drag wipes out the benefit; at roughly ten times that size, two tranches clears the bar (four doesn't) — worth adopting only once the book is much bigger. | B36's stake grows to roughly 10× stage 1 | [comment](https://github.com/philipreese/basis/issues/1054#issuecomment-5983193326) |
| Factor-tilt levers for B36 (momentum, quality, low-volatility, inverse-vol weighting, concentrate-the-winners) | killed | Two tilts lose outright, one wins on return but with a much deeper drawdown (disqualifying on its own), one is a marginal loss, and the one borderline win doesn't repeat in a longer second window. | — | [comment](https://github.com/philipreese/basis/issues/1054#issuecomment-5986501950) |
| A Bitcoin ETF slot for B36 | adopted | A half-weight Bitcoin slot, held only while the same trend rule says "on," cleared its bar in two overlapping windows with a real, mechanism-driven edge; the operator chose the more conservative half-weight version over the full-weight one the study also flagged. A Bitcoin-plus-Ethereum version was tested too and didn't add anything. | — | [comment](https://github.com/philipreese/basis/issues/1054#issuecomment-5987400278) |
| Running B36 on margin leverage | inconclusive | Leverage doesn't meaningfully hurt B36's own risk-adjusted return, but it also never catches up to simply holding more unlevered stock, and amplifies losses in a downturn without buying any extra protection. | a taxable pot exists and B36 has a live record | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5995059080) |
| Running a B36-style book in a taxable account | killed | The rebalance frequency sits right at the edge of wash-sale rules, and modeled tax drag costs a real, ongoing chunk of annual return with no offsetting benefit for this specific book. | — | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5995059080) |
| Turn-of-month calendar effect (hold the market a few days around month-end) | adopted | Beat a matched random-day comparison by a wide margin, survived realistic cost stress, and even beat buy-and-hold on risk-adjusted return while invested only a fraction of the time; now runs as its own paper book. | — | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5982800478) |
| Pre-holiday calendar effect | paper candidate | Also clears its cost-sensitivity tests, with a smaller time-in-market footprint than turn-of-month; not yet built into its own paper book. | — | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5982800478) |
| Closed-end fund discount buying | killed | An early pass that looked promising was driven by a handful of funds with corrupted price data; with those fixed, the result lands right back at a coin-flip against its own benchmark. | — | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5982800478) |

## Stock anomalies

| Idea | Verdict | Why | Revisit when | Link |
|---|---|---|---|---|
| Wide-universe momentum / trend (industries, dual momentum, cross-asset) | inconclusive | Each variant either trades a better return for a much deeper drawdown than a simple 60/40 mix, or loses on risk-adjusted return while being much gentler in drawdowns — none cleanly beats the benchmark on both counts at once. | — | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5981640674) |
| Short-term mean reversion (buying recent dips) | weak survivor | Clears its own low bar (beating sitting in cash) but never comes close to just holding the market, and several variants lost more than buy-and-hold during real crashes — a genuine falling-knife risk, not a free lunch. | — | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5981712745) |
| Long-only merger arbitrage | in progress | No free historical record of deal terms and outcomes exists, so a forward logger now builds one from new SEC filings, under pre-registered rules that score it automatically once 40 cash deals have resolved (roughly a year out). | forward logger reaches its pre-registered verdict | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5982948117) |
| Merger arbitrage, real-money record (MNA and MRGR, ETFs that run the strategy) | weak survivor | Since 2010 the professional version has returned roughly 1–2 points a year more than T-bills before its fees, with drawdowns of up to 17% — a real but thin edge, nowhere near the stock market's return. | — | — |
| S&P 500 index-addition effect | killed | Buying on the announcement is statistically indistinguishable from zero (and mostly one outlier stock); holding through the actual addition is negative after costs — this effect has essentially disappeared since the 2010s. | — | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5982948117) |
| VIX term structure via ETFs | killed | Loses to simply holding the stock market on both risk-adjusted return and worst drawdown, held out. | — | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5984779628) |
| The overnight-only effect (hold stocks overnight, cash during the day) | killed | Earns less per unit of risk than just holding the whole time, and at realistic trade sizes commissions alone eat the entire effect. | — | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5984898503) |
| Insider buying (Form 4 filings) | killed | All three signal variants (officer purchases, insider clusters, "opportunistic" insiders) lose to a liquidity-and-date-matched benchmark after costs, held out — insiders buy stocks that go up in general, and none of the signals beats that baseline. | — | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5988248904) |
| AI/text-scored earnings-release sentiment | killed | Both a modern sentiment model and a classic finance word-list dictionary underperform picking earnings releases at random, held out — text sentiment actively hurt here rather than helping. | — | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5988725855) |
| Pre-FOMC announcement drift | inconclusive | Not cleanly killed, but doesn't beat a time-matched random-day comparison by a meaningful margin since 2010 either; the narrowest version is killed outright. | forward logger reaches its pre-registered verdict | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5984612724) |
| Treasury auction cycle | killed | Every held-out variant and tenor is either a loser or statistically indistinguishable from a matched random-day comparison. | — | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5984612724) |

## Kalshi / prediction markets

| Idea | Verdict | Why | Revisit when | Link |
|---|---|---|---|---|
| Kalshi daily-high-temperature weather markets | killed | Every strategy tested (favorite-buying, longshot-fading, forecast-model edge) loses money or is statistically indistinguishable from zero, in both cities tested, held out. | — | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5983396451) |
| Kalshi S&P 500 daily-range markets (buy the center / buy the tails) | killed | Both framings lose held out; the tail-buying version loses almost everything risked, the same favorite-longshot overpricing pattern found across every market family tested on this issue. | — | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5984348088) |
| Kalshi NFL moneyline mispricing | inconclusive | Too few held-out trades to say anything either way — underpowered, not evidence of no edge. | — | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5984121677) |
| Kalshi MLB moneyline favorite-longshot bias | killed | Tested with real statistical power; Kalshi's own baseball pricing is calibrated closely enough that fading favorites doesn't pay for itself after fees. | — | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5984121677) |
| Kalshi crypto hourly-range markets (buy the calm/favorite bucket) | killed | A real, consistent loser on both Bitcoin and Ethereum under two different ways of picking "the favorite" — the market routinely overprices a confident-looking outcome that isn't actually that likely. | — | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5984121677) |
| Systematic NO-selling (fading longshots) on Kalshi | inconclusive | The crypto and weather legs are killed or too thin to test; the S&P leg has a real-looking positive result built entirely on zero observed losses in a payoff shape that needs far more loss-free trades before that can be trusted. | forward logger reaches its pre-registered verdict | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5984951702) |
| Kalshi economic-release markets vs. consensus (CPI, unemployment, payrolls) | killed | Every strategy loses money held out, decisively — Kalshi's own price is simply a better predictor of these releases than the comparison model used here, matching outside academic findings. | — | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5986741404) |
| Kalshi market-making economics (historical maker P&L on S&P ranges) | inconclusive | The pooled, full-crediting result is weakly positive but not statistically significant; whether a new, small market-maker without queue priority would actually capture a representative share of that edge can't be answered from historical trade data alone. | forward logger reaches its pre-registered verdict | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5987290391) |
| Kalshi market-making, read-only forward simulation | in progress | Running now as a paper simulator; see "Live and running" below. | — | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5994451739) |
| Kalshi gas-price markets (AAA daily national average) | killed | A simple base-rate model lost about 29% of stake per contract held out, no better than random trades in the same markets; the order books hold only a few hundred to about $2,000 per price level anyway. | — | [comment](https://github.com/philipreese/basis/issues/1123#issuecomment-5998469211) |
| Kalshi TSA weekly checkpoint-traffic markets | inconclusive | Lost about 29.5% of stake held out, but the test bet after 6 of the week's 7 daily counts were already public, so it never tested the real opportunity, a mid-week forecast. | untested as a mid-week nowcast; test that version | [comment](https://github.com/philipreese/basis/issues/1123#issuecomment-5998469211) |
| Kalshi Rotten Tomatoes score markets | blocked on data | The most heavily traded niche family, but testing it needs a live scraper of the running review score, which is an engineering project rather than a backtest. | a live score scraper is built | [comment](https://github.com/philipreese/basis/issues/1123#issuecomment-5998469211) |
| Kalshi–Polymarket arbitrage (sanity check 17) | killed | None of the matched market pairs paid after both venues' fees; the one large-looking gap was two differently-settled contracts, not a mispricing. Any real gap would last seconds, so this only works as a bot, and the US version of Polymarket is a separate, likely thinner market than the one measured. | Polymarket's US exchange shows real volume on matched markets | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5996166386) |

## Account and wrapper

| Idea | Verdict | Why | Revisit when | Link |
|---|---|---|---|---|
| What a taxable account would unlock beyond the Roth IRA | killed | Of roughly 25 ideas and variants tested, at most one died specifically because the Roth forbids something; everything else died on lack of edge, decay, data quality or statistical power. A new taxable account isn't justified until a specific idea actually needs one. | — | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5995059080) |

## Small-player edges

| Idea | Verdict | Why | Revisit when | Link |
|---|---|---|---|---|
| Matched betting in Georgia (sportsbook free-bet extraction) | killed | Georgia has no legal sportsbook for the technique to work against — the 2026 legalization bills both died, and offshore books are out of scope as illegal for a Georgia resident to bet on. | Georgia legalizes online sports betting | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5995956077) |
| "+EV" betting via DFS pick'em apps (PrizePicks, Underdog) | blocked on data | No free historical line archive exists to backtest an ongoing edge, and the paid tools that have one require an account this research's rules forbid; a one-time sign-up promo is worth a small amount but isn't a repeatable edge. | a free historical line archive appears | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5995956077) |
| SPAC trust-value arbitrage (sanity check 19) | killed | Today's discount to trust value sits inside the "ordinary, non-crisis" range the literature describes, below the pre-registered bar; the historical record's big returns accrue mostly to IPO-stage warrant buyers, not to someone buying common stock at a discount after the fact. Worth re-checking after a future liquidity dislocation, not a standing harvest today. | SPAC discounts widen past −1.5% median (the SPAC watcher alerts) | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5996043562) |
| Small-cap post-earnings-announcement drift (sanity check 20) | killed | Buying the biggest earnings-day jumps among small caps earned +0.4% over a matched comparison after costs, nowhere near significant, and the biggest losers drifted up just as much, so the signal sorts nothing. Consistent with published evidence that the effect has faded. | — | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5998512981) |
| Georgia tax-deed sales (statutory 20% redemption premium) | weak survivor | Real and set by state law: an owner who buys the property back pays the full winning bid plus 20%, roughly 14–16% after tax. But it means in-person cash auctions, research on every property, and court and attorney costs whenever an owner never buys it back. A part-time real-estate job, passed on for now. | the operator wants a hands-on project | [comment](https://github.com/philipreese/basis/issues/1124#issuecomment-5997965923) |
| Brokerage IRA transfer bonuses and contribution matches | killed | Transfer bonuses work out to roughly 0.1–0.6% a year over their multi-year lock-ups. The one meaningful offer, a 3% match on new contributions, comes from a broker the operator doesn't trust and can end any year. | — | [comment](https://github.com/philipreese/basis/issues/1122#issuecomment-5998064492) |
| Spin-offs (sanity check 21) | inconclusive | The average looks large, but the typical trade earns about 2% and a handful of outliers carry nearly all of it; the spin-off ETF (CSD) beat a matched mid-cap fund by only about half a point a year since 2015. The effect looks mostly decayed. | — | [comment](https://github.com/philipreese/basis/issues/1082#issuecomment-5996224237) |

## Lottery tickets

Hands-off bets with a capped downside and a lopsided upside, meant for a small fixed budget written off on purchase, not as core holdings.

| Idea | Verdict | Why | Revisit when | Link |
|---|---|---|---|---|
| Long-dated far out-of-the-money S&P calls, bought quarterly | inconclusive | The right lottery shape: you lose only the premium, 71% expire worthless, and rare big winners carry the rest. The positive held-out result comes from one bull run, while the earlier window lost, so treat the expected value as roughly break-even to negative. | forward tracker accumulates enough quarters | [comment](https://github.com/philipreese/basis/issues/1127#issuecomment-5998368119) |
| Long-dated far out-of-the-money S&P puts (crash tickets) | killed | All 48 quarterly tickets expired worthless, matching the published finding that crash insurance is persistently overpriced. | — | [comment](https://github.com/philipreese/basis/issues/1127#issuecomment-5998368119) |
| Equity crowdfunding (Reg CF / Reg A+) | killed | Not hands-off: picking deals, a lock-up with no way to sell, and no real-money record of cash returns; failure-rate studies show most deals return nothing. | — | [comment](https://github.com/philipreese/basis/issues/1127#issuecomment-5998368119) |
| A small crypto slot beyond Bitcoin | killed | Adds nothing over the Bitcoin exposure B36 already holds, and today's much larger crypto market weakens the case for another 10–100× run. | — | [comment](https://github.com/philipreese/basis/issues/1127#issuecomment-5998368119) |
| SPAC warrants, bought outright | inconclusive | The evidence for warrant returns comes from buying at the IPO stage, which a retail buyer can't do, and those citations weren't re-verified. | re-verified evidence on post-IPO warrant returns | [comment](https://github.com/philipreese/basis/issues/1127#issuecomment-5998368119) |

## Live and running

These run on paper or read-only today, each waiting on its own kind of evidence before anything changes. The loggers and simulators are plain scheduled scripts with no ongoing AI cost; each one alerts only on a pre-registered verdict, a regime change or its own breakage:

- **B36, the monthly ETF trend book**, now including the half-weight Bitcoin slot — waiting on enough monthly decisions, spanning a real stress episode, to clear its own yardstick (a 60/40 comparison, not the options lab's 30-trade gate).
- **B38, the turn-of-month calendar book** — waiting on the same kind of paper track record; it has no yardstick of its own yet, so it's excluded from any promotion step until one is designed.
- **The Kalshi market-making read-only paper simulator** — polls Kalshi's public market data only, places no real orders, and is waiting out a pre-registered minimum run length before any kill/continue/survive verdict is read as final.
- **The forward logger** — an append-only daily record of the inconclusive ideas scored as they happen (Kalshi S&P NO-selling, pre-FOMC drift, short-term mean reversion, pre-holiday), so a later check rests on evidence no backtest has already seen.
- **The SPAC trust-discount watcher** — weekly; alerts only if SPAC discounts widen into the crisis-style range where the trade historically paid.
- **The merger-arbitrage logger** — daily; builds the missing deal-terms record from new filings and scores itself once 40 cash deals resolve.

Also active: **B37, the wide, far-dated condor paper arm** — the one options packaging that cleared its own bar, running forward as a single-arm hypothesis book, excluded from promotion by design.

## How to add a row

**Check for a real-money record first.** Before building a backtest or a logger for a new idea, look for a fund or ETF that already runs it with real money (as CSD does for spin-offs and MNA for merger arbitrage). That track record is a forward test with real costs, already done: if the professional version barely beats T-bills, a homemade one won't. Build a logger only when no such record exists, the idea has a believable answer to "who pays you, and why would they keep paying?", and it is cheap to run. Every extra logger is another chance for a lucky-looking fluke and another thing to maintain.

New checks get their own GitHub issue, not a comment buried in an existing thread. Once a check has a verdict, add one row to the relevant table above (or a new category if none fits) linking the issue or the comment that carries the result. Every row states its revisit trigger in the "Revisit when" column, or "—" if none exists; a monthly automated check pings the operator with the list.

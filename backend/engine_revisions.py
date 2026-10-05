"""Regime-engine revisions — part of every book's behavior fingerprint (#1049).

A book's evidence era only restarts when its config_hash changes, and engine
CODE sits outside the book's config. So each engine carries a revision number
here, folded into the hash of every book that reads it (book_fingerprint.py):
bump an engine's number when you change what it decides, and every book racing
on it starts a fresh era instead of pooling trades from two different engines.

ENGINE_SOURCE_DIGEST pins a fingerprint of the engine modules' code
(regime.py, regime_variants.py). backend/tests/test_book_fingerprint.py fails
when that code changes until someone updates the digest here, so a behavior
change can't slip through without deciding whether to bump a revision. Data
only — no imports — so seeds, the executor and the backtest can all read it.
"""

# Bump the engine(s) whose decisions a change alters. V0 is regime.py's
# scoring matrix; V1-V6 live in regime_variants.py and also read regime.py's
# catalyst parsing, so a regime.py change may need several bumps.
ENGINE_REVISIONS: dict[str, int] = {
    "V0": 1,
    "V1": 1,
    "V2": 1,
    "V3": 1,
    "V4": 1,
    "V5": 1,
    "V6": 1,
}

# The raced decision-grade engines that vote in the B29 consensus gate
# (#316). V4-V6 are observation-only different-modality lenses; widening the
# electorate to them is a config decision for a future arm, not a default.
CONSENSUS_VARIANTS = ("V0", "V1", "V2", "V3")

# Digest of the engine modules' code, comments and docstrings excluded. Update
# it whenever the pin test reports a change — AFTER bumping ENGINE_REVISIONS
# for any engine whose decisions changed (a pure refactor bumps nothing).
ENGINE_SOURCE_DIGEST = "6ba06a2c94ac3e9b"

# #1054: the monthly ETF trend rules (backend/etf_trend.py) are to a share
# book what an engine is to an options book — code outside the config that
# decides every trade. Same discipline: bump the revision when a change alters
# a signal, a target or an order, then update the digest the pin test reports.
#
# Revision 2 (#1074): the book compounds — investable capital is the whole of
# current equity, no longer min(basis, equity) (operator ruling 2026-10-03).
# That sizing lives in share_book._rebalance_book, outside the digest below,
# but it alters every target, so it bumps the revision all the same. The
# etf_trend.py edit in the same change only ADDS the two signal-day helpers
# the missed-rebalance line reads; no signal, target or order moved there.
#
# #1109 (no bump): target_shares gains an optional slot-weight argument. A
# menu without slot_weights sizes exactly as before, so no existing book's
# targets move; B36's own change (BITB at half a slot) is config, which its
# config_hash already covers. A bump would only restart B38's era, which
# folds this revision in, for nothing.
ETF_TREND_REVISION = 2
ETF_TREND_SOURCE_DIGEST = "4284eb4d57d82be6"

# #1092: the turn-of-month rules (backend/turn_of_month.py) are the second
# share book's engine, same discipline as ETF_TREND_REVISION above. The
# module also calls etf_trend.rebalance_orders/buy_limit/sell_limit for its
# actual order math, so a share book running on turn_of_month reads BOTH
# revisions — book_fingerprint.py folds ETF_TREND_REVISION into its hash too.
TURN_OF_MONTH_REVISION = 1
TURN_OF_MONTH_SOURCE_DIGEST = "77a0aa544e700656"

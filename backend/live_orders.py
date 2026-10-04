"""live_orders.py — the live share-order safety rules, pure (#1065).

No database, broker or clock: plain values in, plain values (or a refusal
reason) out, so every rule is tested on its own. backend/live_executor.py
feeds them and acts on the verdicts.

What each rule protects, in English, next to the code that protects it:

- **Sells before buys.** An IRA cannot borrow. If a buy fills before the sell
  that funds it, the account goes into debit — IBKR may reject the buy, or
  worse, fill it on a loan the account is not allowed to carry. So live mode
  splits a rebalance: the signal evening places the sells only
  (`rebalance_sells`); the buys are sized on a LATER run, after every sell
  is terminal and its fills are booked, from cash that actually exists
  (`size_live_buys`). The paper rebalance (etf_trend.rebalance_orders)
  counts unfilled sells at their limit as funding; this module never does.
- **No order bigger than the stake** (`check_order_caps`). The stake is the
  amount the operator decided they could lose entirely. One order above it
  is a sizing bug, whatever caused it.
- **A run's buys within stake + accrued P&L** (`check_order_caps`) — the
  #1074 sizing (share_book._investable), re-checked independently of the
  sizing that produced the orders.
- **Never sell more than the book holds** (`check_order_caps`): a sell sized
  past the holding would open a short.
- **No debit** (`check_no_debit`): every buy's limit cost plus its previewed
  commission, summed, must fit inside BOTH the book's own cash ledger and
  the broker's reported cash. The whatIf OrderState carries no cash field
  (ib_async 2.1: margin and equity-with-loan figures only), so the broker
  cash read is the debit check; `check_buy_preview` adds the one debit-shaped
  signal the preview does carry — post-trade equity-with-loan below
  post-trade initial margin — and refuses a buy whose preview cannot show
  both figures.
"""

import math

from backend.broker import SharePreview
from backend.etf_trend import (
    COMMISSION_RESERVE_PER_ORDER,
    LIMIT_BAND,
    ShareOrderIntent,
    buy_limit,
    rebalance_orders,
)

_EPS = 1e-9


def rebalance_sells(
    current: dict[str, int], targets: dict[str, int], closes: dict[str, float], cash_symbol: str
) -> list[ShareOrderIntent]:
    """Only the SELL deltas of the paper rebalance — the same symbols, sizes
    and limits etf_trend.rebalance_orders would sell (the sell side does not
    depend on cash), and none of its buys."""
    return [o for o in rebalance_orders(current, targets, closes, 0.0, cash_symbol) if o.side == "SELL"]


def size_live_buys(
    current: dict[str, int],
    targets: dict[str, int],
    closes: dict[str, float],
    budget: float,
    cash_symbol: str,
    band: float = LIMIT_BAND,
    reserve_per_order: float = COMMISSION_RESERVE_PER_ORDER,
) -> list[ShareOrderIntent]:
    """The BUY deltas toward *targets*, funded from *budget* alone — cash that
    exists now, never proceeds still to come. A symbol held above its target
    is not sold here (that was the sell phase's job) and adds nothing.

    When the buys do not fit, the same shrink order as the paper rebalance:
    the cash-leg buy first (it is the residual), then the largest risk buy,
    one share at a time. A non-positive or non-finite budget buys nothing."""
    if not math.isfinite(budget) or budget <= 0:
        return []
    buys = {s: t - current.get(s, 0) for s, t in targets.items() if t - current.get(s, 0) > 0}

    def _cost() -> float:
        n_orders = sum(1 for q in buys.values() if q > 0)
        return sum(q * buy_limit(closes[s], band) for s, q in buys.items()) + reserve_per_order * n_orders

    while buys and _cost() - budget > _EPS:
        if buys.get(cash_symbol, 0) > 0:
            victim = cash_symbol
        else:
            victim = max(buys, key=lambda s: (buys[s] * buy_limit(closes[s], band), s))
        buys[victim] -= 1
        if buys[victim] == 0:
            del buys[victim]
    return [
        ShareOrderIntent(symbol, "BUY", qty, buy_limit(closes[symbol], band), closes[symbol])
        for symbol, qty in sorted(buys.items())
    ]


def check_order_caps(
    intents: list[ShareOrderIntent], stake: float, investable: float, holdings: dict[str, int]
) -> str | None:
    """None when every cap holds, else the first refusal reason. The caller
    refuses the WHOLE batch on any reason: a batch that tripped a cap was
    sized by something nobody meant."""
    if not math.isfinite(stake) or stake <= 0:
        return "the stake is not a positive number"
    if not math.isfinite(investable) or investable <= 0:
        return "stake + accrued P&L is not a positive number"
    for intent in intents:
        notional = intent.quantity * intent.limit_price
        if notional - stake > _EPS:
            return f"{intent.side} {intent.quantity} {intent.symbol} at {intent.limit_price:.2f} exceeds the stake"
        if intent.side == "SELL" and intent.quantity > holdings.get(intent.symbol, 0):
            return f"SELL {intent.quantity} {intent.symbol} exceeds the {holdings.get(intent.symbol, 0)} held"
    buys = sum(i.quantity * i.limit_price for i in intents if i.side == "BUY")
    if buys - investable > _EPS:
        return "the run's buys exceed stake + accrued P&L"
    return None


def check_buy_preview(preview: SharePreview) -> str | None:
    """None when a BUY's preview shows the account solvent after the trade,
    else the refusal reason. Missing figures refuse (fail closed): a preview
    that cannot say whether the buy fits is not a preview that says it does."""
    ewl, init_margin = preview.equity_with_loan_after, preview.init_margin_after
    if ewl is None or init_margin is None:
        return "the preview shows no post-trade equity or margin figure"
    if ewl < init_margin:
        return "the preview shows post-trade equity below initial margin (a margin debit)"
    return None


def check_no_debit(
    buys: list[tuple[ShareOrderIntent, SharePreview]], book_cash: float, broker_cash: float | None
) -> str | None:
    """None when the buys, at their limits plus each one's previewed maximum
    commission (the per-order reserve when the preview gave none), fit in
    both the book's cash and the broker's cash. An unknown broker cash
    refuses."""
    if not buys:
        return None
    if broker_cash is None or not math.isfinite(broker_cash):
        return "broker cash is unknown"
    if not math.isfinite(book_cash):
        return "book cash is not a number"
    cost = sum(
        i.quantity * i.limit_price
        + (p.commission_max if p.commission_max is not None else COMMISSION_RESERVE_PER_ORDER)
        for i, p in buys
    )
    available = min(book_cash, broker_cash)
    if cost - available > _EPS:
        return f"buys cost {cost:,.2f} with commissions, more than the {available:,.2f} cash available (no borrowing)"
    return None

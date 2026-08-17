"""IBKR paper smoke test — converts the Executor-Paper design's medium-confidence
broker claims into ground truth (spec/design/executor-paper.md §2.6, #63).

Run against the BOT's dedicated paper username with IB Gateway up (paper,
port 4002). Never run against a live session: the script refuses any
account id that does not start with 'D' (paper accounts are DU/DF-prefixed).

    pixi run python scripts/ibkr_smoke_test.py safe      # checks (a)(c)(f) — no fills possible
    pixi run python scripts/ibkr_smoke_test.py fill      # check (b)(e) — OPENS a real paper position
    pixi run python scripts/ibkr_smoke_test.py crash     # check (d) step 1 — places order, dies
    pixi run python scripts/ibkr_smoke_test.py reconcile # check (d) step 2 — finds it by orderRef, cleans up

Checks (design §2.6):
  (a) 2-leg XSP vertical BAG at a non-marketable price → Submitted → cancel
  (b) marketable BAG with attached GTC profit-taker → per-leg executions +
      combo orderStatus → child releases on parent fill
  (c) whatIfOrder on the BAG → margin change ≈ (width − credit) × 100,
      commission fields populated or DBL_MAX sentinels
  (d) process killed after placeOrder → next run finds the order by orderRef
  (e) orderRef echoed on every leg execution (observed during (b))
  (f) XSP combo net-price tick increments — is a $0.01 net price rejected?
"""

import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=True)

from ib_async import IB, ComboLeg, Contract, Index, LimitOrder, Option  # noqa: E402

DELAYED = 3
SPREAD_WIDTH = 5.0
CRASH_REF = "basis:SMOKE:crash:open"
DBL_MAX = 1.7e308


@dataclass
class Verticals:
    """A qualified XSP bull-put-spread candidate ~30-45 DTE, ~3% OTM."""

    short_leg: Contract  # SELL — higher strike
    long_leg: Contract  # BUY — lower strike
    expiry: str
    xsp_price: float


def log(check: str, msg: str) -> None:
    print(f"[{check}] {msg}", flush=True)


def connect() -> IB:
    host = os.getenv("IBKR_GATEWAY_HOST", "127.0.0.1")
    port = int(os.getenv("IBKR_GATEWAY_PORT", "4002"))
    client_id = int(os.getenv("IBKR_SMOKE_CLIENT_ID", "19"))
    ib = IB()
    ib.connect(host, port, clientId=client_id, timeout=15)
    ib.reqMarketDataType(DELAYED)
    accounts = ib.managedAccounts()
    if not accounts or not all(a.startswith("D") for a in accounts):
        ib.disconnect()
        raise SystemExit(f"REFUSING TO RUN: managed accounts {accounts} are not all paper (D-prefixed).")
    log("conn", f"Connected. Paper account(s): {accounts}")
    return ib


def pick_verticals(ib: IB) -> Verticals:
    """Pick a ~35 DTE XSP put vertical ~3% below the index."""
    xsp = Index("XSP", "CBOE")
    (xsp,) = ib.qualifyContracts(xsp)
    (ticker,) = ib.reqTickers(xsp)
    price = next((p for p in (ticker.last, ticker.close, ticker.marketPrice()) if p and p > 0), None)
    if not price:
        raise SystemExit("No XSP index price available (162? delayed data not propagated?)")
    log("chain", f"XSP at ~{price:.2f}")

    chains = ib.reqSecDefOptParams("XSP", "", "IND", xsp.conId)
    chain = next((c for c in chains if c.exchange == "SMART"), chains[0])
    today = datetime.now(UTC).date()
    expiry = min(
        (e for e in chain.expirations if (datetime.strptime(e, "%Y%m%d").date() - today).days >= 28),
        default=None,
    )
    if expiry is None:
        raise SystemExit("No expiration ≥28 DTE in the XSP chain")

    target_short = price * 0.97
    strikes = sorted(s for s in chain.strikes if s < target_short)
    short_strike = strikes[-1]
    long_strike = next(s for s in reversed(strikes) if s <= short_strike - SPREAD_WIDTH)
    log("chain", f"Expiry {expiry}, short put {short_strike}, long put {long_strike}")

    legs = [
        Option("XSP", expiry, short_strike, "P", "SMART", currency="USD"),
        Option("XSP", expiry, long_strike, "P", "SMART", currency="USD"),
    ]
    q = ib.qualifyContracts(*legs)
    if len(q) != 2 or not all(c and c.conId for c in q):
        raise SystemExit(f"Could not qualify XSP option legs: {q}")
    return Verticals(short_leg=q[0], long_leg=q[1], expiry=expiry, xsp_price=price)


def make_bag(v: Verticals) -> Contract:
    bag = Contract(secType="BAG", symbol="XSP", currency="USD", exchange="SMART")
    bag.comboLegs = [
        ComboLeg(conId=v.short_leg.conId, ratio=1, action="SELL", exchange="SMART"),
        ComboLeg(conId=v.long_leg.conId, ratio=1, action="BUY", exchange="SMART"),
    ]
    return bag


def wait_status(ib: IB, trade, statuses: set[str], timeout: float = 20.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ib.sleep(0.5)
        if trade.orderStatus.status in statuses:
            break
    return trade.orderStatus.status


def combo_mid(ib: IB, v: Verticals) -> float | None:
    """Net credit mid from leg quotes (negative = credit under BUY convention)."""
    ts, tl = ib.reqTickers(v.short_leg, v.long_leg)

    def mid(t) -> float | None:
        if t.bid and t.ask and t.bid > 0 and t.ask > 0:
            return (t.bid + t.ask) / 2
        return t.close if t.close and t.close > 0 else None

    ms, ml = mid(ts), mid(tl)
    if ms is None or ml is None:
        return None
    return round(-(ms - ml), 2)  # sell short, buy long → net credit as negative price


def check_a_submit_cancel(ib: IB, v: Verticals) -> None:
    bag = make_bag(v)
    # Demand nearly the full width as credit — nobody fills that.
    order = LimitOrder("BUY", 1, -(SPREAD_WIDTH - 0.05))
    order.orderRef = "basis:SMOKE:a:open"
    trade = ib.placeOrder(bag, order)
    status = wait_status(ib, trade, {"Submitted", "PreSubmitted", "Cancelled", "Inactive"})
    log("a", f"non-marketable BAG status: {status} (want Submitted/PreSubmitted)")
    ib.cancelOrder(order)
    status = wait_status(ib, trade, {"Cancelled", "ApiCancelled"})
    log("a", f"after cancel: {status} — {'PASS' if 'ancel' in status else 'FAIL'}")


def check_c_whatif(ib: IB, v: Verticals) -> None:
    bag = make_bag(v)
    mid = combo_mid(ib, v) or -1.0
    state = ib.whatIfOrder(bag, LimitOrder("BUY", 1, mid))
    if state is None:
        log("c", "whatIfOrder returned nothing — FAIL")
        return
    expected = (SPREAD_WIDTH - abs(mid)) * 100
    log("c", f"initMarginChange={state.initMarginChange} maintMarginChange={state.maintMarginChange}")
    log("c", f"expected margin ≈ width−credit = {expected:.0f}")
    for field in ("minCommission", "maxCommission", "commission"):
        val = getattr(state, field, None)
        sentinel = val is not None and abs(float(val or 0)) >= DBL_MAX / 2
        log("c", f"{field}={val}{' (DBL_MAX sentinel)' if sentinel else ''}")
    log("c", f"warningText={state.warningText!r} (adapter must reject preview when non-empty)")


def check_f_ticks(ib: IB, v: Verticals) -> None:
    bag = make_bag(v)
    order = LimitOrder("BUY", 1, -1.01)  # penny net price — allowed or rejected?
    order.orderRef = "basis:SMOKE:f:open"
    trade = ib.placeOrder(bag, order)
    status = wait_status(ib, trade, {"Submitted", "PreSubmitted", "Cancelled", "Inactive"})
    log("f", f"$0.01-increment net price → {status} (Inactive/Cancelled ⇒ penny ticks rejected)")
    if status in {"Submitted", "PreSubmitted"}:
        ib.cancelOrder(order)
        wait_status(ib, trade, {"Cancelled", "ApiCancelled"})
        log("f", "penny net prices ACCEPTED on XSP combos")


def check_b_fill_with_gtc(ib: IB, v: Verticals) -> None:
    """OPENS A REAL PAPER POSITION. Marketable credit-spread entry + GTC profit-taker."""
    mid = combo_mid(ib, v)
    if mid is None:
        raise SystemExit("No leg quotes for marketable order — run during market hours")
    bag = make_bag(v)
    entry_price = round(mid + 0.10, 2)  # give up a dime vs mid → should fill
    entry = LimitOrder("BUY", 1, entry_price, transmit=False)
    entry.orderRef = "basis:SMOKE:b:open"
    entry_trade = ib.placeOrder(bag, entry)

    tp_price = round(mid / 2, 2)  # buy back at half the credit
    tp = LimitOrder("SELL", 1, tp_price, tif="GTC", transmit=True)
    tp.parentId = entry.orderId
    tp.orderRef = "basis:SMOKE:b:tp"
    tp_trade = ib.placeOrder(bag, tp)

    status = wait_status(ib, entry_trade, {"Filled", "Cancelled", "Inactive"}, timeout=120)
    log("b", f"entry status: {status} @ limit {entry_price}")
    if status != "Filled":
        log("b", "entry did not fill — cancelling both; re-run during liquid hours")
        ib.cancelOrder(entry)
        return
    for f in entry_trade.fills:
        log(
            "e",
            f"leg execId={f.execution.execId} conId={f.contract.conId} "
            f"qty={f.execution.shares} px={f.execution.price} orderRef={f.execution.orderRef!r}",
        )
    log("b", f"combo orderStatus filled={entry_trade.orderStatus.filled} (reconcile at BAG level, not leg count)")
    ib.sleep(3)
    log("b", f"profit-taker status: {tp_trade.orderStatus.status} (want PreSubmitted→Submitted after parent fill)")
    log("b", "NOTE: position left open on purpose — close it via the app or let the GTC work")


def check_d_crash(ib: IB, v: Verticals) -> None:
    bag = make_bag(v)
    order = LimitOrder("BUY", 1, -(SPREAD_WIDTH - 0.05))
    order.orderRef = CRASH_REF
    ib.placeOrder(bag, order)
    ib.sleep(2)
    log("d", f"order placed with orderRef={CRASH_REF!r}; dying WITHOUT cancel or disconnect")
    os._exit(1)


def check_d_reconcile(ib: IB) -> None:
    found = False
    open_trades = ib.reqAllOpenOrders()
    for t in open_trades:
        if t.order.orderRef == CRASH_REF:
            found = True
            log("d", f"found by orderRef in reqAllOpenOrders: orderId={t.order.orderId} permId={t.order.permId}")
            ib.cancelOrder(t.order)
            log("d", "cancelled leftover crash order — PASS")
    completed = ib.reqCompletedOrders(apiOnly=True)
    log("d", f"reqCompletedOrders(apiOnly=True) returned {len(completed)} orders (prior-session visibility check)")
    for t in completed:
        if t.order.orderRef == CRASH_REF:
            found = True
            log("d", f"crash ref present in completed orders: status={t.orderStatus.status}")
    if not found:
        log("d", "crash orderRef NOT found — if 'crash' was run this session-gap matters; check reqExecutions fallback")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "safe"
    ib = connect()
    try:
        if mode == "reconcile":
            check_d_reconcile(ib)
            return
        v = pick_verticals(ib)
        if mode == "safe":
            check_a_submit_cancel(ib, v)
            check_c_whatif(ib, v)
            check_f_ticks(ib, v)
        elif mode == "fill":
            check_b_fill_with_gtc(ib, v)
        elif mode == "crash":
            check_d_crash(ib, v)
        else:
            raise SystemExit(f"Unknown mode {mode!r}: use safe | fill | crash | reconcile")
    finally:
        if ib.isConnected():
            ib.disconnect()


if __name__ == "__main__":
    main()

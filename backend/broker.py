"""broker.py — the order-placement adapter over IB Gateway (Executor Paper, #64).

Session-scoped, unlike backend/market_data.py's connect-per-call `_run_ib`:
order placement, fill waiting, and reconciliation need one persistent
ib_async session per nightly run, with live-updating Trade objects. The
session runs on a dedicated thread + event loop; every public method is a
synchronous facade.

Error policy — deliberately the opposite of the data layer: market_data
degrades to None/{} so scans can fall back to stored state, but the order
path NEVER degrades silently. Everything raises a typed BrokerError
subclass; a silent degradation here is how phantom or duplicate orders
happen (spec/design/executor-paper.md §2.1).

Idempotency contract (§2.4): every order carries an `orderRef` the broker
echoes back on open orders and executions. `reconcile()` must run before
any placement (enforced); a ref already OPEN or FILLED at the broker — or
already placed this session — raises DuplicateOrderRefError instead of
resubmitting.

Paper-only guard: the session refuses to open unless every managed account
is D-prefixed (IBKR paper accounts). Executor (Live) will replace this with
the trading-mode mechanism (ADR-0006).
"""

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Self

from backend.market_data import CONNECT_TIMEOUT, _gateway_config, parse_occ_symbol

_DELAYED = 3
CALL_TIMEOUT = 60.0  # seconds for any single broker call
_DBL_MAX_SENTINEL = 1.7e308 / 2  # IBKR returns DBL_MAX for "unavailable"

TERMINAL_STATUSES = frozenset({"Filled", "Cancelled", "ApiCancelled", "Inactive"})


# ---------------------------------------------------------------------------
# Errors — the order path never degrades silently
# ---------------------------------------------------------------------------


class BrokerError(RuntimeError):
    """Base class for all order-path failures."""


class ConnectionFailedError(BrokerError):
    pass


class PaperAccountRequiredError(BrokerError):
    pass


class SessionNotOpenError(BrokerError):
    pass


class NotReconciledError(BrokerError):
    pass


class ContractQualificationError(BrokerError):
    pass


class DuplicateOrderRefError(BrokerError):
    pass


class PreviewRejectedError(BrokerError):
    pass


class UnknownOrderError(BrokerError):
    pass


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


class RefState(str, Enum):
    UNKNOWN = "UNKNOWN"  # not found at the broker — never submitted (or expired server-side)
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class SpreadOrder:
    """A multi-leg spread as ONE native combo order.

    legs: (occ_symbol, action, ratio) — action is the leg's BUY/SELL under
    the combo's BUY convention; net_limit_price is signed (negative = net
    credit received).
    """

    legs: tuple[tuple[str, str, int], ...]
    quantity: int
    net_limit_price: float
    underlying: str
    exchange: str = "SMART"


@dataclass(frozen=True)
class PlacedOrder:
    order_id: int
    perm_id: int | None
    ref: str
    status: str


@dataclass(frozen=True)
class FillInfo:
    exec_id: str
    con_id: int
    side: str
    quantity: float
    price: float
    order_ref: str
    commission: float | None


@dataclass(frozen=True)
class FillResult:
    status: str
    terminal: bool
    filled: float  # combo units at the BAG orderStatus level — never leg counts
    avg_fill_price: float
    fills: tuple[FillInfo, ...]


@dataclass(frozen=True)
class MarginPreview:
    init_margin_change: float | None
    maint_margin_change: float | None
    commission_min: float | None  # None = DBL_MAX sentinel / unavailable
    commission_max: float | None


@dataclass(frozen=True)
class OpenOrderInfo:
    order_ref: str
    order_id: int
    perm_id: int | None
    status: str


@dataclass(frozen=True)
class LegPosition:
    con_id: int
    symbol: str
    sec_type: str  # a non-OPT row here is a No-Stock P1 (UNEXPECTED_INSTRUMENT)
    position: float
    avg_cost: float
    occ_symbol: str | None = None  # for OPT rows: the canonical cross-system key


@dataclass(frozen=True)
class ReconcileReport:
    states: dict[str, RefState]
    broker_refs: frozenset[str] = field(default_factory=frozenset)  # every ref seen at the broker

    def state(self, ref: str) -> RefState:
        return self.states.get(ref, RefState.UNKNOWN)


def _default_ib_factory() -> Any:
    from ib_async import IB

    return IB()


class _LoopThread:
    """A dedicated event loop on its own thread; sync callers submit coroutines."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="broker-ib-loop")
        self._thread.start()

    def run(self, coro: Any, timeout: float = CALL_TIMEOUT) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()


class BrokerSession:
    """One connected session per nightly run. Use as a context manager."""

    def __init__(self, ib_factory: Callable[[], Any] | None = None) -> None:
        self._ib_factory = ib_factory or _default_ib_factory
        self._loop: _LoopThread | None = None
        self._ib: Any = None
        self._reconciled = False
        self._last_report: ReconcileReport | None = None
        self._trades: dict[int, Any] = {}
        self._session_refs: set[str] = set()

    # -- lifecycle ----------------------------------------------------------

    def open(self) -> None:
        host, port, client_id = _gateway_config()
        self._loop = _LoopThread()
        self._ib = self._ib_factory()

        async def _connect() -> None:
            await asyncio.wait_for(self._ib.connectAsync(host, port, clientId=client_id), CONNECT_TIMEOUT)
            self._ib.reqMarketDataType(_DELAYED)
            accounts = list(self._ib.managedAccounts() or [])
            if not accounts or not all(a.startswith("D") for a in accounts):
                raise PaperAccountRequiredError(
                    f"Managed accounts {accounts} are not all paper (D-prefixed) — refusing to trade"
                )

        try:
            self._loop.run(_connect(), CONNECT_TIMEOUT + 10)
        except BrokerError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise ConnectionFailedError(f"Could not open IB Gateway session: {exc}") from exc

    def close(self) -> None:
        if self._loop is not None:
            if self._ib is not None:

                async def _disconnect() -> None:
                    self._ib.disconnect()

                try:
                    self._loop.run(_disconnect(), 10)
                except Exception:
                    pass  # closing must never raise past a failed disconnect
            self._loop.stop()
        self._loop = None
        self._ib = None
        self._reconciled = False

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- guards -------------------------------------------------------------

    def _require_open(self) -> None:
        if self._loop is None or self._ib is None:
            raise SessionNotOpenError("BrokerSession is not open")

    def _require_reconciled(self) -> None:
        if not self._reconciled:
            raise NotReconciledError("reconcile() must run before any order placement")

    def _guard_duplicate(self, ref: str) -> None:
        if ref in self._session_refs:
            raise DuplicateOrderRefError(f"orderRef {ref!r} already placed this session")
        state = self._last_report.state(ref) if self._last_report else RefState.UNKNOWN
        if state in (RefState.OPEN, RefState.FILLED):
            raise DuplicateOrderRefError(f"orderRef {ref!r} already {state.value} at the broker — not resubmitting")

    # -- reconciliation -----------------------------------------------------

    def reconcile(self, refs: list[str], since: str | None = None) -> ReconcileReport:
        """Match our order refs against broker state. MUST precede placement.

        since: optional 'yyyymmdd hh:mm:ss' lower bound for the execution
        sweep (reqExecutions only returns current-day fills anyway — the
        weekly Flex audit covers longer horizons, #74).
        """
        self._require_open()

        async def _op() -> ReconcileReport:
            from ib_async import ExecutionFilter

            open_trades = await self._ib.reqAllOpenOrdersAsync()
            completed = await self._ib.reqCompletedOrdersAsync(apiOnly=True)
            exec_filter = ExecutionFilter(time=since or "")
            executions = await self._ib.reqExecutionsAsync(exec_filter)

            states: dict[str, RefState] = dict.fromkeys(refs, RefState.UNKNOWN)
            seen: set[str] = set()

            for t in completed:
                ref = getattr(t.order, "orderRef", "") or ""
                if not ref:
                    continue
                seen.add(ref)
                status = t.orderStatus.status
                if ref in states:
                    states[ref] = RefState.FILLED if status == "Filled" else RefState.CANCELLED
            for f in executions:
                ref = getattr(f.execution, "orderRef", "") or ""
                if not ref:
                    continue
                seen.add(ref)
                if ref in states:
                    states[ref] = RefState.FILLED
            # Open orders win over completed/execution evidence: partially
            # filled but still working means OPEN for resubmission purposes.
            for t in open_trades:
                ref = getattr(t.order, "orderRef", "") or ""
                if not ref:
                    continue
                seen.add(ref)
                if ref in states:
                    states[ref] = RefState.OPEN
            return ReconcileReport(states=states, broker_refs=frozenset(seen))

        report = self._loop.run(_op())
        self._reconciled = True
        self._last_report = report
        return report

    # -- contract construction ---------------------------------------------

    async def _build_bag(self, spread: SpreadOrder) -> Any:
        from ib_async import ComboLeg, Contract, Option

        options = []
        for occ, _action, _ratio in spread.legs:
            parsed = parse_occ_symbol(occ)
            if parsed is None:
                raise ContractQualificationError(f"Invalid OCC symbol {occ!r}")
            options.append(
                Option(
                    parsed["underlying"],
                    parsed["expiration"],
                    parsed["strike"],
                    parsed["right"],
                    "SMART",
                    currency="USD",
                )
            )
        qualified = await self._ib.qualifyContractsAsync(*options)
        combo_legs = []
        for (occ, action, ratio), contract in zip(spread.legs, qualified, strict=True):
            if contract is None or not contract.conId:
                raise ContractQualificationError(f"Could not qualify {occ!r} (expired or unknown contract)")
            combo_legs.append(ComboLeg(conId=contract.conId, ratio=ratio, action=action, exchange="SMART"))
        bag = Contract(secType="BAG", symbol=spread.underlying, currency="USD", exchange=spread.exchange)
        bag.comboLegs = combo_legs
        return bag

    # -- preview ------------------------------------------------------------

    def preview_spread(self, spread: SpreadOrder) -> MarginPreview:
        """whatIfOrder sanity gate. Raises PreviewRejectedError on any warning —
        whatIf results with a non-empty warningText can be wrong (§2.2)."""
        self._require_open()

        async def _op() -> MarginPreview:
            from ib_async import LimitOrder

            bag = await self._build_bag(spread)
            state = await self._ib.whatIfOrderAsync(bag, LimitOrder("BUY", spread.quantity, spread.net_limit_price))
            if state is None:
                raise PreviewRejectedError("whatIfOrder returned no order state")
            warning = getattr(state, "warningText", "") or ""
            if warning:
                raise PreviewRejectedError(f"whatIfOrder warning: {warning}")
            return MarginPreview(
                init_margin_change=_money(state.initMarginChange),
                maint_margin_change=_money(state.maintMarginChange),
                commission_min=_money(getattr(state, "minCommission", None)),
                commission_max=_money(getattr(state, "maxCommission", None)),
            )

        return self._loop.run(_op())

    # -- placement ----------------------------------------------------------

    def place_spread(self, spread: SpreadOrder, ref: str, profit_target_price: float | None = None) -> PlacedOrder:
        """Submit the combo entry (DAY limit). With profit_target_price, an
        attached GTC child rests at IB and releases on parent fill (§2.2)."""
        self._require_open()
        self._require_reconciled()
        self._guard_duplicate(ref)

        async def _op() -> PlacedOrder:
            from ib_async import LimitOrder

            bag = await self._build_bag(spread)
            entry = LimitOrder(
                "BUY",
                spread.quantity,
                spread.net_limit_price,
                tif="DAY",
                orderRef=ref,
                transmit=profit_target_price is None,
            )
            trade = self._ib.placeOrder(bag, entry)
            self._trades[entry.orderId] = trade
            if profit_target_price is not None:
                child = LimitOrder(
                    "SELL",
                    spread.quantity,
                    profit_target_price,
                    tif="GTC",
                    orderRef=f"{ref}:tp",
                    transmit=True,
                )
                child.parentId = entry.orderId
                self._trades[child.orderId] = self._ib.placeOrder(bag, child)
            return PlacedOrder(
                order_id=entry.orderId,
                perm_id=entry.permId or None,
                ref=ref,
                status=trade.orderStatus.status,
            )

        placed = self._loop.run(_op())
        self._session_refs.add(ref)
        return placed

    def close_spread(self, spread: SpreadOrder, ref: str) -> PlacedOrder:
        """SELL the same bag that opened the position (DAY limit, no child).

        The caller supplies the escalation-ladder rung as net_limit_price —
        ladder logic lives in the pipeline (spec/supervision.md), not here.
        Closes run even under HALT_ENTRIES (exits are risk-reducing), but the
        reconcile-first and duplicate-ref guards still apply."""
        self._require_open()
        self._require_reconciled()
        self._guard_duplicate(ref)

        async def _op() -> PlacedOrder:
            from ib_async import LimitOrder

            bag = await self._build_bag(spread)
            order = LimitOrder("SELL", spread.quantity, spread.net_limit_price, tif="DAY", orderRef=ref, transmit=True)
            trade = self._ib.placeOrder(bag, order)
            self._trades[order.orderId] = trade
            return PlacedOrder(
                order_id=order.orderId, perm_id=order.permId or None, ref=ref, status=trade.orderStatus.status
            )

        placed = self._loop.run(_op())
        self._session_refs.add(ref)
        return placed

    def cancel_by_ref(self, ref: str) -> bool:
        """Cancel a resting order by its orderRef; True if it was found.

        The in-memory cancel() only knows orders placed THIS session — a GTC
        profit-taker child (#258) rests across sessions, so it can only be
        reached through reqAllOpenOrders, the same durable key reconcile uses.
        """
        self._require_open()

        async def _op() -> bool:
            open_trades = await self._ib.reqAllOpenOrdersAsync()
            for t in open_trades:
                if (getattr(t.order, "orderRef", "") or "") == ref:
                    self._ib.cancelOrder(t.order)
                    return True
            return False

        return self._loop.run(_op())

    def cancel(self, order_id: int) -> None:
        self._require_open()
        trade = self._trades.get(order_id)
        if trade is None:
            raise UnknownOrderError(f"No trade tracked for orderId {order_id}")

        async def _op() -> None:
            self._ib.cancelOrder(trade.order)

        self._loop.run(_op())

    def wait_for_terminal(self, order_id: int, timeout_s: float = 120.0) -> FillResult:
        """Poll a tracked trade until it reaches a terminal status or the
        timeout lapses (terminal=False — the caller decides to cancel; entry
        orders never rest overnight, UNFILLED_ENTRY)."""
        self._require_open()
        trade = self._trades.get(order_id)
        if trade is None:
            raise UnknownOrderError(f"No trade tracked for orderId {order_id}")

        async def _op() -> FillResult:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_s
            while trade.orderStatus.status not in TERMINAL_STATUSES and loop.time() < deadline:
                await asyncio.sleep(0.25)
            status = trade.orderStatus.status
            fills = tuple(_fill_info(f) for f in trade.fills)
            return FillResult(
                status=status,
                terminal=status in TERMINAL_STATUSES,
                filled=float(trade.orderStatus.filled),
                avg_fill_price=float(trade.orderStatus.avgFillPrice or 0.0),
                fills=fills,
            )

        return self._loop.run(_op(), timeout_s + CALL_TIMEOUT)

    # -- state views --------------------------------------------------------

    def open_orders(self) -> list[OpenOrderInfo]:
        self._require_open()

        async def _op() -> list[OpenOrderInfo]:
            trades = await self._ib.reqAllOpenOrdersAsync()
            return [
                OpenOrderInfo(
                    order_ref=getattr(t.order, "orderRef", "") or "",
                    order_id=t.order.orderId,
                    perm_id=t.order.permId or None,
                    status=t.orderStatus.status,
                )
                for t in trades
            ]

        return self._loop.run(_op())

    def positions(self) -> list[LegPosition]:
        """Aggregate broker positions — the reconciliation engine's ground
        truth for quantities, and the No-Stock scan's input."""
        self._require_open()

        async def _op() -> list[LegPosition]:
            rows = await self._ib.reqPositionsAsync()
            return [
                LegPosition(
                    con_id=p.contract.conId,
                    symbol=p.contract.symbol,
                    sec_type=p.contract.secType,
                    position=float(p.position),
                    avg_cost=float(p.avgCost),
                    occ_symbol=_occ_from_contract(p.contract),
                )
                for p in rows
                if p.position
            ]

        return self._loop.run(_op())

    def executions(self, since: str | None = None) -> list[FillInfo]:
        """Today's executions (reqExecutions is current-day-only; the weekly
        Flex audit #74 covers longer horizons). Feeds missed-fill backfill."""
        self._require_open()

        async def _op() -> list[FillInfo]:
            from ib_async import ExecutionFilter

            fills = await self._ib.reqExecutionsAsync(ExecutionFilter(time=since or ""))
            return [_fill_info(f) for f in fills]

        return self._loop.run(_op())


def _money(value: Any) -> float | None:
    """Parse IBKR money fields ('123.45 USD', floats, DBL_MAX sentinels)."""
    if value in (None, ""):
        return None
    try:
        number = float(str(value).split()[0])
    except ValueError:
        return None
    return None if abs(number) >= _DBL_MAX_SENTINEL else number


def _fill_info(f: Any) -> FillInfo:
    return FillInfo(
        exec_id=f.execution.execId,
        con_id=f.contract.conId,
        side=f.execution.side,
        quantity=float(f.execution.shares),
        price=float(f.execution.price),
        order_ref=getattr(f.execution, "orderRef", "") or "",
        commission=(
            float(f.commissionReport.commission)
            if getattr(f, "commissionReport", None) and f.commissionReport.commission
            else None
        ),
    )


def _occ_from_contract(contract: Any) -> str | None:
    """OCC symbol for an option contract row, None for non-options."""
    if getattr(contract, "secType", "") != "OPT":
        return None
    from backend.market_data import format_occ_symbol

    raw = str(getattr(contract, "lastTradeDateOrContractMonth", "") or "")
    if len(raw) != 8:
        return None
    expiration = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    option_type = "CALL" if getattr(contract, "right", "") in ("C", "CALL") else "PUT"
    return format_occ_symbol(contract.symbol, expiration, option_type, float(contract.strike))

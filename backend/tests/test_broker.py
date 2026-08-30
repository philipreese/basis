"""Tests for the broker adapter (backend/broker.py, #64).

All broker I/O is faked at the ib_async surface — no network. The FakeIB
implements exactly the methods BrokerSession calls, using the real ib_async
data classes (Contract, LimitOrder, ...) so contract-construction assertions
run against the true shapes.
"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from eventkit import Event

from backend.broker import (
    BrokerSession,
    ContractQualificationError,
    DuplicateOrderRefError,
    NotReconciledError,
    PaperAccountRequiredError,
    PreviewRejectedError,
    RefState,
    SessionNotOpenError,
    SpreadOrder,
    UnknownOrderError,
    _money,
)

BULL_PUT = SpreadOrder(
    legs=(("XSP261218P00610000", "SELL", 1), ("XSP261218P00605000", "BUY", 1)),
    quantity=1,
    net_limit_price=-1.25,
    underlying="XSP",
)

CONDOR = SpreadOrder(
    legs=(
        ("XSP261218P00610000", "SELL", 1),
        ("XSP261218P00605000", "BUY", 1),
        ("XSP261218C00680000", "SELL", 1),
        ("XSP261218C00685000", "BUY", 1),
    ),
    quantity=1,
    net_limit_price=-2.10,
    underlying="XSP",
)


class FakeTrade:
    def __init__(self, contract, order):
        self.contract = contract
        self.order = order
        self.orderStatus = SimpleNamespace(
            status="Submitted", filled=0.0, remaining=order.totalQuantity, avgFillPrice=0.0
        )
        self.fills = []


class FakeIB:
    def __init__(self, accounts=("DUR925279",), qualify_ok=True):
        # #823: the real IB exposes errorEvent as an eventkit Event; open()
        # subscribes to it for the duration of the connect attempt.
        self.errorEvent = Event("errorEvent")
        self._accounts = list(accounts)
        self._qualify_ok = qualify_ok
        self._next_order_id = 100
        self.connected = False
        self.market_data_type = None
        self.placed: list[FakeTrade] = []
        self.cancelled: list[int] = []
        self.open_trades: list = []
        self.completed_trades: list = []
        self.executions: list = []
        self.session_fills: list = []  # what fills() serves; #895
        self.fills_calls = 0
        self.position_rows: list = []
        self.what_if_state = SimpleNamespace(
            initMarginChange="375.0",
            maintMarginChange="375.0",
            minCommission=1.1,
            maxCommission=2.3,
            warningText="",
        )
        # #627: a stand-in for ib_async's real Wrapper — reqCompletedOrdersAsync
        # below fires it per completed trade, same as the real client does,
        # so the capture shim has something to intercept.
        self.wrapper = SimpleNamespace(completedOrder=lambda contract, order, orderState: None)

    async def connectAsync(self, host, port, clientId):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def reqMarketDataType(self, mdt):
        self.market_data_type = mdt

    def managedAccounts(self):
        return list(self._accounts)

    async def qualifyContractsAsync(self, *contracts):
        for i, c in enumerate(contracts):
            c.conId = 0 if not self._qualify_ok else 1000 + i
        return list(contracts)

    def placeOrder(self, contract, order):
        if not order.orderId:
            order.orderId = self._next_order_id
            self._next_order_id += 1
        order.permId = 90000 + order.orderId
        trade = FakeTrade(contract, order)
        self.placed.append(trade)
        return trade

    def cancelOrder(self, order):
        self.cancelled.append(order.orderId)
        for t in self.placed:
            if t.order.orderId == order.orderId:
                t.orderStatus.status = "Cancelled"

    async def reqAllOpenOrdersAsync(self):
        return list(self.open_trades)

    async def reqCompletedOrdersAsync(self, apiOnly=True):
        # #627: fire wrapper.completedOrder per trade, same as the real
        # ib_async client streaming responses into it — this is what the
        # capture shim actually intercepts. A trade with no orderState set
        # gets an empty completedStatus, matching "nothing captured" for
        # tests that don't care about rejection text.
        for t in self.completed_trades:
            order_state = getattr(t, "orderState", None) or SimpleNamespace(
                status=t.orderStatus.status, completedStatus="", completedTime="", warningText=""
            )
            self.wrapper.completedOrder(getattr(t, "contract", None), t.order, order_state)
        return list(self.completed_trades)

    async def reqExecutionsAsync(self, exec_filter=None):
        return list(self.executions)

    def fills(self):
        # #895: the wrapper's canonical per-execId fills, which is where
        # ib_async pairs late CommissionReport messages in place.
        self.fills_calls += 1
        return list(self.session_fills)

    async def whatIfOrderAsync(self, contract, order):
        self.what_if_order = order
        return self.what_if_state

    async def reqPositionsAsync(self):
        return list(self.position_rows)


@pytest.fixture
def fake_ib():
    return FakeIB()


@pytest.fixture
def session(fake_ib):
    s = BrokerSession(ib_factory=lambda: fake_ib)
    s.open()
    yield s
    s.close()


@pytest.fixture
def reconciled(session):
    session.reconcile([])
    return session


class TestLifecycle:
    def test_open_connects_and_sets_delayed_data(self, session, fake_ib):
        assert fake_ib.connected is True
        assert fake_ib.market_data_type == 3

    def test_refuses_non_paper_accounts(self):
        live_ib = FakeIB(accounts=("U1234567",))
        s = BrokerSession(ib_factory=lambda: live_ib)
        with pytest.raises(PaperAccountRequiredError):
            s.open()
        assert live_ib.connected is False  # session closed after the refusal

    def test_refuses_empty_account_list(self):
        s = BrokerSession(ib_factory=lambda: FakeIB(accounts=()))
        with pytest.raises(PaperAccountRequiredError):
            s.open()

    def test_methods_require_open_session(self):
        s = BrokerSession(ib_factory=FakeIB)
        with pytest.raises(SessionNotOpenError):
            s.reconcile([])
        with pytest.raises(SessionNotOpenError):
            s.place_spread(BULL_PUT, "basis:B01:o1:open")

    def test_context_manager_closes(self, fake_ib):
        with BrokerSession(ib_factory=lambda: fake_ib) as s:
            assert fake_ib.connected is True
            s.reconcile([])
        assert fake_ib.connected is False


class TestConnectRetry:
    """#785: a single connect attempt can lose the race against IB Gateway's
    own login/config window — open() must retry the handshake before
    declaring the session unreachable, and the resulting error must name a
    real cause even for exceptions (asyncio.TimeoutError) whose str() is
    empty."""

    class FlakyFakeIB(FakeIB):
        """connectAsync fails *fail_times* times before succeeding (or
        forever, if fail_times >= the retry budget)."""

        def __init__(self, fail_times: int, exc_factory=lambda: TimeoutError(), **kwargs):
            super().__init__(**kwargs)
            self.fail_times = fail_times
            self.exc_factory = exc_factory
            self.connect_attempts = 0

        async def connectAsync(self, host, port, clientId):
            self.connect_attempts += 1
            if self.connect_attempts <= self.fail_times:
                raise self.exc_factory()
            await super().connectAsync(host, port, clientId)

    @pytest.fixture(autouse=True)
    def _no_retry_delay(self, monkeypatch):
        from backend import market_data

        monkeypatch.setattr(market_data, "CONNECT_RETRY_DELAY_SECONDS", 0.0)

    def test_open_succeeds_after_a_transient_connect_failure(self):
        flaky = self.FlakyFakeIB(fail_times=1)
        s = BrokerSession(ib_factory=lambda: flaky)
        s.open()
        try:
            assert flaky.connected is True
            assert flaky.connect_attempts == 2
        finally:
            s.close()

    def test_open_exhausts_retries_then_raises_with_a_nonempty_reason_naming_the_cause(self):
        from backend.broker import ConnectionFailedError
        from backend.market_data import CONNECT_RETRY_ATTEMPTS

        flaky = self.FlakyFakeIB(fail_times=CONNECT_RETRY_ATTEMPTS)  # every attempt fails
        s = BrokerSession(ib_factory=lambda: flaky)
        with pytest.raises(ConnectionFailedError) as exc_info:
            s.open()
        message = str(exc_info.value)
        assert message != "Could not open IB Gateway session: "  # the #785 bug, verbatim
        assert "TimeoutError" in message
        assert flaky.connect_attempts == CONNECT_RETRY_ATTEMPTS
        assert flaky.connected is False  # closed after the exhausted retry

    def test_retry_is_not_applied_to_the_post_connect_paper_account_check(self):
        # #785: only the handshake attempt itself retries — a distinct
        # failure category (non-paper account) must still fail fast, not
        # get folded into the connect-retry budget.
        live_ib = FakeIB(accounts=("U1234567",))
        s = BrokerSession(ib_factory=lambda: live_ib)
        with pytest.raises(PaperAccountRequiredError):
            s.open()
        assert live_ib.connected is False


class TestConnectApiErrorCapture:
    """#823: the terminal connect exception can be a bare TimeoutError while
    the REAL cause (Error 10141: paper-trading disclaimer not accepted)
    arrives only on IB.errorEvent before the peer hangs up. open() must
    capture those events and carry them on ConnectionFailedError."""

    DISCLAIMER_MSG = "Paper trading disclaimer must first be accepted for API connection."

    class ErroringFakeIB(FakeIB):
        """connectAsync emits an API error event, then fails like 2026-08-24:
        the gateway logs 10141, closes the connection, and the client's own
        terminal exception is an anonymous TimeoutError."""

        async def connectAsync(self, host, port, clientId):
            self.errorEvent.emit(-1, 10141, TestConnectApiErrorCapture.DISCLAIMER_MSG, None)
            raise TimeoutError()

    @pytest.fixture(autouse=True)
    def _no_retry_delay(self, monkeypatch):
        from backend import market_data

        monkeypatch.setattr(market_data, "CONNECT_RETRY_DELAY_SECONDS", 0.0)

    def test_failed_connect_carries_captured_api_errors(self):
        from backend.broker import ConnectionFailedError

        erroring = self.ErroringFakeIB()
        s = BrokerSession(ib_factory=lambda: erroring)
        with pytest.raises(ConnectionFailedError) as exc_info:
            s.open()
        assert (10141, self.DISCLAIMER_MSG) in exc_info.value.api_errors
        assert len(erroring.errorEvent) == 0  # detached on the failure path

    def test_failed_connect_with_no_api_errors_carries_an_empty_tuple(self):
        from backend.broker import ConnectionFailedError
        from backend.market_data import CONNECT_RETRY_ATTEMPTS

        flaky = TestConnectRetry.FlakyFakeIB(fail_times=CONNECT_RETRY_ATTEMPTS)
        s = BrokerSession(ib_factory=lambda: flaky)
        with pytest.raises(ConnectionFailedError) as exc_info:
            s.open()
        assert exc_info.value.api_errors == ()

    def test_handler_detached_after_successful_open(self, fake_ib):
        s = BrokerSession(ib_factory=lambda: fake_ib)
        s.open()
        try:
            assert len(fake_ib.errorEvent) == 0  # connect-window listener only
        finally:
            s.close()

    def test_needs_human_mapping_classifies_10141_only(self):
        from backend.broker import NEEDS_HUMAN_BROKER_ERRORS

        assert "paper-trading disclaimer" in NEEDS_HUMAN_BROKER_ERRORS[10141]
        # Connectivity-lost codes recover on their own — a retry can clear
        # them, so they must never be classified as needs-a-human.
        assert 1100 not in NEEDS_HUMAN_BROKER_ERRORS
        assert 2110 not in NEEDS_HUMAN_BROKER_ERRORS


class TestReconcile:
    def test_placement_before_reconcile_is_refused(self, session):
        with pytest.raises(NotReconciledError):
            session.place_spread(BULL_PUT, "basis:B01:o1:open")

    def test_states_mapped_by_order_ref(self, session, fake_ib):
        fake_ib.open_trades = [
            SimpleNamespace(order=SimpleNamespace(orderRef="ref-open"), orderStatus=SimpleNamespace(status="Submitted"))
        ]
        fake_ib.completed_trades = [
            SimpleNamespace(order=SimpleNamespace(orderRef="ref-filled"), orderStatus=SimpleNamespace(status="Filled")),
            SimpleNamespace(
                order=SimpleNamespace(orderRef="ref-cancelled"), orderStatus=SimpleNamespace(status="Cancelled")
            ),
        ]
        report = session.reconcile(["ref-open", "ref-filled", "ref-cancelled", "ref-ghost"])
        assert report.state("ref-open") is RefState.OPEN
        assert report.state("ref-filled") is RefState.FILLED
        assert report.state("ref-cancelled") is RefState.CANCELLED
        assert report.state("ref-ghost") is RefState.UNKNOWN

    def test_execution_evidence_marks_filled(self, session, fake_ib):
        fake_ib.executions = [SimpleNamespace(execution=SimpleNamespace(orderRef="ref-exec"))]
        report = session.reconcile(["ref-exec"])
        assert report.state("ref-exec") is RefState.FILLED

    def test_executions_never_overwrite_a_cancelled_verdict(self, session, fake_ib):
        # Audit II R2 (#406): a same-day partial-fill-then-cancel has BOTH a
        # Cancelled completed-order verdict and executions. FILLED here would
        # book full-size cash for a partial — the PARTIAL latch's scenario.
        fake_ib.completed_trades = [
            SimpleNamespace(
                order=SimpleNamespace(orderRef="ref-partial"), orderStatus=SimpleNamespace(status="Cancelled")
            )
        ]
        fake_ib.executions = [SimpleNamespace(execution=SimpleNamespace(orderRef="ref-partial"))]
        report = session.reconcile(["ref-partial"])
        assert report.state("ref-partial") is RefState.CANCELLED

    def test_broker_refs_include_unrequested(self, session, fake_ib):
        fake_ib.open_trades = [
            SimpleNamespace(
                order=SimpleNamespace(orderRef="ref-orphan"), orderStatus=SimpleNamespace(status="Submitted")
            )
        ]
        report = session.reconcile([])
        assert "ref-orphan" in report.broker_refs

    def test_rejected_completed_order_carries_its_reason(self, session, fake_ib):
        # #627: a real broker rejection ('Rejected by System: Guaranteed-to-
        # Lose combination orders are not allowed') otherwise reads as a
        # bare CANCELLED — the capture shim recovers OrderState.
        # completedStatus, which ib_async's own Wrapper.completedOrder
        # discards.
        fake_ib.completed_trades = [
            SimpleNamespace(
                order=SimpleNamespace(orderRef="ref-rejected"),
                orderStatus=SimpleNamespace(status="Cancelled"),
                orderState=SimpleNamespace(
                    completedStatus="Rejected by System: Guaranteed-to-Lose combination orders are not allowed",
                    completedTime="20260821  09:00:03",
                    warningText="",
                ),
            )
        ]
        report = session.reconcile(["ref-rejected"])
        assert report.state("ref-rejected") is RefState.CANCELLED  # the RefState itself is unchanged
        assert (
            report.rejection_reason("ref-rejected")
            == "Rejected by System: Guaranteed-to-Lose combination orders are not allowed"
        )

    def test_plain_cancel_carries_no_rejection_reason(self, session, fake_ib):
        # A DAY-expired or operator-cancelled order's completedStatus never
        # starts with "Rejected" — must not be misread as a broker refusal.
        fake_ib.completed_trades = [
            SimpleNamespace(
                order=SimpleNamespace(orderRef="ref-expired"),
                orderStatus=SimpleNamespace(status="Cancelled"),
                orderState=SimpleNamespace(completedStatus="Cancelled", completedTime="", warningText=""),
            )
        ]
        report = session.reconcile(["ref-expired"])
        assert report.state("ref-expired") is RefState.CANCELLED
        assert report.rejection_reason("ref-expired") is None

    def test_a_filled_order_carries_no_rejection_reason_even_with_completed_status_set(self, session, fake_ib):
        fake_ib.completed_trades = [
            SimpleNamespace(
                order=SimpleNamespace(orderRef="ref-filled-ok"),
                orderStatus=SimpleNamespace(status="Filled"),
                orderState=SimpleNamespace(completedStatus="Filled", completedTime="", warningText=""),
            )
        ]
        report = session.reconcile(["ref-filled-ok"])
        assert report.state("ref-filled-ok") is RefState.FILLED
        assert report.rejection_reason("ref-filled-ok") is None

    def test_completed_orders_request_apiOnly_false(self, session, fake_ib):
        # #627: apiOnly=True (the old call) empirically omits completedStatus
        # for some order shapes; apiOnly=False is the confirmed-reliable call.
        captured_kwargs = {}
        real = fake_ib.reqCompletedOrdersAsync

        async def _spy(apiOnly=True):
            captured_kwargs["apiOnly"] = apiOnly
            return await real(apiOnly=apiOnly)

        fake_ib.reqCompletedOrdersAsync = _spy
        session.reconcile([])
        assert captured_kwargs["apiOnly"] is False

    def test_capture_shim_is_removed_after_reconcile(self, session, fake_ib):
        # The hook must be scoped to the one request — never left installed,
        # which would otherwise intercept every later completedOrder call
        # (including ones outside this session's own control).
        original = fake_ib.wrapper.completedOrder
        session.reconcile([])
        assert fake_ib.wrapper.completedOrder is original

    def test_capture_failure_never_breaks_reconcile(self, session, fake_ib):
        # Exception-safety (#627): a capture-side bug reading orderState
        # must never take down the reconciliation it's riding along on. A
        # property that raises (not AttributeError, so getattr's default
        # can't save it) isolates the capture's OWN read from orderStatus,
        # which reconcile()'s existing main loop reads independently.
        class _ExplodingOrderState:
            status = "Filled"

            @property
            def completedStatus(self):
                raise RuntimeError("capture bug")

        fake_ib.completed_trades = [
            SimpleNamespace(
                order=SimpleNamespace(orderRef="ref-ok"),
                orderStatus=SimpleNamespace(status="Filled"),
                orderState=_ExplodingOrderState(),
            )
        ]
        report = session.reconcile(["ref-ok"])
        assert report.state("ref-ok") is RefState.FILLED
        assert report.rejection_reason("ref-ok") is None


class TestPlacement:
    def test_bag_construction_for_vertical(self, reconciled, fake_ib):
        reconciled.place_spread(BULL_PUT, "basis:B01:o1:open")
        (trade,) = fake_ib.placed
        bag = trade.contract
        assert bag.secType == "BAG"
        assert bag.symbol == "XSP"
        assert bag.exchange == "SMART"
        assert [(cl.action, cl.ratio) for cl in bag.comboLegs] == [("SELL", 1), ("BUY", 1)]
        assert all(cl.conId for cl in bag.comboLegs)
        order = trade.order
        assert order.orderRef == "basis:B01:o1:open"
        assert order.tif == "DAY"
        assert order.lmtPrice == -1.25
        assert order.transmit is True  # no profit target → transmit immediately

    def test_four_leg_condor_is_one_order(self, reconciled, fake_ib):
        reconciled.place_spread(CONDOR, "basis:B02:o2:open")
        (trade,) = fake_ib.placed
        assert len(trade.contract.comboLegs) == 4

    def test_profit_target_child_is_gtc_and_linked(self, reconciled, fake_ib):
        placed = reconciled.place_spread(BULL_PUT, "basis:B01:o1:open", profit_target_price=-0.62)
        entry, child = (t.order for t in fake_ib.placed)
        assert entry.transmit is False  # held until the child transmits the pair
        assert child.transmit is True
        assert child.parentId == entry.orderId
        assert child.tif == "GTC"
        assert child.orderRef == "basis:B01:o1:open:tp"
        assert placed.order_id == entry.orderId
        assert placed.perm_id == entry.permId

    def test_invalid_occ_symbol_raises(self, reconciled):
        bad = SpreadOrder(legs=(("NOT-AN-OCC", "SELL", 1),), quantity=1, net_limit_price=-1.0, underlying="XSP")
        with pytest.raises(ContractQualificationError):
            reconciled.place_spread(bad, "basis:B01:o3:open")

    def test_unqualified_contract_raises(self, fake_ib):
        fake_ib._qualify_ok = False
        s = BrokerSession(ib_factory=lambda: fake_ib)
        s.open()
        s.reconcile([])
        with pytest.raises(ContractQualificationError, match="expired or unknown"):
            s.place_spread(BULL_PUT, "basis:B01:o4:open")
        s.close()

    def test_close_spread_sells_the_bag_day_limit_no_child(self, reconciled, fake_ib):
        reconciled.close_spread(BULL_PUT, "basis:B01:o1:close")
        (trade,) = fake_ib.placed
        assert trade.order.action == "SELL"  # closing the BUY-opened combo
        assert trade.order.tif == "DAY"
        assert trade.order.orderRef == "basis:B01:o1:close"
        assert len(fake_ib.placed) == 1


class TestIdempotency:
    def test_same_ref_twice_in_one_session_is_refused(self, reconciled):
        reconciled.place_spread(BULL_PUT, "basis:B01:o1:open")
        with pytest.raises(DuplicateOrderRefError, match="this session"):
            reconciled.place_spread(BULL_PUT, "basis:B01:o1:open")

    def test_crash_after_place_order_is_not_resubmitted(self, fake_ib):
        """The design's required test (§2.4): place, crash without cleanup,
        reconnect — the same ref must be refused, not resubmitted."""
        ref = "basis:B01:o9:open"
        s1 = BrokerSession(ib_factory=lambda: fake_ib)
        s1.open()
        s1.reconcile([])
        s1.place_spread(BULL_PUT, ref)
        crashed_trade = fake_ib.placed[0]
        del s1  # crash: no cancel, no close

        # The broker still has the order working; the next session sees it.
        fake_ib.open_trades = [crashed_trade]
        s2 = BrokerSession(ib_factory=lambda: fake_ib)
        s2.open()
        report = s2.reconcile([ref])
        assert report.state(ref) is RefState.OPEN
        with pytest.raises(DuplicateOrderRefError, match="not resubmitting"):
            s2.place_spread(BULL_PUT, ref)
        assert len(fake_ib.placed) == 1  # exactly one submission ever reached the broker
        s2.close()

    def test_filled_at_broker_is_not_resubmitted(self, fake_ib):
        ref = "basis:B01:o10:open"
        fake_ib.completed_trades = [
            SimpleNamespace(order=SimpleNamespace(orderRef=ref), orderStatus=SimpleNamespace(status="Filled"))
        ]
        s = BrokerSession(ib_factory=lambda: fake_ib)
        s.open()
        s.reconcile([ref])
        with pytest.raises(DuplicateOrderRefError):
            s.place_spread(BULL_PUT, ref)
        s.close()

    def test_unknown_ref_may_be_submitted(self, fake_ib):
        """Crash BEFORE placeOrder: ref absent at the broker → placement allowed
        (the intent-expiry decision — expire vs resubmit — is pipeline policy)."""
        s = BrokerSession(ib_factory=lambda: fake_ib)
        s.open()
        report = s.reconcile(["basis:B01:o11:open"])
        assert report.state("basis:B01:o11:open") is RefState.UNKNOWN
        s.place_spread(BULL_PUT, "basis:B01:o11:open")
        assert len(fake_ib.placed) == 1
        s.close()


class TestPreview:
    def test_margin_preview_parsed(self, session):
        preview = session.preview_spread(BULL_PUT)
        assert preview.init_margin_change == 375.0
        assert preview.maint_margin_change == 375.0
        assert preview.commission_min == 1.1
        assert preview.commission_max == 2.3

    def test_what_if_order_sets_tif_explicitly(self, session, fake_ib):
        # #832: without an explicit TIF, IBKR emits the informational 10349
        # "Order TIF was set to DAY based on order preset" notice, which
        # ib_async treats as terminating the what-if request — every preview
        # then fails. The what-if order must carry the entry parent's DAY.
        session.preview_spread(BULL_PUT)
        assert fake_ib.what_if_order.tif == "DAY"

    def test_warning_text_rejects_preview(self, session, fake_ib):
        fake_ib.what_if_state.warningText = "Margin check could not be performed"
        with pytest.raises(PreviewRejectedError, match="Margin check"):
            session.preview_spread(BULL_PUT)

    def test_empty_list_state_rejects_preview(self, session, fake_ib):
        # 2026-08-25 crash: when IBKR answers the what-if request with an
        # API error, ib_async resolves the future with its default result
        # accumulator — a list, not None and not an OrderState. Must refuse
        # the candidate (PreviewRejectedError), not AttributeError the run.
        fake_ib.what_if_state = []
        with pytest.raises(PreviewRejectedError, match="API error instead of an order state"):
            session.preview_spread(BULL_PUT)

    def test_non_empty_list_state_rejects_preview(self, session, fake_ib):
        fake_ib.what_if_state = [object()]
        with pytest.raises(PreviewRejectedError, match="API error instead of an order state"):
            session.preview_spread(BULL_PUT)

    def test_dbl_max_commissions_become_none(self, session, fake_ib):
        fake_ib.what_if_state.minCommission = 1.7976931348623157e308
        fake_ib.what_if_state.maxCommission = 1.7976931348623157e308
        preview = session.preview_spread(BULL_PUT)
        assert preview.commission_min is None
        assert preview.commission_max is None

    def test_dbl_max_margin_rejects_preview(self, session, fake_ib):
        # #626: a real, correctly-priced order always resolves a margin
        # figure — a DBL_MAX (unavailable) initMarginChange means whatIf
        # itself couldn't evaluate the order, independent of warningText.
        fake_ib.what_if_state.initMarginChange = 1.7976931348623157e308
        with pytest.raises(PreviewRejectedError, match="no usable margin"):
            session.preview_spread(BULL_PUT)

    def test_missing_init_margin_field_rejects_preview(self, session, fake_ib):
        fake_ib.what_if_state.initMarginChange = None
        with pytest.raises(PreviewRejectedError, match="no usable margin"):
            session.preview_spread(BULL_PUT)

    def test_hung_whatif_rejects_preview_not_raw_timeout(self, session, fake_ib):
        # #841 (sibling of #826/#828): a whatIf request that never resolves
        # its future — warning-class error codes with the reqId, or an
        # UNSET_DOUBLE initMarginChange — must refuse the candidate, not
        # crash the run with a raw concurrent.futures.TimeoutError.
        never_set = asyncio.Event()

        async def hang_forever(contract, order):
            await never_set.wait()

        fake_ib.whatIfOrderAsync = hang_forever
        # preview_spread calls self._loop.run(_op()) with no explicit
        # timeout, using the CALL_TIMEOUT-bound default — shrink it here so
        # the test doesn't actually wait 60s for the real timeout to fire.
        original_run = session._loop.run
        session._loop.run = lambda coro, timeout=0.05: original_run(coro, timeout)
        with pytest.raises(PreviewRejectedError, match="whatIfOrder timed out"):
            session.preview_spread(BULL_PUT)


class TestFillsAndCancel:
    def test_wait_for_terminal_returns_fill_details(self, reconciled, fake_ib):
        placed = reconciled.place_spread(BULL_PUT, "basis:B01:o1:open")
        trade = fake_ib.placed[0]
        trade.orderStatus.status = "Filled"
        trade.orderStatus.filled = 1.0
        trade.orderStatus.avgFillPrice = -1.24
        trade.fills = [
            SimpleNamespace(
                execution=SimpleNamespace(
                    execId="0001.aa.01",
                    side="SLD",
                    shares=1,
                    price=6.10,
                    orderRef="basis:B01:o1:open",
                    time=datetime(2026, 8, 20, 13, 31, tzinfo=UTC),
                ),
                contract=SimpleNamespace(conId=1000),
                commissionReport=SimpleNamespace(commission=1.05),
            ),
            SimpleNamespace(
                execution=SimpleNamespace(
                    execId="0001.aa.02",
                    side="BOT",
                    shares=1,
                    price=4.86,
                    orderRef="basis:B01:o1:open",
                    time=datetime(2026, 8, 20, 13, 31, tzinfo=UTC),
                ),
                contract=SimpleNamespace(conId=1001),
                commissionReport=None,
            ),
        ]
        result = reconciled.wait_for_terminal(placed.order_id, timeout_s=2.0)
        assert result.terminal is True
        assert result.status == "Filled"
        assert result.filled == 1.0  # BAG-level combo units, not leg count
        assert {f.exec_id for f in result.fills} == {"0001.aa.01", "0001.aa.02"}
        assert result.fills[0].commission == 1.05
        assert result.fills[1].commission is None
        assert all(f.order_ref == "basis:B01:o1:open" for f in result.fills)
        assert all(f.exec_time == "2026-08-20T13:31:00+00:00" for f in result.fills)

    def test_wait_timeout_returns_non_terminal(self, reconciled, fake_ib):
        placed = reconciled.place_spread(BULL_PUT, "basis:B01:o1:open")
        result = reconciled.wait_for_terminal(placed.order_id, timeout_s=0.4)
        assert result.terminal is False
        assert result.status == "Submitted"

    def test_wait_for_terminal_times_out_but_still_yields_at_least_once(self, reconciled, fake_ib, monkeypatch):
        # #897: the poll used to check the deadline before ever awaiting, so a
        # zero budget skipped the event-loop yield entirely — queued
        # orderStatus updates never got a chance to settle before the read.
        import backend.broker as broker_mod

        real_sleep = broker_mod.asyncio.sleep
        sleep_calls: list[float] = []

        async def counting_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)
            await real_sleep(0)

        monkeypatch.setattr(broker_mod.asyncio, "sleep", counting_sleep)

        placed = reconciled.place_spread(BULL_PUT, "basis:B01:o1:open")
        result = reconciled.wait_for_terminal(placed.order_id, timeout_s=0.0)

        assert sleep_calls  # at least one yield happened despite the zero budget
        assert result.terminal is False
        assert result.status == "Submitted"

    def test_cancel_by_order_id(self, reconciled, fake_ib):
        placed = reconciled.place_spread(BULL_PUT, "basis:B01:o1:open")
        reconciled.cancel(placed.order_id)
        assert fake_ib.cancelled == [placed.order_id]

    def test_unknown_order_id_raises(self, reconciled):
        with pytest.raises(UnknownOrderError):
            reconciled.wait_for_terminal(999999, timeout_s=0.1)
        with pytest.raises(UnknownOrderError):
            reconciled.cancel(999999)


class TestStateViews:
    def test_positions_maps_rows_and_drops_flat(self, session, fake_ib):
        fake_ib.position_rows = [
            SimpleNamespace(
                contract=SimpleNamespace(conId=1, symbol="XSP", secType="OPT"), position=-1.0, avgCost=120.0
            ),
            SimpleNamespace(
                contract=SimpleNamespace(conId=2, symbol="SPY", secType="STK"), position=100.0, avgCost=650.0
            ),
            SimpleNamespace(contract=SimpleNamespace(conId=3, symbol="XSP", secType="OPT"), position=0.0, avgCost=0.0),
        ]
        rows = session.positions()
        assert len(rows) == 2  # flat row dropped
        stock = next(r for r in rows if r.sec_type == "STK")
        assert stock.symbol == "SPY"  # visible to the No-Stock scan

    def test_open_orders_view(self, session, fake_ib):
        fake_ib.open_trades = [
            SimpleNamespace(
                order=SimpleNamespace(orderRef="ref-a", orderId=7, permId=90007),
                orderStatus=SimpleNamespace(status="PreSubmitted"),
            )
        ]
        (info,) = session.open_orders()
        assert (info.order_ref, info.order_id, info.perm_id, info.status) == ("ref-a", 7, 90007, "PreSubmitted")

    def test_option_positions_carry_occ_symbol(self, session, fake_ib):
        fake_ib.position_rows = [
            SimpleNamespace(
                contract=SimpleNamespace(
                    conId=1,
                    symbol="XSP",
                    secType="OPT",
                    lastTradeDateOrContractMonth="20261218",
                    right="P",
                    strike=610.0,
                ),
                position=-1.0,
                avgCost=120.0,
            ),
            SimpleNamespace(
                contract=SimpleNamespace(conId=2, symbol="SPY", secType="STK"), position=100.0, avgCost=650.0
            ),
        ]
        rows = session.positions()
        option = next(r for r in rows if r.sec_type == "OPT")
        stock = next(r for r in rows if r.sec_type == "STK")
        assert option.occ_symbol == "XSP261218P00610000"
        assert stock.occ_symbol is None

    def test_executions_sweep(self, session, fake_ib):
        fake_ib.executions = [
            SimpleNamespace(
                execution=SimpleNamespace(
                    execId="0001.bb.01",
                    side="SLD",
                    shares=1,
                    price=6.10,
                    orderRef="basis:B01:o1:open",
                    time=datetime(2026, 8, 20, 13, 31, tzinfo=UTC),
                ),
                contract=SimpleNamespace(conId=1000),
                commissionReport=SimpleNamespace(commission=1.05),
            )
        ]
        (fill,) = session.executions()
        assert fill.exec_id == "0001.bb.01"
        assert fill.order_ref == "basis:B01:o1:open"
        assert fill.commission == 1.05

    def test_commission_read_from_canonical_fill_after_late_pairing(self, session, fake_ib):
        # #895: reqExecutionsAsync resolves on execDetailsEnd, BEFORE the
        # broker's CommissionReport messages pair onto the wrapper's canonical
        # fills — and an execution already seen live gets a FRESH object in the
        # request results while the report lands on the original. Reading the
        # results snapshot directly is how every ledger row ever captured came
        # out at commission 0.0. The sweep must re-read the canonical object.
        execution = SimpleNamespace(
            execId="0001.ee.01",
            side="SLD",
            shares=1,
            price=6.10,
            orderRef="basis:B01:o1:open",
            time=datetime(2026, 8, 20, 13, 31, tzinfo=UTC),
        )
        stale = SimpleNamespace(
            execution=execution,
            contract=SimpleNamespace(conId=1000, secType="OPT"),
            # ib_async's empty placeholder: no execId, no commission yet
            commissionReport=SimpleNamespace(execId="", commission=0.0),
        )
        paired = SimpleNamespace(
            execution=execution,
            contract=SimpleNamespace(conId=1000, secType="OPT"),
            commissionReport=SimpleNamespace(execId="0001.ee.01", commission=0.62),
        )
        fake_ib.executions = [stale]
        fake_ib.session_fills = [paired]
        (fill,) = session.executions()
        assert fill.commission == 0.62
        assert fake_ib.fills_calls >= 1

    def test_commission_wait_times_out_but_still_reads_at_least_once(self, session, fake_ib, monkeypatch):
        # The report never arrives: the sweep must settle for the placeholder
        # rather than wedge — and a zero budget must still perform one
        # canonical read before giving up (#885's zero-probe lesson).
        import backend.broker as broker_mod

        monkeypatch.setattr(broker_mod, "COMMISSION_REPORT_WAIT_S", 0.0)
        fake_ib.executions = [
            SimpleNamespace(
                execution=SimpleNamespace(
                    execId="0001.ff.01",
                    side="SLD",
                    shares=1,
                    price=6.10,
                    orderRef="basis:B01:o1:open",
                    time=datetime(2026, 8, 20, 13, 31, tzinfo=UTC),
                ),
                contract=SimpleNamespace(conId=1000, secType="OPT"),
                commissionReport=SimpleNamespace(execId="", commission=0.0),
            )
        ]
        (fill,) = session.executions()
        assert fill.commission is None
        assert fake_ib.fills_calls >= 1

    def test_bag_level_execution_is_excluded(self, session, fake_ib):
        # IBKR reports a combo fill as legs PLUS the BAG contract at the net
        # price (#331, surfaced by the first live fills 2026-08-20).
        # Ledgering the bag row would double-count every net-fill sum.
        legs = [
            SimpleNamespace(
                execution=SimpleNamespace(
                    execId="0001.cc.01",
                    side="BOT",
                    shares=1,
                    price=11.98,
                    orderRef="basis:B07:o1:open",
                    time=datetime(2026, 8, 20, 13, 31, tzinfo=UTC),
                ),
                contract=SimpleNamespace(conId=1000, secType="OPT"),
                commissionReport=SimpleNamespace(commission=1.05),
            ),
            SimpleNamespace(
                execution=SimpleNamespace(
                    execId="0001.cc.02",
                    side="SLD",
                    shares=1,
                    price=8.90,
                    orderRef="basis:B07:o1:open",
                    time=datetime(2026, 8, 20, 13, 31, tzinfo=UTC),
                ),
                contract=SimpleNamespace(conId=1001, secType="OPT"),
                commissionReport=SimpleNamespace(commission=1.05),
            ),
        ]
        bag = SimpleNamespace(
            execution=SimpleNamespace(
                execId="0001.cc.00",
                side="BOT",
                shares=1,
                price=3.08,
                orderRef="basis:B07:o1:open",
                time=datetime(2026, 8, 20, 13, 31, tzinfo=UTC),
            ),
            contract=SimpleNamespace(conId=28812380, secType="BAG"),
            commissionReport=None,
        )
        fake_ib.executions = [bag, *legs]
        fills = session.executions()
        assert {f.exec_id for f in fills} == {"0001.cc.01", "0001.cc.02"}

    def test_wait_for_terminal_excludes_bag_fill(self, reconciled, fake_ib):
        placed = reconciled.place_spread(BULL_PUT, "basis:B01:o1:open")
        trade = fake_ib.placed[0]
        trade.orderStatus.status = "Filled"
        trade.orderStatus.filled = 1.0
        trade.fills = [
            SimpleNamespace(
                execution=SimpleNamespace(
                    execId="0001.dd.00",
                    side="SLD",
                    shares=1,
                    price=1.24,
                    orderRef="basis:B01:o1:open",
                    time=datetime(2026, 8, 20, 13, 31, tzinfo=UTC),
                ),
                contract=SimpleNamespace(conId=999, secType="BAG"),
                commissionReport=None,
            ),
            SimpleNamespace(
                execution=SimpleNamespace(
                    execId="0001.dd.01",
                    side="SLD",
                    shares=1,
                    price=6.10,
                    orderRef="basis:B01:o1:open",
                    time=datetime(2026, 8, 20, 13, 31, tzinfo=UTC),
                ),
                contract=SimpleNamespace(conId=1000, secType="OPT"),
                commissionReport=None,
            ),
        ]
        result = reconciled.wait_for_terminal(placed.order_id, timeout_s=2.0)
        assert {f.exec_id for f in result.fills} == {"0001.dd.01"}


class TestMoneyParsing:
    def test_currency_suffixed_string(self):
        assert _money("375.0 USD") == 375.0

    def test_plain_float(self):
        assert _money(2.5) == 2.5

    def test_none_and_empty(self):
        assert _money(None) is None
        assert _money("") is None

    def test_garbage(self):
        assert _money("N/A") is None

    def test_dbl_max_sentinel(self):
        assert _money(1.7976931348623157e308) is None

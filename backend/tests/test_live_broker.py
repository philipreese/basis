"""The live BrokerSession (#1065): inverted account guard, locked
transmission, no option orders, share previews and account cash. Faked at
the ib_async surface with test_broker.FakeIB."""

from types import SimpleNamespace

import pytest

from backend.broker import (
    AccountDataError,
    BrokerSession,
    LiveAccountRequiredError,
    PaperAccountRequiredError,
    PreviewRejectedError,
    TransmitNotArmedError,
    check_live_accounts,
)
from backend.tests.test_broker import BULL_PUT, FakeIB

LIVE_ID = "U0000000"  # synthetic
GATEWAY = ("127.0.0.1", 4001, 17)


def _live(fake: FakeIB, transmit: bool | None = None) -> BrokerSession:
    return BrokerSession(ib_factory=lambda: fake, live_account_id=LIVE_ID, gateway=GATEWAY, transmit=transmit)


@pytest.mark.parametrize(
    ("accounts", "expected", "fragment"),
    [
        (["DU111"], LIVE_ID, "paper"),
        (["U999"], LIVE_ID, "does not match"),
        ([LIVE_ID, "U999"], LIVE_ID, "exactly one"),
        ([], LIVE_ID, "no managed accounts"),
        ([LIVE_ID], "", "no live account id"),
        ([LIVE_ID], None, "no live account id"),
        (["DU111"], "DU111", "configured live account id is a paper"),
    ],
)
def test_live_guard_refuses_every_ambiguity_without_naming_an_account(accounts, expected, fragment):
    with pytest.raises(LiveAccountRequiredError) as exc:
        check_live_accounts(accounts, expected)
    assert fragment in str(exc.value)
    for account in [*accounts, expected or ""]:
        if account:
            assert account not in str(exc.value)


def test_live_guard_accepts_exactly_the_configured_account():
    check_live_accounts([LIVE_ID], f" {LIVE_ID} ")


def test_live_session_refuses_a_paper_account_and_closes():
    fake = FakeIB(accounts=("DUR925279",))
    with pytest.raises(LiveAccountRequiredError):
        _live(fake).open()
    assert fake.connected is False


def test_paper_session_still_refuses_a_live_account():
    with pytest.raises(PaperAccountRequiredError):
        BrokerSession(ib_factory=lambda: FakeIB(accounts=(LIVE_ID,))).open()


def test_live_session_requires_an_explicit_gateway():
    with pytest.raises(ValueError):
        BrokerSession(ib_factory=FakeIB, live_account_id=LIVE_ID)


def test_live_session_transmission_is_locked_by_default():
    fake = FakeIB(accounts=(LIVE_ID,))
    s = _live(fake)
    s.open()
    try:
        assert s.is_live
        s.reconcile([])
        with pytest.raises(TransmitNotArmedError):
            s.place_share_order("SCHB", "BUY", 1, 30.0, "basis:B36:x:share")
        assert fake.placed == []
    finally:
        s.close()


def test_armed_live_session_places_shares_but_never_options():
    fake = FakeIB(accounts=(LIVE_ID,))
    s = _live(fake, transmit=True)
    s.open()
    try:
        s.reconcile([])
        s.place_share_order("SCHB", "BUY", 2, 30.0, "basis:B36:x:share")
        assert len(fake.placed) == 1
        for call in (
            lambda: s.place_spread(BULL_PUT, "basis:B01:o:open"),
            lambda: s.close_spread(BULL_PUT, "basis:B01:o:close"),
            lambda: s.preview_spread(BULL_PUT),
        ):
            with pytest.raises(LiveAccountRequiredError):
                call()
    finally:
        s.close()


def test_paper_session_can_be_explicitly_locked():
    fake = FakeIB()
    s = BrokerSession(ib_factory=lambda: fake, transmit=False)
    s.open()
    try:
        s.reconcile([])
        with pytest.raises(TransmitNotArmedError):
            s.place_share_order("SCHB", "BUY", 1, 30.0, "r")
    finally:
        s.close()


@pytest.fixture
def live_open():
    fake = FakeIB(accounts=(LIVE_ID,))
    fake.what_if_state = SimpleNamespace(
        initMarginChange="30.0",
        maintMarginChange="30.0",
        equityWithLoanAfter="5000.0",
        initMarginAfter="200.0",
        minCommission=1.0,
        maxCommission=1.0,
        warningText="",
    )
    s = _live(fake)
    s.open()
    yield s, fake
    s.close()


def test_share_preview_runs_while_locked_and_reads_post_trade_figures(live_open):
    s, fake = live_open
    preview = s.preview_share_order("SCHB", "BUY", 2, 30.0)
    assert preview.equity_with_loan_after == 5000.0
    assert preview.init_margin_after == 200.0
    assert fake.what_if_order.action == "BUY" and fake.what_if_order.tif == "DAY"
    assert fake.placed == []


def test_share_preview_refuses_warning_api_error_none_and_missing_margin(live_open):
    s, fake = live_open
    fake.what_if_state = SimpleNamespace(initMarginChange="1", warningText="insufficient funds")
    with pytest.raises(PreviewRejectedError, match="warning"):
        s.preview_share_order("SCHB", "BUY", 1, 30.0)
    fake.what_if_state = []
    with pytest.raises(PreviewRejectedError, match="API error"):
        s.preview_share_order("SCHB", "BUY", 1, 30.0)
    fake.what_if_state = None
    with pytest.raises(PreviewRejectedError, match="no order state"):
        s.preview_share_order("SCHB", "BUY", 1, 30.0)
    fake.what_if_state = SimpleNamespace(initMarginChange=None, warningText="")
    with pytest.raises(PreviewRejectedError, match="margin"):
        s.preview_share_order("SCHB", "BUY", 1, 30.0)


def test_share_preview_refuses_an_unqualified_stock():
    fake = FakeIB(accounts=(LIVE_ID,), qualify_ok=False)
    s = _live(fake)
    s.open()
    try:
        with pytest.raises(Exception, match="qualify"):
            s.preview_share_order("NOPE", "BUY", 1, 30.0)
    finally:
        s.close()


def _cash_rows(*rows):
    async def summary(account=""):
        return [SimpleNamespace(account=a, tag=t, value=v, currency=c) for a, t, v, c in rows]

    return summary


def test_account_cash_reads_only_the_live_accounts_usd_cash(live_open):
    s, fake = live_open
    fake.accountSummaryAsync = _cash_rows(
        (LIVE_ID, "TotalCashValue", "1234.5", "USD"),
        (LIVE_ID, "NetLiquidation", "9999", "USD"),
        ("U000", "TotalCashValue", "5", "USD"),
    )
    assert s.account_cash() == 1234.5


@pytest.mark.parametrize(
    "rows",
    [
        (),
        ((LIVE_ID, "TotalCashValue", "abc", "USD"),),
        ((LIVE_ID, "TotalCashValue", "inf", "USD"),),
        ((LIVE_ID, "TotalCashValue", "1", "USD"), (LIVE_ID, "TotalCashValue", "2", "USD")),
    ],
)
def test_account_cash_fails_closed(live_open, rows):
    s, fake = live_open
    fake.accountSummaryAsync = _cash_rows(*rows)
    with pytest.raises(AccountDataError):
        s.account_cash()


def test_account_cash_wraps_transport_errors(live_open):
    s, fake = live_open

    async def boom(account=""):
        raise OSError("socket closed")

    fake.accountSummaryAsync = boom
    with pytest.raises(AccountDataError, match="unavailable"):
        s.account_cash()

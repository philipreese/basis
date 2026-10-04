"""#1074: share drift goes through the audited resolution flow.

Two corrections, both human-only and audited (actor=resolution):
- correct_share_holding — a hand sale, a reinvested dividend, a corporate
  action: set a DESIGNATED book's holding for a DESIGNATED symbol, compare-
  and-set, with raising it gated as an explicit claim the latest drift run
  must support (an assignment can never be adopted silently);
- settle_share_order — a held share order (FILLED with its executions out of
  reach after a missed night): the operator states the total execution and
  the sync's own arithmetic books it.
Fail-closed refusals first."""

import math

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import get_db
from backend.models import (
    AuditEventModel,
    Base,
    BookModel,
    ReconciliationRunModel,
    ShareHoldingModel,
    ShareOrderModel,
    TradingControlModel,
)
from backend.resolution import ResolutionError, correct_share_holding, settle_share_order
from backend.seeds import LAB_BOOKS

B36_CONFIG = next(b for b in LAB_BOOKS if b["id"] == "B36")["config"]


@pytest_asyncio.fixture
async def maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with m() as session:
        for book_id, config in (("B36", B36_CONFIG), ("B01", {"engine_variant": "V0", "underlying": "XSP"})):
            session.add(
                BookModel(
                    id=book_id,
                    name=book_id,
                    config=config,
                    config_version=1,
                    config_hash=f"hash-{book_id}",
                    starting_capital=10000.0,
                    cash_balance=10000.0,
                    status="ACTIVE",
                    created_at="2026-10-01T00:00:00+00:00",
                )
            )
            session.add(TradingControlModel(scope=book_id, state="HALT_ENTRIES", reason="", actor="t", changed_at="t0"))
        session.add(
            TradingControlModel(scope="GLOBAL", state="HALT_ENTRIES", reason="drift", actor="t", changed_at="t0")
        )
        await session.commit()
    yield m
    await engine.dispose()


@pytest_asyncio.fixture
async def client(maker):
    from backend.main import app

    async def override_get_db():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _hold(m, symbol: str, qty: float, book_id: str = "B36") -> None:
    async with m() as session:
        session.add(ShareHoldingModel(book_id=book_id, symbol=symbol, quantity=qty, updated_at="t0"))
        await session.commit()


async def _drift_run(m, items: list[dict], *, resolved: bool = False) -> int:
    async with m() as session:
        run = ReconciliationRunModel(
            run_at="2026-11-03T22:45:00+00:00",
            broker_snapshot={},
            books_expected={},
            result="DRIFT",
            drift_details=items,
            resolved_at="2026-11-04T00:00:00+00:00" if resolved else None,
        )
        session.add(run)
        await session.commit()
        return run.id


def _share_item(symbol: str, broker: float, expected: float, kind: str = "SHARE_DRIFT", **extra) -> dict:
    return {
        "kind": kind,
        "key": symbol,
        "sec_type": "STK",
        "broker_qty": broker,
        "expected_qty": expected,
        "unexpected_instrument": True,
        "unexpected_qty": max(0.0, broker - expected),
        "mixed_sign": False,
        **extra,
    }


async def _order(m, *, quantity: int = 4, status: str = "SUBMITTED", fills: list | None = None, ref: str = "o1"):
    async with m() as session:
        session.add(
            ShareOrderModel(
                id=ref,
                book_id="B36",
                order_ref=f"basis:B36:{ref}:share",
                symbol="IAUM",
                side="BUY",
                quantity=quantity,
                limit_price=337.0,
                decision_close=330.0,
                signal_date="2026-10-30",
                status=status,
                config_hash="hash-B36",
                created_at="t0",
                fills=fills or [],
            )
        )
        await session.commit()
    return f"basis:B36:{ref}:share"


async def _quantity(m, symbol: str, book_id: str = "B36") -> float | None:
    async with m() as session:
        row = await session.get(ShareHoldingModel, (book_id, symbol))
        return row.quantity if row else None


async def _cash(m, book_id: str = "B36") -> float:
    async with m() as session:
        return (await session.get(BookModel, book_id)).cash_balance


async def _events(m, event_type: str) -> list[AuditEventModel]:
    async with m() as session:
        return list((await session.execute(select(AuditEventModel).filter_by(event_type=event_type))).scalars().all())


async def _correct(m, **kwargs):
    args = {
        "book_id": "B36",
        "symbol": "SCHB",
        "current_quantity": 5.0,
        "corrected_quantity": 3.0,
        "cause": "HAND_TRADE",
        "reason": "sold 2 SCHB by hand at IBKR on 11/3",
    }
    args.update(kwargs)
    async with m() as session:
        return await correct_share_holding(session, **args)


# ---------------------------------------------------------------------------
# correct_share_holding
# ---------------------------------------------------------------------------


class TestCorrectionRefusals:
    @pytest.mark.asyncio
    async def test_undesignated_symbol_is_refused(self, maker):
        # An assignment's SPY shares are never adopted into the share book.
        with pytest.raises(ResolutionError, match="not designated"):
            await _correct(maker, symbol="SPY", current_quantity=0.0, corrected_quantity=100.0, claim_increase=True)

    @pytest.mark.asyncio
    async def test_an_options_book_can_never_hold_shares(self, maker):
        with pytest.raises(ResolutionError, match="not designated"):
            await _correct(maker, book_id="B01", current_quantity=0.0)

    @pytest.mark.asyncio
    async def test_unknown_book_is_refused(self, maker):
        with pytest.raises(ResolutionError, match="No book"):
            await _correct(maker, book_id="B99")

    @pytest.mark.asyncio
    async def test_negative_holding_is_refused(self, maker):
        await _hold(maker, "SCHB", 5.0)
        with pytest.raises(ResolutionError, match="cannot be negative"):
            await _correct(maker, corrected_quantity=-1.0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["current_quantity", "corrected_quantity", "cash_delta"])
    async def test_non_finite_numbers_are_refused(self, maker, field):
        with pytest.raises(ResolutionError, match="finite"):
            await _correct(maker, **{field: math.nan})

    @pytest.mark.asyncio
    async def test_a_reason_is_required(self, maker):
        with pytest.raises(ResolutionError, match="reason"):
            await _correct(maker, reason=" ")

    @pytest.mark.asyncio
    async def test_a_pending_order_on_the_symbol_must_be_settled_first(self, maker):
        await _hold(maker, "IAUM", 0.0)
        await _order(maker)
        with pytest.raises(ResolutionError, match="still pending"):
            await _correct(maker, symbol="IAUM", current_quantity=0.0, corrected_quantity=0.0, cash_delta=-1.0)

    @pytest.mark.asyncio
    async def test_stale_current_quantity_is_refused(self, maker):
        # The sync booked a fill since the operator looked.
        await _hold(maker, "SCHB", 9.0)
        with pytest.raises(ResolutionError, match="changed since you looked"):
            await _correct(maker)
        assert await _quantity(maker, "SCHB") == 9.0

    @pytest.mark.asyncio
    async def test_a_correction_that_changes_nothing_is_refused(self, maker):
        await _hold(maker, "SCHB", 5.0)
        with pytest.raises(ResolutionError, match="nothing to correct"):
            await _correct(maker, corrected_quantity=5.0)


class TestCorrectionDecrease:
    @pytest.mark.asyncio
    async def test_a_hand_sale_sets_the_holding_and_moves_its_cash_in_one_audited_act(self, maker):
        await _hold(maker, "SCHB", 5.0)
        before, after, balance = await _correct(maker, cash_delta=598.0)
        assert (before, after, balance) == (5.0, 3.0, 10_598.0)
        assert await _quantity(maker, "SCHB") == 3.0
        assert await _cash(maker) == 10_598.0
        (event,) = await _events(maker, "RESOLUTION_SHARE_HOLDING_CORRECTED")
        assert event.actor == "resolution" and event.book_id == "B36"
        assert event.payload["quantity_before"] == 5.0
        assert event.payload["quantity_after"] == 3.0
        assert event.payload["cause"] == "HAND_TRADE"
        assert event.payload["reason"] == "sold 2 SCHB by hand at IBKR on 11/3"
        assert event.payload["cash_delta"] == 598.0
        assert event.payload["claimed_shares"] == 0.0

    @pytest.mark.asyncio
    async def test_a_decrease_needs_no_claim_and_no_drift_run(self, maker):
        await _hold(maker, "SCHB", 5.0)
        _, after, balance = await _correct(maker, corrected_quantity=0.0)
        assert after == 0.0 and balance == 10_000.0

    @pytest.mark.asyncio
    async def test_never_touches_control_state(self, maker):
        await _hold(maker, "SCHB", 5.0)
        await _correct(maker)
        async with maker() as session:
            states = {c.scope: c.state for c in (await session.execute(select(TradingControlModel))).scalars().all()}
        assert set(states.values()) == {"HALT_ENTRIES"}


class TestCorrectionIncreaseIsAClaim:
    @pytest.mark.asyncio
    async def test_an_increase_without_the_claim_is_refused(self, maker):
        await _hold(maker, "TBIL", 80.0)
        await _drift_run(maker, [_share_item("TBIL", 80.37, 80.0)])
        with pytest.raises(ResolutionError, match="claim"):
            await _correct(
                maker, symbol="TBIL", current_quantity=80.0, corrected_quantity=80.37, cause="DIVIDEND_REINVESTED"
            )

    @pytest.mark.asyncio
    async def test_an_increase_with_no_drift_run_is_refused(self, maker):
        await _hold(maker, "TBIL", 80.0)
        with pytest.raises(ResolutionError, match="no"):
            await _correct(maker, symbol="TBIL", current_quantity=80.0, corrected_quantity=81.0, claim_increase=True)

    @pytest.mark.asyncio
    async def test_an_increase_against_a_resolved_run_is_refused(self, maker):
        await _hold(maker, "TBIL", 80.0)
        await _drift_run(maker, [_share_item("TBIL", 81.0, 80.0)], resolved=True)
        with pytest.raises(ResolutionError, match="unresolved"):
            await _correct(maker, symbol="TBIL", current_quantity=80.0, corrected_quantity=81.0, claim_increase=True)

    @pytest.mark.asyncio
    async def test_an_increase_on_a_symbol_the_run_did_not_flag_is_refused(self, maker):
        await _hold(maker, "TBIL", 80.0)
        await _drift_run(maker, [_share_item("SCHB", 6.0, 5.0)])
        with pytest.raises(ResolutionError, match="no share drift on TBIL"):
            await _correct(maker, symbol="TBIL", current_quantity=80.0, corrected_quantity=81.0, claim_increase=True)

    @pytest.mark.asyncio
    async def test_a_claim_beyond_what_the_broker_showed_is_refused(self, maker):
        await _hold(maker, "TBIL", 80.0)
        await _drift_run(maker, [_share_item("TBIL", 80.37, 80.0)])
        with pytest.raises(ResolutionError, match="more than"):
            await _correct(maker, symbol="TBIL", current_quantity=80.0, corrected_quantity=81.0, claim_increase=True)

    @pytest.mark.asyncio
    async def test_a_suspected_assignment_on_the_symbol_blocks_the_claim(self, maker):
        # A suspected option assignment on the same underlying (hypothetical
        # here, since IAUM is not itself optioned in this lab) must be closed,
        # never folded into the trend book's holding.
        await _hold(maker, "IAUM", 3.0)
        await _drift_run(
            maker,
            [
                _share_item("IAUM", 103.0, 3.0),
                {"kind": "ASSIGNMENT_SUSPECTED", "key": "IAUM261120P00041000", "sec_type": "OPT", "broker_qty": 0},
            ],
        )
        with pytest.raises(ResolutionError, match="assignment"):
            await _correct(maker, symbol="IAUM", current_quantity=3.0, corrected_quantity=103.0, claim_increase=True)
        assert await _quantity(maker, "IAUM") == 3.0

    @pytest.mark.asyncio
    async def test_mixed_sign_rows_block_the_claim(self, maker):
        await _hold(maker, "IAUM", 3.0)
        await _drift_run(maker, [_share_item("IAUM", 4.0, 3.0, mixed_sign=True)])
        with pytest.raises(ResolutionError, match="short row"):
            await _correct(maker, symbol="IAUM", current_quantity=3.0, corrected_quantity=4.0, claim_increase=True)

    @pytest.mark.asyncio
    async def test_a_reinvested_dividend_claimed_within_the_broker_count_is_recorded(self, maker):
        await _hold(maker, "TBIL", 80.0)
        run_id = await _drift_run(maker, [_share_item("TBIL", 80.37, 80.0)])
        _, after, _ = await _correct(
            maker,
            symbol="TBIL",
            current_quantity=80.0,
            corrected_quantity=80.37,
            cause="DIVIDEND_REINVESTED",
            reason="DRIP was on for the October TBIL distribution",
            claim_increase=True,
        )
        assert after == 80.37
        (event,) = await _events(maker, "RESOLUTION_SHARE_HOLDING_CORRECTED")
        assert event.payload["claim_increase"] is True
        assert event.payload["claimed_shares"] == pytest.approx(0.37)
        assert event.payload["reconciliation_run_id"] == run_id
        assert event.payload["broker_qty"] == 80.37

    @pytest.mark.asyncio
    async def test_a_holding_row_is_created_when_none_existed(self, maker):
        # A missed first fill booked by hand (no order row left to settle):
        # an ORPHAN on a designated symbol, nothing in the books.
        await _drift_run(maker, [_share_item("SCHB", 5.0, 0.0, kind="ORPHAN")])
        _, after, _ = await _correct(
            maker, current_quantity=0.0, corrected_quantity=5.0, cause="MISSED_FILL", claim_increase=True
        )
        assert after == 5.0 and await _quantity(maker, "SCHB") == 5.0


class TestCorrectionEndpoint:
    @pytest.mark.asyncio
    async def test_applies_and_reports(self, maker, client):
        await _hold(maker, "SCHB", 5.0)
        resp = await client.post(
            "/api/resolution/share-holding",
            json={
                "book_id": "b36",
                "symbol": "schb",
                "current_quantity": 5,
                "corrected_quantity": 3,
                "cause": "HAND_TRADE",
                "reason": "hand sale",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "book_id": "B36",
            "symbol": "SCHB",
            "quantity_before": 5.0,
            "quantity_after": 3.0,
            "cash_balance": 10000.0,
        }

    @pytest.mark.asyncio
    async def test_refusals_are_400(self, maker, client):
        resp = await client.post(
            "/api/resolution/share-holding",
            json={
                "book_id": "B36",
                "symbol": "SPY",
                "current_quantity": 0,
                "corrected_quantity": 100,
                "cause": "OTHER",
                "reason": "adopt the assignment",
                "claim_increase": True,
            },
        )
        assert resp.status_code == 400 and "not designated" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# settle_share_order
# ---------------------------------------------------------------------------


async def _settle(m, ref: str, **kwargs):
    args = {"filled_quantity": 4.0, "avg_fill_price": 331.0, "commission": 1.0, "reason": "Flex: 4 IAUM @ 331.00"}
    args.update(kwargs)
    async with m() as session:
        return await settle_share_order(session, ref, **args)


class TestSettleRefusals:
    @pytest.mark.asyncio
    async def test_unknown_ref(self, maker):
        with pytest.raises(ResolutionError, match="No share order"):
            await _settle(maker, "basis:B36:nope:share")

    @pytest.mark.asyncio
    async def test_terminal_order(self, maker):
        ref = await _order(maker, status="FILLED")
        with pytest.raises(ResolutionError, match="not pending"):
            await _settle(maker, ref)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("qty", [-1.0, 5.0])
    async def test_quantity_out_of_range(self, maker, qty):
        ref = await _order(maker)
        with pytest.raises(ResolutionError, match="between 0"):
            await _settle(maker, ref, filled_quantity=qty)

    @pytest.mark.asyncio
    async def test_negative_commission(self, maker):
        ref = await _order(maker)
        with pytest.raises(ResolutionError, match="magnitude"):
            await _settle(maker, ref, commission=-1.0)

    @pytest.mark.asyncio
    async def test_non_finite(self, maker):
        ref = await _order(maker)
        with pytest.raises(ResolutionError, match="finite"):
            await _settle(maker, ref, filled_quantity=math.inf)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("price", [None, 0.0, math.nan])
    async def test_a_price_is_required_for_unrecorded_shares(self, maker, price):
        ref = await _order(maker)
        with pytest.raises(ResolutionError, match="avg_fill_price"):
            await _settle(maker, ref, avg_fill_price=price)

    @pytest.mark.asyncio
    async def test_less_than_already_recorded(self, maker):
        fills = [{"exec_id": "e1", "quantity": 3.0, "price": 330.0, "commission": 1.0, "exec_time": "t"}]
        ref = await _order(maker, fills=fills)
        with pytest.raises(ResolutionError, match="already recorded"):
            await _settle(maker, ref, filled_quantity=2.0)

    @pytest.mark.asyncio
    async def test_less_commission_than_recorded(self, maker):
        fills = [{"exec_id": "e1", "quantity": 3.0, "price": 330.0, "commission": 1.0, "exec_time": "t"}]
        ref = await _order(maker, fills=fills)
        with pytest.raises(ResolutionError, match="commission is already recorded"):
            await _settle(maker, ref, commission=0.5)

    @pytest.mark.asyncio
    async def test_an_average_inconsistent_with_recorded_executions(self, maker):
        fills = [{"exec_id": "e1", "quantity": 3.0, "price": 400.0, "commission": 1.0, "exec_time": "t"}]
        ref = await _order(maker, fills=fills)
        with pytest.raises(ResolutionError, match="inconsistent"):
            await _settle(maker, ref, avg_fill_price=100.0)


class TestSettle:
    @pytest.mark.asyncio
    async def test_a_missed_fill_night_is_booked_by_the_syncs_arithmetic(self, maker):
        ref = await _order(maker)
        status, filled, holding_after = await _settle(maker, ref)
        assert (status, filled, holding_after) == ("FILLED", 4.0, 4.0)
        assert await _quantity(maker, "IAUM") == 4.0
        assert await _cash(maker) == pytest.approx(10_000.0 - 4 * 331.0 - 1.0)
        async with maker() as session:
            order = (await session.execute(select(ShareOrderModel))).scalars().one()
        assert order.status == "FILLED" and order.filled_quantity == 4.0
        assert order.fills[-1]["exec_id"] == f"resolution:{ref}"
        (booked,) = await _events(maker, "SHARE_FILL_BOOKED")
        assert booked.actor == "resolution"
        (settled,) = await _events(maker, "RESOLUTION_SHARE_ORDER_SETTLED")
        assert settled.payload["reason"] == "Flex: 4 IAUM @ 331.00" and settled.actor == "resolution"

    @pytest.mark.asyncio
    async def test_a_partial_keeps_the_recorded_executions_and_closes_cancelled(self, maker):
        fills = [{"exec_id": "e1", "quantity": 1.0, "price": 330.0, "commission": 1.0, "exec_time": "t"}]
        ref = await _order(maker, fills=fills)
        status, filled, _ = await _settle(maker, ref, filled_quantity=3.0, avg_fill_price=331.0, commission=1.0)
        assert (status, filled) == ("CANCELLED", 3.0)
        # The average holds: 1 @ 330 recorded + 2 @ 331.5 appended = 3 @ 331.
        assert await _cash(maker) == pytest.approx(10_000.0 - 3 * 331.0 - 1.0)
        async with maker() as session:
            order = (await session.execute(select(ShareOrderModel))).scalars().one()
        assert [f["exec_id"] for f in order.fills] == ["e1", f"resolution:{ref}"]
        assert order.fills[-1]["price"] == pytest.approx(331.5)
        assert order.fills[-1]["commission"] == 0.0

    @pytest.mark.asyncio
    async def test_nothing_filled_closes_cancelled_and_books_nothing(self, maker):
        ref = await _order(maker)
        status, filled, holding_after = await _settle(
            maker, ref, filled_quantity=0.0, avg_fill_price=None, commission=0
        )
        assert (status, filled, holding_after) == ("CANCELLED", 0.0, 0.0)
        assert await _quantity(maker, "IAUM") is None
        assert await _cash(maker) == 10_000.0
        assert await _events(maker, "SHARE_FILL_BOOKED") == []

    @pytest.mark.asyncio
    async def test_commission_only_correction_when_every_share_is_recorded(self, maker):
        fills = [{"exec_id": "e1", "quantity": 4.0, "price": 330.0, "commission": 0.0, "exec_time": "t"}]
        ref = await _order(maker, fills=fills)
        status, _, _ = await _settle(maker, ref, avg_fill_price=None, commission=1.0)
        assert status == "FILLED"
        assert await _cash(maker) == pytest.approx(10_000.0 - 4 * 330.0 - 1.0)

    @pytest.mark.asyncio
    async def test_then_the_holding_correction_is_unblocked(self, maker):
        ref = await _order(maker)
        await _settle(maker, ref)
        _, after, _ = await _correct(
            maker, symbol="IAUM", current_quantity=4.0, corrected_quantity=3.0, reason="sold 1 by hand"
        )
        assert after == 3.0


class TestSettleEndpoint:
    @pytest.mark.asyncio
    async def test_applies_and_reports(self, maker, client):
        ref = await _order(maker)
        resp = await client.post(
            "/api/resolution/share-order",
            json={"order_ref": ref, "filled_quantity": 4, "avg_fill_price": 331, "commission": 1, "reason": "Flex"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"order_ref": ref, "status": "FILLED", "filled_quantity": 4.0, "holding_after": 4.0}

    @pytest.mark.asyncio
    async def test_refusals_are_400(self, maker, client):
        resp = await client.post(
            "/api/resolution/share-order",
            json={"order_ref": "basis:B36:x:share", "filled_quantity": 1, "reason": "Flex"},
        )
        assert resp.status_code == 400 and "No share order" in resp.json()["detail"]

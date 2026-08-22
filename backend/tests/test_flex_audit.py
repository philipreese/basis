"""Tests for the weekly Flex Query audit (backend/flex_audit.py, #74).

The audit is the end-to-end check on the incremental fills ledger — its
classification rules (missing execution, unknown ref, mismatched fill,
refless export) are each pinned here, along with the Flex Web Service
protocol handling (success, hard failure, generation-in-progress retry).
"""

import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import flex_audit as fa
from backend.models import AuditEventModel, Base, BookModel, FillModel, FlexAckModel, OrderModel

REF = "basis:B01:o1:open"


@pytest_asyncio.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as session:
        session.add(
            BookModel(
                id="B01",
                name="lab",
                config={},
                config_version=1,
                config_hash="",
                starting_capital=10000.0,
                cash_balance=10000.0,
                status="ACTIVE",
                created_at="t0",
            )
        )
        session.add(
            OrderModel(
                id="o1",
                book_id="B01",
                position_id=None,
                order_ref=REF,
                ib_order_id=1,
                ib_perm_id=100,
                action="OPEN",
                combo_legs={"legs": [], "quantity": 1},
                order_type="LIMIT",
                limit_price=-1.0,
                decision_midpoint=-1.0,
                status="FILLED",
                encumbered_risk=0.0,
            )
        )
        session.add(
            FillModel(
                exec_id="exec1",
                order_id="o1",
                book_id="B01",
                con_id=1,
                side="SLD",
                quantity=1.0,
                price=1.05,
                commission=1.1,
                fill_time="2026-08-14T20:00:00+00:00",
                raw={},
            )
        )
        await session.commit()
    yield maker
    await engine.dispose()


def _trade(exec_id="exec1", ref=REF, qty=1.0, price=1.05) -> fa.FlexTrade:
    return fa.FlexTrade(exec_id=exec_id, order_ref=ref, quantity=qty, price=price, commission=1.1)


async def _audit(maker, trades):
    async with maker() as session:
        return await fa.audit_fills(session, trades)


class TestAuditRules:
    @pytest.mark.asyncio
    async def test_matching_ledger_is_clean_and_audited(self, session_maker):
        result = await _audit(session_maker, [_trade()])
        assert result.clean
        assert result.trades_ours == 1
        async with session_maker() as session:
            events = (await session.execute(select(AuditEventModel))).scalars().all()
        assert [e.event_type for e in events] == ["FLEX_AUDIT"]
        assert events[0].payload["discrepancies"] == []

    @pytest.mark.asyncio
    async def test_missing_execution_is_flagged(self, session_maker):
        result = await _audit(session_maker, [_trade(), _trade(exec_id="exec-ghost")])
        assert any(d.startswith("MISSING_FROM_LEDGER exec exec-ghost") for d in result.discrepancies)

    @pytest.mark.asyncio
    async def test_unknown_order_ref_is_flagged(self, session_maker):
        result = await _audit(session_maker, [_trade(exec_id="exec2", ref="basis:B09:o_nope:open")])
        assert any(d.startswith("UNKNOWN_ORDER_REF basis:B09") for d in result.discrepancies)

    @pytest.mark.asyncio
    async def test_quantity_or_price_mismatch_is_flagged(self, session_maker):
        result = await _audit(session_maker, [_trade(price=1.15)])
        assert any(d.startswith("FILL_MISMATCH exec exec1") for d in result.discrepancies)

    @pytest.mark.asyncio
    async def test_foreign_activity_is_ignored(self, session_maker):
        # Manual trades in the same paper account are not ours to audit.
        result = await _audit(session_maker, [_trade(), _trade(exec_id="exec-m", ref="manual-trade")])
        assert result.clean
        assert result.trades_ours == 1

    @pytest.mark.asyncio
    async def test_export_without_any_order_refs_is_a_finding(self, session_maker):
        # The §4.5 empirical question: if orderRef never survives into the
        # export, the audit chain is blind and must say so loudly.
        result = await _audit(session_maker, [_trade(exec_id="a", ref=""), _trade(exec_id="b", ref="")])
        assert any(d.startswith("NO_ORDER_REFS_IN_EXPORT") for d in result.discrepancies)

    @pytest.mark.asyncio
    async def test_empty_statement_is_clean(self, session_maker):
        result = await _audit(session_maker, [])
        assert result.clean and result.trades_total == 0

    @pytest.mark.asyncio
    async def test_acked_missing_execution_is_suppressed_not_re_alerted(self, session_maker):
        # #544: a corrected discrepancy (12 lost exec_ids, explained through
        # the resolution endpoints) must stop re-alerting at urgent priority
        # every week — the sole long-horizon detector must stay legible.
        async with session_maker() as session:
            session.add(FlexAckModel(exec_id="exec-ghost", reason="restored from backup, cash-adjusted", acked_at="t0"))
            await session.commit()
        result = await _audit(session_maker, [_trade(), _trade(exec_id="exec-ghost")])
        assert result.clean  # no discrepancy surfaced
        assert result.acknowledged == 1
        async with session_maker() as session:
            events = (await session.execute(select(AuditEventModel).filter_by(event_type="FLEX_AUDIT"))).scalars().all()
        assert events[0].payload["acknowledged"] == 1

    @pytest.mark.asyncio
    async def test_acked_unknown_order_ref_is_suppressed(self, session_maker):
        async with session_maker() as session:
            session.add(FlexAckModel(exec_id="exec-mystery", reason="manual close, cash-adjusted", acked_at="t0"))
            await session.commit()
        result = await _audit(session_maker, [_trade(), _trade(exec_id="exec-mystery", ref="basis:B09:o_nope:open")])
        assert result.clean
        assert result.acknowledged == 1

    @pytest.mark.asyncio
    async def test_ack_covering_only_one_of_two_leaves_the_other_alerting(self, session_maker):
        async with session_maker() as session:
            session.add(FlexAckModel(exec_id="exec-ghost", reason="explained", acked_at="t0"))
            await session.commit()
        result = await _audit(session_maker, [_trade(), _trade(exec_id="exec-ghost"), _trade(exec_id="exec-real")])
        assert not result.clean
        assert result.acknowledged == 1
        assert any(d.startswith("MISSING_FROM_LEDGER exec exec-real") for d in result.discrepancies)
        assert not any("exec-ghost" in d for d in result.discrepancies)


async def _add_order_and_fill(maker, order_id, exec_id, qty, price, ref):
    async with maker() as session:
        session.add(
            OrderModel(
                id=order_id,
                book_id="B01",
                position_id=None,
                order_ref=ref,
                ib_order_id=99,
                ib_perm_id=9900,
                action="OPEN",
                combo_legs={"legs": [], "quantity": 1},
                order_type="LIMIT",
                limit_price=-1.0,
                decision_midpoint=-1.0,
                status="FILLED",
                encumbered_risk=0.0,
            )
        )
        session.add(
            FillModel(
                exec_id=exec_id,
                order_id=order_id,
                book_id="B01",
                con_id=1,
                side="SLD",
                quantity=qty,
                price=price,
                commission=1.1,
                fill_time="2026-08-14T20:00:00+00:00",
                raw={},
            )
        )
        await session.commit()


class TestExecIdNormalization:
    """#631: the 2026-08-22 Saturday flex audit's first real run reported 6
    false MISSING_FROM_LEDGER rows purely from the API/ledger side's extra
    trailing version/correction segment that the Flex export omits."""

    _REF = "basis:B01:o_suffix:open"
    _LEDGER_ID = "00020057.6a86a40d.02.01.01"  # API/ledger form (5 segments)
    _FLEX_ID = "00020057.6a86a40d.02.01"  # Flex-reported form (4 segments) — the SAME execution

    @pytest.mark.asyncio
    async def test_ledger_id_with_trailing_suffix_matches_the_shorter_flex_id(self, session_maker):
        await _add_order_and_fill(session_maker, "o_suffix", self._LEDGER_ID, 1.0, 2.5, self._REF)
        result = await _audit(session_maker, [_trade(), _trade(exec_id=self._FLEX_ID, ref=self._REF, price=2.5)])
        assert result.clean
        assert not any("00020057" in d for d in result.discrepancies)

    @pytest.mark.asyncio
    async def test_genuinely_absent_execution_still_flagged_after_normalization(self, session_maker):
        # A ledger id that shares no base with anything on the Flex side —
        # normalization must never manufacture a match out of nothing.
        result = await _audit(session_maker, [_trade(), _trade(exec_id="00099999.deadbeef.01.01")])
        assert any(d.startswith("MISSING_FROM_LEDGER exec 00099999.deadbeef.01.01") for d in result.discrepancies)

    @pytest.mark.asyncio
    async def test_a_bust_and_correction_pair_still_matches_on_the_shared_base(self, session_maker):
        # "corrections get new ids" (FillModel's own comment) — the ledger
        # can hold BOTH the busted original and its correction as separate
        # rows sharing the same base; Flex reports the execution once, at
        # its final (corrected) quantity/price, and must still match.
        await _add_order_and_fill(
            session_maker, "o_busted", "00020057.6a86a40d.02.01.01", 1.0, 2.0, f"{self._REF}:busted"
        )  # busted
        await _add_order_and_fill(
            session_maker, "o_corrected", "00020057.6a86a40d.02.01.02", 1.0, 2.5, self._REF
        )  # corrected
        result = await _audit(session_maker, [_trade(), _trade(exec_id=self._FLEX_ID, ref=self._REF, price=2.5)])
        assert result.clean

    @pytest.mark.asyncio
    async def test_a_ledger_id_that_is_already_base_form_is_not_over_stripped(self, session_maker):
        # A ledger exec id can itself contain a dot with nothing to strip
        # (e.g. it's already the base form Flex reports). Normalizing it
        # unconditionally would truncate a real segment and manufacture a
        # false MISSING_FROM_LEDGER — the opposite of what #631 fixes.
        base_id = "00020057.6a86a40d"
        await _add_order_and_fill(session_maker, "o_base", base_id, 1.0, 3.0, self._REF)
        result = await _audit(session_maker, [_trade(), _trade(exec_id=base_id, ref=self._REF, price=3.0)])
        assert result.clean
        assert not any("00020057" in d for d in result.discrepancies)

    @pytest.mark.asyncio
    async def test_ack_keyed_on_the_flex_side_id_is_unaffected_by_ledger_normalization(self, session_maker):
        # #544 ack keying: the console panel's ack form always submits the
        # exec id straight from the discrepancy text (trade.exec_id, the
        # Flex/short form) — never the ledger's longer form. A pre-existing
        # ack for a DIFFERENT, still-genuinely-missing execution must keep
        # suppressing correctly in the same run that also normalizes an
        # unrelated ledger id — the two mechanisms must not interfere.
        await _add_order_and_fill(session_maker, "o_suffix", self._LEDGER_ID, 1.0, 2.5, self._REF)
        async with session_maker() as session:
            session.add(FlexAckModel(exec_id="exec-ghost", reason="restored from backup", acked_at="t0"))
            await session.commit()
        result = await _audit(
            session_maker,
            [
                _trade(),  # exec1 — matches directly, as always
                _trade(exec_id=self._FLEX_ID, ref=self._REF, price=2.5),  # normalized match, no ack involved
                _trade(exec_id="exec-ghost"),  # genuinely missing, suppressed by the pre-existing ack
            ],
        )
        assert result.clean
        assert result.acknowledged == 1


STATEMENT_XML = f"""<FlexQueryResponse queryName="basis" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="DUR925279">
      <Trades>
        <Trade ibExecID="exec1" orderReference="{REF}" quantity="-1" tradePrice="1.05" ibCommission="-1.1" assetCategory="OPT"/>
        <Trade orderReference="{REF}" quantity="-1" tradePrice="1.05"/>
        <Trade ibExecID="exec_bag" orderReference="{REF}" quantity="-1" tradePrice="3.08" assetCategory="BAG"/>
      </Trades>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>"""


class TestParsing:
    def test_parse_trades_reads_executions_and_skips_summary_rows(self):
        # Skipped: the row without ibExecID (order-level summary) and the
        # BAG row (#352 — combo-level duplicate of the leg executions, which
        # the nightly capture never ledgers; keeping it would raise a false
        # MISSING_FROM_LEDGER on every combo fill each week).
        trades = fa.parse_trades(ET.fromstring(STATEMENT_XML))
        assert len(trades) == 1
        assert trades[0] == fa.FlexTrade(exec_id="exec1", order_ref=REF, quantity=1.0, price=1.05, commission=1.1)


def _resp(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text, raise_for_status=lambda: None)


_SEND_OK = "<FlexStatementResponse><Status>Success</Status><ReferenceCode>RC1</ReferenceCode></FlexStatementResponse>"
_IN_PROGRESS = (
    "<FlexStatementResponse><ErrorCode>1019</ErrorCode><ErrorMessage>generating</ErrorMessage></FlexStatementResponse>"
)


class TestProtocol:
    def test_send_then_get(self):
        with patch.object(fa.httpx.Client, "get", side_effect=[_resp(_SEND_OK), _resp(STATEMENT_XML)]):
            statement = fa.fetch_flex_statement("tok", "q1")
        assert statement.tag == "FlexQueryResponse"

    def test_in_progress_is_retried(self, monkeypatch):
        monkeypatch.setattr(fa.time, "sleep", lambda _s: None)
        with patch.object(
            fa.httpx.Client, "get", side_effect=[_resp(_SEND_OK), _resp(_IN_PROGRESS), _resp(STATEMENT_XML)]
        ):
            statement = fa.fetch_flex_statement("tok", "q1")
        assert statement.tag == "FlexQueryResponse"

    def test_send_failure_raises(self):
        bad = "<FlexStatementResponse><Status>Fail</Status><ErrorCode>1012</ErrorCode><ErrorMessage>bad token</ErrorMessage></FlexStatementResponse>"
        with patch.object(fa.httpx.Client, "get", return_value=_resp(bad)), pytest.raises(fa.FlexError, match="1012"):
            fa.fetch_flex_statement("tok", "q1")

    @pytest.mark.asyncio
    async def test_missing_config_raises(self, monkeypatch):
        monkeypatch.delenv("IBKR_FLEX_TOKEN", raising=False)
        monkeypatch.delenv("IBKR_FLEX_QUERY_ID", raising=False)
        with pytest.raises(fa.FlexError, match="not set"):
            await fa.run_flex_audit()


class TestMainCrashAlerting:
    """#607: init_db() used to sit OUTSIDE the crash-alerting try block in
    main() — a schema/DB-open failure there (reproduced manually: pointing
    DATABASE_URL at a path whose parent directory doesn't exist) escaped as
    a bare unhandled traceback with Python's default exit code — no audit
    row, no ntfy. main() now wraps init_db() in the same try/except as
    run_flex_audit(), matching gateway_lifecycle.py's parity pattern of one
    crash boundary around the whole entrypoint."""

    def _capture_alert_crash(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "backend.operator.alert_crash",
            lambda title, body, priority="urgent", event_type="CRASH_ALERT": calls.append(
                (title, body, priority, event_type)
            ),
        )
        monkeypatch.setattr("backend.operator.send_ntfy", lambda *a, **k: False)
        return calls

    @pytest.mark.asyncio
    async def test_init_db_failure_is_alerted_not_left_unhandled(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BASIS_LOG_DIR", str(tmp_path / "logs"))
        monkeypatch.setenv("IBKR_FLEX_TOKEN", "tok")
        monkeypatch.setenv("IBKR_FLEX_QUERY_ID", "q1")
        calls = self._capture_alert_crash(monkeypatch)

        async def _boom():
            raise RuntimeError("disk full")

        monkeypatch.setattr("backend.database.init_db", _boom)

        with pytest.raises(SystemExit) as exc_info:
            await fa.main()

        assert exc_info.value.code == 1
        assert len(calls) == 1
        title, body, _priority, event_type = calls[0]
        assert title == "basis flex audit CRASHED"
        assert "disk full" in body
        assert event_type == "CRASH_ALERT"  # a genuine crash, not a config/scheduler condition

    @pytest.mark.asyncio
    async def test_missing_flex_config_still_alerts_as_scheduler_not_crash(self, monkeypatch, tmp_path):
        # A known FlexError (missing config) discovered AFTER init_db() must
        # keep its SCHEDULER_ALERT classification (#472) — widening the try
        # block must not sweep it into the generic CRASHED path.
        monkeypatch.setenv("BASIS_LOG_DIR", str(tmp_path / "logs"))
        monkeypatch.delenv("IBKR_FLEX_TOKEN", raising=False)
        monkeypatch.delenv("IBKR_FLEX_QUERY_ID", raising=False)
        calls = self._capture_alert_crash(monkeypatch)

        async def _noop_init_db():
            return None

        monkeypatch.setattr("backend.database.init_db", _noop_init_db)

        with pytest.raises(SystemExit) as exc_info:
            await fa.main()

        assert exc_info.value.code == 1
        assert len(calls) == 1
        title, _body, _priority, event_type = calls[0]
        assert title == "basis flex audit: FAILED"
        assert event_type == "SCHEDULER_ALERT"

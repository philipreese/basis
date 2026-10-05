"""#1131: the research ledgers and the operator picks book (P01).

Against a database seeded by the real init_db (so P01 arrives exactly as
production seeds it). The attribution paths first, since they are what keeps
a hand-bought pick from halting the account: a recorded buy reconciles
clean, an unrecorded one is ordinary drift, and B36's holding of the same
symbol stays B36's. Then the fail-closed refusals, the cap, append-only, and
the brief's snapshot integrity check."""

import json
from pathlib import Path

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import research
from backend.broker import FillInfo, LegPosition
from backend.models import (
    AppendOnlyViolationError,
    AuditEventModel,
    BookModel,
    OperatorPickFillModel,
    OperatorPickFillRequest,
    OperatorPickModel,
    OperatorPickRequest,
    ResearchBriefCreate,
    ResearchCandidateCreate,
    ResearchCandidateModel,
    ResearchShortlistPositionModel,
    ShareHoldingModel,
    TradingControlModel,
)
from backend.reconciliation import ORPHAN, SHARE_DRIFT, BrokerSnapshot, compare_books, run_reconciliation
from backend.research_snapshot import MANIFEST, content_hash_of, sha256_file
from backend.seeds import MANUAL_BOOKS, PICKS_BOOK_ID
from backend.states import BOOK_MANUAL_STATUS, BOOK_OPS_STATUS
from backend.trading_control import ACTIVE, HALT_ENTRIES

CAP_VAR = "BASIS_MANUAL_CAP_P01"
CAP = 1000.0  # synthetic; the real cap is private (.env.live), never in the repo


@pytest.fixture
def maker(tmp_path, monkeypatch):
    import backend.database as db_mod

    url = f"sqlite+aiosqlite:///{(tmp_path / 'research.db').as_posix()}"
    monkeypatch.setattr(db_mod, "DATABASE_URL", url)
    engine = create_async_engine(url)
    m = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(db_mod, "async_session_maker", m)
    # Research writes run against the live database only.
    monkeypatch.setattr(db_mod, "TRADING_MODE", "live")
    monkeypatch.delenv(CAP_VAR, raising=False)
    return db_mod, m


async def _seed(maker_pair) -> async_sessionmaker:
    db_mod, m = maker_pair
    await db_mod.init_db()
    return m


async def _resume_p01(m) -> None:
    async with m() as session:
        await session.execute(
            update(TradingControlModel).where(TradingControlModel.scope == PICKS_BOOK_ID).values(state=ACTIVE)
        )
        await session.commit()


def _write_snapshot(root: Path, prices: dict[str, float]) -> tuple[str, str]:
    """A minimal snapshot folder with a valid manifest. Returns (path, hash)."""
    folder = root / "20261005T213000Z"
    folder.mkdir(parents=True)
    (folder / "prices.json").write_text(
        json.dumps({s: {"close": p, "source": "screener"} for s, p in prices.items()}), encoding="utf-8"
    )
    files = {"prices.json": sha256_file(folder / "prices.json")}
    digest = content_hash_of(files)
    (folder / MANIFEST).write_text(json.dumps({"files": files, "content_hash": digest}), encoding="utf-8")
    return str(folder), digest


async def _snapshot(m, tmp_path, *, status="COMPLETE", kind="NIGHTLY", prices=None) -> str:
    path, digest = _write_snapshot(tmp_path / "snaps", prices or {"ABCD": 20.0, "WXYZ": 50.0, "SCHB": 25.0})
    async with m() as session:
        await research.record_snapshot(
            session,
            snapshot_id="20261005T213000Z",
            kind=kind,
            as_of="2026-10-05",
            created_at="2026-10-05T21:30:00+00:00",
            path=path,
            content_hash=digest,
            status=status,
            reasons=[] if status == "COMPLETE" else ["prices: 1 of 3 failed"],
            counts={"universe": 3},
        )
    return "20261005T213000Z"


def _brief(snapshot_id: str, symbols=("ABCD", "WXYZ"), kind="NIGHTLY") -> ResearchBriefCreate:
    return ResearchBriefCreate(
        snapshot_id=snapshot_id,
        kind=kind,
        model_id="model-pinned-20261001",
        prompt_hash="abc123",
        summary="two new candidates",
        candidates=[
            ResearchCandidateCreate(symbol=s, thesis=f"{s} thesis", risks="risks", proves_wrong="margin falls")
            for s in symbols
        ],
    )


async def _candidates(m) -> dict[str, int]:
    async with m() as session:
        rows = (await session.execute(select(ResearchCandidateModel))).scalars().all()
    return {r.symbol: r.id for r in rows}


async def _pick(m, tmp_path, monkeypatch, symbol="ABCD", symbols=("ABCD", "WXYZ")) -> int:
    """A brief, a resumed P01, a cap, and a PICK on *symbol*. Returns the pick id."""
    snap = await _snapshot(m, tmp_path, prices={s: 20.0 for s in {*symbols, "SCHB"}})
    async with m() as session:
        await research.record_brief(session, _brief(snap, symbols))
    await _resume_p01(m)
    monkeypatch.setenv(CAP_VAR, str(CAP))
    cid = (await _candidates(m))[symbol]
    async with m() as session:
        pick = await research.record_pick(session, OperatorPickRequest(candidate_id=cid, decision="PICK"))
    return pick.id


def _buy(qty: float, price: float = 20.0, exec_id: str | None = None) -> OperatorPickFillRequest:
    return OperatorPickFillRequest(
        side="BUY", quantity=qty, price=price, commission=1.0, executed_at="2026-10-06T10:00:00-04:00", exec_id=exec_id
    )


def _sell(qty: float, price: float = 22.0) -> OperatorPickFillRequest:
    return OperatorPickFillRequest(side="SELL", quantity=qty, price=price, executed_at="2026-10-07T10:00:00-04:00")


def _stk(symbol: str, qty: float) -> LegPosition:
    return LegPosition(con_id=hash(symbol) % 10_000, symbol=symbol, sec_type="STK", position=qty, avg_cost=1.0)


# ---------------------------------------------------------------------------
# Seeding and the every-book readers
# ---------------------------------------------------------------------------


class TestSeeding:
    @pytest.mark.asyncio
    async def test_p01_is_a_manual_book_seeded_halted(self, maker):
        m = await _seed(maker)
        async with m() as session:
            book = await session.get(BookModel, PICKS_BOOK_ID)
            control = await session.get(TradingControlModel, PICKS_BOOK_ID)
        assert book.status == BOOK_MANUAL_STATUS
        assert book.config == {}
        assert control.state == HALT_ENTRIES
        assert [b["id"] for b in MANUAL_BOOKS] == [PICKS_BOOK_ID]

    @pytest.mark.asyncio
    async def test_restart_does_not_resync_p01(self, maker):
        db_mod, m = maker
        await _seed(maker)
        await db_mod.init_db()
        async with m() as session:
            book = await session.get(BookModel, PICKS_BOOK_ID)
            synced = (await session.execute(select(AuditEventModel).filter_by(book_id=PICKS_BOOK_ID))).scalars().all()
        assert book.config_version == 1
        assert not [e for e in synced if e.event_type == "BOOK_CONFIG_SYNCED"]

    @pytest.mark.asyncio
    async def test_p01_is_not_a_lab_book_anywhere(self, maker, monkeypatch):
        from backend.console import book_summaries
        from backend.empirical_null_drill import EXCLUDED_BOOK_IDS

        db_mod, m = maker
        monkeypatch.setattr(db_mod, "TRADING_MODE", "paper")
        await _seed(maker)
        async with m() as session:
            ids = {s.id for s in await book_summaries(session)}
        assert PICKS_BOOK_ID not in ids
        assert "R01" not in ids
        assert PICKS_BOOK_ID in EXCLUDED_BOOK_IDS

    def test_not_lab_statuses_cover_ops_and_manual(self):
        from backend.states import BOOK_ACTIVE_STATUS, BOOK_MANAGED_STATUSES, BOOK_NOT_LAB_STATUSES

        assert BOOK_NOT_LAB_STATUSES == {BOOK_OPS_STATUS, BOOK_MANUAL_STATUS}
        assert BOOK_MANUAL_STATUS not in BOOK_MANAGED_STATUSES
        assert BOOK_MANUAL_STATUS != BOOK_ACTIVE_STATUS


# ---------------------------------------------------------------------------
# Attribution: what reconciliation expects
# ---------------------------------------------------------------------------


class TestAttribution:
    @pytest.mark.asyncio
    async def test_recorded_buy_reconciles_clean(self, maker, tmp_path, monkeypatch):
        m = await _seed(maker)
        pick_id = await _pick(m, tmp_path, monkeypatch)
        async with m() as session:
            await research.record_pick_fill(session, pick_id, _buy(10))
        async with m() as session:
            comparison = await compare_books(session, BrokerSnapshot(positions=(_stk("ABCD", 10),)))
        assert comparison.expected_shares == {"ABCD": 10.0}
        assert not [d for d in comparison.drifts if d.sec_type == "STK"]

    @pytest.mark.asyncio
    async def test_unrecorded_hand_buy_is_drift_and_halts_global(self, maker):
        m = await _seed(maker)
        async with m() as session:
            result = await run_reconciliation(session, BrokerSnapshot(positions=(_stk("ABCD", 10),)))
        assert [(d.kind, d.key) for d in result.drifts] == [(ORPHAN, "ABCD")]
        async with m() as session:
            assert (await session.get(TradingControlModel, "GLOBAL")).state == HALT_ENTRIES

    @pytest.mark.asyncio
    async def test_more_shares_than_recorded_is_share_drift(self, maker, tmp_path, monkeypatch):
        m = await _seed(maker)
        pick_id = await _pick(m, tmp_path, monkeypatch)
        async with m() as session:
            await research.record_pick_fill(session, pick_id, _buy(10))
        async with m() as session:
            comparison = await compare_books(session, BrokerSnapshot(positions=(_stk("ABCD", 15),)))
        (drift,) = [d for d in comparison.drifts if d.sec_type == "STK"]
        assert (drift.kind, drift.expected_qty, drift.unexpected_qty) == (SHARE_DRIFT, 10.0, 5.0)

    @pytest.mark.asyncio
    async def test_unrecorded_hand_sale_is_share_drift(self, maker, tmp_path, monkeypatch):
        m = await _seed(maker)
        pick_id = await _pick(m, tmp_path, monkeypatch)
        async with m() as session:
            await research.record_pick_fill(session, pick_id, _buy(10))
        async with m() as session:
            comparison = await compare_books(session, BrokerSnapshot(positions=()))
        (drift,) = [d for d in comparison.drifts if d.sec_type == "STK"]
        assert (drift.kind, drift.key, drift.broker_qty) == (SHARE_DRIFT, "ABCD", 0.0)

    @pytest.mark.asyncio
    async def test_recorded_sale_reconciles_clean(self, maker, tmp_path, monkeypatch):
        m = await _seed(maker)
        pick_id = await _pick(m, tmp_path, monkeypatch)
        async with m() as session:
            await research.record_pick_fill(session, pick_id, _buy(10))
            await research.record_pick_fill(session, pick_id, _sell(10))
        async with m() as session:
            comparison = await compare_books(session, BrokerSnapshot(positions=()))
        assert comparison.expected_shares == {}
        assert not comparison.drifts

    @pytest.mark.asyncio
    async def test_b36_and_picks_holdings_of_one_symbol_stay_separate(self, maker, tmp_path, monkeypatch):
        m = await _seed(maker)
        pick_id = await _pick(m, tmp_path, monkeypatch, symbol="SCHB", symbols=("SCHB",))
        async with m() as session:
            session.add(ShareHoldingModel(book_id="B36", symbol="SCHB", quantity=40.0, updated_at="2026-10-01"))
            await session.commit()
            await research.record_pick_fill(session, pick_id, _buy(5))
        async with m() as session:
            clean = await compare_books(session, BrokerSnapshot(positions=(_stk("SCHB", 45),)))
            short = await compare_books(session, BrokerSnapshot(positions=(_stk("SCHB", 40),)))
            b36 = await session.get(ShareHoldingModel, ("B36", "SCHB"))
            p01_rows = (await session.execute(select(ShareHoldingModel).filter_by(book_id=PICKS_BOOK_ID))).all()
        assert clean.expected_shares == {"SCHB": 45.0} and not clean.drifts
        assert [(d.kind, d.expected_qty) for d in short.drifts] == [(SHARE_DRIFT, 45.0)]
        assert b36.quantity == 40.0  # B36's ledger is untouched by the pick
        assert p01_rows == []  # picks never land in share_holdings (no flatten, no rebalance reads them)

    @pytest.mark.asyncio
    async def test_fills_count_only_on_a_manual_book(self, maker, tmp_path, monkeypatch):
        m = await _seed(maker)
        pick_id = await _pick(m, tmp_path, monkeypatch)
        async with m() as session:
            await research.record_pick_fill(session, pick_id, _buy(10))
            await session.execute(update(BookModel).where(BookModel.id == PICKS_BOOK_ID).values(status=BOOK_OPS_STATUS))
            await session.commit()
        async with m() as session:
            assert await research.picks_book_expected_shares(session) == {}
            comparison = await compare_books(session, BrokerSnapshot(positions=(_stk("ABCD", 10),)))
        assert [d.kind for d in comparison.drifts] == [ORPHAN]

    @pytest.mark.asyncio
    async def test_recorded_exec_id_is_not_an_unknown_ref_execution(self, maker, tmp_path, monkeypatch):
        m = await _seed(maker)
        pick_id = await _pick(m, tmp_path, monkeypatch)
        async with m() as session:
            await research.record_pick_fill(session, pick_id, _buy(10, exec_id="0001.01"))
        execs = (
            FillInfo("0001.01", 1, "BOT", 10, 20.0, "", 1.0, "2026-10-06T10:00:00"),
            FillInfo("0002.01", 2, "BOT", 3, 9.0, "", 1.0, "2026-10-06T11:00:00"),
        )
        async with m() as session:
            result = await run_reconciliation(session, BrokerSnapshot(positions=(_stk("ABCD", 10),), executions=execs))
        assert result.unknown_ref_exec_ids == ("0002.01",)
        assert result.clean


# ---------------------------------------------------------------------------
# Marking picks
# ---------------------------------------------------------------------------


class TestRecordPick:
    async def _setup(self, m, tmp_path):
        snap = await _snapshot(m, tmp_path)
        async with m() as session:
            await research.record_brief(session, _brief(snap))
        return await _candidates(m)

    @pytest.mark.asyncio
    async def test_pick_refused_while_p01_is_halted(self, maker, tmp_path, monkeypatch):
        m = await _seed(maker)
        cands = await self._setup(m, tmp_path)
        monkeypatch.setenv(CAP_VAR, str(CAP))
        async with m() as session:
            with pytest.raises(research.ResearchError, match="RESUME"):
                await research.record_pick(session, OperatorPickRequest(candidate_id=cands["ABCD"], decision="PICK"))

    @pytest.mark.asyncio
    async def test_pass_is_recorded_even_while_halted(self, maker, tmp_path):
        m = await _seed(maker)
        cands = await self._setup(m, tmp_path)
        async with m() as session:
            row = await research.record_pick(
                session, OperatorPickRequest(candidate_id=cands["ABCD"], decision="PASS", note="too thin")
            )
        assert (row.decision, row.symbol, row.book_id, row.note) == ("PASS", "ABCD", PICKS_BOOK_ID, "too thin")
        assert row.decided_at

    @pytest.mark.asyncio
    async def test_pick_refused_without_a_cap(self, maker, tmp_path, monkeypatch):
        m = await _seed(maker)
        cands = await self._setup(m, tmp_path)
        await _resume_p01(m)
        async with m() as session:
            with pytest.raises(research.ResearchError, match=CAP_VAR):
                await research.record_pick(session, OperatorPickRequest(candidate_id=cands["ABCD"], decision="PICK"))
        monkeypatch.setenv(CAP_VAR, "lots")
        async with m() as session:
            with pytest.raises(research.ResearchError, match="unset or malformed"):
                await research.record_pick(session, OperatorPickRequest(candidate_id=cands["ABCD"], decision="PICK"))

    @pytest.mark.asyncio
    async def test_pick_refused_with_no_room_left(self, maker, tmp_path, monkeypatch):
        m = await _seed(maker)
        pick_id = await _pick(m, tmp_path, monkeypatch)
        async with m() as session:
            await research.record_pick_fill(session, pick_id, _buy(49.95))  # 999 + 1 commission = the cap
        cands = await _candidates(m)
        async with m() as session:
            with pytest.raises(research.ResearchError, match="no room"):
                await research.record_pick(session, OperatorPickRequest(candidate_id=cands["WXYZ"], decision="PICK"))

    @pytest.mark.asyncio
    async def test_one_decision_per_candidate(self, maker, tmp_path):
        m = await _seed(maker)
        cands = await self._setup(m, tmp_path)
        async with m() as session:
            await research.record_pick(session, OperatorPickRequest(candidate_id=cands["ABCD"], decision="PASS"))
            with pytest.raises(research.ResearchError, match="already has a decision"):
                await research.record_pick(session, OperatorPickRequest(candidate_id=cands["ABCD"], decision="PASS"))

    @pytest.mark.asyncio
    async def test_unknown_candidate_refused(self, maker):
        m = await _seed(maker)
        async with m() as session:
            with pytest.raises(research.ResearchError, match="no candidate"):
                await research.record_pick(session, OperatorPickRequest(candidate_id=999, decision="PASS"))

    @pytest.mark.asyncio
    async def test_missing_picks_book_refused(self, maker, tmp_path):
        m = await _seed(maker)
        cands = await self._setup(m, tmp_path)
        async with m() as session:
            await session.execute(update(BookModel).where(BookModel.id == PICKS_BOOK_ID).values(status=BOOK_OPS_STATUS))
            await session.commit()
            with pytest.raises(research.ResearchError, match="not a MANUAL book"):
                await research.record_pick(session, OperatorPickRequest(candidate_id=cands["ABCD"], decision="PASS"))

    @pytest.mark.asyncio
    async def test_paper_database_refuses_every_write(self, maker, tmp_path):
        db_mod, m = maker
        await _seed(maker)
        db_mod.TRADING_MODE = "paper"  # restored by monkeypatch at teardown
        async with m() as session:
            with pytest.raises(research.ResearchError, match="live database only"):
                await research.record_pick(session, OperatorPickRequest(candidate_id=1, decision="PASS"))
            with pytest.raises(research.ResearchError, match="live database only"):
                await research.record_pick_fill(session, 1, _buy(1))


# ---------------------------------------------------------------------------
# Recording fills, and the cap
# ---------------------------------------------------------------------------


class TestRecordFill:
    @pytest.mark.asyncio
    async def test_buy_within_cap_is_recorded_without_breach(self, maker, tmp_path, monkeypatch):
        m = await _seed(maker)
        pick_id = await _pick(m, tmp_path, monkeypatch)
        async with m() as session:
            result = await research.record_pick_fill(session, pick_id, _buy(10, exec_id="x1"))
        assert not result.cap_breached and result.note is None
        assert (result.fill.symbol, result.fill.side, result.fill.quantity, result.fill.exec_id) == (
            "ABCD",
            "BUY",
            10,
            "x1",
        )

    @pytest.mark.asyncio
    async def test_over_cap_buy_is_recorded_and_halts_the_book(self, maker, tmp_path, monkeypatch):
        m = await _seed(maker)
        pick_id = await _pick(m, tmp_path, monkeypatch)
        async with m() as session:
            result = await research.record_pick_fill(session, pick_id, _buy(60))
        assert result.cap_breached and "over its private cap" in result.note
        async with m() as session:
            fills = (await session.execute(select(OperatorPickFillModel))).scalars().all()
            control = await session.get(TradingControlModel, PICKS_BOOK_ID)
            breach = (
                (await session.execute(select(AuditEventModel).filter_by(event_type=research.PICKS_CAP_BREACH)))
                .scalars()
                .all()
            )
            global_state = (await session.get(TradingControlModel, "GLOBAL")).state
        assert len(fills) == 1  # never refused: the trade already happened
        assert control.state == HALT_ENTRIES
        assert len(breach) == 1 and breach[0].payload["symbol"] == "ABCD"
        assert global_state == ACTIVE  # B36 and the lab are not frozen by a pick

    @pytest.mark.asyncio
    async def test_buy_with_cap_removed_counts_as_breach(self, maker, tmp_path, monkeypatch):
        m = await _seed(maker)
        pick_id = await _pick(m, tmp_path, monkeypatch)
        monkeypatch.delenv(CAP_VAR)
        async with m() as session:
            result = await research.record_pick_fill(session, pick_id, _buy(1))
        assert result.cap_breached and CAP_VAR in result.note

    @pytest.mark.asyncio
    async def test_sell_reduces_at_average_cost(self, maker, tmp_path, monkeypatch):
        m = await _seed(maker)
        pick_id = await _pick(m, tmp_path, monkeypatch)
        async with m() as session:
            await research.record_pick_fill(session, pick_id, _buy(10, price=20.0))  # cost 201
            await research.record_pick_fill(session, pick_id, _buy(10, price=30.0))  # cost 301
            result = await research.record_pick_fill(session, pick_id, _sell(5))
            view = await research.picks_book_view(session)
        assert not result.cap_breached
        (holding,) = view.holdings
        assert holding.quantity == 15
        assert holding.cost_basis == pytest.approx(502.0 * 15 / 20)
        assert view.cap_configured and view.cap_headroom == pytest.approx(CAP - holding.cost_basis)
        assert view.control_state == ACTIVE and [p.symbol for p in view.picks] == ["ABCD"]

    @pytest.mark.asyncio
    async def test_oversell_refused(self, maker, tmp_path, monkeypatch):
        m = await _seed(maker)
        pick_id = await _pick(m, tmp_path, monkeypatch)
        async with m() as session:
            await research.record_pick_fill(session, pick_id, _buy(5))
            with pytest.raises(research.ResearchError, match="cannot record a sell"):
                await research.record_pick_fill(session, pick_id, _sell(6))

    @pytest.mark.asyncio
    async def test_fill_refused_on_pass_unknown_pick_and_duplicate_exec(self, maker, tmp_path, monkeypatch):
        m = await _seed(maker)
        pick_id = await _pick(m, tmp_path, monkeypatch)
        cands = await _candidates(m)
        async with m() as session:
            passed = await research.record_pick(
                session, OperatorPickRequest(candidate_id=cands["WXYZ"], decision="PASS")
            )
            with pytest.raises(research.ResearchError, match="only a PICK"):
                await research.record_pick_fill(session, passed.id, _buy(1))
            with pytest.raises(research.ResearchError, match="no pick"):
                await research.record_pick_fill(session, 999, _buy(1))
            await research.record_pick_fill(session, pick_id, _buy(1, exec_id="dup"))
            with pytest.raises(research.ResearchError, match="already recorded"):
                await research.record_pick_fill(session, pick_id, _buy(1, exec_id="dup"))

    @pytest.mark.asyncio
    async def test_view_without_cap(self, maker):
        m = await _seed(maker)
        async with m() as session:
            view = await research.picks_book_view(session)
        assert (view.cap_configured, view.cap_headroom, view.committed_cost, view.control_state) == (
            False,
            None,
            0.0,
            HALT_ENTRIES,
        )


class TestPrivateCap:
    def test_parsing(self):
        assert research.private_manual_cap("P01", {}) is None
        assert research.private_manual_cap("P01", {CAP_VAR: "  "}) is None
        assert research.private_manual_cap("P01", {CAP_VAR: " 250.5 "}) == 250.5
        with pytest.raises(ValueError, match=f"{CAP_VAR} is not a number"):
            research.private_manual_cap("P01", {CAP_VAR: "abc"})
        for bad in ("0", "-5", "inf", "nan"):
            with pytest.raises(ValueError, match="finite, positive"):
                research.private_manual_cap("P01", {CAP_VAR: bad})


# ---------------------------------------------------------------------------
# Append-only
# ---------------------------------------------------------------------------


class TestAppendOnly:
    @pytest.mark.asyncio
    async def test_pick_and_fill_rows_cannot_be_edited_or_deleted(self, maker, tmp_path, monkeypatch):
        m = await _seed(maker)
        pick_id = await _pick(m, tmp_path, monkeypatch)
        async with m() as session:
            await research.record_pick_fill(session, pick_id, _buy(1))
        async with m() as session:
            pick = await session.get(OperatorPickModel, pick_id)
            pick.decision = "PASS"
            with pytest.raises(AppendOnlyViolationError):
                await session.flush()
        async with m() as session:
            fill = (await session.execute(select(OperatorPickFillModel))).scalar_one()
            await session.delete(fill)
            with pytest.raises(AppendOnlyViolationError):
                await session.flush()


# ---------------------------------------------------------------------------
# Snapshots and briefs
# ---------------------------------------------------------------------------


class TestSnapshotsAndBriefs:
    @pytest.mark.asyncio
    async def test_record_snapshot_validation(self, maker):
        m = await _seed(maker)
        base = {
            "snapshot_id": "s",
            "as_of": "2026-10-05",
            "created_at": "t",
            "path": "p",
            "content_hash": "h",
            "counts": {},
        }
        async with m() as session:
            with pytest.raises(research.ResearchError, match="kind"):
                await research.record_snapshot(session, kind="WEEKLY", status="COMPLETE", reasons=[], **base)
            with pytest.raises(research.ResearchError, match="status"):
                await research.record_snapshot(session, kind="NIGHTLY", status="PARTIAL", reasons=[], **base)
            with pytest.raises(research.ResearchError, match="cannot carry reasons"):
                await research.record_snapshot(session, kind="NIGHTLY", status="COMPLETE", reasons=["x"], **base)
            with pytest.raises(research.ResearchError, match="must say why"):
                await research.record_snapshot(session, kind="NIGHTLY", status="INCOMPLETE", reasons=[], **base)

    @pytest.mark.asyncio
    async def test_brief_writes_candidates_and_equal_weight_shortlist(self, maker, tmp_path):
        m = await _seed(maker)
        snap = await _snapshot(m, tmp_path)
        async with m() as session:
            brief = await research.record_brief(session, _brief(snap, symbols=("abcd", "WXYZ")))
        async with m() as session:
            shortlist = (await session.execute(select(ResearchShortlistPositionModel))).scalars().all()
            listed = await research.list_briefs(session)
            snaps = await research.list_snapshots(session)
        assert [(p.symbol, p.entry_price, p.weight, p.opened_on) for p in shortlist] == [
            ("ABCD", 20.0, 1.0, "2026-10-05"),
            ("WXYZ", 50.0, 1.0, "2026-10-05"),
        ]
        assert listed[0].id == brief.id and [c.symbol for c in listed[0].candidates] == ["ABCD", "WXYZ"]
        assert listed[0].candidates[0].proves_wrong == "margin falls"
        assert snaps[0].status == "COMPLETE" and snaps[0].counts == {"universe": 3}

    @pytest.mark.asyncio
    async def test_brief_refused_on_incomplete_or_missing_snapshot(self, maker, tmp_path):
        m = await _seed(maker)
        async with m() as session:
            with pytest.raises(research.ResearchError, match="no snapshot"):
                await research.record_brief(session, _brief("nope"))
        snap = await _snapshot(m, tmp_path, status="INCOMPLETE")
        async with m() as session:
            with pytest.raises(research.ResearchError, match="needs a COMPLETE snapshot"):
                await research.record_brief(session, _brief(snap))

    @pytest.mark.asyncio
    async def test_brief_refused_on_kind_mismatch(self, maker, tmp_path):
        m = await _seed(maker)
        snap = await _snapshot(m, tmp_path, kind="MONTHLY")
        async with m() as session:
            with pytest.raises(research.ResearchError, match="cannot read a MONTHLY snapshot"):
                await research.record_brief(session, _brief(snap))

    @pytest.mark.asyncio
    async def test_brief_refused_when_the_snapshot_was_altered(self, maker, tmp_path):
        m = await _seed(maker)
        snap = await _snapshot(m, tmp_path)
        prices = tmp_path / "snaps" / snap / "prices.json"
        prices.write_text(prices.read_text(encoding="utf-8").replace("20.0", "19.0"), encoding="utf-8")
        async with m() as session:
            with pytest.raises(research.ResearchError, match="integrity"):
                await research.record_brief(session, _brief(snap))

    @pytest.mark.asyncio
    async def test_brief_refused_for_unpriced_or_repeated_candidates(self, maker, tmp_path):
        m = await _seed(maker)
        snap = await _snapshot(m, tmp_path)
        async with m() as session:
            with pytest.raises(research.ResearchError, match="not priced in snapshot"):
                await research.record_brief(session, _brief(snap, symbols=("ABCD", "MADEUP")))
            with pytest.raises(research.ResearchError, match="once"):
                await research.record_brief(session, _brief(snap, symbols=("ABCD", "abcd")))
            assert (await session.execute(select(ResearchCandidateModel))).first() is None


# ---------------------------------------------------------------------------
# The API surface
# ---------------------------------------------------------------------------


class TestApi:
    @pytest.mark.asyncio
    async def test_endpoints(self, maker, tmp_path, monkeypatch):
        from backend.database import get_db
        from backend.main import app

        m = await _seed(maker)
        snap = await _snapshot(m, tmp_path)
        async with m() as session:
            await research.record_brief(session, _brief(snap))
        cands = await _candidates(m)

        async def override_get_db():
            async with m() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        try:
            async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.get("/api/research/snapshots")).json()[0]["id"] == snap
                assert len((await client.get("/api/research/briefs")).json()[0]["candidates"]) == 2
                halted = await client.post(
                    "/api/research/picks", json={"candidate_id": cands["ABCD"], "decision": "PICK"}
                )
                assert halted.status_code == 400
                await _resume_p01(m)
                monkeypatch.setenv(CAP_VAR, str(CAP))
                picked = await client.post(
                    "/api/research/picks", json={"candidate_id": cands["ABCD"], "decision": "PICK"}
                )
                assert picked.status_code == 200
                pick_id = picked.json()["id"]
                body = {"side": "BUY", "quantity": 5, "price": 20, "executed_at": "2026-10-06T10:00:00-04:00"}
                filled = await client.post(f"/api/research/picks/{pick_id}/fills", json=body)
                assert filled.status_code == 200 and filled.json()["cap_breached"] is False
                bad = await client.post("/api/research/picks/999/fills", json=body)
                assert bad.status_code == 400
                view = (await client.get("/api/research/picks-book")).json()
                assert view["holdings"] == [{"symbol": "ABCD", "quantity": 5.0, "cost_basis": 100.0}]
                async with m() as session:
                    await session.execute(
                        update(BookModel).where(BookModel.id == PICKS_BOOK_ID).values(status=BOOK_OPS_STATUS)
                    )
                    await session.commit()
                assert (await client.get("/api/research/picks-book")).status_code == 409
        finally:
            app.dependency_overrides.clear()

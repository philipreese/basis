"""Operator live grants (#1065, backend/live_grant.py): stage-1 grant,
step-up, manual revoke — attested, never automatic."""

import datetime
import os

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import database, live_grant
from backend.live_grant import GrantRefused, grant_stage1, revoke, step_up
from backend.models import (
    AuditEventModel,
    Base,
    BookModel,
    BookMtmHistoryModel,
    DbMetaModel,
    LiveGrantModel,
    TradingControlModel,
)
from backend.stage1 import DEMOTION_POLICY_VERSION

MENU = ["SCHB", "SCHF", "UTEN", "IAUM", "SCHH", "DBMF"]
CONFIG = {
    "envelope": {},
    "share_symbols": [*MENU, "TBIL"],
    "etf_trend": {"menu": MENU, "cash_symbol": "TBIL", "trend_months": 10},
}
STAKE_VAR = "BASIS_LIVE_STAKE_B36"  # #1098: the live stake is private, read from the overlay
ATTEST = "I signed off: the stage-1 entry bar is met for this book."
TODAY = datetime.date(2026, 10, 20)


@pytest_asyncio.fixture
async def maker(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "TRADING_MODE", "live")
    monkeypatch.setenv(STAKE_VAR, "1000")  # synthetic
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'g.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    m = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with m() as session:
        session.add(DbMetaModel(key="trading_mode", value="live"))
        for book_id, config in (("B36", CONFIG), ("B01", {"envelope": {}, "underlying": "XSP"})):
            session.add(
                BookModel(
                    id=book_id,
                    name=book_id,
                    config=config,
                    config_hash=f"hash-{book_id}",
                    starting_capital=10_000.0,
                    cash_balance=10_000.0,
                    status="ACTIVE",
                    created_at="2026-09-01T00:00:00+00:00",
                )
            )
            session.add(TradingControlModel(scope=book_id, state="ACTIVE", reason="", actor="t", changed_at="t0"))
            session.add(BookMtmHistoryModel(book_id=book_id, date="2026-10-19", mtm=10_000.0))
        await session.commit()
    yield m
    await engine.dispose()


@pytest.mark.asyncio
async def test_stage1_grant_records_hash_policy_and_sets_authority(maker):
    async with maker() as session:
        result = await grant_stage1(session, "B36", ATTEST, TODAY)
        book = await session.get(BookModel, "B36")
        grant = await session.get(LiveGrantModel, result.grant_id)
        event = (
            await session.execute(select(AuditEventModel).filter_by(event_type="LIVE_AUTHORITY_GRANTED"))
        ).scalar_one()
    assert book.live_authority == "LIVE" and book.promoted_at == grant.granted_at
    assert book.demotion_policy_version == DEMOTION_POLICY_VERSION == grant.demotion_policy_version
    assert grant.as_raced_config_hash == "hash-B36" and grant.stake == 1000.0 and grant.kind == "STAGE1"
    assert event.payload["attestation"] == ATTEST


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("book_id", "attest", "fragment"),
    [
        ("B36", "ok", "attestation"),
        ("B01", ATTEST, "options book"),
        ("B99", ATTEST, "no book"),
    ],
)
async def test_stage1_grant_refusals(maker, book_id, attest, fragment):
    async with maker() as session:
        with pytest.raises(GrantRefused, match=fragment):
            await grant_stage1(session, book_id, attest, TODAY)


@pytest.mark.asyncio
async def test_stage1_grant_refuses_unstaked_retired_live_and_unmarked(maker, monkeypatch):
    async with maker() as session:
        book = await session.get(BookModel, "B36")
        monkeypatch.delenv(STAKE_VAR)
        with pytest.raises(GrantRefused, match="no private live stake"):
            await grant_stage1(session, "B36", ATTEST, TODAY)
        monkeypatch.setenv(STAKE_VAR, "not-a-number")
        with pytest.raises(GrantRefused, match="is not a number"):
            await grant_stage1(session, "B36", ATTEST, TODAY)
        monkeypatch.setenv(STAKE_VAR, "1000")
        book.config = {**CONFIG, "stage1_stake": 1000.0}
        await session.commit()
        with pytest.raises(GrantRefused, match="seeded stage1_stake"):
            await grant_stage1(session, "B36", ATTEST, TODAY)
        book.config = CONFIG
        book.status = "RETIRED"
        await session.commit()
        with pytest.raises(GrantRefused, match="not ACTIVE"):
            await grant_stage1(session, "B36", ATTEST, TODAY)
        book.status = "ACTIVE"
        await session.commit()
        with pytest.raises(GrantRefused, match="no nightly mark"):
            await grant_stage1(session, "B36", ATTEST, datetime.date(2026, 10, 19))
        await grant_stage1(session, "B36", ATTEST, TODAY)
        with pytest.raises(GrantRefused, match="already LIVE"):
            await grant_stage1(session, "B36", ATTEST, TODAY)


@pytest.mark.asyncio
async def test_grants_refuse_a_paper_database(maker):
    async with maker() as session:
        (await session.get(DbMetaModel, "trading_mode")).value = "paper"
        await session.commit()
        with pytest.raises(GrantRefused, match="not stamped live"):
            await grant_stage1(session, "B36", ATTEST, TODAY)


CLEAN = [datetime.date(2026, 10, 30), datetime.date(2026, 11, 30), datetime.date(2026, 12, 31)]
LATER = datetime.date(2027, 1, 5)


async def _granted_then_raised(session, new_stake="5000"):
    # #1098: a step-up raises only the private overlay stake; the public
    # config and its hash stay exactly as granted.
    await grant_stage1(session, "B36", ATTEST, TODAY)
    os.environ[STAKE_VAR] = new_stake  # monkeypatch in the fixture restores it


@pytest.mark.asyncio
async def test_step_up_records_new_stake_same_hash_keeps_policy_version(maker):
    async with maker() as session:
        await _granted_then_raised(session)
        result = await step_up(session, "B36", CLEAN, ATTEST, LATER)
        grant = await session.get(LiveGrantModel, result.grant_id)
    assert grant.kind == "STEP_UP" and grant.as_raced_config_hash == "hash-B36"
    assert grant.stake == 5000.0 and grant.previous_grant_id is not None
    assert "stage1_stake" not in grant.config_snapshot  # the snapshot is the public config
    assert grant.demotion_policy_version == DEMOTION_POLICY_VERSION
    assert grant.clean_rebalance_dates == [d.isoformat() for d in CLEAN]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dates", "fragment"),
    [
        (CLEAN[:2], "exactly 3"),
        ([CLEAN[0], CLEAN[0], CLEAN[1]], "exactly 3"),
        ([datetime.date(2026, 10, 29), CLEAN[1], CLEAN[2]], "not a month-end"),
        ([datetime.date(2026, 9, 30), CLEAN[0], CLEAN[1]], "after the stage-1 grant"),
        ([CLEAN[0], CLEAN[2], datetime.date(2027, 1, 29)], "consecutive"),
    ],
)
async def test_step_up_date_rules(maker, dates, fragment):
    async with maker() as session:
        await _granted_then_raised(session)
        with pytest.raises(GrantRefused, match=fragment):
            await step_up(session, "B36", dates, ATTEST, datetime.date(2027, 2, 5))


@pytest.mark.asyncio
async def test_step_up_refuses_future_dates_smaller_stake_other_edits_and_non_live(maker, monkeypatch):
    async with maker() as session:
        with pytest.raises(GrantRefused, match="not LIVE"):
            await step_up(session, "B36", CLEAN, ATTEST, LATER)
        await _granted_then_raised(session)
        with pytest.raises(GrantRefused, match="future"):
            await step_up(session, "B36", CLEAN, ATTEST, datetime.date(2026, 12, 30))
        monkeypatch.setenv(STAKE_VAR, "500")
        with pytest.raises(GrantRefused, match="not larger") as refused:
            await step_up(session, "B36", CLEAN, ATTEST, LATER)
        assert "500" not in str(refused.value) and "1000" not in str(refused.value)  # never the value
        monkeypatch.setenv(STAKE_VAR, "5000")
        book = await session.get(BookModel, "B36")
        book.config = {**CONFIG, "etf_trend": {**CONFIG["etf_trend"], "trend_months": 12}}
        book.config_hash = "hash-B36-edited"
        await session.commit()
        with pytest.raises(GrantRefused, match="config changed since its grant"):
            await step_up(session, "B36", CLEAN, ATTEST, LATER)


@pytest.mark.asyncio
async def test_revoke_demotes_and_halts_but_never_downgrades_a_flatten(maker):
    async with maker() as session:
        await grant_stage1(session, "B36", ATTEST, TODAY)
        await revoke(session, "B36", "operator pulled the book after review")
        assert (await session.get(BookModel, "B36")).live_authority == "REVOKED"
        assert (await session.get(TradingControlModel, "B36")).state == "HALT_ENTRIES"
        (await session.get(TradingControlModel, "B01")).state = "FLATTEN_REQUESTED"
        await session.commit()
        await revoke(session, "B01", "operator pulled the book after review")
        assert (await session.get(TradingControlModel, "B01")).state == "FLATTEN_REQUESTED"
        with pytest.raises(GrantRefused, match="no book"):
            await revoke(session, "B99", "operator pulled the book after review")


@pytest.mark.asyncio
async def test_step_up_without_any_grant_row_refuses(maker):
    async with maker() as session:
        book = await session.get(BookModel, "B36")
        book.live_authority = "LIVE"
        await session.commit()
        with pytest.raises(GrantRefused, match="no recorded grant"):
            await step_up(session, "B36", CLEAN, ATTEST, LATER)
        session.add(
            LiveGrantModel(
                book_id="B36",
                kind="STEP_UP",
                granted_at="2026-10-01T00:00:00+00:00",
                as_raced_config_hash="hash-B36",
                config_snapshot=CONFIG,
                stake=500.0,
                demotion_policy_version=1,
                attestation=ATTEST,
            )
        )
        await session.commit()
        with pytest.raises(GrantRefused, match="no stage-1 grant"):
            await step_up(session, "B36", CLEAN, ATTEST, LATER)
    assert live_grant.STEP_UP_CLEAN_REBALANCES == 3


# ---------------------------------------------------------------------------
# #1101: a grant never turns trading on by itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_and_step_up_halt_an_active_book(maker):
    async with maker() as session:
        result = await grant_stage1(session, "B36", ATTEST, TODAY)
        assert result.control_state == "HALT_ENTRIES"
        assert (await session.get(TradingControlModel, "B36")).state == "HALT_ENTRIES"
        # The operator RESUMEs on the live console, then steps up: halted again.
        (await session.get(TradingControlModel, "B36")).state = "ACTIVE"
        await session.commit()
        os.environ[STAKE_VAR] = "5000"  # the fixture's monkeypatch restores it
        stepped = await step_up(session, "B36", CLEAN, ATTEST, LATER)
        assert stepped.control_state == "HALT_ENTRIES"
        assert (await session.get(TradingControlModel, "B36")).state == "HALT_ENTRIES"


@pytest.mark.asyncio
async def test_regrant_after_a_manual_revoke_and_resume_is_halted_until_resumed_again(maker):
    # The issue's scenario: revoke -> RESUME on the console -> re-grant. The
    # re-grant used to leave the book ACTIVE, trading the next armed night.
    async with maker() as session:
        await grant_stage1(session, "B36", ATTEST, TODAY)
        await revoke(session, "B36", "operator pulled the book after review")
        (await session.get(TradingControlModel, "B36")).state = "ACTIVE"
        await session.commit()
        again = await grant_stage1(session, "B36", ATTEST, TODAY)
    assert again.control_state == "HALT_ENTRIES"


@pytest.mark.asyncio
async def test_grant_never_overwrites_a_flatten_and_reports_it(maker):
    from backend.live_cli import grant_state_line

    async with maker() as session:
        (await session.get(TradingControlModel, "B36")).state = "FLATTEN_REQUESTED"
        await session.commit()
        result = await grant_stage1(session, "B36", ATTEST, TODAY)
        assert (await session.get(TradingControlModel, "B36")).state == "FLATTEN_REQUESTED"
    assert result.control_state == "FLATTEN_REQUESTED"
    assert "FLATTEN_REQUESTED" in grant_state_line("B36", result.control_state)
    assert "RESUME" in grant_state_line("B36", "HALT_ENTRIES")
    assert "check the live console" in grant_state_line("B36", "SOMETHING_ELSE")


# ---------------------------------------------------------------------------
# #1101: re-grant after a -30% drawdown revoke
# ---------------------------------------------------------------------------

REVOKED_AT = "2026-10-21T23:00:00+00:00"  # market date 2026-10-21
COOLED = datetime.date(2026, 11, 13)  # 17 trading days later
PAPER_MARKS = [d.isoformat() for d in (datetime.date(2026, 10, 21) + datetime.timedelta(days=i) for i in range(25))]


def _paper(**overrides) -> live_grant.PaperEvidence:
    values: dict = {
        "config_hash": "hash-B36",
        "status": "ACTIVE",
        "era_start": "2026-09-01",
        "mark_dates": PAPER_MARKS,
        "filled_at": ["2026-10-30T20:00:00+00:00"],
        "breach_at": [],
    }
    values.update(overrides)
    return live_grant.PaperEvidence(**values)


async def _drawdown_revoked(session) -> None:
    book = await session.get(BookModel, "B36")
    book.live_authority = "REVOKED"
    session.add(
        AuditEventModel(
            run_at=REVOKED_AT,
            book_id="B36",
            event_type="LIVE_AUTHORITY_REVOKED",
            actor="anomaly",
            payload={"rule": "STAKE_DRAWDOWN_HALT", "previous": "LIVE"},
        )
    )
    await session.commit()


def test_the_drawdown_rule_name_matches_the_anomaly_module():
    from backend.anomaly import STAKE_DRAWDOWN_HALT

    assert live_grant.STAKE_DRAWDOWN_RULE == STAKE_DRAWDOWN_HALT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("today", "evidence", "fragment"),
    [
        (datetime.date(2026, 11, 2), _paper(), "cool-down"),
        (COOLED, None, "cannot be read"),
        (COOLED, _paper(config_hash="hash-other"), "config hash differs"),
        (COOLED, _paper(filled_at=["2026-10-01T20:00:00+00:00"]), "15d paper"),  # the only fill predates the revoke
        (COOLED, _paper(mark_dates=PAPER_MARKS[:5]), "15d paper"),
        (COOLED, _paper(breach_at=["2026-10-25T20:00:00+00:00"]), "0 breach"),
        (COOLED, _paper(status="RETIRED"), "not retired"),
    ],
)
async def test_regrant_after_a_drawdown_revoke_refuses_without_cooldown_and_fresh_paper_evidence(
    maker, today, evidence, fragment
):
    async with maker() as session:
        await _drawdown_revoked(session)
        with pytest.raises(GrantRefused, match=fragment):
            await grant_stage1(session, "B36", ATTEST, today, paper_evidence=lambda book_id: evidence)
        assert (await session.get(BookModel, "B36")).live_authority == "REVOKED"
        assert (await session.get(TradingControlModel, "B36")).state == "ACTIVE"  # nothing written


@pytest.mark.asyncio
async def test_regrant_after_a_drawdown_revoke_passes_once_cooled_and_re_earned(maker):
    seen: list[str] = []

    def loader(book_id):
        seen.append(book_id)
        return _paper()

    async with maker() as session:
        await _drawdown_revoked(session)
        result = await grant_stage1(session, "B36", ATTEST, COOLED, paper_evidence=loader)
    assert seen == ["B36"] and result.control_state == "HALT_ENTRIES"


@pytest.mark.asyncio
async def test_a_first_grant_never_reads_the_paper_database(maker):
    def loader(book_id):
        raise AssertionError("no drawdown revoke, so no paper read")

    async with maker() as session:
        await grant_stage1(session, "B36", ATTEST, TODAY, paper_evidence=loader)


def test_load_paper_evidence_reads_the_paper_file_read_only(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from backend.models import ShareOrderModel

    path = tmp_path / "paper.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            BookModel(
                id="B36",
                name="B36",
                config=CONFIG,
                config_hash="hash-B36",
                starting_capital=10_000.0,
                cash_balance=10_000.0,
                status="ACTIVE",
                created_at="2026-09-01T00:00:00+00:00",
            )
        )
        db.add(BookMtmHistoryModel(book_id="B36", date="2026-10-22", mtm=10_000.0))
        db.add(
            AuditEventModel(
                run_at="2026-10-02T23:00:00+00:00",
                book_id="B36",
                event_type="BOOK_CONFIG_SYNCED",
                actor="t",
                payload={},
            )
        )
        db.add(
            AuditEventModel(
                run_at="2026-10-26T23:00:00+00:00",
                book_id="B36",
                event_type="ENVELOPE_BREACH_POSTHOC",
                actor="t",
                payload={},
            )
        )
        db.add(
            ShareOrderModel(
                id="o1",
                book_id="B36",
                order_ref="basis:B36:o1:share",
                symbol="SCHB",
                side="BUY",
                quantity=1,
                limit_price=30.0,
                decision_close=30.0,
                signal_date="2026-10-30",
                status="FILLED",
                created_at="2026-10-30T23:00:00+00:00",
                completed_at="2026-11-02T20:00:00+00:00",
                filled_quantity=1.0,
                fills=[],
            )
        )
        db.commit()
    engine.dispose()
    monkeypatch.setattr(
        "backend.env.base_env_values", lambda: {"DATABASE_URL": f"sqlite+aiosqlite:///{path.as_posix()}"}
    )
    before = path.stat().st_mtime_ns
    evidence = live_grant.load_paper_evidence("B36")
    assert evidence is not None
    assert evidence.config_hash == "hash-B36" and evidence.era_start == "2026-10-02"
    assert evidence.mark_dates == ["2026-10-22"]
    assert evidence.filled_at == ["2026-11-02T20:00:00+00:00"]
    assert evidence.breach_at == ["2026-10-26T23:00:00+00:00"]
    assert live_grant.load_paper_evidence("B99") is None
    assert path.stat().st_mtime_ns == before  # read-only
    monkeypatch.setattr("backend.env.base_env_values", lambda: {"DATABASE_URL": "sqlite:///nope/missing.db"})
    assert live_grant.load_paper_evidence("B36") is None

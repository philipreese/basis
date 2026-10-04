"""Operator live grants (#1065, backend/live_grant.py): stage-1 grant,
step-up, manual revoke — attested, never automatic."""

import datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import live_grant
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
    "stage1_stake": 1000.0,
    "share_symbols": [*MENU, "TBIL"],
    "etf_trend": {"menu": MENU, "cash_symbol": "TBIL", "trend_months": 10},
}
ATTEST = "I signed off: the stage-1 entry bar is met for this book."
TODAY = datetime.date(2026, 10, 20)


@pytest_asyncio.fixture
async def maker(tmp_path):
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
async def test_stage1_grant_refuses_unstaked_retired_live_and_unmarked(maker):
    async with maker() as session:
        book = await session.get(BookModel, "B36")
        book.config = {k: v for k, v in CONFIG.items() if k != "stage1_stake"}
        await session.commit()
        with pytest.raises(GrantRefused, match="no stage1_stake"):
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


async def _granted_then_raised(session, new_stake=5000.0):
    await grant_stage1(session, "B36", ATTEST, TODAY)
    book = await session.get(BookModel, "B36")
    book.config = {**CONFIG, "stage1_stake": new_stake}
    book.config_hash = "hash-B36-raised"
    await session.commit()


@pytest.mark.asyncio
async def test_step_up_records_new_hash_keeps_policy_version(maker):
    async with maker() as session:
        await _granted_then_raised(session)
        result = await step_up(session, "B36", CLEAN, ATTEST, LATER)
        grant = await session.get(LiveGrantModel, result.grant_id)
    assert grant.kind == "STEP_UP" and grant.as_raced_config_hash == "hash-B36-raised"
    assert grant.stake == 5000.0 and grant.previous_grant_id is not None
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
async def test_step_up_refuses_future_dates_smaller_stake_other_edits_and_non_live(maker):
    async with maker() as session:
        with pytest.raises(GrantRefused, match="not LIVE"):
            await step_up(session, "B36", CLEAN, ATTEST, LATER)
        await _granted_then_raised(session)
        with pytest.raises(GrantRefused, match="future"):
            await step_up(session, "B36", CLEAN, ATTEST, datetime.date(2026, 12, 30))
        book = await session.get(BookModel, "B36")
        book.config = {**CONFIG, "stage1_stake": 500.0}
        await session.commit()
        with pytest.raises(GrantRefused, match="not larger"):
            await step_up(session, "B36", CLEAN, ATTEST, LATER)
        book.config = {**CONFIG, "stage1_stake": 5000.0, "etf_trend": {**CONFIG["etf_trend"], "trend_months": 12}}
        await session.commit()
        with pytest.raises(GrantRefused, match="more than stage1_stake"):
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
                as_raced_config_hash="h",
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

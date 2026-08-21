"""Tests for backend/database.py's init_db seeding/sync (#548).

Covers the recovery-tail findings from Audit II Round 4:
- LOW-1: playbooks now sync to an existing DB, not just seed an empty one.
- LOW-3: concurrent first-starts don't abort init_db, and a book's config
  version bump is a conditional UPDATE rather than read-modify-write.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import SEED_PLAYBOOKS, _config_hash


@pytest.fixture
def _maker(tmp_path, monkeypatch):
    import backend.database as db_mod

    url = f"sqlite+aiosqlite:///{(tmp_path / 'init_db_test.db').as_posix()}"
    monkeypatch.setattr(db_mod, "DATABASE_URL", url)
    engine = create_async_engine(url)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    monkeypatch.setattr(db_mod, "async_session_maker", maker)
    yield db_mod, maker


class TestPlaybookSync:
    @pytest.mark.asyncio
    async def test_init_db_seeds_playbooks_on_a_fresh_db(self, _maker):
        db_mod, maker = _maker
        from backend.models import PlaybookDefinitionModel

        await db_mod.init_db()
        async with maker() as session:
            rows = (await session.execute(select(PlaybookDefinitionModel))).scalars().all()
        assert {r.id for r in rows} == {pb["id"] for pb in SEED_PLAYBOOKS}
        assert all(r.content_hash for r in rows)
        assert all(r.sync_version == 1 for r in rows)

    @pytest.mark.asyncio
    async def test_a_playbook_fix_syncs_into_an_existing_db(self, _maker):
        # #548 LOW-1: playbooks previously only seeded an EMPTY table — a
        # seeds.py playbook fix silently never reached a live DB, asymmetric
        # with the #436 book-config sync and in tension with ADR-0013.
        db_mod, maker = _maker
        from backend.models import AuditEventModel, PlaybookDefinitionModel

        await db_mod.init_db()
        seed = SEED_PLAYBOOKS[0]
        async with maker() as session:
            pb = await session.get(PlaybookDefinitionModel, (seed["id"], seed["version"]))
            pb.entry_filters = {**pb.entry_filters, "min_ivr": 999.0}  # simulate stale/hand-edited content
            pb.content_hash = "stale-hash"
            await session.commit()

        await db_mod.init_db()

        async with maker() as session:
            pb = await session.get(PlaybookDefinitionModel, (seed["id"], seed["version"]))
            audits = (
                (await session.execute(select(AuditEventModel).filter_by(event_type="PLAYBOOK_SYNCED"))).scalars().all()
            )
        assert pb.entry_filters == seed["entry_filters"]
        assert pb.content_hash == _config_hash(
            {
                "name": seed["name"],
                "underlying_ticker": seed["underlying_ticker"],
                "strategy_type": seed["strategy_type"],
                "enabled": seed.get("enabled", True),
                "entry_filters": seed["entry_filters"],
                "execution_specs": seed["execution_specs"],
                "exit_rules": seed["exit_rules"],
            }
        )
        assert pb.sync_version == 2
        assert any(a.payload["playbook_id"] == seed["id"] for a in audits)

    @pytest.mark.asyncio
    async def test_unchanged_playbooks_are_not_re_synced(self, _maker):
        db_mod, maker = _maker
        from backend.models import AuditEventModel

        await db_mod.init_db()
        await db_mod.init_db()  # a plain restart — nothing changed in seeds.py
        async with maker() as session:
            audits = (
                (await session.execute(select(AuditEventModel).filter_by(event_type="PLAYBOOK_SYNCED"))).scalars().all()
            )
        assert audits == []

    @pytest.mark.asyncio
    async def test_positions_keep_their_frozen_snapshot_through_a_sync(self, _maker):
        # #260: a running position must be exited under the rules it was
        # entered under, even if the stored playbook row changes mid-flight.
        db_mod, maker = _maker
        from backend.models import PlaybookDefinitionModel, PositionModel

        await db_mod.init_db()
        seed = SEED_PLAYBOOKS[0]
        async with maker() as session:
            pb = await session.get(PlaybookDefinitionModel, (seed["id"], seed["version"]))
            frozen_snapshot = pb.to_schema().model_dump()
            session.add(
                PositionModel(
                    id="pos_frozen",
                    underlying=seed["underlying_ticker"],
                    strategy_type=seed["strategy_type"],
                    legs=[],
                    entry_date="2026-08-01",
                    expiration_date="2026-09-01",
                    entry_premium=1.0,
                    premium_direction="CREDIT",
                    current_value_per_share=1.0,
                    contracts=1,
                    max_profit=1.0,
                    max_loss=1.0,
                    notes="",
                    rolls=0,
                    status="OPEN",
                    journal={
                        "core_thesis_rationale": "t",
                        "structural_invalidation": "t",
                        "expected_underlying_move_pct": 1.0,
                        "pre_trade_emotional_state": "Calm",
                        "pre_trade_confidence_rating": 3,
                    },
                    playbook_snapshot=frozen_snapshot,
                    book_id="B00",
                )
            )
            pb.entry_filters = {**pb.entry_filters, "min_ivr": 999.0}
            pb.content_hash = "stale-hash"
            await session.commit()

        # Re-read the stored (JSON-round-tripped) snapshot as the frozen
        # baseline — model_dump() alone still carries Python-native types
        # (e.g. a tuple) that JSON storage coerces (to a list), which would
        # be a false mismatch unrelated to the sync this test is pinning.
        async with maker() as session:
            frozen_snapshot = (await session.get(PositionModel, "pos_frozen")).playbook_snapshot

        await db_mod.init_db()

        async with maker() as session:
            pos = await session.get(PositionModel, "pos_frozen")
        assert pos.playbook_snapshot["entry_filters"]["min_ivr"] != 999.0
        assert pos.playbook_snapshot == frozen_snapshot


class TestInitDbConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_integrity_error_is_retried_not_raised(self, _maker, monkeypatch):
        # #548 LOW-3: two simultaneous first-starts can both see empty seed
        # rows and both INSERT the same primary key — transient (the loser's
        # data is identical to the winner's). init_db must retry, not crash
        # a process's startup over it.
        db_mod, _maker_fn = _maker
        from sqlalchemy.exc import IntegrityError

        original = db_mod._seed_and_sync
        calls = {"n": 0}

        async def flaky(session, force_seed):
            calls["n"] += 1
            if calls["n"] == 1:
                raise IntegrityError("stmt", {}, Exception("UNIQUE constraint failed"))
            return await original(session, force_seed)

        monkeypatch.setattr(db_mod, "_seed_and_sync", flaky)
        await db_mod.init_db()  # must not raise
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_trading_mode_stamp_race_is_retried_not_raised(self, _maker, monkeypatch):
        # #548 LOW-3: two simultaneous first-starts both see no trading_mode
        # row and both try to INSERT it — one wins, one's commit raises
        # IntegrityError. The loser must re-read and move on, not crash.
        db_mod, maker = _maker
        from backend.models import DbMetaModel

        db_mod._ensure_schema_sync(db_mod.DATABASE_URL)
        real_commit = AsyncSession.commit
        calls = {"n": 0}

        async def flaky_commit(self):
            calls["n"] += 1
            if calls["n"] == 1:
                # A concurrent writer's INSERT lands in the DB in the gap
                # between our failed commit and our retry-read — exactly
                # what a real second process racing us would do.
                async with maker() as other:
                    other.add(DbMetaModel(key="trading_mode", value=db_mod.TRADING_MODE))
                    await real_commit(other)
                from sqlalchemy.exc import IntegrityError

                raise IntegrityError("stmt", {}, Exception("UNIQUE constraint failed"))
            return await real_commit(self)

        monkeypatch.setattr(AsyncSession, "commit", flaky_commit)
        await db_mod._assert_trading_mode_stamp()  # must not raise
        assert calls["n"] == 1  # our own INSERT attempt only; no retry commit needed
        async with maker() as session:
            row = await session.get(DbMetaModel, "trading_mode")
        assert row is not None
        assert row.value == db_mod.TRADING_MODE


class TestBookConfigSyncConcurrency:
    @pytest.mark.asyncio
    async def test_config_version_bump_is_conditional_not_double_applied(self, _maker, monkeypatch):
        # #548 LOW-3: a read-modify-write version bump (book.config_version
        # += 1) would double-apply — two processes both reading
        # config_version=N and both writing N+1 — if a second writer races
        # in on the SAME stale snapshot. The conditional UPDATE (WHERE
        # config_hash = old_hash) makes the second writer's statement match
        # zero rows once the first has already applied the identical seed
        # hash, so only one bump and one audit row survive. Races the REAL
        # init_db()/_seed_and_sync code path: intercept the UPDATE this fix
        # issues and, from inside it, let a second init_db() apply the same
        # drift first — the original (now-stale) UPDATE must no-op.
        db_mod, maker = _maker
        from backend.database import LAB_BOOKS
        from backend.models import AuditEventModel, BookModel

        await db_mod.init_db()
        book_id = LAB_BOOKS[0]["id"]
        async with maker() as session:
            book = await session.get(BookModel, book_id)
            stale = {**book.config, "envelope": {**book.config["envelope"], "max_positions": 1}}
            book.config = stale
            book.config_hash = _config_hash(stale)
            await session.commit()

        original_execute = AsyncSession.execute
        triggered = False

        async def racing_execute(self_session, statement, *a, **kw):
            nonlocal triggered
            sql = str(statement)
            if not triggered and "UPDATE books" in sql and "config_version" in sql:
                triggered = True
                monkeypatch.setattr(AsyncSession, "execute", original_execute)
                await db_mod.init_db()  # a concurrent process applies the SAME drift first
            return await original_execute(self_session, statement, *a, **kw)

        monkeypatch.setattr(AsyncSession, "execute", racing_execute)
        await db_mod.init_db()
        assert triggered, "the config-sync UPDATE never ran — test setup invalid"

        async with maker() as session:
            book = await session.get(BookModel, book_id)
            audits = (
                (
                    await session.execute(
                        select(AuditEventModel).filter_by(event_type="BOOK_CONFIG_SYNCED", book_id=book_id)
                    )
                )
                .scalars()
                .all()
            )
        assert book.config_version == 2  # not 3 — the stale racer's UPDATE matched zero rows
        assert len(audits) == 1

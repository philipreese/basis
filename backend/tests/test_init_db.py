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
                "role": seed.get("role"),
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


class TestB35InitialControl:
    @pytest.mark.asyncio
    async def test_b35_cannot_submit_until_an_operator_explicitly_enables_it(self, _maker):
        """The effective seeded book/control path, not raw playbook.enabled, is fail-closed."""
        db_mod, maker = _maker
        from backend.models import BookModel, TradingControlModel
        from backend.trading_control import TradingHaltedError, assert_entries_allowed

        await db_mod.init_db()
        async with maker() as session:
            book = await session.get(BookModel, "B35")
            control = await session.get(TradingControlModel, "B35")
            assert book is not None
            assert control is not None
            assert control.state == "HALT_ENTRIES"
            with pytest.raises(TradingHaltedError) as exc_info:
                await assert_entries_allowed(session, "B35")
        assert exc_info.value.scope == "B35"
        assert exc_info.value.state == "HALT_ENTRIES"


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


class TestBehaviorOnlyResync:
    @pytest.mark.asyncio
    async def test_unchanged_config_with_moved_fingerprint_restarts_the_era_quietly(self, _maker, monkeypatch):
        # #1049: the config held but a playbook/engine moved the book's
        # fingerprint. The era must restart (BOOK_CONFIG_SYNCED, the event
        # era_start reads) with a stated reason — and no "config reverted"
        # page, which exists for clobbered config edits only.
        db_mod, maker = _maker
        from backend.database import LAB_BOOKS
        from backend.models import AuditEventModel, BookModel

        pages: list[tuple] = []
        monkeypatch.setattr("backend.operator.send_ntfy", lambda *a, **kw: pages.append(a))
        await db_mod.init_db()
        book_id = LAB_BOOKS[0]["id"]
        async with maker() as session:
            book = await session.get(BookModel, book_id)
            book.config_hash = "pre-1049-hash"
            session.add(_history_order(book_id))
            await session.commit()

        await db_mod.init_db()

        async with maker() as session:
            audit = (
                await session.execute(
                    select(AuditEventModel).filter_by(event_type="BOOK_CONFIG_SYNCED", book_id=book_id)
                )
            ).scalar_one()
        assert audit.payload["from_hash"] == "pre-1049-hash"
        assert audit.payload["diff"] == {}
        assert audit.payload["reason"] == "playbooks, engine or regime table changed"
        assert pages == []


def _history_order(book_id: str):
    from backend.models import OrderModel

    return OrderModel(
        id="o-history",
        book_id=book_id,
        position_id=None,
        order_ref=f"basis:{book_id}:o-history:open",
        ib_order_id=1,
        ib_perm_id=1,
        action="OPEN",
        combo_legs={"quantity": 1, "underlying": "XSP"},
        order_type="LIMIT",
        limit_price=-1.0,
        decision_midpoint=-1.0,
        status="FILLED",
        submitted_at="2026-08-27T22:45:00+00:00",
        completed_at="2026-08-28T13:31:00+00:00",
    )


# The 2026-10-04 operator ruling (#1088), spelled out independently of
# seeds.py so a typo there (retiring B01, missing B12) fails here.
RULED_RETIRED = {
    "B02", "B03", "B05", "B06", "B19", "B20", "B12",
    "B07", "B08", "B13", "B14", "B15", "B16", "B17", "B23", "B24", "B25", "B26", "B27", "B28", "B29", "B31", "B33",
    "B34",
    "B11", "B18", "B21",
}  # fmt: skip
RULED_ACTIVE = {"B01", "B04", "B09", "B10", "B22", "B30", "B32", "B35", "B36", "B37", "B38"}


async def _statuses_and_events(maker):
    from backend.models import AuditEventModel, BookModel
    from backend.states import BOOK_RETIRED_EVENT

    async with maker() as session:
        books = {b.id: b for b in (await session.execute(select(BookModel))).scalars().all()}
        events = (
            (await session.execute(select(AuditEventModel).filter_by(event_type=BOOK_RETIRED_EVENT))).scalars().all()
        )
        synced = (
            (await session.execute(select(AuditEventModel).filter_by(event_type="BOOK_CONFIG_SYNCED"))).scalars().all()
        )
    return books, events, synced


class TestBookRetirementSync:
    """#1088: seeds.py declares a retirement; init_db converges it to
    status RETIRED with one audit event, and never touches the era."""

    def test_ruling_partitions_the_whole_matrix(self):
        from backend.database import LAB_BOOKS

        assert len(RULED_RETIRED) == 27
        assert RULED_RETIRED.isdisjoint(RULED_ACTIVE)
        assert {spec["id"] for spec in LAB_BOOKS} == RULED_RETIRED | RULED_ACTIVE
        assert {spec["id"] for spec in LAB_BOOKS if "retired" in spec} == RULED_RETIRED
        for spec in LAB_BOOKS:
            if "retired" in spec:
                assert spec["retired"]["reason"] and spec["retired"]["on"] == "2026-10-04"
                # Outside config, so config_hash can't see it.
                assert "retired" not in spec["config"]

    @pytest.mark.asyncio
    async def test_fresh_db_seeds_the_ruling_with_one_event_per_retired_book(self, _maker):
        db_mod, maker = _maker
        from backend.states import BOOK_ACTIVE_STATUS, BOOK_RETIRED_STATUS

        await db_mod.init_db()
        books, events, synced = await _statuses_and_events(maker)
        assert {i for i, b in books.items() if b.status == BOOK_RETIRED_STATUS} == RULED_RETIRED
        assert {i for i, b in books.items() if b.status == BOOK_ACTIVE_STATUS} == RULED_ACTIVE
        assert sorted(e.book_id for e in events) == sorted(RULED_RETIRED)
        assert all(e.payload["reason"] and e.payload["retired_on"] == "2026-10-04" for e in events)
        assert synced == []  # retirement never starts a new era

    @pytest.mark.asyncio
    async def test_resync_is_idempotent(self, _maker):
        db_mod, maker = _maker

        await db_mod.init_db()
        first, first_events, _ = await _statuses_and_events(maker)
        await db_mod.init_db()
        await db_mod.init_db()
        again, events, synced = await _statuses_and_events(maker)
        assert len(events) == len(first_events) == 27
        assert {i: b.status for i, b in again.items()} == {i: b.status for i, b in first.items()}
        assert synced == []

    @pytest.mark.asyncio
    async def test_retiring_an_existing_book_keeps_its_era_and_history(self, _maker, monkeypatch):
        # The production path: a DB seeded before #1088 holds B12 ACTIVE with
        # trade history. The sync retires it in place — same config_hash and
        # config_version (no era restart), its order untouched, one event.
        db_mod, maker = _maker
        from backend.models import AuditEventModel, BookModel, OrderModel
        from backend.states import BOOK_ACTIVE_STATUS, BOOK_RETIRED_EVENT, BOOK_RETIRED_STATUS

        pages: list[tuple] = []
        monkeypatch.setattr("backend.operator.send_ntfy", lambda *a, **kw: pages.append(a))
        ruled = db_mod.LAB_BOOKS
        # A pre-#1088 start: the same matrix with no retirements declared.
        monkeypatch.setattr(db_mod, "LAB_BOOKS", [{k: v for k, v in s.items() if k != "retired"} for s in ruled])
        await db_mod.init_db()
        async with maker() as session:
            book = await session.get(BookModel, "B12")
            assert book.status == BOOK_ACTIVE_STATUS
            session.add(_history_order("B12"))
            await session.commit()
            hash_before, version_before = book.config_hash, book.config_version

        monkeypatch.setattr(db_mod, "LAB_BOOKS", ruled)
        await db_mod.init_db()

        async with maker() as session:
            book = await session.get(BookModel, "B12")
            order = await session.get(OrderModel, "o-history")
            (event,) = (
                (await session.execute(select(AuditEventModel).filter_by(event_type=BOOK_RETIRED_EVENT, book_id="B12")))
                .scalars()
                .all()
            )
            synced = (
                (await session.execute(select(AuditEventModel).filter_by(event_type="BOOK_CONFIG_SYNCED")))
                .scalars()
                .all()
            )
        assert book.status == BOOK_RETIRED_STATUS
        assert (book.config_hash, book.config_version) == (hash_before, version_before)
        assert order is not None and order.status == "FILLED"
        assert event.payload["from_status"] == BOOK_ACTIVE_STATUS
        assert "rule-experiment null" in event.payload["reason"]
        assert synced == []
        assert pages == []

    @pytest.mark.asyncio
    async def test_retirement_is_one_way(self, _maker, monkeypatch):
        # Deleting the seed's "retired" key must fail closed: the book stays
        # RETIRED rather than quietly reopening risk on the next start.
        db_mod, maker = _maker
        from backend.models import BookModel
        from backend.states import BOOK_RETIRED_STATUS

        await db_mod.init_db()
        unretired = [{k: v for k, v in spec.items() if k != "retired"} for spec in db_mod.LAB_BOOKS]
        monkeypatch.setattr(db_mod, "LAB_BOOKS", unretired)
        await db_mod.init_db()
        async with maker() as session:
            book = await session.get(BookModel, "B12")
        assert book.status == BOOK_RETIRED_STATUS


class TestPre672Backfill:
    """#766, exercised through the REAL init_db() entrypoint (not the
    isolated unit-level tests in test_backfill_pre_672.py) — this is the
    path the production DB actually runs at next startup, against a real
    file-backed sqlite engine, so the .bak snapshot it takes is a genuine
    file write, not a mock."""

    @pytest.mark.asyncio
    async def test_init_db_corrects_a_pre_672_close_and_snapshots_first(self, _maker, tmp_path):
        db_mod, maker = _maker
        from backend.models import (
            AuditEventModel,
            BookModel,
            ClosurePostMortemModel,
            FillModel,
            OrderModel,
            PositionModel,
        )

        # Schema first (mirrors what a real process does before any data exists).
        await db_mod.init_db()

        legs = [
            {"occ": "occA", "expiration": "2026-09-18", "option_type": "PUT", "strike": 610.0, "direction": "SHORT"},
            {"occ": "occB", "expiration": "2026-09-18", "option_type": "PUT", "strike": 605.0, "direction": "LONG"},
        ]
        async with maker() as session:
            session.add(
                PositionModel(
                    id="pos_o_8da86ccd",
                    underlying="XSP",
                    strategy_type="BULL_PUT_SPREAD",
                    execution_mode="PAPER",
                    legs=[
                        {
                            "option_type": leg["option_type"],
                            "direction": leg["direction"],
                            "strike": leg["strike"],
                            "expiration": leg["expiration"],
                            "delta": -0.2,
                            "theta": 0.02,
                            "vega": 0.1,
                            "gamma": 0.01,
                        }
                        for leg in legs
                    ],
                    entry_date="2026-08-01",
                    expiration_date="2026-09-18",
                    entry_premium=2.90,
                    premium_direction="DEBIT",
                    current_value_per_share=1.02,
                    contracts=1,
                    max_profit=1.0,
                    max_loss=2.90,
                    notes="",
                    rolls=0,
                    status="CLOSED",
                    journal={},
                    book_id="B25",
                )
            )
            session.add(
                ClosurePostMortemModel(
                    id="pm_pos_o_8da86ccd",
                    position_id="pos_o_8da86ccd",
                    outcome="LOSS",
                    realized_pnl=-188.00,
                    actual_underlying_move_pct=0.0,
                    exit_date="2026-08-21",
                    exit_trigger="MANUAL",
                    lesson_tags=[],
                    user_override_logged=False,
                    playbook_id=None,
                    playbook_version=None,
                )
            )
            session.add(
                OrderModel(
                    id="o_b395626e",
                    book_id="B25",
                    position_id="pos_o_8da86ccd",
                    order_ref="basis:B25:o_b395626e:close",
                    ib_order_id=None,
                    ib_perm_id=None,
                    action="CLOSE",
                    combo_legs={"legs": legs, "quantity": 1, "underlying": "XSP"},
                    order_type="LIMIT",
                    limit_price=1.02,
                    decision_midpoint=1.02,
                    status="FILLED",
                    submitted_at="2026-08-21T22:40:00+00:00",
                    completed_at="2026-08-21T22:45:26+00:00",  # B25's real timestamp — predates the fix
                    encumbered_risk=0.0,
                )
            )
            session.add_all(
                [
                    FillModel(
                        exec_id="f1",
                        order_id="o_b395626e",
                        book_id="B25",
                        con_id=1,
                        side="BOT",
                        quantity=1,
                        price=13.14,
                        commission=0.65,
                        fill_time="2026-08-21T22:45:00+00:00",
                    ),
                    FillModel(
                        exec_id="f2",
                        order_id="o_b395626e",
                        book_id="B25",
                        con_id=2,
                        side="SLD",
                        quantity=1,
                        price=15.40,
                        commission=0.65,
                        fill_time="2026-08-21T22:45:00+00:00",
                    ),
                ]
            )
            await session.commit()

        cash_before = 10000.0
        async with maker() as session:
            book = await session.get(BookModel, "B25")
            cash_before = book.cash_balance

        db_path = tmp_path / "init_db_test.db"
        await db_mod.init_db()  # the real entrypoint the production DB will run at next startup

        async with maker() as session:
            pos = await session.get(PositionModel, "pos_o_8da86ccd")
            pm = await session.get(ClosurePostMortemModel, "pm_pos_o_8da86ccd")
            book = await session.get(BookModel, "B25")
            corrected = (
                (await session.execute(select(AuditEventModel).filter_by(event_type="FILL_PRICE_BACKFILL_CORRECTED")))
                .scalars()
                .all()
            )

        assert pos.current_value_per_share == pytest.approx(2.26)
        assert pm.realized_pnl == pytest.approx(-64.00)
        assert book.cash_balance == pytest.approx(cash_before + 124.00)
        assert len(corrected) == 1

        # A real .bak snapshot landed next to the real db file before the
        # correction was applied (same helper the schema migrations use).
        backups = list(tmp_path.glob(f"{db_path.name}.pre-migration-*.bak"))
        assert backups, "expected a pre-migration .bak snapshot, found none"

    @pytest.mark.asyncio
    async def test_init_db_rerun_is_a_noop_second_time(self, _maker):
        db_mod, maker = _maker
        from backend.models import BookModel, ClosurePostMortemModel, FillModel, OrderModel, PositionModel

        await db_mod.init_db()
        legs = [
            {"occ": "occA", "expiration": "2026-09-18", "option_type": "PUT", "strike": 610.0, "direction": "SHORT"},
            {"occ": "occB", "expiration": "2026-09-18", "option_type": "PUT", "strike": 605.0, "direction": "LONG"},
        ]
        async with maker() as session:
            session.add(
                PositionModel(
                    id="pos_x",
                    underlying="XSP",
                    strategy_type="BULL_PUT_SPREAD",
                    execution_mode="PAPER",
                    legs=[
                        {
                            "option_type": leg["option_type"],
                            "direction": leg["direction"],
                            "strike": leg["strike"],
                            "expiration": leg["expiration"],
                            "delta": -0.2,
                            "theta": 0.02,
                            "vega": 0.1,
                            "gamma": 0.01,
                        }
                        for leg in legs
                    ],
                    entry_date="2026-08-01",
                    expiration_date="2026-09-18",
                    entry_premium=2.90,
                    premium_direction="DEBIT",
                    current_value_per_share=1.02,
                    contracts=1,
                    max_profit=1.0,
                    max_loss=2.90,
                    notes="",
                    rolls=0,
                    status="CLOSED",
                    journal={},
                    book_id="B25",
                )
            )
            session.add(
                ClosurePostMortemModel(
                    id="pm_x",
                    position_id="pos_x",
                    outcome="LOSS",
                    realized_pnl=-188.00,
                    actual_underlying_move_pct=0.0,
                    exit_date="2026-08-21",
                    exit_trigger="MANUAL",
                    lesson_tags=[],
                    user_override_logged=False,
                    playbook_id=None,
                    playbook_version=None,
                )
            )
            session.add(
                OrderModel(
                    id="o_x",
                    book_id="B25",
                    position_id="pos_x",
                    order_ref="basis:B25:o_x:close",
                    ib_order_id=None,
                    ib_perm_id=None,
                    action="CLOSE",
                    combo_legs={"legs": legs, "quantity": 1, "underlying": "XSP"},
                    order_type="LIMIT",
                    limit_price=1.02,
                    decision_midpoint=1.02,
                    status="FILLED",
                    submitted_at="2026-08-21T22:40:00+00:00",
                    completed_at="2026-08-21T22:45:26+00:00",
                    encumbered_risk=0.0,
                )
            )
            session.add_all(
                [
                    FillModel(
                        exec_id="fx1",
                        order_id="o_x",
                        book_id="B25",
                        con_id=1,
                        side="BOT",
                        quantity=1,
                        price=13.14,
                        commission=0.65,
                        fill_time="2026-08-21T22:45:00+00:00",
                    ),
                    FillModel(
                        exec_id="fx2",
                        order_id="o_x",
                        book_id="B25",
                        con_id=2,
                        side="SLD",
                        quantity=1,
                        price=15.40,
                        commission=0.65,
                        fill_time="2026-08-21T22:45:00+00:00",
                    ),
                ]
            )
            await session.commit()

        await db_mod.init_db()
        async with maker() as session:
            book = await session.get(BookModel, "B25")
            cash_after_first = book.cash_balance

        await db_mod.init_db()  # second full startup on the already-corrected data
        async with maker() as session:
            book = await session.get(BookModel, "B25")
            pos = await session.get(PositionModel, "pos_x")
        assert book.cash_balance == pytest.approx(cash_after_first)  # unchanged
        assert pos.current_value_per_share == pytest.approx(2.26)

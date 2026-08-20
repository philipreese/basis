"""Trading-mode isolation (ADR-0006, #204): paper and live never share a
database file, every database is stamped with the mode that created it, and
the paper executor refuses to run in live mode at all."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend import executor as executor_mod
from backend.database import _assert_trading_mode_stamp, _migrate_legacy_database_file, default_database_url
from backend.executor import run_executor_evening
from backend.models import Base, DbMetaModel


@pytest_asyncio.fixture
async def session_maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'mode.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    await engine.dispose()


def test_each_mode_gets_its_own_default_file():
    assert default_database_url("paper").endswith("basis.db")
    assert default_database_url("live").endswith("basis.live.db")
    assert default_database_url("paper") != default_database_url("live")


class TestLegacyFileRename:
    """#313: the database files carried the project's pre-basis name."""

    def test_moves_legacy_file_and_wal_shm_siblings(self, tmp_path):
        (tmp_path / "options_playbook.db").write_bytes(b"evidence")
        (tmp_path / "options_playbook.db-wal").write_bytes(b"wal")
        (tmp_path / "options_playbook.db-shm").write_bytes(b"shm")
        renamed = _migrate_legacy_database_file(f"sqlite+aiosqlite:///{(tmp_path / 'basis.db').as_posix()}")
        assert renamed == ("renamed", "options_playbook.db")
        assert (tmp_path / "basis.db").read_bytes() == b"evidence"
        assert (tmp_path / "basis.db-wal").exists() and (tmp_path / "basis.db-shm").exists()
        assert not (tmp_path / "options_playbook.db").exists()

    def test_locked_file_reports_locked_without_moving_anything(self, tmp_path, monkeypatch):
        # #340: a running console server holds the DB open — on Windows the
        # rename raises PermissionError. Nothing must move, and the caller
        # must fall back to the legacy file (never an empty new one).
        from pathlib import Path

        (tmp_path / "options_playbook.db").write_bytes(b"evidence")
        (tmp_path / "options_playbook.db-wal").write_bytes(b"wal")
        monkeypatch.setattr(Path, "rename", lambda self, target: (_ for _ in ()).throw(PermissionError(13, "held")))
        result = _migrate_legacy_database_file(f"sqlite+aiosqlite:///{(tmp_path / 'basis.db').as_posix()}")
        assert result == ("locked", "options_playbook.db")
        assert (tmp_path / "options_playbook.db").exists()
        assert not (tmp_path / "basis.db").exists()

    def test_partial_move_rolls_back(self, tmp_path, monkeypatch):
        # The main file renames fine but a sibling is held (AV/sync tool):
        # a split WAL loses commits, so everything must roll back.
        from pathlib import Path

        (tmp_path / "options_playbook.db").write_bytes(b"evidence")
        (tmp_path / "options_playbook.db-wal").write_bytes(b"wal")
        real_rename = Path.rename

        def _flaky(self, target):
            if str(self).endswith("-wal") and "options_playbook" in str(self):
                raise PermissionError(13, "sibling held")
            return real_rename(self, target)

        monkeypatch.setattr(Path, "rename", _flaky)
        result = _migrate_legacy_database_file(f"sqlite+aiosqlite:///{(tmp_path / 'basis.db').as_posix()}")
        assert result == ("locked", "options_playbook.db")
        assert (tmp_path / "options_playbook.db").read_bytes() == b"evidence"
        assert (tmp_path / "options_playbook.db-wal").exists()
        assert not (tmp_path / "basis.db").exists()

    def test_lost_race_to_another_process_is_a_clean_noop(self, tmp_path, monkeypatch):
        # Two entrypoints import simultaneously; the other one completed the
        # move between our exists() check and our rename.
        from pathlib import Path

        (tmp_path / "options_playbook.db").write_bytes(b"evidence")
        real_rename = Path.rename

        def _race(self, target):
            # Simulate the winner finishing first: legacy vanishes, new appears.
            real_rename(self, target)
            raise FileNotFoundError(2, "already moved by the winner")

        monkeypatch.setattr(Path, "rename", _race)
        # After the "winner's" move the loser sees new_path present, legacy gone.
        result = _migrate_legacy_database_file(f"sqlite+aiosqlite:///{(tmp_path / 'basis.db').as_posix()}")
        assert result is None
        assert (tmp_path / "basis.db").read_bytes() == b"evidence"

    def test_never_overwrites_an_existing_new_file(self, tmp_path):
        (tmp_path / "options_playbook.db").write_bytes(b"old")
        (tmp_path / "basis.db").write_bytes(b"new")
        assert _migrate_legacy_database_file(f"sqlite+aiosqlite:///{(tmp_path / 'basis.db').as_posix()}") is None
        assert (tmp_path / "basis.db").read_bytes() == b"new"
        assert (tmp_path / "options_playbook.db").read_bytes() == b"old"

    def test_noop_without_a_legacy_file_or_on_explicit_urls(self, tmp_path):
        assert _migrate_legacy_database_file(f"sqlite+aiosqlite:///{(tmp_path / 'basis.db').as_posix()}") is None
        # An explicit URL naming the old file is respected untouched.
        (tmp_path / "options_playbook.db").write_bytes(b"old")
        assert (
            _migrate_legacy_database_file(f"sqlite+aiosqlite:///{(tmp_path / 'options_playbook.db').as_posix()}")
            is None
        )
        assert (tmp_path / "options_playbook.db").exists()
        assert _migrate_legacy_database_file("sqlite+aiosqlite:///:memory:") is None


@pytest.mark.asyncio
async def test_fresh_database_is_stamped_with_the_process_mode(session_maker):
    await _assert_trading_mode_stamp(session_maker, mode="paper")
    async with session_maker() as session:
        row = await session.get(DbMetaModel, "trading_mode")
    assert row.value == "paper"
    # Re-open in the same mode: fine.
    await _assert_trading_mode_stamp(session_maker, mode="paper")


@pytest.mark.asyncio
async def test_mode_mismatch_refuses_hard(session_maker):
    await _assert_trading_mode_stamp(session_maker, mode="live")
    with pytest.raises(RuntimeError, match="Trading-mode mismatch"):
        await _assert_trading_mode_stamp(session_maker, mode="paper")


@pytest.mark.asyncio
async def test_paper_executor_refuses_to_run_in_live_mode(monkeypatch):
    # The live executor is a separate, unbuilt thing (approval-per-trade) —
    # this pipeline running against live money must be impossible.
    monkeypatch.setattr(executor_mod, "TRADING_MODE", "live")
    with pytest.raises(RuntimeError, match="PAPER executor"):
        await run_executor_evening(session_maker=None, broker_factory=None)

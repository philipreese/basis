"""The nightly database backup (#207): once real fills land, the DB is Live
Gate evidence — the copy must happen, rotate, and never fail silently.
Audit II (#353): the snapshot goes through SQLite's backup API (WAL frames
included) and the prune glob is date-shaped so paper and live rotations in
the shared dir never delete each other."""

import datetime
import sqlite3
from unittest.mock import patch

import backend.gateway_lifecycle as gl
from backend import db_backup
from backend.db_backup import BACKUP_KEEP, backup_database

MONDAY = datetime.date(2026, 8, 24)


def _make_tracked_db(path, rows_per_table=1):
    """A source DB carrying the tables _verify_snapshot checks (#649),
    minimally shaped — verification only cares about table names and row
    counts, not the real ORM schema."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    for table in db_backup._VERIFY_TABLES:
        conn.execute(f"CREATE TABLE {table} (v TEXT)")
        if rows_per_table:
            conn.executemany(f"INSERT INTO {table} VALUES (?)", [(f"{table}-{i}",) for i in range(rows_per_table)])
    conn.commit()
    conn.close()


def _make_db(path, markers=("evidence",)):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    conn.executemany("INSERT INTO t VALUES (?)", [(m,) for m in markers])
    conn.commit()
    conn.close()


def _rows(path):
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT v FROM t ORDER BY v")]
    finally:
        conn.close()


def _point_at(monkeypatch, tmp_path, db_name="basis.db"):
    src = tmp_path / db_name
    _make_db(src)
    monkeypatch.setattr(db_backup, "DATABASE_URL", f"sqlite+aiosqlite:///{src.as_posix()}")
    dest_dir = tmp_path / "backups"
    monkeypatch.setenv("DB_BACKUP_DIR", str(dest_dir))
    return src, dest_dir


class TestBackupDatabase:
    def test_copies_to_dated_file(self, monkeypatch, tmp_path):
        _, dest_dir = _point_at(monkeypatch, tmp_path)
        dest = backup_database(today=MONDAY)
        assert dest == dest_dir / "basis.2026-08-24.db"
        assert _rows(dest) == ["evidence"]

    def test_uncheckpointed_wal_frames_reach_the_backup(self, monkeypatch, tmp_path):
        # #353: a plain file copy of a WAL-mode DB misses frames not yet
        # checkpointed into the main file. Keep a writer connection open so
        # closing never checkpoints, then back up.
        src, _ = _point_at(monkeypatch, tmp_path)
        writer = sqlite3.connect(src)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("INSERT INTO t VALUES ('wal-only-row')")
            writer.commit()  # lands in basis.db-wal, not the main file
            dest = backup_database(today=MONDAY)
        finally:
            writer.close()
        assert "wal-only-row" in _rows(dest)

    def test_same_day_rerun_overwrites_not_duplicates(self, monkeypatch, tmp_path):
        src, dest_dir = _point_at(monkeypatch, tmp_path)
        backup_database(today=MONDAY)
        _make_db(src, markers=("later",))
        dest = backup_database(today=MONDAY)
        assert "later" in _rows(dest)
        assert len(list(dest_dir.iterdir())) == 1

    def test_rotation_prunes_oldest_beyond_keep(self, monkeypatch, tmp_path):
        _, dest_dir = _point_at(monkeypatch, tmp_path)
        for offset in range(BACKUP_KEEP + 3):
            backup_database(today=MONDAY + datetime.timedelta(days=offset))
        kept = sorted(p.name for p in dest_dir.iterdir())
        assert len(kept) == BACKUP_KEEP
        assert kept[0] == "basis.2026-08-27.db"  # 3 oldest pruned

    def test_prune_never_touches_the_other_modes_rotations(self, monkeypatch, tmp_path):
        # #353: `basis.*.db` also matched basis.live.YYYY-MM-DD.db, and
        # digits sort before letters — with ≥7 live rotations a paper prune
        # deleted EVERY paper backup, including the one just written.
        _, dest_dir = _point_at(monkeypatch, tmp_path)
        dest_dir.mkdir(parents=True, exist_ok=True)
        live_names = {f"basis.live.2026-08-{10 + i:02d}.db" for i in range(BACKUP_KEEP + 2)}
        for name in live_names:
            (dest_dir / name).write_bytes(b"live-evidence")
        for offset in range(BACKUP_KEEP + 3):
            backup_database(today=MONDAY + datetime.timedelta(days=offset))
        survivors = {p.name for p in dest_dir.iterdir()}
        assert live_names <= survivors  # live rotations untouched by the paper prune
        paper = sorted(n for n in survivors if n not in live_names)
        assert len(paper) == BACKUP_KEEP
        assert paper[-1] == f"basis.{(MONDAY + datetime.timedelta(days=BACKUP_KEEP + 2)).isoformat()}.db"

    def test_orphaned_legacy_rotations_are_warned_not_deleted(self, monkeypatch, tmp_path, caplog):
        _, dest_dir = _point_at(monkeypatch, tmp_path)
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "options_playbook.2026-08-01.db").write_bytes(b"pre-rename")
        with caplog.at_level("WARNING", logger="backend.db_backup"):
            backup_database(today=MONDAY)
        assert (dest_dir / "options_playbook.2026-08-01.db").exists()
        assert any("options_playbook" in r.message for r in caplog.records)

    def test_legacy_warning_suppressed_while_running_against_the_legacy_file(self, monkeypatch, tmp_path, caplog):
        # Audit II R2 (#422): under the #362 lock fallback the active stem IS
        # options_playbook — its backups rotate fine, and warning about the
        # very files this run just wrote is noise that trains the operator to
        # ignore the real orphan case.
        _, dest_dir = _point_at(monkeypatch, tmp_path, db_name="options_playbook.db")
        with caplog.at_level("WARNING", logger="backend.db_backup"):
            dest = backup_database(today=MONDAY)
        assert dest == dest_dir / "options_playbook.2026-08-24.db"
        assert not any("orphaned" in r.message for r in caplog.records)

    def test_failed_snapshot_leaves_no_truncated_dated_file(self, monkeypatch, tmp_path):
        # Audit II R2 (#422): a partial file wearing today's date counts
        # toward the rotation glob — able to push a GOOD older backup out of
        # the BACKUP_KEEP window — while itself being unrestorable.
        from unittest.mock import MagicMock

        import pytest

        src, dest_dir = _point_at(monkeypatch, tmp_path)
        real_connect = sqlite3.connect
        failing_src = MagicMock()
        failing_src.backup.side_effect = sqlite3.OperationalError("disk I/O error")

        def connect(path, *a, **kw):
            # The source connection fails its backup; the dest connection is
            # real so the truncated dated file genuinely exists to clean up.
            return failing_src if str(path) == str(src) else real_connect(path, *a, **kw)

        with patch.object(db_backup.sqlite3, "connect", side_effect=connect), pytest.raises(sqlite3.OperationalError):
            backup_database(today=MONDAY)
        assert not (dest_dir / "basis.2026-08-24.db").exists()

    def test_memory_url_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(db_backup, "DATABASE_URL", "sqlite+aiosqlite:///:memory:")
        assert backup_database(today=MONDAY) is None

    def test_missing_database_file_is_a_noop(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db_backup, "DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'gone.db').as_posix()}")
        monkeypatch.setenv("DB_BACKUP_DIR", str(tmp_path / "backups"))
        assert backup_database(today=MONDAY) is None

    def test_non_sqlite_url_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(db_backup, "DATABASE_URL", "postgresql://elsewhere/db")
        assert backup_database(today=MONDAY) is None


class TestBackupAfterRun:
    def test_failure_alerts_but_does_not_raise(self):
        with (
            patch("backend.db_backup.backup_database", side_effect=OSError("disk full")),
            patch("backend.operator.send_ntfy") as mock_ntfy,
        ):
            gl._backup_after_run()  # must not raise
        assert mock_ntfy.call_count == 1
        title, body, priority = mock_ntfy.call_args.args
        assert "backup FAILED" in title
        assert "disk full" in body
        assert priority == "high"

    def test_success_stays_quiet(self, monkeypatch, tmp_path):
        _point_at(monkeypatch, tmp_path)
        with patch("backend.operator.send_ntfy") as mock_ntfy:
            gl._backup_after_run()
        mock_ntfy.assert_not_called()


class TestSnapshotVerification:
    """#649: the 2026-08-20 backup was a 4096-byte, zero-table snapshot —
    sqlite's backup API returned without error, so nothing caught it for
    two days. A snapshot missing a tracked table, or with one unexpectedly
    empty while the source's is not, must be refused."""

    def test_a_zero_table_snapshot_is_refused_and_deleted(self, monkeypatch, tmp_path):
        # Reproduces the reported shape directly: the backup API call
        # "succeeds" but the result holds nothing.
        src, dest_dir = _point_at(monkeypatch, tmp_path)
        _make_tracked_db(src)

        def _empty_snapshot(_src, dest):
            sqlite3.connect(dest).close()  # a valid, empty sqlite file

        monkeypatch.setattr(db_backup, "_snapshot_sqlite", _empty_snapshot)
        with patch("backend.operator.send_ntfy") as mock_ntfy:
            result = backup_database(today=MONDAY)

        assert result is None
        assert not (dest_dir / "basis.2026-08-24.db").exists()
        assert not any(dest_dir.iterdir())  # no .staging leftover either
        assert mock_ntfy.call_count == 1
        title, _body, priority = mock_ntfy.call_args.args
        assert "FAILED verification" in title
        assert priority == "urgent"

    def test_a_snapshot_with_a_table_unexpectedly_empty_is_refused(self, monkeypatch, tmp_path):
        src, dest_dir = _point_at(monkeypatch, tmp_path)
        _make_tracked_db(src, rows_per_table=3)

        def _partial_snapshot(_src, dest):
            conn = sqlite3.connect(dest)
            for table in db_backup._VERIFY_TABLES:
                conn.execute(f"CREATE TABLE {table} (v TEXT)")
            # "books" comes through empty despite the source having rows.
            conn.executemany("INSERT INTO orders VALUES (?)", [("o1",)])
            conn.executemany("INSERT INTO positions VALUES (?)", [("p1",)])
            conn.executemany("INSERT INTO audit_events VALUES (?)", [("a1",)])
            conn.commit()
            conn.close()

        monkeypatch.setattr(db_backup, "_snapshot_sqlite", _partial_snapshot)
        with patch("backend.operator.send_ntfy") as mock_ntfy:
            result = backup_database(today=MONDAY)

        assert result is None
        assert not (dest_dir / "basis.2026-08-24.db").exists()
        assert mock_ntfy.call_count == 1
        _title, body, _priority = mock_ntfy.call_args.args
        assert "books" in body

    def test_a_snapshot_missing_only_fills_is_refused(self, monkeypatch, tmp_path):
        # #682: fills used to be absent from _VERIFY_TABLES entirely — a
        # snapshot that dropped just this table (execution-price evidence
        # the Live Gate's expectancy/slippage-haircut criterion depends on,
        # #672/ADR-0006) passed verification cleanly. Reproduces that gap
        # directly: every other tracked table comes through, fills doesn't.
        src, dest_dir = _point_at(monkeypatch, tmp_path)
        _make_tracked_db(src, rows_per_table=3)

        def _fills_dropped_snapshot(_src, dest):
            conn = sqlite3.connect(dest)
            for table in db_backup._VERIFY_TABLES:
                conn.execute(f"CREATE TABLE {table} (v TEXT)")
            for table in ("books", "orders", "positions", "audit_events"):
                conn.executemany(f"INSERT INTO {table} VALUES (?)", [(f"{table}-1",)])
            # "fills" comes through empty despite the source having rows.
            conn.commit()
            conn.close()

        monkeypatch.setattr(db_backup, "_snapshot_sqlite", _fills_dropped_snapshot)
        with patch("backend.operator.send_ntfy") as mock_ntfy:
            result = backup_database(today=MONDAY)

        assert result is None
        assert not (dest_dir / "basis.2026-08-24.db").exists()
        assert mock_ntfy.call_count == 1
        _title, body, _priority = mock_ntfy.call_args.args
        assert "fills" in body

    def test_an_empty_source_is_not_a_false_positive(self, monkeypatch, tmp_path):
        # A brand-new, genuinely empty database backing up for the first
        # time must not be refused for being... empty like its source.
        src, dest_dir = _point_at(monkeypatch, tmp_path)
        _make_tracked_db(src, rows_per_table=0)
        with patch("backend.operator.send_ntfy") as mock_ntfy:
            result = backup_database(today=MONDAY)
        assert result == dest_dir / "basis.2026-08-24.db"
        mock_ntfy.assert_not_called()

    def test_a_previous_good_backup_survives_a_refused_snapshot(self, monkeypatch, tmp_path):
        src, dest_dir = _point_at(monkeypatch, tmp_path)
        _make_tracked_db(src)
        backup_database(today=MONDAY - datetime.timedelta(days=1))  # yesterday's good backup
        good_bytes = (dest_dir / "basis.2026-08-23.db").read_bytes()

        def _empty_snapshot(_src, dest):
            sqlite3.connect(dest).close()

        monkeypatch.setattr(db_backup, "_snapshot_sqlite", _empty_snapshot)
        with patch("backend.operator.send_ntfy"):
            backup_database(today=MONDAY)

        assert (dest_dir / "basis.2026-08-23.db").read_bytes() == good_bytes
        assert not (dest_dir / "basis.2026-08-24.db").exists()


class TestShrinkageGuard:
    """#649: a snapshot that is dramatically smaller than the newest
    existing good rotation is suspect even when it passes table-level
    verification — refuse to let it become (or overwrite) the trusted
    dated file."""

    def test_a_dramatically_smaller_snapshot_is_quarantined_as_suspect(self, monkeypatch, tmp_path):
        src, dest_dir = _point_at(monkeypatch, tmp_path)
        _make_tracked_db(src)
        # A synthetic, deliberately large "yesterday" baseline — only its
        # byte size matters to the guard, never its content.
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "basis.2026-08-23.db").write_bytes(b"x" * 1_000_000)

        with patch("backend.operator.send_ntfy") as mock_ntfy:
            result = backup_database(today=MONDAY)

        assert result is None
        assert not (dest_dir / "basis.2026-08-24.db").exists()
        assert (dest_dir / "basis.2026-08-24.db.suspect").exists()
        assert (dest_dir / "basis.2026-08-23.db").exists()  # the good baseline is untouched
        assert mock_ntfy.call_count == 1
        title, _body, priority = mock_ntfy.call_args.args
        assert "SHRINKAGE" in title
        assert priority == "urgent"

    def test_a_same_day_rerun_is_not_compared_against_itself(self, monkeypatch, tmp_path):
        src, dest_dir = _point_at(monkeypatch, tmp_path)
        _make_tracked_db(src)
        with patch("backend.operator.send_ntfy"):
            backup_database(today=MONDAY)
        with patch("backend.operator.send_ntfy") as mock_ntfy:
            result = backup_database(today=MONDAY)  # rerun, same tiny content, same day
        assert result == dest_dir / "basis.2026-08-24.db"
        mock_ntfy.assert_not_called()

    def test_a_comparably_sized_snapshot_is_not_flagged(self, monkeypatch, tmp_path):
        src, dest_dir = _point_at(monkeypatch, tmp_path)
        _make_tracked_db(src, rows_per_table=50)
        backup_database(today=MONDAY - datetime.timedelta(days=1))
        with patch("backend.operator.send_ntfy") as mock_ntfy:
            result = backup_database(today=MONDAY)
        assert result == dest_dir / "basis.2026-08-24.db"
        mock_ntfy.assert_not_called()

    def test_three_consecutive_quarantine_nights_auto_accepts_the_smaller_size(self, monkeypatch, tmp_path):
        # #689: a stale baseline (e.g. surviving a disaster-recovery restore
        # that replaced the live DB but never touched DB_BACKUP_DIR) used to
        # wedge the guard into refusing every backup forever. A repeat, not
        # a one-off, is itself evidence the smaller size is the new normal.
        src, dest_dir = _point_at(monkeypatch, tmp_path)
        _make_tracked_db(src)
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "basis.2026-08-20.db").write_bytes(b"x" * 1_000_000)

        night1 = MONDAY - datetime.timedelta(days=3)
        night2 = MONDAY - datetime.timedelta(days=2)
        night3 = MONDAY - datetime.timedelta(days=1)
        with patch("backend.operator.send_ntfy") as mock_ntfy:
            r1 = backup_database(today=night1)
            r2 = backup_database(today=night2)
            r3 = backup_database(today=night3)

        assert r1 is None
        assert r2 is None
        assert r3 == dest_dir / f"basis.{night3.isoformat()}.db"  # third night auto-accepted
        assert mock_ntfy.call_count == 3
        title3, _body3, priority3 = mock_ntfy.call_args_list[2].args
        assert "ACCEPTED" in title3
        assert priority3 == "urgent"
        assert not (dest_dir / "basis.suspect.count").exists()  # streak cleared on accept

        # The stale baseline no longer blocks future nights — the accepted
        # smaller snapshot is the baseline now.
        with patch("backend.operator.send_ntfy") as mock_ntfy2:
            result = backup_database(today=MONDAY)
        assert result == dest_dir / "basis.2026-08-24.db"
        mock_ntfy2.assert_not_called()

    def test_operator_sentinel_accepts_the_snapshot_immediately(self, monkeypatch, tmp_path):
        # #689's other escape: an operator who KNOWS the shrink is legitimate
        # (right after a restore, or a deliberate bulk purge) doesn't have to
        # wait out the consecutive-night counter.
        src, dest_dir = _point_at(monkeypatch, tmp_path)
        _make_tracked_db(src)
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "basis.2026-08-23.db").write_bytes(b"x" * 1_000_000)
        (dest_dir / "basis.accept_shrinkage").touch()

        with patch("backend.operator.send_ntfy") as mock_ntfy:
            result = backup_database(today=MONDAY)

        assert result == dest_dir / "basis.2026-08-24.db"
        assert not (dest_dir / "basis.accept_shrinkage").exists()  # one-shot, consumed
        assert mock_ntfy.call_count == 1
        title, _body, priority = mock_ntfy.call_args.args
        assert "ACCEPTED" in title
        assert priority == "urgent"

    def test_a_successful_backup_resets_the_quarantine_streak(self, monkeypatch, tmp_path):
        src, dest_dir = _point_at(monkeypatch, tmp_path)
        _make_tracked_db(src)
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / "basis.2026-08-20.db").write_bytes(b"x" * 1_000_000)

        with patch("backend.operator.send_ntfy"):
            backup_database(today=MONDAY - datetime.timedelta(days=2))  # quarantine night 1
        state_path = dest_dir / "basis.suspect.count"
        assert state_path.read_text() == "1"

        (dest_dir / "basis.2026-08-20.db").unlink()  # the stale baseline is gone
        with patch("backend.operator.send_ntfy") as mock_ntfy:
            result = backup_database(today=MONDAY - datetime.timedelta(days=1))
        assert result is not None
        mock_ntfy.assert_not_called()
        assert not state_path.exists()  # the streak does not carry into a future, unrelated quarantine


class TestBackupDirIsolation:
    """#649: the same class of guard as #561's real-DB tripwire, one seam
    over — no test may resolve the operator's real OneDrive backup dir."""

    def test_opening_a_path_under_the_real_backup_dir_is_blocked(self):
        from pathlib import Path

        import pytest

        real_dir = Path.home() / "OneDrive" / "basis-db-backups"
        probe_path = real_dir / "probe-should-never-be-created.db"
        with pytest.raises(RuntimeError, match=r"BLOCKED \(#649\)"):
            sqlite3.connect(str(probe_path))
        assert not probe_path.exists()

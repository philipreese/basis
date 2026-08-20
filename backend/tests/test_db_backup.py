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

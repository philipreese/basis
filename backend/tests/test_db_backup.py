"""The nightly database backup (#207): once real fills land, the DB is Live
Gate evidence — the copy must happen, rotate, and never fail silently."""

import datetime
from unittest.mock import patch

import backend.gateway_lifecycle as gl
from backend import db_backup
from backend.db_backup import BACKUP_KEEP, backup_database

MONDAY = datetime.date(2026, 8, 24)


def _point_at(monkeypatch, tmp_path, db_name="basis.db"):
    src = tmp_path / db_name
    src.write_bytes(b"sqlite-bytes")
    monkeypatch.setattr(db_backup, "DATABASE_URL", f"sqlite+aiosqlite:///{src.as_posix()}")
    dest_dir = tmp_path / "backups"
    monkeypatch.setenv("DB_BACKUP_DIR", str(dest_dir))
    return src, dest_dir


class TestBackupDatabase:
    def test_copies_to_dated_file(self, monkeypatch, tmp_path):
        src, dest_dir = _point_at(monkeypatch, tmp_path)
        dest = backup_database(today=MONDAY)
        assert dest == dest_dir / "basis.2026-08-24.db"
        assert dest.read_bytes() == src.read_bytes()

    def test_same_day_rerun_overwrites_not_duplicates(self, monkeypatch, tmp_path):
        src, dest_dir = _point_at(monkeypatch, tmp_path)
        backup_database(today=MONDAY)
        src.write_bytes(b"later-bytes")
        dest = backup_database(today=MONDAY)
        assert dest.read_bytes() == b"later-bytes"
        assert len(list(dest_dir.iterdir())) == 1

    def test_rotation_prunes_oldest_beyond_keep(self, monkeypatch, tmp_path):
        _, dest_dir = _point_at(monkeypatch, tmp_path)
        for offset in range(BACKUP_KEEP + 3):
            backup_database(today=MONDAY + datetime.timedelta(days=offset))
        kept = sorted(p.name for p in dest_dir.iterdir())
        assert len(kept) == BACKUP_KEEP
        assert kept[0] == "basis.2026-08-27.db"  # 3 oldest pruned

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

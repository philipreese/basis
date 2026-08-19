"""db_backup.py — nightly copy of the SQLite database (#207).

Once the first real paper fill lands, the database IS the Live Gate evidence
(ADR-0006: ≥30 trades / ≥3 months / zero breaches) — three months of history
on a single un-backed-up file is a disk failure away from restarting the
clock. The nightly pipeline copies the file after the executor finishes and
keeps a short rotation.

The destination defaults to a OneDrive folder so copies survive the machine;
the database holds no credentials, so cloud sync is acceptable (unlike IBC's
config.ini — see scripts/setup-ibc.ps1).
"""

import datetime
import logging
import os
import shutil
from pathlib import Path

from backend.database import DATABASE_URL

logger = logging.getLogger(__name__)

BACKUP_KEEP = 7  # rotations to retain — one trading week is plenty


def _database_path(url: str) -> Path | None:
    """Filesystem path of a sqlite database URL, or None for non-file DBs."""
    if not url.startswith("sqlite") or ":memory:" in url:
        return None
    _, _, path = url.partition("///")
    return Path(path) if path else None


def _backup_dir() -> Path:
    default = Path.home() / "OneDrive" / "basis-db-backups"
    return Path(os.getenv("DB_BACKUP_DIR", str(default)))


def backup_database(today: datetime.date | None = None) -> Path | None:
    """Copy the database to the backup dir, prune old copies, return the copy.

    Returns None (with a log line) when there is nothing to back up — an
    in-memory/non-sqlite URL or a database file that doesn't exist yet.
    Raises on copy failure so the caller can alert; a silent backup failure
    would defeat the point.
    """
    src = _database_path(DATABASE_URL)
    if src is None or not src.exists():
        logger.info("No database file to back up (%s)", DATABASE_URL)
        return None

    today = today or datetime.date.today()
    dest_dir = _backup_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{src.stem}.{today.isoformat()}{src.suffix}"
    shutil.copy2(src, dest)

    rotations = sorted(dest_dir.glob(f"{src.stem}.*{src.suffix}"))
    for stale in rotations[:-BACKUP_KEEP]:
        stale.unlink()
    logger.info("Database backed up to %s (%d rotation(s) kept)", dest, min(len(rotations), BACKUP_KEEP))
    return dest

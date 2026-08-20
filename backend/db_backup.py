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
import sqlite3
from pathlib import Path

from backend.database import DATABASE_URL
from backend.dates import market_today

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

    # #540: date.today() reads the host's local date — a UTC-configured
    # box would name/dedupe/prune backups by the wrong calendar day.
    today = today or market_today()
    dest_dir = _backup_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{src.stem}.{today.isoformat()}{src.suffix}"
    _snapshot_sqlite(src, dest)

    # Date-shaped glob (#353): `{stem}.*{suffix}` for the PAPER file also
    # matched `basis.live.YYYY-MM-DD.db` in the shared dir — and digits sort
    # before letters, so with ≥7 live rotations present a paper prune would
    # delete every paper backup, including the one just written.
    rotations = sorted(dest_dir.glob(f"{src.stem}.????-??-??{src.suffix}"))
    for stale in rotations[:-BACKUP_KEEP]:
        stale.unlink()
    # Suppress while the lock fallback runs against the legacy file (#422):
    # then options_playbook.* IS the active stem, its backups rotate fine,
    # and the warning would flag the very files this run just wrote.
    legacy = [] if src.stem.startswith("options_playbook") else sorted(dest_dir.glob("options_playbook.*"))
    if legacy:
        logger.warning(
            "%d legacy 'options_playbook.*' backup(s) in %s are orphaned since the #313 rename "
            "and will never rotate — delete them by hand when no longer wanted",
            len(legacy),
            dest_dir,
        )
    logger.info("Database backed up to %s (%d rotation(s) kept)", dest, min(len(rotations), BACKUP_KEEP))
    return dest


def _snapshot_sqlite(src: Path, dest: Path) -> None:
    """Consistent snapshot via SQLite's online backup API (#353): the live
    file runs WAL mode, and a plain file copy can miss every frame not yet
    checkpointed into the main file — silently dropping the newest evening's
    fills from the one copy that exists to protect them."""
    src_conn = sqlite3.connect(str(src))
    dest_conn = sqlite3.connect(str(dest))
    try:
        src_conn.backup(dest_conn)
    except BaseException:
        # A failed snapshot must not leave a truncated file wearing today's
        # date (#422): it counts toward the rotation glob — able to push a
        # GOOD older backup out of the BACKUP_KEEP window — while itself
        # being unrestorable.
        dest_conn.close()
        Path(dest).unlink(missing_ok=True)
        raise
    finally:
        src_conn.close()
        dest_conn.close()  # idempotent — already closed on the failure path

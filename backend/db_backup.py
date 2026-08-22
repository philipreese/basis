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

# #649: the 2026-08-20 backup was a 4096-byte, zero-table snapshot — sqlite's
# backup API returned without error, so nothing before this caught it, and
# the operator's disaster-recovery position was silently "yesterday or
# nothing" for two days. Every snapshot is now verified against these
# tables before it's trusted; a suspiciously small snapshot (below this
# fraction of the newest existing good rotation) is refused too.
# #682: "fills" was missing from this list — the Live Gate's expectancy /
# slippage-haircut criterion (ADR-0006) is computed from actual fill prices
# (#672), not position/order rows alone, so a snapshot that silently dropped
# just the fills table used to pass verification while losing exactly the
# evidence the promotion decision depends on.
_VERIFY_TABLES = ("books", "orders", "positions", "audit_events", "fills")
_MIN_SHRINKAGE_RATIO = 0.25

# #689: the shrinkage baseline is always "the newest EXISTING dated rotation
# on disk" — a quarantined `.suspect` file never matches that glob, so it can
# never itself become the new baseline. Without an escape, a single
# legitimate shrink (a bulk data purge, #532) or a disaster-recovery restore
# (the live DB is replaced but DB_BACKUP_DIR is untouched, so it keeps
# offering the pre-restore size every night) wedges the guard into refusing
# every backup forever, right when continuity matters most. Two escapes,
# both loud — never a silent accept:
#   1. An operator sentinel file (touch-and-forget) accepts the very next
#      snapshot immediately, one-shot.
#   2. N consecutive quarantine nights in a row (nothing else has changed
#      the baseline in between) auto-promotes the newest quarantined
#      snapshot — a repeat is itself evidence this is the new normal, not a
#      one-off corruption.
_SHRINKAGE_ACCEPT_AFTER_CONSECUTIVE = 3


def _database_path(url: str) -> Path | None:
    """Filesystem path of a sqlite database URL, or None for non-file DBs."""
    if not url.startswith("sqlite") or ":memory:" in url:
        return None
    _, _, path = url.partition("///")
    return Path(path) if path else None


def _backup_dir() -> Path:
    default = Path.home() / "OneDrive" / "basis-db-backups"
    return Path(os.getenv("DB_BACKUP_DIR", str(default)))


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _suspect_path(dest: Path) -> Path:
    return dest.with_name(f"{dest.name}.suspect")


def _suspect_count_path(dest_dir: Path, src: Path) -> Path:
    """Tracks consecutive quarantine nights (#689) — reset whenever a
    snapshot is accepted (normally, by operator sentinel, or by the
    consecutive-night auto-accept itself)."""
    return dest_dir / f"{src.stem}.suspect.count"


def _accept_shrinkage_sentinel_path(dest_dir: Path, src: Path) -> Path:
    """An operator drops this empty file to accept the very next
    suspiciously-small snapshot immediately (#689) — e.g. right after a
    disaster-recovery restore, where the new baseline is legitimately
    smaller than DB_BACKUP_DIR's stale pre-restore rotations. Consumed
    (deleted) the run it's used."""
    return dest_dir / f"{src.stem}.accept_shrinkage"


def _read_suspect_count(state_path: Path) -> int:
    try:
        return int(state_path.read_text().strip())
    except (OSError, ValueError):
        return 0


def _verify_snapshot(src: Path, snapshot: Path) -> str | None:
    """Open the snapshot and confirm it actually holds the database it
    claims to be a copy of — sqlite's backup API returning without error is
    not proof of that (#649). Returns None when the snapshot looks healthy,
    else a short description of what's wrong."""
    src_conn = sqlite3.connect(str(src))
    dest_conn = sqlite3.connect(str(snapshot))
    try:
        src_tables = _table_names(src_conn)
        dest_tables = _table_names(dest_conn)
        missing = [t for t in _VERIFY_TABLES if t in src_tables and t not in dest_tables]
        if missing:
            return f"snapshot is missing table(s) present in the source: {', '.join(missing)}"
        emptied = []
        for t in _VERIFY_TABLES:
            if t not in src_tables:
                continue
            src_count = src_conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            dest_count = dest_conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            if src_count > 0 and dest_count == 0:
                emptied.append(t)
        if emptied:
            return f"snapshot table(s) unexpectedly empty (source is non-empty): {', '.join(emptied)}"
        return None
    finally:
        src_conn.close()
        dest_conn.close()


def _alert_bad_snapshot(title: str, body: str) -> None:
    # Local import (matches gateway_lifecycle._backup_after_run's pattern):
    # db_backup must not import operator at module scope. Urgent + its own
    # SCHEDULER_ALERT event type — distinct from the generic "high"-priority
    # alert _backup_after_run raises for a genuine I/O failure, since this is
    # a specific, diagnosable finding, not an unhandled exception.
    from backend.operator import alert_crash

    logger.error("%s: %s", title, body)
    alert_crash(title, body, priority="urgent", event_type="SCHEDULER_ALERT")


def backup_database(today: datetime.date | None = None) -> Path | None:
    """Copy the database to the backup dir, prune old copies, return the copy.

    Returns None (with a log line) when there is nothing to back up — an
    in-memory/non-sqlite URL or a database file that doesn't exist yet —
    or when the new snapshot was refused (failed verification, or looks
    suspiciously smaller than the newest existing good rotation; #649).
    A refused snapshot alerts urgently itself; it does not raise, so a
    caller's generic failure-alert path never double-alerts on it.

    Raises on an actual copy failure (sqlite backup API error) so the
    caller can alert; a silent backup failure would defeat the point.
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

    # Date-shaped glob (#353): `{stem}.*{suffix}` for the PAPER file also
    # matched `basis.live.YYYY-MM-DD.db` in the shared dir — and digits sort
    # before letters, so with ≥7 live rotations present a paper prune would
    # delete every paper backup, including the one just written.
    rotations = sorted(dest_dir.glob(f"{src.stem}.????-??-??{src.suffix}"))
    # Shrinkage baseline (#649): the newest EXISTING good rotation other
    # than today's own file — a same-day rerun legitimately differs in size
    # from its own earlier run this same day and must not be compared
    # against itself.
    baseline = next((p for p in reversed(rotations) if p != dest), None)

    # Snapshot to a staging file first — verified before it ever becomes (or
    # overwrites) the dated file the prune glob and any future restore would
    # reach for. A failed sqlite backup still raises past this function (the
    # existing #422 unlink-on-failure behavior, unchanged) so the caller's
    # generic alert path keeps covering real I/O failures.
    staging = dest_dir / f"{dest.name}.staging"
    staging.unlink(missing_ok=True)
    _snapshot_sqlite(src, staging)

    state_path = _suspect_count_path(dest_dir, src)
    sentinel_path = _accept_shrinkage_sentinel_path(dest_dir, src)

    problem = _verify_snapshot(src, staging)
    if problem is None and baseline is not None:
        baseline_size = baseline.stat().st_size
        staging_size = staging.stat().st_size
        if baseline_size > 0 and staging_size < baseline_size * _MIN_SHRINKAGE_RATIO:
            shrink_detail = (
                f"snapshot is {staging_size}B, under {_MIN_SHRINKAGE_RATIO:.0%} of the newest existing "
                f"backup {baseline.name} ({baseline_size}B) — suspect shrinkage"
            )
            operator_override = sentinel_path.exists()
            consecutive = _read_suspect_count(state_path) + 1
            auto_accept = consecutive >= _SHRINKAGE_ACCEPT_AFTER_CONSECUTIVE
            if operator_override or auto_accept:
                # #689: audited escape, never a silent accept — falls through
                # to the normal accept path below (staging -> dest); `problem`
                # stays None so the verification-failure branch is skipped.
                reason = (
                    f"operator {sentinel_path.name} sentinel present"
                    if operator_override
                    else f"{consecutive} consecutive quarantine nights — treating the smaller size as the new normal"
                )
                sentinel_path.unlink(missing_ok=True)
                _alert_bad_snapshot(
                    "basis: DB backup shrinkage ACCEPTED",
                    f"{shrink_detail}. Accepted as the new baseline ({reason}) — this becomes {dest.name}.",
                )
            else:
                suspect = _suspect_path(dest)
                suspect.unlink(missing_ok=True)
                staging.replace(suspect)
                state_path.write_text(str(consecutive))
                _alert_bad_snapshot(
                    "basis: DB backup SUSPECT SHRINKAGE",
                    f"{shrink_detail}. The existing good backup was kept; the suspect snapshot is at {suspect} "
                    f"for inspection (quarantine night {consecutive} of {_SHRINKAGE_ACCEPT_AFTER_CONSECUTIVE} "
                    f"before this size auto-accepts). To accept sooner — e.g. right after a disaster-recovery "
                    f"restore — create an empty file at {sentinel_path}.",
                )
                return None

    if problem is not None:
        staging.unlink(missing_ok=True)
        _alert_bad_snapshot(
            "basis: DB backup FAILED verification",
            f"{problem}. The snapshot was refused and deleted; the previous good backup (if any) is untouched.",
        )
        return None

    staging.replace(dest)  # atomic same-filesystem rename — same-day rerun still overwrites, not duplicates
    # A snapshot reaching here was accepted outright — reset the quarantine
    # streak and clear any leftover suspect file from an earlier night (#689).
    state_path.unlink(missing_ok=True)
    _suspect_path(dest).unlink(missing_ok=True)

    # Re-glob now that dest is in place (the `rotations` computed above, for
    # the shrinkage baseline, deliberately predates this write).
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

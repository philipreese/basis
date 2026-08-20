"""run_lock.py — one executor run at a time (#275, audit H5).

Without this, a manual `pixi run executor-nightly` alongside the scheduled
task places a SECOND live close on the same positions and adjusts book cash
twice. A lock file taken with O_EXCL is the cross-process arbiter; the PID
and timestamp inside are diagnostics, not the mechanism.

Staleness: a lock older than STALE_AFTER_SECONDS belongs to a crashed run
(the scheduled task's own execution limit is 30 minutes) and is broken —
a crash between create and the finally-release must not brick every future
evening. Override the directory with BASIS_LOCK_DIR (tests point it at a
tmp dir).
"""

import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

STALE_AFTER_SECONDS = 2 * 60 * 60


def _lock_path(name: str) -> Path:
    return Path(os.getenv("BASIS_LOCK_DIR", ".")) / f"{name}.lock"


def acquire_run_lock(name: str = "executor") -> Path | None:
    """Take the lock, breaking a stale one. None = a live run holds it."""
    path = _lock_path(name)
    for attempt in (1, 2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                continue  # holder released between our attempts — retry
            if age <= STALE_AFTER_SECONDS or attempt == 2:
                return None
            logger.warning("Breaking stale run lock %s (%.0fs old)", path, age)
            try:
                path.unlink()
            except OSError:
                return None
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"pid": os.getpid(), "at": datetime.now(UTC).isoformat()}, fh)
            return path
    return None


def release_run_lock(path: Path) -> None:
    try:
        path.unlink()
    except OSError as exc:  # never let cleanup mask the run's real outcome
        logger.warning("Could not release run lock %s: %s", path, exc)

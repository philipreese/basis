"""run_lock.py — one executor run at a time (#275, audit H5).

Without this, a manual `pixi run executor-nightly` alongside the scheduled
task places a SECOND live close on the same positions and adjusts book cash
twice. A lock file taken with O_EXCL is the cross-process arbiter; the PID
and timestamp inside are diagnostics; the token inside IS the mechanism's
ownership proof (#416).

Staleness: a lock older than STALE_AFTER_SECONDS belongs to a crashed run
(the scheduled task's own execution limit is 30 minutes) and is broken —
a crash between create and the finally-release must not brick every future
evening. Override the directory with BASIS_LOCK_DIR (tests point it at a
tmp dir).

Races (#416): the old break was read-check-unlink-create, so two processes
could both break one stale lock and both run; and release unlinked
unconditionally, so a process whose lock had been broken later deleted the
NEW owner's lock, letting a third run in. Now: breaking is an atomic
os.replace of a freshly written candidate onto the stale path followed by a
read-back — whoever's token survives owns the lock, the loser backs off —
and release only unlinks when the file still carries the caller's token.
"""

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

STALE_AFTER_SECONDS = 2 * 60 * 60


@dataclass(frozen=True)
class RunLock:
    path: Path
    token: str


def _lock_path(name: str) -> Path:
    return Path(os.getenv("BASIS_LOCK_DIR", ".")) / f"{name}.lock"


def _payload(token: str) -> str:
    return json.dumps({"pid": os.getpid(), "at": datetime.now(UTC).isoformat(), "token": token})


def _read_token(path: Path) -> str | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("token")
    except (OSError, ValueError):
        return None


def lock_is_held(name: str = "executor") -> bool:
    """True when a FRESH lock file exists — someone's run is live (#418).
    Used by neighbors (the fill check) to avoid tearing down a Gateway a
    concurrently running executor is still using. A stale file is a crashed
    run's debris, not a holder."""
    path = _lock_path(name)
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return age <= STALE_AFTER_SECONDS


def acquire_run_lock(name: str = "executor") -> RunLock | None:
    """Take the lock, breaking a stale one. None = a live run holds it."""
    path = _lock_path(name)
    token = uuid.uuid4().hex
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            # Holder released between our attempts — one clean retry.
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except OSError:
                return None
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(_payload(token))
            return RunLock(path, token)
        if age <= STALE_AFTER_SECONDS:
            return None
        logger.warning("Breaking stale run lock %s (%.0fs old)", path, age)
        # Atomic break: write our candidate beside the lock and replace().
        # Two concurrent breakers both replace — last write wins, and the
        # read-back below tells each who actually owns the lock now.
        candidate = path.with_name(f"{path.name}.{token}")
        # One brief retry: on Windows, AV/indexer tools transiently hold
        # just-written files and os.replace raises PermissionError.
        for attempt in (1, 2):
            try:
                candidate.write_text(_payload(token), encoding="utf-8")
                os.replace(candidate, path)
                break
            except OSError as exc:
                if attempt == 2:
                    logger.warning("Stale-lock break failed for %s: %s", path, exc)
                    candidate.unlink(missing_ok=True)
                    return None
                time.sleep(0.1)
        if _read_token(path) != token:
            return None  # another breaker won the replace race
        return RunLock(path, token)
    else:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_payload(token))
        return RunLock(path, token)


def release_run_lock(lock: RunLock) -> None:
    """Release only what we still own — a broken-and-replaced lock belongs
    to the new holder now, and deleting it would let a third run in."""
    if _read_token(lock.path) != lock.token:
        logger.warning("Run lock %s no longer ours (broken as stale?) — not releasing", lock.path)
        return
    try:
        lock.path.unlink()
    except OSError as exc:  # never let cleanup mask the run's real outcome
        logger.warning("Could not release run lock %s: %s", lock.path, exc)

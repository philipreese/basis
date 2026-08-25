"""run_lock.py — one executor run at a time (#275, audit H5).

Without this, a manual `pixi run executor-nightly` alongside the scheduled
task places a SECOND live close on the same positions and adjusts book cash
twice. A lock file taken with O_EXCL is the cross-process arbiter; the PID
and timestamp inside are diagnostics; the token inside IS the mechanism's
ownership proof (#416).

Staleness: a lock older than STALE_AFTER_SECONDS belongs to a crashed run
(the scheduled task's own execution limit is 30 minutes) and is broken —
a crash between create and the finally-release must not brick every future
evening. Long legitimate runs call refresh_run_lock at phase boundaries so
a live lock never classifies stale (#471). Override the directory with
BASIS_LOCK_DIR (tests point it at a tmp dir); the default anchors to the
repo root, NOT the CWD — two scheduled tasks with different Start-in
directories must arbitrate on the same file (#471).

Races: the #416 break was write-candidate-then-replace, which could replace
a FRESH lock a faster breaker had just created (this breaker's staleness
verdict predating the other's acquire) — both read back their own token,
both ran (#471). Breaking is now by REMOVING: os.replace the stale file to
a unique graveyard name — atomic, single-winner; a concurrent breaker's
rename raises FileNotFoundError — verify what was actually claimed (a fresh
file goes straight back), then everyone re-contends through the normal
O_CREAT|O_EXCL acquire. Release only unlinks while the file still carries
the caller's token.
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
    return Path(os.getenv("BASIS_LOCK_DIR") or Path(__file__).resolve().parents[1]) / f"{name}.lock"


def _payload(token: str) -> str:
    return json.dumps({"pid": os.getpid(), "at": datetime.now(UTC).isoformat(), "token": token})


def _read_token(path: Path) -> str | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("token")
    except (OSError, ValueError):
        return None


def lock_is_held(name: str = "executor") -> bool:
    """True when a FRESH lock file exists — someone's run is live (#418).
    Used by neighbors (the fill check, the gateway teardown) to avoid
    tearing down a Gateway a concurrent process is still using. A stale
    file is a crashed run's debris, not a holder."""
    path = _lock_path(name)
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return age <= STALE_AFTER_SECONDS


# Every process that can launch (and therefore may tear down) the shared IB
# Gateway (#418/#471/#641). #681: fill_check.py and gateway_lifecycle.py's
# teardown/acquire-time deferrals predated restore_drill — each independently
# spelled out its neighbors by name and never learned about the new tenant,
# the exact "predicate not re-verified against every state that now exists"
# shape backend/states.py's docstring describes, applied to lock names
# instead of status literals. Centralized here so a FIFTH tenant is one
# addition, and every consumer of other_gateway_tenant_active() picks it up
# automatically instead of needing its own review pass.
GATEWAY_TENANT_LOCKS: tuple[str, ...] = ("executor", "gateway", "fill_check", "restore_drill", "preflight")


def other_gateway_tenant_active(caller: str) -> bool:
    """True when some OTHER Gateway-tenant lock (not *caller*'s own) is
    live. *caller* is excluded so a process never defers to (or refuses to
    tear down for) its own lock."""
    return any(lock_is_held(name) for name in GATEWAY_TENANT_LOCKS if name != caller)


def _break_stale(path: Path, token: str, expected_mtime: float | None = None) -> bool:
    """Remove a stale lock file; True when the path is (now) free to acquire.

    Re-stat immediately before the replace (#589, follow-up from #536): the
    staleness verdict in acquire_run_lock and this function's os.replace are
    two separate syscalls with a window between them — a refresh_run_lock
    call landing in that window (the holder is alive and mid-run, not
    crashed) would otherwise get broken out from under it anyway, the exact
    #536 theft shape via a different door. expected_mtime is the mtime the
    caller's staleness verdict was based on; if the file's mtime has since
    changed, someone refreshed it after the verdict — treat it as live and
    bail before ever touching the path. This narrows the window (verdict to
    re-stat is still not atomic with the stat-to-verdict decision itself);
    it does not close it — refresh_run_lock returning False and the
    executor aborting (#536) is the actual backstop for whatever this
    narrower window still misses.

    Single-winner by construction: os.replace to a unique graveyard name is
    atomic, and a concurrent breaker's rename raises FileNotFoundError. The
    graveyard file is then VERIFIED — if the staleness verdict raced another
    breaker's break-and-reacquire, the claimed file is that winner's FRESH
    lock, and it goes straight back (os.rename refuses to clobber on
    Windows, where production runs). Only verified-stale debris is deleted.
    """
    if expected_mtime is not None:
        try:
            current_mtime = path.stat().st_mtime
        except OSError:
            return True  # vanished on its own since the verdict — free to create
        if current_mtime != expected_mtime:
            logger.warning("Run lock %s was refreshed since the staleness verdict — not breaking it", path)
            return False
    graveyard = path.with_name(f"{path.name}.stale.{token}")
    for attempt in (1, 2):
        try:
            os.replace(path, graveyard)
            break
        except FileNotFoundError:
            return True  # another breaker already removed it — contend for the acquire
        except OSError as exc:
            # On Windows, AV/indexer tools transiently hold files (#416).
            if attempt == 2:
                logger.warning("Stale-lock break failed for %s: %s", path, exc)
                return False
            time.sleep(0.1)
    try:
        grave_age = time.time() - graveyard.stat().st_mtime
    except OSError:
        return True  # claimed file vanished — nothing live was taken
    if grave_age <= STALE_AFTER_SECONDS:
        try:
            os.rename(graveyard, path)
        except OSError as exc:
            logger.warning("Could not hand back a freshly-taken lock %s: %s", path, exc)
        return False
    graveyard.unlink(missing_ok=True)
    return True


def acquire_run_lock(name: str = "executor") -> RunLock | None:
    """Take the lock, breaking a stale one. None = a live run holds it."""
    path = _lock_path(name)
    token = uuid.uuid4().hex

    def _try_create() -> RunLock | None:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            return None
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_payload(token))
        return RunLock(path, token)

    lock = _try_create()
    if lock is not None:
        return lock
    try:
        stat = path.stat()
    except OSError:
        # Holder released between our attempts — one clean retry.
        return _try_create()
    age = time.time() - stat.st_mtime
    if age <= STALE_AFTER_SECONDS:
        return None
    logger.warning("Breaking stale run lock %s (%.0fs old)", path, age)
    if not _break_stale(path, token, stat.st_mtime):
        return None
    # The break freed the path; every contender re-arbitrates through the
    # same O_EXCL create — exactly one of them gets a lock file.
    return _try_create()


def refresh_run_lock(lock: RunLock) -> bool:
    """Stamp the lock fresh at a phase boundary (#471): a legitimate run
    longer than STALE_AFTER_SECONDS must not have its LIVE lock classified
    stale — breakable by the next scheduled task, invisible to
    lock_is_held's Gateway-tenancy check — mid-run.

    Returns False when the lock file no longer carries our token (#536): a
    losing verify-restore race in _break_stale can strand a FRESH holder's
    lock in the graveyard, letting a third contender acquire the path while
    this run keeps going with no lock at all. The caller MUST treat False as
    fatal — abort the rest of the run before any further broker mutation;
    do NOT release a lock this run no longer owns (release_run_lock already
    no-ops on a token mismatch, but the run itself must stop acting as the
    sole owner)."""
    if _read_token(lock.path) != lock.token:
        logger.warning("Run lock %s no longer ours — not refreshing", lock.path)
        return False
    try:
        os.utime(lock.path)
    except OSError as exc:
        logger.warning("Could not refresh run lock %s: %s", lock.path, exc)
    return True


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

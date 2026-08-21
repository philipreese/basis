"""Tests for the run lock (backend/run_lock.py, #275/#416).

The executor-level held/stale behavior is pinned in test_executor.py's
TestRunLock; these pin the #416 ownership mechanics — the token is the
proof, and release never deletes a lock that stopped being ours.
"""

import json
import os
import time

from backend.run_lock import RunLock, acquire_run_lock, release_run_lock


def test_acquire_writes_token_and_release_removes(tmp_path, monkeypatch):
    monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
    lock = acquire_run_lock("t1")
    assert isinstance(lock, RunLock)
    on_disk = json.loads(lock.path.read_text(encoding="utf-8"))
    assert on_disk["token"] == lock.token
    release_run_lock(lock)
    assert not lock.path.exists()


def test_release_refuses_a_lock_that_is_no_longer_ours(tmp_path, monkeypatch):
    # Audit II R2 (#416): our lock was broken as stale and REPLACED — the
    # file belongs to the new holder now; deleting it would let a third
    # run in behind their back.
    monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
    lock = acquire_run_lock("t2")
    lock.path.write_text(json.dumps({"pid": 999, "token": "someone-else"}), encoding="utf-8")
    release_run_lock(lock)
    assert lock.path.exists()  # untouched — not ours to delete


def test_stale_lock_is_broken_atomically_and_owned(tmp_path, monkeypatch):
    monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
    stale = tmp_path / "t3.lock"
    stale.write_text(json.dumps({"pid": 1, "token": "dead-run"}), encoding="utf-8")
    ancient = 1_000_000_000
    os.utime(stale, (ancient, ancient))
    lock = acquire_run_lock("t3")
    assert lock is not None
    assert json.loads(stale.read_text(encoding="utf-8"))["token"] == lock.token
    assert not list(tmp_path.glob("t3.lock.*"))  # no candidate debris
    release_run_lock(lock)
    assert not stale.exists()


def test_lock_is_held_only_for_fresh_locks(tmp_path, monkeypatch):
    # #418: neighbors (the fill check) check this before tearing down a
    # Gateway a running executor may be using; a stale file is debris.
    from backend.run_lock import lock_is_held

    monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
    assert lock_is_held("t5") is False  # no file
    lock = tmp_path / "t5.lock"
    lock.write_text(json.dumps({"pid": 1, "token": "live"}), encoding="utf-8")
    assert lock_is_held("t5") is True  # fresh
    ancient = 1_000_000_000
    os.utime(lock, (ancient, ancient))
    assert lock_is_held("t5") is False  # stale = crashed debris


def test_fresh_lock_is_respected(tmp_path, monkeypatch):
    monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
    held = tmp_path / "t4.lock"
    held.write_text(json.dumps({"pid": 1, "token": "live-run"}), encoding="utf-8")
    os.utime(held, None)  # fresh mtime
    assert acquire_run_lock("t4") is None
    assert time.time() - held.stat().st_mtime < 60  # untouched


def test_break_hands_back_a_freshly_taken_lock(tmp_path, monkeypatch):
    # Audit II R3 (#471): a breaker whose staleness verdict predates another
    # breaker's break-and-reacquire must NOT walk off with the winner's
    # FRESH lock — the graveyard rename claims whatever file is at the path,
    # so what was claimed gets verified and a fresh file goes straight back.
    from backend.run_lock import _break_stale

    monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
    path = tmp_path / "t6.lock"
    path.write_text(json.dumps({"pid": 1, "token": "fresh-winner"}), encoding="utf-8")
    os.utime(path, None)  # fresh — someone else's live lock
    assert _break_stale(path, "loser-token") is False
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == "fresh-winner"  # restored
    assert not list(tmp_path.glob("t6.lock.stale.*"))  # nothing kept


def test_break_removes_verified_stale_debris(tmp_path, monkeypatch):
    from backend.run_lock import _break_stale

    monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
    path = tmp_path / "t7.lock"
    path.write_text(json.dumps({"pid": 1, "token": "dead-run"}), encoding="utf-8")
    ancient = 1_000_000_000
    os.utime(path, (ancient, ancient))
    assert _break_stale(path, "breaker-token") is True
    assert not path.exists()
    assert not list(tmp_path.glob("t7.lock.stale.*"))  # graveyard cleaned


def test_break_of_an_already_removed_lock_just_contends(tmp_path, monkeypatch):
    # The losing concurrent breaker's rename raises FileNotFoundError —
    # it falls through to the normal O_EXCL contention, never an error.
    from backend.run_lock import _break_stale

    monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
    assert _break_stale(tmp_path / "t8.lock", "loser-token") is True


def test_break_skips_a_lock_refreshed_since_the_staleness_verdict(tmp_path, monkeypatch):
    # #589 (follow-up from #536): a refresh_run_lock call landing in the
    # window between acquire_run_lock's staleness verdict and _break_stale's
    # os.replace would otherwise get broken out from under a still-live
    # holder — the holder is mid-run, not crashed. Re-stating immediately
    # before the replace and comparing against the verdict's own mtime
    # catches exactly this: a changed mtime means someone touched the file
    # since the verdict, so bail without ever moving it.
    from backend.run_lock import _break_stale

    monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
    path = tmp_path / "t9r.lock"
    path.write_text(json.dumps({"pid": 1, "token": "live-run"}), encoding="utf-8")
    ancient = 1_000_000_000
    os.utime(path, (ancient, ancient))
    stale_verdict_mtime = path.stat().st_mtime
    # The holder refreshes between the verdict and the break attempt.
    os.utime(path, None)
    assert _break_stale(path, "breaker-token", expected_mtime=stale_verdict_mtime) is False
    assert path.exists()  # never touched
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == "live-run"  # untouched, not a restore
    assert not list(tmp_path.glob("t9r.lock.stale.*"))  # never even reached the replace


def test_break_proceeds_when_mtime_still_matches_the_verdict(tmp_path, monkeypatch):
    # #589: the common case — nothing refreshed the lock between the
    # verdict and the break — must behave exactly as before the fix.
    from backend.run_lock import _break_stale

    monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
    path = tmp_path / "t9m.lock"
    path.write_text(json.dumps({"pid": 1, "token": "dead-run"}), encoding="utf-8")
    ancient = 1_000_000_000
    os.utime(path, (ancient, ancient))
    verdict_mtime = path.stat().st_mtime
    assert _break_stale(path, "breaker-token", expected_mtime=verdict_mtime) is True
    assert not path.exists()


def test_break_treats_a_vanished_lock_as_free_regardless_of_expected_mtime(tmp_path, monkeypatch):
    # #589: if the path is already gone by the re-stat, some other breaker
    # (or the holder's own release) beat us to it — free to create either
    # way, same as the no-expected-mtime path already handles below.
    from backend.run_lock import _break_stale

    monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
    assert _break_stale(tmp_path / "t9v.lock", "loser-token", expected_mtime=123.456) is True


def test_acquire_run_lock_declines_a_stale_verdict_the_holder_refreshed_in_time(tmp_path, monkeypatch):
    # #589 end-to-end: acquire_run_lock's own staleness verdict, followed by
    # a refresh landing before _break_stale's re-stat, must leave the
    # refreshed lock alone and report it as held (None), not steal it.
    import backend.run_lock as rl

    monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
    path = tmp_path / "executor.lock"
    path.write_text(json.dumps({"pid": 1, "token": "live-run"}), encoding="utf-8")
    ancient = 1_000_000_000
    os.utime(path, (ancient, ancient))

    real_break_stale = rl._break_stale

    def refreshing_break_stale(p, token, expected_mtime=None):
        # Simulate the racing refresh landing between acquire_run_lock's
        # stat() and _break_stale's own re-stat.
        os.utime(p, None)
        return real_break_stale(p, token, expected_mtime)

    monkeypatch.setattr(rl, "_break_stale", refreshing_break_stale)
    assert rl.acquire_run_lock("executor") is None  # declined — the holder is live
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == "live-run"  # untouched


def test_refresh_keeps_a_long_run_fresh_but_only_for_the_owner(tmp_path, monkeypatch):
    # Audit II R3 (#471): a legitimate run longer than STALE_AFTER_SECONDS
    # used to have its LIVE lock classify stale — breakable by the next
    # scheduled task, invisible to lock_is_held — mid-run.
    from backend.run_lock import lock_is_held, refresh_run_lock

    monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
    lock = acquire_run_lock("t9")
    ancient = 1_000_000_000
    os.utime(lock.path, (ancient, ancient))
    assert lock_is_held("t9") is False  # aged past stale
    assert refresh_run_lock(lock) is True
    assert lock_is_held("t9") is True  # phase-boundary refresh restores it
    # Not ours any more → refresh must not resurrect someone else's file.
    lock.path.write_text(json.dumps({"pid": 9, "token": "new-holder"}), encoding="utf-8")
    os.utime(lock.path, (ancient, ancient))
    assert refresh_run_lock(lock) is False
    assert lock_is_held("t9") is False  # left stale — not ours to freshen


def test_refresh_returns_false_when_the_lock_was_stolen(tmp_path, monkeypatch):
    # #536: a stolen lock (a losing verify-restore race stranding this run's
    # lock in the graveyard while a third contender takes the path) must be
    # detectable by the caller — refresh_run_lock is the only phase-boundary
    # signal the executor has that it no longer owns the exact lock it
    # thinks it does.
    from backend.run_lock import refresh_run_lock

    monkeypatch.setenv("BASIS_LOCK_DIR", str(tmp_path))
    lock = acquire_run_lock("t10")
    lock.path.write_text(json.dumps({"pid": 999, "token": "thief"}), encoding="utf-8")
    assert refresh_run_lock(lock) is False


def test_default_lock_dir_is_the_repo_root_not_cwd(monkeypatch):
    # Audit II R3 (#471): two scheduled tasks with different Start-in
    # directories must arbitrate on the SAME file, or the guard no-ops.
    from pathlib import Path

    import backend.run_lock as rl

    monkeypatch.delenv("BASIS_LOCK_DIR", raising=False)
    expected_root = Path(rl.__file__).resolve().parents[1]
    assert rl._lock_path("x").parent == expected_root

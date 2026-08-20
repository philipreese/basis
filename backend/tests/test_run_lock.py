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
    refresh_run_lock(lock)
    assert lock_is_held("t9") is True  # phase-boundary refresh restores it
    # Not ours any more → refresh must not resurrect someone else's file.
    lock.path.write_text(json.dumps({"pid": 9, "token": "new-holder"}), encoding="utf-8")
    os.utime(lock.path, (ancient, ancient))
    refresh_run_lock(lock)
    assert lock_is_held("t9") is False  # left stale — not ours to freshen


def test_default_lock_dir_is_the_repo_root_not_cwd(monkeypatch):
    # Audit II R3 (#471): two scheduled tasks with different Start-in
    # directories must arbitrate on the SAME file, or the guard no-ops.
    from pathlib import Path

    import backend.run_lock as rl

    monkeypatch.delenv("BASIS_LOCK_DIR", raising=False)
    expected_root = Path(rl.__file__).resolve().parents[1]
    assert rl._lock_path("x").parent == expected_root

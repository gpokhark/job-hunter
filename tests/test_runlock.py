"""Tests for `src/job_hunter/runlock.py` — previously untested despite being the only thing
preventing two overlapping job-hunter/agent processes from racing shared state (see
docs/agent-runtime-audit.md)."""

import multiprocessing
import os
import subprocess
import sys
import time

import pytest

from job_hunter.runlock import RunLockHeld, run_lock


def test_lock_file_contains_own_pid(tmp_path):
    with run_lock("test", lock_dir=tmp_path) as lock_path:
        assert lock_path == tmp_path / "test.lock"
        lines = lock_path.read_text().splitlines()
        assert int(lines[0]) == os.getpid()


def test_lock_released_on_clean_exit(tmp_path):
    with run_lock("test", lock_dir=tmp_path) as lock_path:
        pass
    assert not lock_path.exists()


def test_lock_released_on_exception(tmp_path):
    lock_path = tmp_path / "test.lock"
    with pytest.raises(ValueError), run_lock("test", lock_dir=tmp_path):
        raise ValueError("boom")
    assert not lock_path.exists()


def test_second_acquire_while_held_raises(tmp_path):
    with run_lock("test", lock_dir=tmp_path):
        with pytest.raises(RunLockHeld) as exc_info, run_lock("test", lock_dir=tmp_path):
            pass
        assert exc_info.value.holder_pid == os.getpid()


def test_holder_command_and_started_at_are_reported(tmp_path):
    with run_lock("test", lock_dir=tmp_path), pytest.raises(RunLockHeld) as exc_info, run_lock(
        "test", lock_dir=tmp_path
    ):
        pass
    assert exc_info.value.holder_command is not None
    assert exc_info.value.holder_started_at is not None
    assert str(os.getpid()) in str(exc_info.value)


def test_stale_lock_with_dead_pid_is_reclaimed(tmp_path):
    lock_path = tmp_path / "test.lock"
    # Spawn a short-lived process, wait for it to exit, then plant its now-dead pid as the
    # lock's holder -- this is what a crashed previous run's lock file actually looks like.
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    dead_pid = proc.pid
    lock_path.write_text(f"{dead_pid}\nold command\n2020-01-01T00:00:00+00:00\n")

    with run_lock("test", lock_dir=tmp_path) as reclaimed:
        assert reclaimed == lock_path
        assert int(lock_path.read_text().splitlines()[0]) == os.getpid()


def test_lock_file_with_garbage_content_is_reclaimed(tmp_path):
    lock_path = tmp_path / "test.lock"
    lock_path.write_text("not-a-pid\n")

    with run_lock("test", lock_dir=tmp_path):
        assert int(lock_path.read_text().splitlines()[0]) == os.getpid()


def test_empty_lock_file_is_reclaimed(tmp_path):
    lock_path = tmp_path / "test.lock"
    lock_path.write_text("")

    with run_lock("test", lock_dir=tmp_path):
        assert int(lock_path.read_text().splitlines()[0]) == os.getpid()


def _acquire_and_report(
    lock_dir: str, name: str, queue: multiprocessing.Queue, barrier: multiprocessing.Barrier
) -> None:
    # Wait for every worker to finish starting up (process spawn time is variable) before any of
    # them attempts to acquire -- otherwise a slow-to-start worker can arrive after a fast one has
    # already acquired *and released* the lock, making them never actually race at all.
    barrier.wait(timeout=5)
    try:
        with run_lock(name, lock_dir=lock_dir):
            queue.put(("held", os.getpid()))
            time.sleep(0.5)  # hold it long enough that any true racer is still waiting
    except RunLockHeld as exc:
        queue.put(("denied", exc.holder_pid))


def test_concurrent_acquire_exactly_one_wins(tmp_path):
    """Several processes race to acquire a lock nobody holds yet -- exactly one should win the
    `O_CREAT|O_EXCL` race and the rest should see it as held, never crash on an unhandled error."""
    queue: multiprocessing.Queue = multiprocessing.Queue()
    worker_count = 5
    barrier = multiprocessing.Barrier(worker_count)
    procs = [
        multiprocessing.Process(
            target=_acquire_and_report, args=(str(tmp_path), "race", queue, barrier)
        )
        for _ in range(worker_count)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=5)
    results = [queue.get(timeout=1) for _ in procs]
    held = [r for r in results if r[0] == "held"]
    denied = [r for r in results if r[0] == "denied"]
    assert len(held) == 1
    assert len(denied) == len(procs) - 1

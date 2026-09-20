"""Tests for `src/job_hunter/runlock.py` — previously untested despite being the only thing
preventing two overlapping job-hunter/agent processes from racing shared state (see
docs/agent-runtime-audit.md)."""

import multiprocessing
import os
import subprocess
import sys
import time

import pytest

from job_hunter.runlock import (
    LOCK_INHERITED_ENV,
    RunLockHeld,
    current_lock_token,
    process_start_time,
    run_lock,
    run_lock_or_inherited,
)


def test_lock_file_contains_own_pid(tmp_path):
    with run_lock("test", lock_dir=tmp_path) as lock_path:
        assert lock_path == tmp_path / "test.lock"
        lines = lock_path.read_text().splitlines()
        assert int(lines[0]) == os.getpid()


def test_lock_file_records_a_token(tmp_path):
    """The 4th line backs the `run_lock_or_inherited` capability-token check (see
    LOCK_INHERITED_ENV's docstring) — must be present and non-empty on every fresh acquire."""
    with run_lock("test", lock_dir=tmp_path) as lock_path:
        lines = lock_path.read_text().splitlines()
        assert len(lines) == 4
        assert lines[3]
        assert current_lock_token("test", lock_dir=tmp_path) == lines[3]


def test_lock_token_changes_across_separate_acquisitions(tmp_path):
    with run_lock("test", lock_dir=tmp_path) as lock_path:
        first_token = lock_path.read_text().splitlines()[3]
    with run_lock("test", lock_dir=tmp_path) as lock_path:
        second_token = lock_path.read_text().splitlines()[3]
    assert first_token != second_token


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


# --- run_lock_or_inherited's validated-token inheritance check (docs/agent-runtime-audit.md's
# "lock bypass is caller-controlled" finding) ---


def test_current_lock_token_is_none_when_nothing_is_held(tmp_path):
    assert current_lock_token("test", lock_dir=tmp_path) is None


def test_inherited_with_matching_token_is_a_true_no_op(tmp_path, monkeypatch):
    """A genuine parent-held lock's own token, presented back via the env var, must be trusted —
    the whole point of this mechanism is that a real parent pipeline's child stages don't
    re-acquire the lock and deadlock against it."""
    with run_lock("test", lock_dir=tmp_path) as parent_lock_path:
        token = parent_lock_path.read_text().splitlines()[3]
        monkeypatch.setenv(LOCK_INHERITED_ENV, token)
        with run_lock_or_inherited("test", lock_dir=tmp_path) as inherited_path:
            assert inherited_path == parent_lock_path
            # Genuinely a no-op: the lock file is untouched, still owned by the outer run_lock,
            # and a real second acquire attempt would still correctly see it as held.
            with pytest.raises(RunLockHeld), run_lock("test", lock_dir=tmp_path):
                pass


def test_inherited_with_mismatched_token_falls_back_to_acquiring(tmp_path, monkeypatch):
    """A stale/copy-pasted env var whose token doesn't match the lock file currently on disk must
    never be trusted -- this is the exact bypass the plain boolean env var used to allow."""
    with run_lock("test", lock_dir=tmp_path):
        pass  # acquire and release once, just to produce *some* real token history
    monkeypatch.setenv(LOCK_INHERITED_ENV, "not-the-real-token")
    lock_path = tmp_path / "test.lock"
    # A real lock was actually acquired here (and will be released once this whole `with` exits)
    # -- confirmed by a concurrent acquire attempt seeing it as held, not merely by the file
    # existing.
    with run_lock_or_inherited("test", lock_dir=tmp_path), pytest.raises(RunLockHeld), run_lock(
        "test", lock_dir=tmp_path
    ):
        pass
    assert not lock_path.exists()


def test_inherited_with_no_lock_file_falls_back_to_acquiring(tmp_path, monkeypatch):
    """The env var can be set with no real lock ever having been taken at all (e.g. a stale
    environment copied from an unrelated shell/run) -- must still fail closed."""
    monkeypatch.setenv(LOCK_INHERITED_ENV, "some-token")
    with run_lock_or_inherited("test", lock_dir=tmp_path) as lock_path:
        assert lock_path.exists()
    assert not lock_path.exists()


def test_process_start_time_is_stable_for_the_same_pid(tmp_path):
    """(docs/agent-runtime-audit.md's "PID reuse" finding.) Two reads for this test process's own
    still-running pid must agree -- the whole mechanism depends on this being a stable identity
    signal, not a value that drifts between reads of the same live process."""
    first = process_start_time(os.getpid())
    second = process_start_time(os.getpid())
    assert first is not None
    assert first == second


def test_process_start_time_returns_none_for_a_dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert process_start_time(proc.pid) is None


def test_inherited_with_no_env_var_behaves_exactly_like_run_lock(tmp_path, monkeypatch):
    """Regression coverage for the pre-token-mechanism default path: unset env var -> normal
    acquire, unchanged."""
    monkeypatch.delenv(LOCK_INHERITED_ENV, raising=False)
    with run_lock_or_inherited("test", lock_dir=tmp_path) as lock_path:
        assert lock_path.exists()
        assert int(lock_path.read_text().splitlines()[0]) == os.getpid()
    assert not lock_path.exists()

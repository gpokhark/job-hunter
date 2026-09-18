"""A simple per-project run lock.

Its job is to stop two job-hunter/agent processes from concurrently driving the same
write-heavy stage — most importantly the sequential local-model review
(`scripts/review_with_lm_studio.py`), where a second overlapping run would both burn real,
sequential local-model time re-scoring jobs the first run is already scoring, and race the first
run writing `data/assessments.json`/the `assessments` table.

Not a distributed lock — one lock file per project, holding the holder's PID. A lock file whose
PID no longer exists is treated as abandoned (the previous holder crashed or was killed without
cleaning up) and is silently reclaimed, so a dead process never wedges future runs.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

#: Set (to any truthy string) in a stage subprocess's environment by `job-hunter pipeline`
#: (`pipeline.py`) when it spawns `scripts/refilter_archive.py`/`scripts/review_with_lm_studio.py`
#: — the pipeline process already holds `run_lock("job-hunter")` for the whole run, so a stage
#: script trying to acquire the identically-named lock again would deadlock against its own
#: parent (confirmed live: a real `job-hunter pipeline --no-scrape` run failed immediately this
#: way before `run_lock_or_inherited` existed). Unset for a standalone invocation of either
#: script, which then locks exactly as before.
LOCK_INHERITED_ENV = "JOB_HUNTER_LOCK_INHERITED"

#: How many times to retry the "stale lock found -> unlink -> re-create" sequence before giving
#: up. More than one attempt is needed because that sequence is itself a race: two processes can
#: both decide the same lock is stale and both try to reclaim it, and only one `O_CREAT|O_EXCL`
#: open can win. A retry lets the loser re-check who actually holds it now (either the winner of
#: the reclaim race, or a third process that acquired cleanly in between) instead of crashing on
#: an unhandled FileExistsError.
_RECLAIM_ATTEMPTS = 3

#: `_read_holder` retries this many times, `_READ_HOLDER_RETRY_DELAY` apart, before concluding a
#: lock file's content is genuinely stale rather than mid-write. `os.open(O_CREAT|O_EXCL)`
#: atomically claims the lock, but the holder's own PID isn't written until the very next
#: `os.write` call -- a second process's exclusive-create can fail (file exists) and then read
#: that file in the brief window while it's still empty. Treating "empty" as "stale" immediately
#: (the pre-hardening behavior) let a loser reclaim a lock a winner had *just* created but hadn't
#: finished writing yet, confirmed live via a 5-process race that intermittently produced multiple
#: "successful" acquisitions of the same lock. ~200ms of total retry budget is far longer than the
#: real gap between `os.open` and `os.write` ever takes, so this costs nothing in the common case
#: and only ever matters when a lock file is momentarily empty.
_READ_HOLDER_RETRIES = 20
_READ_HOLDER_RETRY_DELAY = 0.01


class RunLockHeld(Exception):
    def __init__(
        self,
        lock_path: Path,
        holder_pid: int,
        *,
        holder_command: str | None = None,
        holder_started_at: str | None = None,
    ):
        self.lock_path = lock_path
        self.holder_pid = holder_pid
        self.holder_command = holder_command
        self.holder_started_at = holder_started_at
        detail = f"pid {holder_pid}"
        if holder_command:
            detail += f", running `{holder_command}`"
        if holder_started_at:
            detail += f", since {holder_started_at}"
        super().__init__(f"another job-hunter process ({detail}) already holds {lock_path}")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, just isn't ours to signal
    except OSError:
        return True  # unknown platform behavior -- fail safe, assume alive
    return True


def _read_holder(lock_path: Path) -> tuple[int, str | None, str | None]:
    """(pid, command, started_at) from a lock file's contents. `command`/`started_at` are `None`
    for a lock file written before those extra lines existed, or for unparseable content — only
    the first line (the PID) is load-bearing for correctness; the rest is diagnostic detail.

    Retries briefly on empty/unparseable content (see `_READ_HOLDER_RETRIES`) before concluding
    there's genuinely no valid PID — a fresh lock file is briefly empty between its holder's
    `O_CREAT|O_EXCL` create and the `os.write` that fills it in, and a reader landing in that
    window must not mistake "not written yet" for "stale, abandoned by a dead process."
    """
    for attempt in range(_READ_HOLDER_RETRIES):
        try:
            lines = lock_path.read_text().splitlines()
        except OSError:
            return -1, None, None
        if lines:
            try:
                pid = int(lines[0].strip())
            except ValueError:
                pid = -1
            if pid > 0:
                command = lines[1] if len(lines) > 1 and lines[1] else None
                started_at = lines[2] if len(lines) > 2 and lines[2] else None
                return pid, command, started_at
        if attempt < _READ_HOLDER_RETRIES - 1:
            time.sleep(_READ_HOLDER_RETRY_DELAY)
    return -1, None, None


def _try_create(lock_path: Path) -> int | None:
    """Attempts one `O_CREAT|O_EXCL` create. Returns the open fd on success, `None` if the path
    already exists (caller decides whether that's a live holder or a stale lock to reclaim)."""
    try:
        return os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None


@contextmanager
def run_lock(name: str, *, lock_dir: Path | str = Path("data/locks")) -> Iterator[Path]:
    lock_dir = Path(lock_dir)
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{name}.lock"

    fd = _try_create(lock_path)
    for _ in range(_RECLAIM_ATTEMPTS):
        if fd is not None:
            break
        holder_pid, holder_command, holder_started_at = _read_holder(lock_path)
        if holder_pid > 0 and pid_alive(holder_pid):
            raise RunLockHeld(
                lock_path, holder_pid, holder_command=holder_command, holder_started_at=holder_started_at
            )
        # Stale lock (dead PID, or unparseable content): reclaim it. Another process may win the
        # race to recreate it between our unlink and our own open — retrying re-checks who holds
        # it now rather than assuming we always win.
        lock_path.unlink(missing_ok=True)
        fd = _try_create(lock_path)
    if fd is None:
        holder_pid, holder_command, holder_started_at = _read_holder(lock_path)
        raise RunLockHeld(
            lock_path, holder_pid, holder_command=holder_command, holder_started_at=holder_started_at
        )

    try:
        command = " ".join(sys.argv)
        started_at = datetime.now(UTC).isoformat(timespec="seconds")
        os.write(fd, f"{os.getpid()}\n{command}\n{started_at}\n".encode())
        os.close(fd)
        yield lock_path
    finally:
        lock_path.unlink(missing_ok=True)


def run_lock_or_inherited(
    name: str, *, lock_dir: Path | str = Path("data/locks")
) -> contextlib.AbstractContextManager[Path]:
    """Like `run_lock`, but a no-op when `LOCK_INHERITED_ENV` is set in this process's
    environment — see that constant's docstring. A caller that might run either standalone (must
    lock) or as a `job-hunter pipeline` stage subprocess (lock already held by its parent) should
    use this instead of `run_lock` directly."""
    if os.environ.get(LOCK_INHERITED_ENV):
        return contextlib.nullcontext(Path(lock_dir) / f"{name}.lock")
    return run_lock(name, lock_dir=lock_dir)

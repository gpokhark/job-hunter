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

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class RunLockHeld(Exception):
    def __init__(self, lock_path: Path, holder_pid: int):
        self.lock_path = lock_path
        self.holder_pid = holder_pid
        super().__init__(f"another job-hunter process (pid {holder_pid}) already holds {lock_path}")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, just isn't ours to signal
    except OSError:
        return True  # unknown platform behavior -- fail safe, assume alive
    return True


@contextmanager
def run_lock(name: str, *, lock_dir: Path | str = Path("data/locks")) -> Iterator[Path]:
    lock_dir = Path(lock_dir)
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{name}.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            holder_pid = int(lock_path.read_text().strip())
        except (ValueError, OSError):
            holder_pid = -1
        if holder_pid > 0 and _pid_alive(holder_pid):
            raise RunLockHeld(lock_path, holder_pid) from None
        # Stale lock: previous holder is gone. Reclaim it.
        lock_path.unlink(missing_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield lock_path
    finally:
        lock_path.unlink(missing_ok=True)

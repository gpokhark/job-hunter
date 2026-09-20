"""Atomic text writes: write to a same-directory temp file, then `os.replace()` it over the
real path. A plain `Path.write_text()` truncates the destination before writing its new
content — a process interrupted (killed, crashed, or racing a second job-hunter/agent process
writing the same path, e.g. two review runs sharing `data/assessments.json`) mid-write leaves a
truncated or empty file behind. `os.replace()` is atomic on both POSIX and Windows, so a reader
only ever sees the old complete content or the new complete content, never a partial write.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path | str, content: str, *, encoding: str = "utf-8") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise

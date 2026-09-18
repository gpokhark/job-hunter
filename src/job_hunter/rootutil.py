"""Project-root resolution shared by the `job-hunter` CLI and every `scripts/*.py` entry point.

Every config/data default in this codebase (`config/settings.yaml`, `data/jobs.sqlite3`,
`data/searches`, `data/assessments.json`, ...) is a bare relative `Path`, resolved against the
process's current working directory only when it's actually opened — not when the `Path` object
is constructed. That means the single choke point that makes every one of those paths portable
across agent runtimes (an agent that hasn't already `cd`'d into the repo) is resolving and
`chdir`-ing into the real project root once, early — before any command touches a single one of
them. `--project`/`JOB_HUNTER_ROOT` wins; with neither set, behavior is unchanged (CWD as before).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def resolve_project_root(explicit: str | os.PathLike[str] | None) -> Path:
    candidate = explicit or os.environ.get("JOB_HUNTER_ROOT")
    if candidate is None:
        return Path.cwd()
    root = Path(candidate).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(
            f"--project/JOB_HUNTER_ROOT path does not exist or is not a directory: {root}"
        )
    return root


def chdir_to_project_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the project root and chdir into it (a no-op when neither --project nor
    JOB_HUNTER_ROOT is set, so nothing changes for anyone already running from the repo root).
    Every other relative path — config, data, and any relative path passed on the command
    line — is then interpreted relative to it, the same convention `git -C <path>` uses."""
    root = resolve_project_root(explicit)
    os.chdir(root)
    return root


def add_project_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project",
        type=Path,
        default=None,
        help=(
            "job-hunter project root to run against (falls back to $JOB_HUNTER_ROOT, then the "
            "current directory). Equivalent to running from that directory first: every other "
            "relative path on this command is then resolved against it, not your original "
            "shell directory."
        ),
    )

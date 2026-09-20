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
    if not (root / "pyproject.toml").exists() or not (root / "config" / "settings.yaml").exists():
        raise FileNotFoundError(
            f"--project/JOB_HUNTER_ROOT path does not look like a job-hunter checkout (missing "
            f"pyproject.toml and/or config/settings.yaml): {root}"
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


def nonneg_int(value: str) -> int:
    """`argparse`'s `type=` for any numeric option that's conceptually a count/limit/window
    (`--limit`, `--max-candidates`, `--new-days`, `--min-support`, ... — docs/agent-runtime-
    audit.md's "input validation" finding). Plain `type=int` accepts a negative value with no
    complaint, which then flows into downstream Python — e.g. `to_review[:args.limit]` — where a
    negative slice bound is silently *valid* Python (it means "all but the last N"), producing
    surprising, hard-to-debug behavior instead of a clear error at the command line. Shared here,
    not duplicated per-script, since every `scripts/*.py` entry point that defines one of these
    options already imports from this module for `--project`/root resolution."""
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"must be a non-negative integer, got {value!r}")
    return parsed


def add_project_argument(parser: argparse.ArgumentParser, *, suppress_default: bool = False) -> None:
    """Register `--project` on `parser`. `suppress_default=True` is for a *subparser* that already
    shares this flag with its root parser: argparse re-applies a subparser's own default over
    whatever the root parser already parsed whenever the subcommand's own args don't repeat the
    flag, so `job-hunter --project X doctor` would otherwise silently lose `X` the moment
    `doctor`'s subparser re-defaults it to `None`. `default=argparse.SUPPRESS` makes the subparser
    leave `args.project` untouched when it wasn't given at that position, so whichever parser
    actually saw the flag wins — confirmed against a standalone argparse repro of both orders
    plus the "neither given" case before relying on it here."""
    parser.add_argument(
        "--project",
        type=Path,
        default=argparse.SUPPRESS if suppress_default else None,
        help=(
            "job-hunter project root to run against (falls back to $JOB_HUNTER_ROOT, then the "
            "current directory). Equivalent to running from that directory first: every other "
            "relative path on this command is then resolved against it, not your original "
            "shell directory."
        ),
    )

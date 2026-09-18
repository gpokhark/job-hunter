"""Shared logic behind job-hunter's candidate-profile diff hook.

Both agent runtimes this project installs into (Claude Code, Hermes) support a "a tool just ran"
hook, but each speaks a different stdin JSON wire format for it — Claude Code's `PostToolUse`
carries the edited path at `tool_input.file_path`, Hermes's `post_tool_call` carries it at
`tool_input.path` (see `docs/skill-frontmatter-and-hook-plan.md` section 4.4, verified live
against each runtime's own docs). The two runtime-specific adapters
(`scripts/claude_profile_hook.py`, `scripts/hermes_profile_hook.py`) are responsible for parsing
their own payload shape into one plain path string — everything after that is identical, and
lives here: does this path resolve to *this project's* `config/candidate_profile.yaml`, and if
so, run `scripts/diff_profile.py` to regenerate its check-mode diff report against the tracked
baseline.

Deliberately no argparse, no stdin/CLI handling in this module — it is imported and its two
functions called directly, which is what makes it directly unit-testable (`tests/
test_hook_adapter.py`) without spawning a subprocess or faking stdin at all; that concern belongs
entirely to the runtime-specific scripts, exercised separately via real stdin-payload subprocess
tests. Every failure mode (`uv` not found, a non-zero `diff_profile.py` exit, a timeout) is
logged to `logs/profile-hook.log` instead of being silently swallowed the way the previous
Hermes-only hook's bare `contextlib.suppress(...)` did (see the plan doc's section 2.3) — a hook
that fails invisibly is worse than one that's merely advisory, since nothing else in this
pipeline would ever hint an edit-triggered report quietly stopped updating.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

#: The script this hook exists to re-run — check mode only, no flags, exactly what a human
#: running `job-feedback`'s step 5 would invoke by hand.
DIFF_SCRIPT = "scripts/diff_profile.py"

#: The one file this hook watches for, relative to the project root — matches
#: `config.py`'s own default `candidate_profile.yaml` location.
PROFILE_RELATIVE_PATH = "config/candidate_profile.yaml"

#: Relative to the project root, mirroring every other project log/data path in this codebase
#: (see `rootutil.py`'s docstring) — resolved against `repo_root`, never CWD, so the hook logs to
#: the same place regardless of which directory the calling runtime happened to start in.
LOG_RELATIVE_PATH = "logs/profile-hook.log"

#: `diff_profile.py`'s own check-mode run is a full SQLite sweep (`evaluate_prefilter` against
#: every stored, US-eligible, recency-passing job) — comfortably under a minute in practice, but
#: a hook must never hang a runtime indefinitely if something goes wrong downstream (a locked
#: database, a huge job pool). Matches the previous Hermes hook's own timeout value.
RUN_TIMEOUT_SECONDS = 55


def should_run_diff(tool_input_path: str, repo_root: Path, *, cwd: Path | None = None) -> bool:
    """True exactly when `tool_input_path` — as a runtime reports it, possibly relative,
    possibly `~`-prefixed — resolves to `repo_root`'s own `config/candidate_profile.yaml`.

    `cwd` is the directory a relative path should be resolved against: a runtime's own reported
    working directory when it has one (Hermes's `payload["cwd"]`), else `None` to fall back to
    the process's real current directory. An empty or non-existent path is never a match — this
    function only ever returns True/False, it never raises for "nothing to check" input; callers
    are expected to have already handled "this payload carries no path at all" before calling it.
    """
    if not tool_input_path:
        return False
    edited = Path(tool_input_path).expanduser()
    if not edited.is_absolute():
        edited = (cwd or Path.cwd()) / edited
    target = repo_root / PROFILE_RELATIVE_PATH
    try:
        return edited.resolve() == target.resolve()
    except OSError:
        # A component that can't be resolved (e.g. a broken symlink somewhere in the path) is
        # not a match — fail closed, the same way a job with no discoverable date is kept rather
        # than guessed at elsewhere in this codebase, just inverted: here "can't tell" means
        # "don't run the diff," since running it spuriously is the higher-cost mistake.
        return False


def _log_failure(repo_root: Path, message: str) -> None:
    log_path = repo_root / LOG_RELATIVE_PATH
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).isoformat(timespec="seconds")
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} {message}\n")


def run_diff(repo_root: Path) -> bool:
    """Runs `uv run python scripts/diff_profile.py` (check mode — no flags) against `repo_root`.

    Discovers `uv` via `shutil.which` rather than assuming it's on `PATH` — the environment a
    hook subprocess runs under is exactly the kind of place that assumption has failed before
    (see `docs/skill-frontmatter-and-hook-plan.md` section 2.3's audit of the previous Hermes
    hook, which called `uv` directly with no location check at all). Returns `True` on a clean,
    zero-exit run; `False` for every failure mode (`uv` missing, non-zero exit, timeout) — each
    one logged to `logs/profile-hook.log` first via `_log_failure`, never swallowed silently.
    """
    uv_path = shutil.which("uv")
    if uv_path is None:
        _log_failure(repo_root, "uv not found on PATH — skipping candidate-profile diff")
        return False
    try:
        proc = subprocess.run(
            [uv_path, "run", "python", DIFF_SCRIPT],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _log_failure(repo_root, f"diff_profile.py timed out after {RUN_TIMEOUT_SECONDS}s")
        return False
    except OSError as exc:
        _log_failure(repo_root, f"failed to start diff_profile.py: {exc}")
        return False
    if proc.returncode != 0:
        stderr_tail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ""
        _log_failure(repo_root, f"diff_profile.py exited {proc.returncode}: {stderr_tail}")
        return False
    return True

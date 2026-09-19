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

Explicit scope decision (docs/agent-runtime-audit.md's "no debounce/serialization" finding, and
`docs/agent-runtime-remaining-fixes-plan.md` item 5, which asked for this to be decided rather
than left implicit): Claude Code's hooks reference documents a `FileChanged` event — a genuine
filesystem watch independent of which tool (or non-tool mechanism: a Bash `sed`, an external
editor, a `git checkout`) touched the file — as a broader alternative to `PostToolUse`, which only
ever fires after one of *Claude's own* tool calls succeeds. This module's coverage is deliberately
scoped to **"profile changes made by the agent via a tool call"** (`PostToolUse` — see
`.claude/settings.json`'s `Edit|Write` matcher — plus Hermes's equivalent `post_tool_call`), not
"by any mechanism." `FileChanged` was not wired up in this round: its exact stdin payload shape
couldn't be confirmed against a live-firing event in this session (unlike every other piece of
this hook infrastructure, which this codebase's own history treats as a hard requirement before
shipping — see `docs/skill-frontmatter-and-hook-plan.md`'s repeated "confirmed live" citations),
and shipping a parser for an unverified wire format is exactly the class of bug this project's own
`CLAUDE.md`/audit history warns is caught only by live reproduction, never by a plausible-looking
mock. An out-of-band edit (a hand-edit in another program, a `git checkout` that changes the
profile) genuinely won't trigger a fresh report until the next agent-driven edit or a manual
`job-hunter pipeline --no-scrape` — an accepted, narrower-than-ideal gap, not an oversight. Adding
`FileChanged` support is a reasonable, self-contained follow-up once its payload shape can be
confirmed the same way — register a temporary debug hook that dumps its own stdin, trigger a real
file change, and read back what was actually sent, rather than coding against inferred docs.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from .atomic import atomic_write_text
from .runlock import RunLockHeld, run_lock

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

#: Named separately from `runlock.py`'s `"job-hunter"` pipeline lock — this guards only
#: overlapping *hook* runs against each other, never against a `job-hunter pipeline`/`cleanup
#: --apply` run (those touch SQLite/assessments.json; this only ever reads SQLite and rewrites
#: the profile-diff snapshot/report, a disjoint concern that shouldn't block or be blocked by the
#: other). Relative to the project root the same way every other lock/log path in this module is.
HOOK_LOCK_NAME = "profile-hook"
HOOK_LOCK_DIR_RELATIVE_PATH = "data/locks"

#: Minimum gap between two profile-diff runs for the same project (docs/agent-runtime-audit.md's
#: "no debounce/serialization" finding) — collapses a burst of rapid edits (e.g. a multi-file
#: refactor that happens to touch candidate_profile.yaml more than once) into one report
#: reflecting the final state, instead of one subprocess spawn per edit event. Comfortably longer
#: than the gap between a handful of edits in the same tool-call burst, comfortably shorter than
#: a person would ever notice as "stale."
DEBOUNCE_SECONDS = 5.0
DEBOUNCE_MARKER_RELATIVE_PATH = "logs/.profile-hook-last-run"


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


def _recently_run(repo_root: Path) -> bool:
    marker = repo_root / DEBOUNCE_MARKER_RELATIVE_PATH
    try:
        last_started = float(marker.read_text().strip())
    except (OSError, ValueError):
        return False
    return (time.time() - last_started) < DEBOUNCE_SECONDS


def _record_run_start(repo_root: Path) -> None:
    marker = repo_root / DEBOUNCE_MARKER_RELATIVE_PATH
    marker.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(marker, str(time.time()))


def run_diff(repo_root: Path) -> bool:
    """Runs `uv run python scripts/diff_profile.py` (check mode — no flags) against `repo_root`.

    Discovers `uv` via `shutil.which` rather than assuming it's on `PATH` — the environment a
    hook subprocess runs under is exactly the kind of place that assumption has failed before
    (see `docs/skill-frontmatter-and-hook-plan.md` section 2.3's audit of the previous Hermes
    hook, which called `uv` directly with no location check at all). Returns `True` on a clean,
    zero-exit run; `False` for every failure mode (`uv` missing, non-zero exit, timeout, or one of
    the two guards below) — each one logged to `logs/profile-hook.log` first via `_log_failure`,
    never swallowed silently. No caller currently branches on the return value (both runtime
    adapters call this and ignore it — a hook is advisory by design), so folding "skipped
    on purpose" into the same `False` as "genuinely failed" costs nothing today, and keeps the
    interface to one boolean rather than a three-way result nothing yet needs.

    Two protections against overlapping/redundant runs from a burst of rapid edits
    (docs/agent-runtime-audit.md's "no debounce/serialization" finding — several edits touching
    `candidate_profile.yaml` in quick succession could previously start several overlapping
    `diff_profile.py` runs, and the final written report depended on whichever process happened
    to finish last, not necessarily the most recent edit):
    - A non-blocking `run_lock(HOOK_LOCK_NAME, ...)` — if another hook invocation for this project
      is already mid-run (e.g. a `PostToolUse` and a `FileChanged` event both firing for the same
      edit), this one is skipped entirely rather than queuing behind it or racing its report
      write. Nothing is lost: the in-progress run's report reflects whatever the profile looked
      like when *it* started, and if a later edit lands after that, the next qualifying hook event
      calls `run_diff` again once this one is no longer holding the lock.
    - A `DEBOUNCE_SECONDS` window — even when the lock is free, a run is skipped if another one
      for this project *started* within the last few seconds, collapsing several edits in the
      same short burst into a single report instead of one per edit.
    Both are best-effort and purely advisory, matching this hook's own "a stale report is
    tolerable, a silently-lost failure is not" philosophy — every skip is logged, never silent.
    """
    if _recently_run(repo_root):
        _log_failure(
            repo_root,
            f"skipped candidate-profile diff — another run started within the last "
            f"{DEBOUNCE_SECONDS:g}s (debounce)",
        )
        return False
    try:
        lock_dir = repo_root / HOOK_LOCK_DIR_RELATIVE_PATH
        with run_lock(HOOK_LOCK_NAME, lock_dir=lock_dir):
            _record_run_start(repo_root)
            return _run_diff_once(repo_root)
    except RunLockHeld:
        _log_failure(
            repo_root, "skipped candidate-profile diff — another hook run is already in progress"
        )
        return False


def _run_diff_once(repo_root: Path) -> bool:
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

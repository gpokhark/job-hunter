# Agent Runtime — Remaining Fixes Implementation Plan

**Written:** 2026-09-18. **Audience:** an independent Claude Code agent with no memory of the
session that wrote this — every item below is self-contained: current state (verified against the
live repo, with exact commands/line numbers, not assumed), root cause, recommended fix, files to
touch, tests to add, and how to verify.

## How to use this document

1. This is a companion to `docs/agent-runtime-audit.md` (the audit), not a replacement — that file
   gets **fully regenerated** by an external audit tool on each re-run and does not preserve
   resolution notes reliably. This plan is the durable, version-controlled record of what's left
   and how to fix it. Update the checkboxes below as you go; when you close an item, also add a
   dated one-paragraph "Resolved" note to the relevant finding in `docs/agent-runtime-audit.md`
   itself (see existing resolved notes there for the format), since that's still what a fresh audit
   pass reads first.
2. Work top to bottom (roughly priority order), but each item is independent — no item below
   depends on another being done first, except where explicitly noted.
3. **Before starting any item, re-verify its "Current state" section against the live repo** — this
   plan was accurate as of 2026-09-18, but the codebase moves. If a `grep`/behavior no longer
   matches what's described, the item may already be partially or fully resolved; check before
   assuming the fix is still needed.
4. After every item: run `uv run pytest -q && uv run ruff check . && python -m compileall -q src
   scripts tests` and confirm all three are clean before moving to the next item. Baseline as of
   this writing: **352 passed**, `ruff` clean, `compileall` clean.
5. **Two audit findings are false positives — do not act on them.** Both were checked directly
   against the actual files, twice, across separate audit re-runs that kept repeating them anyway:
   - `skills/job-radar/SKILL.md`'s `/job-radar --keyword ADAS --refilter` example and README's
     identical example are **not** broken CLI claims — both are immediately followed by an explicit
     inline explanation that this is slash-command shorthand for `job-hunter pipeline --no-scrape`,
     not a literal `render_radar.py --refilter` flag. Nothing to fix here.
   - README's "installable... same as the other five" (describing `onboard-source`, one of six
     skills) reads unambiguously in context. Not a real bug.

## ToDo checklist

- [x] 1. Safe default pipeline stage timeout + process-group cancellation on timeout
- [x] 2. Replace `JOB_HUNTER_LOCK_INHERITED` boolean env-var bypass with a validated parent-run token
- [x] 3. Authoritative run identity/recovery (PID-reuse-safe abandoned detection, non-mtime latest-run selection)
- [x] 4. Relocatable Hermes hook registration (survive a moved/copied checkout)
- [x] 5. Claude hook coverage: debounce/serialize rapid edits; decide `PostToolUse` vs `FileChanged` scope
- [x] 6. Reject negative values on numeric CLI options
- [ ] 7. Remove or implement `--all-companies`
- [ ] 8. SQLite schema-version table
- [ ] 9. Documentation drift: README company count, `docs/SPEC.md` onboarding section, universal `--project` claims
- [ ] 10. Provenance fields on refiltered/stale-source-fallback output
- [ ] 11. Bootstrap command + CI workflow (larger, do last)

---

## 1. Safe default pipeline stage timeout + process-group cancellation

**Priority: P0/P1.** This is the split-in-two finding from `docs/agent-runtime-audit.md` — the
`TimeoutExpired`-carries-raw-bytes crash half is **already fixed** (see
`src/job_hunter/pipeline.py`'s `_decode_timeout_output()` and the "Resolved" note under the audit's
Finding #2). Two halves remain:

### 1a. Timeout is opt-in, not a safe default

**Current state (verified):**
```
$ grep -n "stage_timeout_seconds" src/job_hunter/config.py config/settings.yaml
src/job_hunter/config.py:76:    stage_timeout_seconds: int | None = Field(
config/settings.yaml:34:#   stage_timeout_seconds: 14400  # 4 hours   <- commented out
```
`PipelineConfig.stage_timeout_seconds` defaults to `None` (no timeout) and the shipped
`config/settings.yaml` leaves it commented out. A fresh clone can hang indefinitely on a stuck
search/review/refilter/radar subprocess.

**Fix:** Uncomment the default in `config/settings.yaml` (pick a genuinely generous value — 4 hours
was the original placeholder reasoning: "sized to not clip a large sequential LM Studio review";
confirm this is still reasonable, or make it larger, e.g. 6-8 hours, since a very large candidate
pool reviewed one-job-at-a-time against a local model can legitimately run long). Do **not** lower
`PipelineConfig`'s own Python-level default from `None` — that would silently change behavior for
anyone who has `stage_timeout_seconds` unset in their own `config/settings.yaml` (breaking the
"unset = no timeout" contract documented in `PipelineConfig`'s docstring); the fix belongs in the
*shipped config file* only, so a fresh clone gets the safe default but an existing user's
already-`None`-configured setup is unaffected unless they re-copy the file.

### 1b. No process-group cancellation

**Current state (verified):** `_run_stage_subprocess()` in `src/job_hunter/pipeline.py` calls
```python
subprocess.run(cmd, cwd=project_root, capture_output=True, text=True, timeout=timeout, env=env)
```
with no `start_new_session=True`/process-group setup. `subprocess.run(..., timeout=...)` on
expiry kills the **direct child only** (`Popen.kill()` internally) — any descendant process that
child spawned (e.g. if `review_with_lm_studio.py` or a future adapter ever shells out to something)
is not guaranteed to be terminated and can keep running, holding files/sockets/model connections.

**Fix:**
1. In `_run_stage_subprocess()`, add `start_new_session=True` to the `subprocess.run(...)` call —
   this puts the child in its own process group (POSIX; this codebase is macOS/Linux-oriented per
   existing `os.kill`/`os.getpid()` usage elsewhere in `runlock.py`, so this is consistent).
2. On `subprocess.TimeoutExpired`, `subprocess.run`'s own internal handling already calls
   `process.kill()` on the *direct* child before re-raising — but to kill the whole group, you need
   the `Popen` object directly rather than going through the `run()` convenience wrapper (which
   doesn't expose the pid post-timeout the way you'd need for a group kill). Recommended approach:
   replace the `subprocess.run(...)` call in `_run_stage_subprocess` with a `subprocess.Popen(...,
   start_new_session=True)` + manual `communicate(timeout=timeout)` + `except
   subprocess.TimeoutExpired: os.killpg(os.getpgid(proc.pid), signal.SIGKILL); proc.communicate()`
   pattern — killing the whole process group, not just the child, then re-raising with whatever
   partial output was captured (still needs `_decode_timeout_output()` on the result, same as
   today).
3. Add an integration test with a real subprocess that spawns a grandchild (e.g. a tiny fixture
   script under `tests/fixtures/` that does `subprocess.Popen([sys.executable, "-c", "import
   time; time.sleep(30)"])` then sleeps itself) and assert the grandchild is actually gone
   (`os.kill(grandchild_pid, 0)` raises `ProcessLookupError`) after the pipeline stage times out —
   this is the one thing a mocked `subprocess.run` replacement (the existing test style in
   `tests/test_pipeline.py`) cannot verify; it needs a real process tree.

**Files:** `src/job_hunter/pipeline.py` (`_run_stage_subprocess`), `config/settings.yaml`,
`tests/test_pipeline.py` (new real-subprocess integration test), possibly a new fixture script
under `tests/fixtures/`.

---

## 2. Replace the `JOB_HUNTER_LOCK_INHERITED` boolean bypass with a validated token

**Priority: P1.** `src/job_hunter/runlock.py` defines:
```python
LOCK_INHERITED_ENV = "JOB_HUNTER_LOCK_INHERITED"

def run_lock_or_inherited(name, *, lock_dir=Path("data/locks")):
    if os.environ.get(LOCK_INHERITED_ENV):
        return contextlib.nullcontext(Path(lock_dir) / f"{name}.lock")
    return run_lock(name, lock_dir=lock_dir)
```
`src/job_hunter/pipeline.py`'s `_run_stage_subprocess(..., lock_inherited=True)` sets
`{**os.environ, LOCK_INHERITED_ENV: "1"}` when spawning `review_with_lm_studio.py`/
`refilter_archive.py` as pipeline stages, since the pipeline already holds `run_lock("job-hunter")`
for the whole run and those scripts would otherwise deadlock trying to reacquire the identically
named lock (this mechanism itself was built and verified live in an earlier session — the deadlock
it fixes is real and confirmed).

**The problem:** any standalone invocation of `review_with_lm_studio.py`/`refilter_archive.py` can
set `JOB_HUNTER_LOCK_INHERITED=1` in its own shell before running, and skip the lock entirely —
there's no verification that a real parent pipeline process is actually holding the lock right now.
This defeats the entire purpose of `run_lock("job-hunter")` for anyone (human or agent) who sets
that env var, intentionally or by copy-pasting a stale environment from a prior pipeline run.

**Fix (as designed in a prior session, not yet implemented):** replace the boolean with a
per-run capability token:
1. When `run_pipeline()` acquires `run_lock("job-hunter")`, generate a random token (e.g.
   `secrets.token_hex(16)`) and write it into the lock file's own content (extend
   `runlock.py`'s existing `"{pid}\n{command}\n{started_at}\n"` format to a fourth line, or store
   it in a sibling `.lock.token` file — pick one and update `_read_holder()`/the lock-writing code
   in `run_lock()` to match).
2. Pass that same token (not just `"1"`) as the env var value:
   `{LOCK_INHERITED_ENV: token}` instead of `{LOCK_INHERITED_ENV: "1"}`.
3. `run_lock_or_inherited()` reads the env var, then reads the *current* lock file's own recorded
   token and compares — only treats the lock as inherited if the two match. If the lock file has no
   token recorded, or doesn't match, or the lock file doesn't exist at all, fall back to acquiring
   `run_lock()` normally (i.e., "fail closed" — an unverifiable inheritance claim is not trusted).
4. This makes the env var alone insufficient to bypass — an attacker/copy-paste would also need to
   read the actual live lock file's current token, which changes every run and only exists while
   the real parent pipeline is genuinely holding the lock.

**Files:** `src/job_hunter/runlock.py` (`run_lock`, `run_lock_or_inherited`, `_read_holder`),
`src/job_hunter/pipeline.py` (`_run_stage_subprocess`'s `lock_inherited` env construction).

**Tests:** extend `tests/test_runlock.py` with: a valid token in both places → inherited (no lock
acquired); a mismatched/stale token → falls back to acquiring normally; no token in env → unchanged
normal-acquire behavior (regression coverage for the existing behavior). Extend
`tests/test_pipeline.py`'s existing lock/timeout tests to confirm the pipeline's own child spawns
still work end-to-end with the new token mechanism (they currently mock `subprocess.run` entirely,
so this should mostly be confirming the env dict construction is still correct, not a new
integration test).

---

## 3. Authoritative run identity/recovery

**Priority: P1.** Two related but distinct gaps, both in `src/job_hunter/pipeline.py`:

### 3a. `latest_run_id()` uses filesystem mtime

```python
def latest_run_id(*, runs_dir: Path = RUNS_DIR) -> str | None:
    ...
    manifests = list(runs_dir.glob("*/manifest.json"))
    ...
    return max(manifests, key=lambda p: p.stat().st_mtime).parent.name
```
An imported/copied run directory (e.g. from a backup, or copied between machines) or a
partially-written manifest can become the "latest" target purely by having a newer mtime than the
real latest run, even if its actual `started_at`/`completed_at` timestamps (already recorded in
`PipelineManifest`) are older.

**Fix:** read each manifest's own `started_at` field (already a `datetime` on `PipelineManifest`)
instead of the file's mtime. This requires actually parsing each candidate manifest
(`PipelineManifest.model_validate_json(...)`) rather than just globbing paths — more I/O per call,
but `data/runs/` is not expected to hold thousands of entries, so this is an acceptable tradeoff for
correctness. Keep mtime as a tiebreaker only if two manifests have identical `started_at` (should be
rare given `new_run_id()`'s own uniqueness).

### 3b. PID reuse can make an abandoned manifest look "live"

`cli.py`'s `pipeline-status` handler checks `pid_alive(manifest.pid)` to distinguish a genuinely
still-running pipeline from an abandoned one. If the original process died and the OS later reuses
that same PID for an unrelated process, `pid_alive()` returns `True` for the wrong reason, and a
truly-abandoned manifest gets reported as `running` instead of `abandoned`.

**Fix:** record process start-time identity alongside the PID, where the platform permits it — on
Linux, `/proc/<pid>/stat`'s start-time field (jiffies since boot); this doesn't have a portable
stdlib equivalent, so either: (a) shell out to `ps -o lstart= -p <pid>` (works on both macOS and
Linux, though format differs) and store the raw string for later exact-string comparison (not
parsed/interpreted — just "does this match what we recorded"), or (b) accept this as
best-effort/Linux-only via `/proc` and fall back to PID-only liveness on other platforms (document
the limitation explicitly rather than silently degrading). Given this project's actual deployment
context (single-operator machines, not long-lived multi-tenant servers where PID reuse collisions
are likely within a session's lifetime), option (a) with graceful fallback to PID-only-liveness
when `ps` fails/is unavailable is the pragmatic choice — don't over-engineer a cross-platform
process-identity library for this.

**Files:** `src/job_hunter/pipeline.py` (`latest_run_id`), `src/job_hunter/models.py`
(`PipelineManifest` — add a `pid_start_time: str | None` field), `src/job_hunter/cli.py`
(`pipeline-status`'s abandoned-detection logic), `src/job_hunter/runlock.py` (if reused for the
`ps`-based start-time helper — consider whether it belongs there alongside `pid_alive()`, since
both are "is this specific process instance the one I think it is" concerns).

**Tests:** `tests/test_pipeline.py` — `latest_run_id` picks by `started_at` not mtime (construct
two manifests with mtimes reversed relative to their `started_at` values, assert the
`started_at`-newer one wins). `tests/test_cli.py` — extend the existing abandoned/live pid tests
with a start-time mismatch case (same PID number, different recorded start-time → treated as
abandoned even though `pid_alive()` alone would say "alive").

---

## 4. Relocatable Hermes hook registration

**Priority: P1.** `scripts/install_hermes_hook.py`:
```python
command = shlex.join([
    "python3", str(hermes_home / "agent-hooks/job-hunter-profile.py"), str(repo_root),
])
```
This embeds the **absolute `repo_root` path** (and a bare `python3`, assuming it's on `PATH` and is
the right interpreter) directly into the registered command string in Hermes's `config.yaml`.
`install()`/`uninstall()` (same file) both match/deduplicate entries by this exact string. If the
repository is moved or the checkout is re-cloned to a new path, the old registration:
- still exists in `config.yaml` pointing at a path that no longer has the repo,
- is never found/removed by `--update` (which only refreshes the *hook script symlink* via
  `install_one()` — note: item 3 from the **already-completed** installer-ownership-marker work
  covers file/symlink ownership, not this registration-string problem; they're different halves of
  the same audit finding, and only the file/symlink half is done),
- a fresh `install_skill.sh --hermes` from the new path adds a **second**, different-path
  registration alongside the stale one, rather than replacing it.

**Fix:**
1. Identify registrations by a stable marker independent of the absolute path — e.g. embed a fixed
   sentinel argument (`--job-hunter-hook`) in the command, or match on the hook script's *basename*
   (`job-hunter-profile.py`) appearing anywhere in the command string, rather than requiring the
   exact full string (including `repo_root`) to match.
2. On `install()`, before appending a new entry, remove any existing entry matching that same
   marker (regardless of its embedded path) — so re-running install after a move replaces the stale
   registration instead of accumulating a second one.
3. Consider using `sys.executable`-equivalent resolution or a documented `#!/usr/bin/env python3`
   wrapper script (already how `hermes_profile_hook.py` itself likely starts — check) instead of a
   bare `python3`, to reduce interpreter-mismatch risk; this is a smaller, separate concern from
   path relocation — don't conflate the two fixes.
4. Add a `job-hunter doctor`-style check (or extend the existing `doctor` command) that reports
   "Hermes hook registered but the script it points to no longer exists" as a diagnosable warning,
   so a stale registration is at least visible rather than silently inert.

**Files:** `scripts/install_hermes_hook.py` (`install`, `uninstall`), possibly
`src/job_hunter/cli.py`'s `doctor()` for the new diagnostic check.

**Tests:** extend `tests/test_install_skill.py`'s existing `test_hermes_install_and_uninstall_round_trip`-style
coverage (or a new test file for `install_hermes_hook.py` specifically, if one doesn't already
exist — check `tests/` first) with: install from path A, "move" (simulate by installing again with
a different `repo_root` value pointing at a second fixture directory), assert only one entry
remains in `config.yaml` and it reflects the new path, not both.

---

## 5. Claude hook coverage and debounce

**Priority: P1/P2.** `.claude/settings.json`'s hook registration:
```
$ grep -n matcher .claude/settings.json
5:        "matcher": "Edit|Write",
```
Current gaps (all still open, none touched by prior sessions' work):
- Only successful `Edit`/`Write` tool calls trigger the profile-diff hook — a `Bash`-driven edit to
  `candidate_profile.yaml`, a failed tool call that Claude retries differently, or an out-of-band
  edit (another editor, a git checkout) never triggers it.
- No debounce/serialization: several rapid edits (e.g. a multi-file refactor touching the profile
  more than once in quick succession) can start overlapping `diff_profile.py` runs, and the final
  written report depends on which process finishes last — not necessarily the most recent edit.
- `.claude/settings.json` invokes the hook via `uv` directly; if `uv` isn't on the invoking
  process's `PATH`, the launcher fails before `src/job_hunter/hook_adapter.py`'s own
  `shutil.which("uv")` check (which exists specifically to produce a clear diagnostic) ever runs.

**Fix (decide explicitly, don't guess):**
1. Read Claude Code's current hooks reference (https://code.claude.com/docs/en/hooks) to confirm
   whether `FileChanged` (watches on-disk changes regardless of tool) is still the right
   alternative/addition to `PostToolUse`, and whether it's available for this use case. Make an
   explicit choice: "profile changes made by the agent" vs. "profile changes by any mechanism" —
   document the decision in `CLAUDE.md` or the hook's own docstring, don't leave it implicit.
2. Add debounce/lock in `src/job_hunter/hook_adapter.py` (the shared adapter both Claude's and
   Hermes's hook scripts call into) rather than duplicating it per-runtime — e.g. a short-lived
   lock file (reuse `runlock.py`'s primitive with a dedicated lock name like `"profile-hook"`, or a
   simpler debounce: skip if a hook run for the same profile path started within the last N
   seconds) so a burst of edits collapses into one diff run reflecting the final state, not several
   racing ones.
3. For the `uv`-launcher problem: either change `.claude/settings.json` to invoke a portable
   wrapper that itself locates `python3`/`uv` and falls back with a clear stderr message (matching
   what `hook_adapter.py`'s `run_diff()` already does once it's reached), or document this as a
   known limitation with a specific remediation step (e.g. "ensure `uv` is on PATH for the
   Claude Code process, not just your interactive shell").

**Files:** `.claude/settings.json`, `src/job_hunter/hook_adapter.py`, possibly a new portable
launcher script.

**Tests:** extend `tests/test_hook_adapter.py` (check it exists first) with a debounce/lock
scenario — two rapid `run_diff()` calls for the same profile path, assert only one actually runs
`diff_profile.py` (or that they serialize rather than race, depending on which strategy is chosen).

---

## 6. Reject negative values on numeric CLI options

**Priority: P2.** `src/job_hunter/cli.py` has several `type=int` options
(`--limit`, `--max-candidates`, and equivalents on `pipeline`) with no lower-bound validation:
```
$ grep -n '"--limit"\|"--max-candidates"' src/job_hunter/cli.py
41:        "--max-candidates", ...
166:    pipeline.add_argument("--limit", ...)
169:    pipeline.add_argument("--max-candidates", ...)
```
A negative value (e.g. `--limit -5`) is currently accepted by argparse and passed straight through;
downstream Python slicing (`to_review[: args.limit]` in `scripts/review_with_lm_studio.py`, or
similar patterns) silently reinterprets a negative slice bound rather than erroring — producing
surprising, hard-to-debug behavior (e.g. "reviewing everything except the last 5" instead of an
error).

**Fix:** add a small reusable argparse `type=` validator function (e.g.
`_nonneg_int(value: str) -> int` raising `argparse.ArgumentTypeError` for `< 0`) in `cli.py`, apply
it to every numeric option that's conceptually a count/limit/window (grep for `type=int` across
`cli.py` and the standalone scripts — `--limit`, `--max-candidates`, `--new-days`,
`--undated-new-days`, `--undated-stale-days`, report-age windows in `cleanup`'s options, etc. — and
apply consistently, not just to the two named above).

**Files:** `src/job_hunter/cli.py`, and any standalone script (`scripts/*.py`) that defines its own
equivalent numeric argument independently (check `review_with_lm_studio.py`, `render_radar.py`,
`cleanup`-related options for duplicated `type=int` definitions).

**Tests:** a small parametrized test asserting `parser().parse_args(["search", "--limit", "-1"])`
(or equivalent) raises/exits with a clear error, for each option touched.

---

## 7. Remove or implement `--all-companies`

**Priority: P2.** Confirmed dead:
```
$ grep -n "all_companies\|all-companies" src/job_hunter/cli.py
35:    search.add_argument("--all-companies", action="store_true")
```
No other reference anywhere in `cli.py` — the flag parses but does nothing, since "all enabled
companies" is already `search`'s unconditional default when `--companies` is omitted.

**Fix (pick one, both are one-line-ish):**
- **Remove it** — simplest, matches "don't keep dead flags" — but check `README.md`/skill docs for
  any example that references `--all-companies` first (if one exists, remove that too, and note it
  in item 9's documentation-drift cleanup rather than doing it twice).
- **Or give it real, distinct semantics** — e.g. make it an explicit override that ignores a
  `--companies` filter if both are somehow passed together (currently `--companies`/`--all-companies`
  aren't in a mutually-exclusive group, so both could be passed with unclear precedence) — only do
  this if there's an actual use case; don't invent one just to keep the flag.

Given no evidence of real demand for a distinct behavior, **removal is the recommended default**
unless investigation turns up a reason to keep it.

**Files:** `src/job_hunter/cli.py`, `README.md`/skill docs if they mention it (grep first).

**Tests:** if removed, confirm no existing test references `--all-companies` (grep `tests/`
first); if kept with new semantics, add a test for the new behavior.

---

## 8. SQLite schema-version table

**Priority: P2.** Confirmed absent:
```
$ grep -n "schema_version\|PRAGMA user_version" src/job_hunter/storage.py
(no output)
```
`storage.py`'s `_migrate()` performs ad-hoc `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`-style checks
(`PRAGMA table_info(jobs)` + conditional `ALTER TABLE`) for each column that's been added since the
schema's original version — functional today, but with no explicit version marker, a future
migration that needs to do something more complex than "add a nullable column" (a rename, a type
change, a data backfill with side effects) has no reliable way to know which prior migrations have
already run against a given database file, especially one that's been through several versions
of this codebase.

**Fix:**
1. Add `PRAGMA user_version` (SQLite's built-in integer version pragma — no new table needed) as
   the schema-version marker. Read it once at `Storage.__init__`/`_migrate()` time.
2. Refactor `_migrate()` into a list of discrete, numbered migration steps (each one a small
   function or inline block, tagged with the `user_version` it upgrades *to*), applied in order
   starting from whatever the current `user_version` is, ending by setting `PRAGMA user_version =
   <latest>`.
3. Convert the *existing* ad-hoc column-presence checks into the first 1-2 numbered migrations
   (preserving their exact current behavior — this is a refactor of the mechanism, not a change to
   what already got migrated) so `user_version` starts accurately reflecting "which of the
   already-shipped schema changes has this database seen," not just future ones.
4. Add a test that constructs a database at an old `user_version` (or no pragma set at all — the
   SQLite default is `0`) and confirms `_migrate()` brings it to the current version idempotently
   (running it twice is a no-op the second time).

**Files:** `src/job_hunter/storage.py` (`_migrate`, `Storage.__init__`).

**Tests:** `tests/test_storage.py` — migration-from-old-version and migration-idempotency cases.

---

## 9. Documentation drift

**Priority: P1 (user-visible, first-run-affecting) but mechanically simple — good to batch together.**

Verified still-current drift, each with exact evidence:

1. **README company count.** `README.md` says "60 companies with implemented adapters" / "63
   companies" total; `job-hunter doctor` currently reports **65**:
   ```
   $ uv run job-hunter doctor | grep -i companies
   OK   configuration: 65 companies
   $ grep -n "60 companies\|63 companies" README.md
   README.md:7:- **Collect:** 60 companies with implemented adapters. Add, disable, or remove sources.
   README.md:39:The [registry](config/companies.yaml) contains **63 companies: 60 with implemented adapters
   ```
   Fix: re-run `job-hunter doctor` (or count `config/companies.yaml` entries directly, splitting
   `unsupported`-adapter entries from implemented ones) and update both numbers. **Do this last in
   the batch**, or re-verify the count right before editing — the company registry changes over
   time as sources are onboarded, so don't hardcode "65" without re-checking at fix time.

2. **`docs/SPEC.md` onboarding section is stale.** Line 804 still says onboard-source is
   `.claude/skills/onboard-source` — "separate from the four job-hunter skills in §11" — while line
   1124 of the same file *correctly* describes the current six-skill layout under `skills/` with
   `.claude/skills/onboard-source` as a symlink. Fix: update line ~804's "four" → "six" (or better,
   remove the hardcoded count and reference §11 by name so it can't drift again the same way), and
   confirm the `.claude/skills/` vs `skills/` path is consistent with line 1124's already-correct
   description.

3. **"Every `scripts/*.py` entry point accepts `--project`" overclaim.** README/SPEC/CLAUDE.md all
   make this claim in some form; the actual exceptions (confirmed in an earlier session, re-verify
   before fixing since new scripts may have been added since): `scripts/search_to_csv.py`,
   `scripts/endpoint_probe.py`, `scripts/prototype_tfidf_broad_match.py`, and any
   runtime/installer helper scripts that use their own positional arguments instead. Fix: either
   (a) add `--project` support to those scripts too (if they'd genuinely benefit — check whether
   they touch any project-relative path at all first; a pure stdin/stdout converter might not
   need it), or (b) rephrase the claim in all three docs to name the *operational* script set
   explicitly and call out the diagnostic/prototype exceptions, rather than claiming universality.
   **Pick one approach and apply it in all three documents consistently** — don't fix README
   without also fixing SPEC.md and CLAUDE.md, since the whole point of this finding is that they've
   drifted apart.

**Files:** `README.md`, `docs/SPEC.md`, `CLAUDE.md`.

**Tests:** none applicable (documentation-only), but consider whether item 11's CI work should
include an automated check for at least the company-count claim (e.g. a test that greps the
registry and compares to whatever number README asserts) to prevent this specific drift from
recurring — low priority, only worth it if item 11 is being done anyway.

---

## 10. Provenance fields for refiltered/stale-source-fallback output

**Priority: P2.** `scripts/refilter_archive.py`'s in-place rewrite and `render_radar.py`'s
stale-source-collection fallback (merging a failed source's last-known-good SQLite jobs into a
report) both make a report's contents depend on *when* and *against what SQLite state* it was
generated — not reproducible from the archive file alone. Neither currently records:
- `refiltered_at` (when the refilter ran),
- what SQLite/database snapshot state it queried against (there's no natural "snapshot ID" today,
  but at minimum the wall-clock time of the query),
- which sources' data in a given report came from the live archive vs. the stale-source fallback
  merge, and when that fallback data was last actually collected.

**Fix:** Add these as explicit fields on the relevant output structures:
- `refilter_archive.py`'s `--result-json` output (already exists — see the "structured stage
  results" work from a prior session) gains a `refiltered_at` timestamp field.
- `render_radar.py`'s per-source "Collection Issues" note (already prints "Failed to scrape
  today — showing N job(s) from the last successful scrape on `<date>`" per
  `skills/job-radar/SKILL.md`'s documented behavior) — confirm this date is already sourced from
  real data (`source_health`'s `last_success_at` or similar) and, if not already structured data
  (only prose text in the HTML), also expose it in the `--result-json` output for machine
  consumption, not just the human-readable report.
- `PipelineManifest` (in `no_scrape` mode) already has a `diff_report` path — consider whether
  `refiltered_at` belongs on the manifest too (likely yes, for consistency with how
  `profile_fingerprint`/`resume_fingerprint` already exist there for provenance-only purposes per
  `CLAUDE.md`'s documented philosophy — **do not** use these new fields for cache invalidation,
  same explicit rule that already applies to `profile_fingerprint`/`resume_fingerprint`).

**Files:** `scripts/refilter_archive.py`, `scripts/render_radar.py`, `src/job_hunter/models.py`
(`PipelineManifest` if extended), `src/job_hunter/pipeline.py` (if reading the new refilter result
field).

**Tests:** extend existing tests for these scripts' `--result-json` output to assert the new
field(s) are present and correctly populated.

---

## 11. Bootstrap command + CI workflow

**Priority: P2, largest scope — do last, and consider splitting into its own follow-up plan rather
than treating it as one item.** Per `docs/agent-runtime-audit.md`'s "Clone-anywhere assessment"
section, a fresh clone currently has no single command that: verifies Python/`uv`, runs `uv sync`,
creates `config/*.yaml` from the `.example.yaml` files, validates the result, and runs `doctor`.
There's also no CI workflow running `pytest`/`ruff`/`compileall`/the installer test suite/both
`--project` argument-order regression tests automatically on push/PR.

This is explicitly the largest, least-urgent item — **recommend treating it as its own separate
plan document** once items 1-10 are done, rather than scoping it fully here. At minimum, before
starting it, re-read `docs/agent-runtime-audit.md`'s "Clone-anywhere assessment" and "Recommended
bootstrap contract" sections (they list a fairly complete 6-step sequence and a set of "also add"
items — CI, platform matrix, license decision, ownership manifest for installs) and treat those as
the starting spec rather than re-deriving requirements from scratch.

---

## Definition of done for this plan

- All 11 checkboxes above are checked, each with its own tests passing and full-suite verification
  (`pytest`/`ruff`/`compileall`) clean after every item.
- Each closed item has a dated "Resolved" note added to the relevant finding in
  `docs/agent-runtime-audit.md` (per item 1 of "How to use this document" above).
- No item's fix broke a previously-passing test; the full suite's pass count only ever goes up
  across this plan's execution, never down.
- Item 5's Claude-hook-scope decision and item 9's `--project`-universality decision are each
  documented as an explicit choice (not left ambiguous) in the file(s) they affect.

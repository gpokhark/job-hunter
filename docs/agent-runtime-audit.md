# Agent Runtime Audit

**Audit date:** 2026-09-18  
**Repository:** `job-hunter`  
**Scope:** repository layout, skills, Claude Code and Hermes integration, hooks, scripts, pipeline, `SPEC.md`, `README.md`, `CLAUDE.md`, installer behavior, and clone portability.

## Executive verdict

The repository is in materially better shape than the previous audit. The main runtime boundaries are now explicit: project-root discovery is shared, the pipeline has a repository-wide lock, stages return structured JSON, run manifests record process identity, stale runs can be reported, and the six skills share versioned contracts. The current verification baseline is:

- `339 passed` with `uv run pytest -q`.
- `ruff check .` passes.
- `python -m compileall -q src scripts tests` passes.
- The CLI works from another working directory with both `--project /path` and `/path --project` forms, and with `JOB_HUNTER_ROOT`.
- `job-hunter doctor` passes repository/config/database/dependency checks. DNS probing fails in the restricted audit environment, so that result is environmental rather than a repository failure.

This is a strong development baseline, but it is not yet a fully portable, self-healing agent runtime. The highest-value remaining work is:

1. Make timeout behavior safe by default and terminate complete subprocess trees.
2. Make install/update/uninstall ownership-aware and make Hermes registration relocatable.
3. Remove stale examples and claims from the README, specification, skill instructions, and `CLAUDE.md`.
4. Replace the caller-controlled lock bypass with a private parent-run identity.
5. Add a first-class bootstrap/CI path for a genuinely fresh clone.

## Findings at a glance

| Area | Status | Assessment |
|---|---|---|
| Project-root discovery | Improved | Shared root resolver supports explicit path, environment variable, and repository discovery. Both CLI argument positions are tested. |
| Pipeline concurrency | Improved | Repository-wide lock and inherited-lock handling prevent normal overlapping runs. The inherited bypass still trusts an environment variable. |
| Pipeline result transport | Good | Operational stages use `--result-json`; the orchestrator no longer depends on human-readable stdout. |
| Timeout handling | Partial | Raw/decoded timeout output is normalized and tested, but the default is unlimited and descendant processes are not guaranteed to be terminated. |
| Run recovery | Partial | PID/status/abandoned-run reporting exists. Latest-run selection still relies too heavily on filesystem mtime and PID liveness. |
| Skills | Good | Six versioned, frontmatter-bearing skills have contracts and runtime-neutral project-root guidance. One radar example is stale. |
| Claude hooks | Partial | Shared adapter and error logging are good, but launcher and event coverage are narrow and there is no debounce/serialization policy. |
| Hermes hooks | Partial | Native stdin integration exists, but registration uses absolute paths and a fixed `python3` command. Relocation/update cleanup is incomplete. |
| Installer | Partial (was Risk) | File/symlink removal is now ownership-marker-guarded with `--force` override (resolved 2026-09-18, see Priority plan). Hermes registration relocation after a moved checkout is still open. |
| Documentation | Drift | README/SPEC/CLAUDE and the skill plan contain pre-implementation or contradictory claims. |
| Clone portability | Partial | Code is mostly path-independent, but bootstrap, dependency preflight, ownership metadata, CI, and supported-platform guarantees are incomplete. |

## What has been closed since the previous audit

The following earlier findings are now addressed in the current tree:

- Project-root arguments are accepted in both positions, and `JOB_HUNTER_ROOT` is supported.
- Root/config validation is centralized instead of duplicated across scripts.
- The pipeline acquires one shared `job-hunter` lock for the complete run; cleanup, review, and refilter reuse that lock when invoked as child stages.
- Lock metadata includes PID, command, and start information; stale lock reclaim has retry/empty-file protection.
- Stage output is transported through explicit result JSON files rather than regex parsing stdout.
- Run manifests include PID and the CLI can identify abandoned runs.
- Company-scoped archive resolution is available for `--companies` runs.
- Skills have been consolidated into six canonical directories under `skills/`, with frontmatter and contracts.
- Claude and Hermes profile hooks use the shared hook adapter, and hook failures are logged without failing the main workflow.
- Timeout-expiry output that arrives as raw bytes is decoded before being written to the manifest; regression coverage is present.

These fixes reduce operational ambiguity, but they do not remove the residual risks below.

## Skills audit

The canonical skill set is:

| Skill | Version | Assessment |
|---|---:|---|
| `job-hunter` | 1.2.0 | Good orchestration contract; should make timeout/lock failure states prominent in the caller-facing procedure. |
| `job-scout` | 1.1.0 | Good source/search boundary; should state exact result-file ownership and retry semantics. |
| `job-reviewer` | 1.1.0 | Good review boundary; cache invalidation and model/rubric provenance remain intentionally limited. |
| `job-radar` | 1.2.0 | Contract is mostly correct, but an example still advertises `render_radar.py --refilter`, which is not a current CLI option. |
| `job-feedback` | 1.1.0 | Good feedback loop; should expose the same explicit project-root and lock expectations as the operational scripts. |
| `onboard-source` | 1.0.0 | Canonical onboarding skill exists under `skills/`; older specification text still describes a separate `.claude/skills` location. |

The frontmatter is appropriate for local Claude Code and Hermes-style filesystem skills: each skill has a name, version, description, compatibility, metadata, and a contract. The repository should continue treating `skills/` as the source of truth and generate/adapt runtime-specific installations from it if a consumer requires another layout. Do not maintain independent copies unless a synchronization check is added.

For Claude Code, the skills should remain narrowly scoped and should not assume a particular current directory. For Hermes, a skill file is useful but not sufficient by itself: the agent also needs project context and a reliable tool/runtime installation. The existing root `CLAUDE.md` is the important shared project-context file; Hermes does not require a separate `HERMES.md`. Hermes supports context files and skills through its own runtime conventions. References:

- [Claude Code skills](https://code.claude.com/docs/en/skills)
- [Claude Code hooks](https://code.claude.com/docs/en/hooks)
- [Hermes context files](https://hermes-agent.nousresearch.com/docs/user-guide/features/context-files)
- [Hermes skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- [Hermes hooks](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks)

Recommended skill hardening:

- Add an automated check that frontmatter versions, skill directory names, and documented invocation examples agree.
- Add a skill-contract test that runs from outside the repository and verifies root discovery.
- Remove the obsolete radar `--refilter` example and document the supported `job-hunter pipeline --no-scrape` path.
- Include expected exit/result-file behavior for lock-held, timeout, abandoned, and partial-stage outcomes.
- Mark historical snapshots in `docs/skill-frontmatter-and-hook-plan.md` as historical, or split them from the current-state design.

## Hooks audit

### Shared hook adapter

The shared adapter is a good boundary: it centralizes profile generation, uses `shutil.which("uv")`, applies a bounded timeout, and records failures in `logs/profile-hook.log`. The main remaining concerns are:

- The Claude launcher calls `uv` before the adapter can handle a missing executable. A fresh machine without `uv` therefore gets a launcher-level failure.
- Claude registration currently focuses on `Edit|Write`. It does not establish policy for external file changes, Bash-created changes, or repeated rapid edits.
- There is no explicit debounce or per-project hook serialization. A batch edit can start overlapping profile updates and make the final result dependent on timing.
- Hook execution is best-effort, but the documentation should say when stale profile data is acceptable and how to manually rebuild it.

Recommended behavior is a small portable launcher that finds `uv`/Python, emits a clear diagnostic, and exits successfully when profile refresh is intentionally best-effort. Add debounce/lock behavior in the adapter, not separately in each runtime.

### Hermes registration

The Hermes hook reads native stdin and delegates to the shared adapter, which is the right design. The installation is not yet clone-portable:

- The registration embeds an absolute repository path and a fixed `python3` command.
- Moving or copying the checkout can leave an old registration active.
- `--update` refreshes the current registration but does not reliably remove registrations pointing to an old checkout.
- YAML rewriting is direct rather than an ownership-aware, atomic update with a backup/rollback path.

Use a repository-relative launcher where Hermes permits it; otherwise install a generated wrapper whose path is derived from the current checkout and record that registration in a manifest. On update, remove only entries owned by this repository and preserve unrelated user hooks.

## Scripts and pipeline audit

The operational scripts are substantially more consistent: they use shared root/config helpers, structured result files, and common locking. However, the README statement that every `scripts/*.py` entry point accepts `--project` is too broad. Some utilities are probes, converters, prototypes, or runtime helpers and do not use the operational CLI contract. Document the supported operational subset explicitly, or add a uniform wrapper for every user-facing script.

Remaining pipeline risks:

### P1: timeout is still opt-in

`PipelineConfig.stage_timeout_seconds` defaults to `None`, and the sample setting is commented out. A fresh clone can therefore wait indefinitely on a stuck search, review, or refilter subprocess. The byte-decoding bug is fixed, but safety is not complete.

Make a bounded, documented default part of the shipped configuration. Allow an explicit `0`/`null` only as a deliberate expert override. Use a process group/session so timeout cleanup terminates the complete child tree, not just the direct process, and add an integration test with a grandchild that must be gone after cancellation.

### P1: lock bypass is caller-controlled

`JOB_HUNTER_LOCK_INHERITED` is convenient for child stages but can be set by any standalone caller. A script launched with that variable can skip the repository lock. Replace the boolean bypass with a private parent-run token containing the lock identity, passed only to children and validated against the active lock metadata. Keep a clearly documented emergency/manual override separate from the normal path.

### P1: run identity and recovery are not fully authoritative

The manifest records PID and status, but PID reuse and filesystem mtime can make an old manifest appear current. Store a run UUID, monotonic/UTC start and end timestamps, parent run UUID, lock token, and process start identity where available. Select “latest” by recorded start time, not directory mtime, and distinguish “process exists” from “this exact run still owns the process.”

### P2: input validation and semantics

- Numeric options such as limits, maximum candidates, ages, and timeouts should reject negative values centrally.
- `--all-companies` is parsed, but the enabled-company default already covers the normal case; give it distinct documented semantics or remove it.
- Review caches intentionally key primarily on content hash. That is acceptable only if model, rubric, prompt, and parser versions are recorded and `--force` remains the escape hatch.
- Refiltering against current SQLite data and stale-source fallback can make an old report non-reproducible. Record source snapshots, query parameters, and fallback decisions in provenance.
- Storage migrations are additive but there is no explicit schema-version table/check. Add one before future destructive or semantic migrations.

## Documentation audit

### `README.md`

The README is useful but stale in several user-visible places:

- It reports 60 implemented / 63 total companies while the current doctor reports 65.
- It says every Python script accepts `--project`, although several utility scripts do not.
- It advertises `/job-radar --keyword ADAS --refilter`, while `render_radar.py` does not expose that option. The supported workflow is the pipeline refilter path.
- The onboarding wording still says “the other five” and should be phrased in terms of the current six-skill set.

These are easy to fix, but they directly affect first-run success and should be treated as release-blocking documentation drift.

### `docs/SPEC.md`

The specification has the correct newer six-skill model in later sections, but an earlier onboarding section still describes `.claude/skills/onboard-source` as canonical and calls the other skills a four-skill set. Make one canonical declaration near the top and reference it everywhere. The project-root convention is now documented well; extend the normative contract to cover:

- structured result-file schema and versioning;
- timeout defaults and process-tree cancellation;
- lock ownership/inheritance and abandoned-run transitions;
- exact supported script set and exit-code meanings;
- installer ownership and safe update/uninstall behavior.

### `CLAUDE.md`

The architecture guidance is generally accurate and helpful. The statement that `rootutil.py`, `atomic.py`, and `runlock.py` are used by every `scripts/*.py` entry point is too broad; qualify it to the operational scripts or make the utilities universal. Add a short “runtime contract” section linking to the current skill locations, supported pipeline commands, lock/timeout policy, and the canonical audit.

### Audit/planning documents

`docs/skill-frontmatter-and-hook-plan.md` correctly records implementation status, but its early confirmed-state sections describe an older repository snapshot. Label those sections historical or update them so future agents do not mistake design history for current behavior.

## Clone-anywhere assessment

The repository is mostly path-independent once invoked through the CLI, but a reliable fresh-clone experience needs a documented bootstrap sequence. A clone should not depend on the operator already knowing which directory is the project root, having a particular Python executable name, or manually creating runtime directories.

Recommended bootstrap contract:

1. Require a supported Python version and verify `uv` or provide a documented Python/pip fallback.
2. Install dependencies into an isolated environment.
3. Validate `config/settings.yaml`, create required data/log directories, and run migrations.
4. Install or refresh skills/hooks using an ownership manifest.
5. Run tests, lint, compile checks, and `job-hunter doctor`.
6. Print the exact configured project root, database path, enabled-company count, hook status, and any network checks skipped or failed.

Also add:

- a root `scripts/bootstrap.*` or `make setup` entry point;
- a minimal CI workflow for tests/lint/compile and a smoke CLI invocation;
- a supported OS/Python matrix;
- a root license file if this is intended for distribution;
- an ownership/version manifest for generated hook and skill installations;
- a copy/relocation test that installs from a temporary path and runs outside the checkout.

There is no need to add `AGENTS.md` or `HERMES.md` solely for the runtimes audited here. A root `CLAUDE.md` is the shared context file currently in use; add other context files only when a specific runtime requires different instructions and the synchronization policy is explicit.

## Priority plan

### P0 — before calling the runtime production-safe

- Set a safe default stage timeout and implement process-group cancellation.
- Replace unguarded installer deletion with ownership-marker validation, `--force` for exceptional removal, atomic updates, and rollback.
- Make Hermes registration relocatable or regenerate it safely on every invocation; remove only owned stale registrations.
- Correct the stale radar command and README/SPEC/CLAUDE claims that can cause a fresh user to run an invalid command.

**Resolved (2026-09-18) — installer ownership-marker validation and `--force`.**
`scripts/install_skill.sh`'s `install_one()` now writes a plain-text ownership marker
(`<destination's parent dir>/.job-hunter-installed`, one installed basename per line — no
JSON/YAML parser dependency, matching the script's existing POSIX-`sh`-only toolset) after every
real install/update, and both `--uninstall`'s and `--update`'s `rm -rf` calls are gated on
ownership: a marker entry, or (for a destination installed before the marker existed) the same
already-up-to-date/stale-symlink-by-basename signal the function already computed for its own
`OK`/`STALE` reporting — so an install already made by an older version of this same script (this
repo's own real, live `.claude/skills/*` symlinks, concretely — verified directly, not just via
fixtures) keeps working under `--update`/`--uninstall` with no forced one-time reclaim step. A
genuinely unowned destination (hand-placed content, another tool's directory at the same
conventional path) is refused with a clear message; new `--force` bypasses the check explicitly.
Caught and fixed one real bug during this work, live, before it shipped: the "already up to date"
branch's marker backfill was originally unconditional, so a *dry run* against this repo's own real
`.claude/skills/` actually wrote the marker file despite `--dry-run` promising to touch nothing —
confirmed by reproducing it live against the real checkout, fixed by gating the backfill on
`! dry_run`, and covered by a new regression test
(`test_dry_run_against_an_already_up_to_date_legacy_install_writes_nothing`) so a dry run staying
a true no-op is now enforced, not just assumed. 19 tests in `tests/test_install_skill.py` (was 11).
**Still open:** atomic config updates/rollback for the *Hermes registration* side specifically
(`install_hermes_hook.py`'s `config.yaml` rewrite already has its own separate backup-before-write,
untouched here) and the Hermes relocatable-registration finding below — this fix is scoped to
file/symlink ownership only, not registration-string portability.

**Resolved (2026-09-18) — safe default pipeline stage timeout + process-group cancellation.**
`config/settings.yaml` now ships `pipeline: {stage_timeout_seconds: 28800}` (8 hours, uncommented
— a fresh clone gets this safety net by default); `PipelineConfig`'s Python-level default stays
`None`/no-timeout, untouched, so an existing installation with the old commented-out block is
unaffected unless it re-copies the file (`tests/test_config.py` asserts the shipped value).
`_run_stage_subprocess` (`src/job_hunter/pipeline.py`) no longer calls `subprocess.run(...,
timeout=...)` — that wrapper's own internal timeout handling only ever kills the *direct* child
(`Popen.kill()`), even with `start_new_session=True` set, so a grandchild process (e.g. a future
adapter/script shelling out to something) could survive a timeout kill. It now calls `Popen`
directly with `start_new_session=True`, and on `TimeoutExpired` from the first
`communicate(timeout=...)`, kills the whole process group via `os.killpg(os.getpgid(proc.pid),
signal.SIGKILL)` (swallowing `OSError` — the group may have already exited in the gap between the
timeout firing and the kill call), then drains a final no-timeout `communicate()` for partial
output, exactly as `subprocess.run` used to do internally. Verified two ways: all 13 existing
mock-based `tests/test_pipeline.py` timeout/manifest tests were ported from mocking
`subprocess.run` to mocking `subprocess.Popen` (a new `_fake_popen()` adapter wraps each test's
existing `fake_run` closure unchanged, so no test assertions needed to change) and still pass; and
a new real-process-tree integration test,
`test_stage_timeout_kills_the_whole_process_group_including_grandchildren`, spawns an actual
grandchild subprocess via a fixture script (`tests/fixtures/scripts/spawn_grandchild_and_hang.py`)
and confirms live, via `os.kill(pid, 0)` polling, that the grandchild is actually gone after the
stage times out — the one thing a mocked `subprocess.Popen` replacement cannot prove, run and
confirmed passing directly (not just written). 345 tests passing (was 344 after re-verifying this
plan's stated 352-test baseline against the live tree — see the implementation session's own
notes for that discrepancy), `ruff`/`compileall` clean.

**Resolved (2026-09-18) — validated parent-run token replaces the boolean lock-inheritance
bypass.** `LOCK_INHERITED_ENV` (`src/job_hunter/runlock.py`) now carries a per-acquisition
`secrets.token_hex(16)` capability token, not a bare `"1"`. `run_lock()` generates a fresh token on
every acquisition and writes it as a 4th line in the lock file's own content (extending
`_read_holder`'s existing `(pid, command, started_at)` tuple with a 4th `token` field); a new
`current_lock_token(name, lock_dir=...)` re-reads that live file. `run_lock_or_inherited()` now
compares the env var's claimed token against `current_lock_token()`'s real, current value and
only treats the lock as inherited on an exact match — any mismatch, missing lock file, or unset
env var falls back to acquiring the lock normally (fails closed), so a standalone caller can no
longer skip the lock just by copy-pasting `JOB_HUNTER_LOCK_INHERITED=1` into their own shell.
`pipeline.py`'s `_run_stage_subprocess` no longer hardcodes `"1"` — it calls
`current_lock_token("job-hunter")` at the moment it spawns a refilter/review stage subprocess
(the parent's own `with run_lock("job-hunter"):` block is still open at that point, so the live
lock file is readable) and only sets the env var when a real token comes back, avoiding a new
parameter threaded through every intervening stage-sequencing function. Verified: `run_lock`'s
yielded value is unchanged (still a bare `Path`), so every existing `with run_lock(...):`/
`with run_lock_or_inherited(...):` call site needed no changes. New coverage in
`tests/test_runlock.py` (token present/changes-per-acquisition, matching-token inheritance is a
true no-op vs. a real second acquire seeing it held, mismatched/absent/unset-env each fall back to
a real acquire) and a new `tests/test_pipeline.py` test
(`test_lock_inherited_stages_receive_the_real_live_lock_token`) that reads the actual on-disk
`data/locks/job-hunter.lock` file mid-run and asserts the refilter/review stages' `env` dict
carries that exact token while the radar stage (never `lock_inherited`) gets no override at all.
353 tests passing, `ruff`/`compileall` clean.

**Resolved (2026-09-18) — authoritative run identity: started_at over mtime, process-identity over
bare PID liveness.** `pipeline.py`'s `latest_run_id()` no longer sorts `data/runs/*/manifest.json`
by filesystem mtime — it now parses each manifest and sorts by its own recorded `started_at`
(mtime only breaks a tie between two identical `started_at` values), so an imported/copied run
directory or a manifest written on a clock-drifted machine can no longer become "latest" purely by
having a newer mtime; a manifest that fails to parse at all is never preferred over one that does,
regardless of either timestamp. Separately, `PipelineManifest` gained `pid_start_time: str | None`,
populated from a new `runlock.process_start_time(pid)` helper (`ps -o lstart= -p <pid>`, exact
string only ever compared for equality, never parsed — no portable stdlib equivalent to Linux's
`/proc/<pid>/stat`, and this project's actual deployment context is single-operator machines, not
a scenario calling for a cross-platform process-identity library) at the moment `run_pipeline`
writes its first manifest. `cli.py`'s `pipeline-status` abandoned-detection now also compares that
recorded value against the pid's *current* start time (when both are available) — a mismatch means
the OS reused the pid number for an unrelated process, and the run is reported `abandoned` even
though `pid_alive()` alone would say "alive"; either value being unavailable (`None` — an older
manifest, or `ps` unavailable) means "can't verify" and falls back to the original PID-only
liveness check, never treated as evidence either way. One real bug caught fixing this: mocking
`subprocess.Popen` at the shared `subprocess` module level (item 1's approach) turned out to
globally break *any* other `subprocess.run`/`Popen` call made during the same test process, since
there's only one `sys.modules["subprocess"]` — `runlock.process_start_time`'s own `ps` shell-out
(now called from inside manifest construction) started raising `TypeError` under the existing
mocked pipeline tests until `pipeline.py` was changed to call a module-level `_popen = subprocess.
Popen` alias captured at import time instead, with tests patching that alias rather than the
shared module attribute — confirmed via a full mocked-test run failing 14/353, then passing again
after the fix. New coverage: `tests/test_pipeline.py` (`latest_run_id` prefers `started_at` over a
reversed mtime; an unparseable manifest never wins regardless of mtime), `tests/test_runlock.py`
(`process_start_time` stable across reads of the same live pid, `None` for a dead one), and
`tests/test_cli.py` (`pipeline-status` reports `abandoned` for a live pid whose recorded
`pid_start_time` doesn't match reality, and unchanged `running` behavior when no `pid_start_time`
was ever recorded). 359 tests passing, `ruff`/`compileall` clean.

### P1 — next reliability increment

- Replace the boolean inherited-lock environment variable with a validated parent-run token.
- Make run UUID/timestamps/process identity authoritative for latest-run and abandoned-run decisions.
- Add bootstrap and CI smoke paths, then test them from a temporary clone/outside working directory.
- Add hook debounce/serialization and explicit best-effort/staleness documentation.

### P2 — maintainability and reproducibility

- Centralize numeric argument validation.
- Decide and document `--all-companies` semantics.
- Add cache provenance and database schema versioning.
- Add an automated documentation/skill example consistency check.
- Add license, supported-platform, and generated-file ownership documentation.

## Definition of done for the next audit

The next audit should be able to demonstrate all of the following from a clean temporary clone:

- setup completes without hand-edited paths;
- tests, lint, compile, and doctor pass with network checks clearly classified;
- a hung descendant process is terminated by pipeline timeout;
- concurrent parent/child and independent runs have deterministic lock outcomes;
- moving the checkout does not leave stale Hermes or Claude registrations;
- uninstall refuses unowned files and removes only generated artifacts;
- every README/SPEC/CLAUDE/skill invocation example executes against the current CLI;
- a run can be resumed or diagnosed from its manifest without relying on filesystem mtime or PID reuse.


# Pipeline, refilter-only, and stale-source fallback — Plan

Status: **implemented and reviewed, 2026-09-18.** Drafted after an audit of the current codebase
(`docs/agent-runtime-audit.md`'s follow-on work) confirmed which of the three requested abilities
already existed, which were partial, and which didn't exist at all. Three design questions were
resolved with the user before the draft (section 3). Implementation was delegated to one agent,
independently reviewed and live-tested by a second, and the four problems that review surfaced
were then fixed directly (not by a third agent round) — see section 10 for the full account of
what was built, what the review found, and what was fixed.

## 1. Problem

The user wants three things from the `job-hunter` skill/CLI layer:

1. Run the full pipeline end to end — scrape through radar report — as one reliable operation.
2. Re-run filtering only (no new scraping) after editing `candidate_profile.yaml`, producing both
   an updated radar report and a diff report, with local-LLM review of the newly-surfaced
   candidates as an explicit opt-in (default: skip review).
3. When a source fails to scrape on a given day (e.g. Waymo), the radar report should still show
   that source's previously-collected jobs, with a note explaining the data is stale and when it
   was last collected successfully.

## 2. Confirmed current state

- **Ability 1 is already built.** `job-hunter pipeline` (added in the previous session) already
  sequences search → review → radar as one command with a durable
  `data/runs/<run_id>/manifest.json`. The only real gap is that `skills/job-hunter/SKILL.md` still
  documents the old three-manual-command sequence and doesn't know this command exists.
- **Ability 2 is partially built, but as a two-script manual chain, not one command.**
  `scripts/refilter_archive.py` already rebuilds `candidates` from SQLite (no network calls) against
  the current profile, and already writes its own gained/lost HTML diff report by default.
  `skills/job-radar/SKILL.md` step 3 already documents chaining it into `render_radar.py`
  afterward. `review_with_lm_studio.py` already skips any job with a still-valid cached assessment
  (matched by `content_hash`), so "only review the newly-surfaced candidates" is its natural
  behavior with zero new dedup logic needed — it just needs to be wired in as an optional step.
  There is no `--review`/`--no-review` toggle anywhere in this chain today, and no single command.
- **Ability 3 does not exist at all.** In `collector.py`'s `_collect_source`, a source whose fetch
  raises returns `(health, [])` — zero jobs enter that run's `candidates`, full stop. The source's
  previously-`active` SQLite rows are untouched but never resurface anywhere. `render_radar.py`
  already has a "Collection Issues" section (company + status + error message per failed source),
  but it has no "last successful scrape" date, because that value only lives in SQLite's
  `source_health.last_success_at` — it was never part of the per-run `SourceHealth` object that
  gets written into an archive, so nothing downstream of the archive can currently see it.
- **A real conflict surfaced during the audit:** `refilter_archive.py`'s own
  `_successful_source_scope()` *deliberately excludes* failed sources from its rebuilt candidate
  pool today, specifically so a source that failed *this run* can't resurface its old jobs as a
  false "Gained" entry in the diff report. That protection is correct and must stay — ability 3
  needs the opposite behavior in the *radar* report, not the diff report. These need to stay two
  different pools built for two different purposes, not one shared list.

## 3. Decisions already resolved with the user

1. **Stale-source signal is a source-level note in Collection Issues only** — no per-job-row
   "stale" badge. Simpler, and sufficient per the user.
2. **Stale fallback jobs are still subject to the normal recency filter**
   (`settings.search.max_posting_age_days`). A source down for a long time will eventually show
   zero fallback jobs (all filtered as stale) while still carrying the collection-issue note —
   confirmed as the intended, consistent behavior.
3. **Where the fallback logic lives:** in the *report-building* step, not the live collector
   (`collector.py` stays untouched). Rationale walked through with the user: the collector is the
   most central, most depended-on piece of the project, with a currently simple meaning ("jobs I
   actually fetched this run") — mixing in old jobs there would require new bookkeeping throughout
   (`is_new`/`is_changed`, `jobs_observed`) to avoid misrepresenting stale data as fresh. Doing it
   only where the report gets built is a much smaller, more contained change, and directly targets
   what was actually asked for (the report shows old data with a note).

## 4. Proposed design

### 4.1 Ability 1 — wire the skill to the already-built command

Rewrite `skills/job-hunter/SKILL.md`'s procedure to call `job-hunter pipeline` (and
`job-hunter pipeline-status` to report progress/outcome) instead of manually sequencing `search`,
`review_with_lm_studio.py`, and `render_radar.py`. `job-scout`/`job-reviewer`/`job-radar` stay
exactly as they are for single-stage use — they already say "for the full pipeline, use
`job-hunter` instead," so no change needed there beyond what ability 2 touches (below).

No code changes required for this part — it's a documentation/procedure rewrite only.

### 4.2 Ability 2 — `job-hunter pipeline --no-scrape [--review]`

**Extend the existing `pipeline` command rather than add a parallel one.** A new `--no-scrape` flag
skips the live search stage entirely and starts instead by re-running `scripts/refilter_archive.py`
against an already-resolved archive (same `--keyword`/`--search`-style resolution every other
stage uses) to rebuild `candidates` from SQLite's current active/eligible pool, against whatever's
currently in `candidate_profile.yaml`. This reuses ~90% of `run_pipeline`'s existing plumbing
(manifest writing, subprocess supervision, path pinning) instead of duplicating it in a second
command. `--companies` doesn't apply in this mode (refiltering re-evaluates an existing archive's
own source scope, not a fresh selection) — passing both is rejected with a clear error.

**Review defaults off in this mode, on an opt-in `--review` flag** — this is deliberately the
opposite default from normal full-pipeline mode (where review defaults on, opted out via the
existing `--skip-review`). Two flags with opposite-sense defaults depending on mode is a minor
UX wrinkle, called out here explicitly rather than hidden; the alternative (one `--review
{auto,always,never}`-style flag covering both modes) was considered and rejected as more
machinery than this codebase's existing plain-boolean-flag style (`--skip-review`, `--skip-radar`,
`--no-vacuum`) uses elsewhere — consistency with the rest of the CLI wins over a marginally
cleaner flag in one spot.

Sequence when `--no-scrape` is passed:

1. Resolve the archive (`--keyword`/`--search`, same rule as `review`/`radar` commands).
2. Run `refilter_archive.py` against it (subprocess, same pattern already used for review/radar) —
   rewrites the archive in place and writes its own gained/lost diff HTML report, unchanged
   behavior from today. Manifest records `archive`, `diff_report` (new field), `gained`, `lost`
   (new fields, parsed from `refilter_archive.py`'s own summary line, same technique already used
   for `review`'s "Reviewed N; skipped M" line).
3. If `--review` was passed: run `review_with_lm_studio.py` against the rewritten archive exactly
   like full-pipeline mode does — it naturally only calls the model for candidates without a valid
   cached assessment, so this correctly limits review to whatever the refilter actually surfaced
   as new/changed. If `--review` was not passed, this stage is skipped and the manifest's `stage`
   goes straight to `radar`.
4. Render the radar report (subprocess, path pinned to the exact archive from step 2 — same fix
   already made for full-pipeline mode).

New `PipelineStage.REFILTER` value (replaces `SEARCH` as the first stage in this mode).
`PipelineManifest` gains `diff_report: str | None`, `gained: int | None`, `lost: int | None`.

### 4.3 Ability 3 — stale-source fallback, in `render_radar.py` only

Single point of change: `render_radar.py`'s `build()` function, which is the one place both
ability 1 (live pipeline → radar) and ability 2 (refilter → radar) end up, so fixing it here covers
both paths without touching `collector.py`, `pipeline.py`'s live-search branch, or
`refilter_archive.py`'s own diff logic at all.

For each entry in the archive's `source_health` with `status == "failed"` (deliberately **not**
`warning` — a warning source did produce real live data this run, just fewer jobs than expected;
mixing in old jobs on top of a partial real result would be confusing, not helpful — and not
`unsupported`, which never has cached data to fall back to):

1. Look up that source's `last_success_at` from SQLite's `source_health` table (this is why
   `render_radar.py` needs a `Storage`/database-path dependency for the first time — see 4.4).
2. Pull that source's currently active, US-eligible, prefilter-passing, recency-passing jobs from
   SQLite (reusing the exact same query `refilter_archive.py`'s `_active_jobs()` already does,
   moved to a shared location — see 4.4), excluding any job ID already present in `candidates` (a
   `failed` status can still coexist with a job appearing in `candidates` in edge cases, e.g. a
   partially-succeeded detail fetch before the failure — dedupe defensively).
3. Merge the result into the candidate set used for rendering only — **the archive file on disk is
   never rewritten by this step.** The archive stays the true record of what was actually fetched
   that run; only the rendered HTML shows the merged, best-effort view. This also means re-running
   `render_radar.py` against the same archive is still idempotent and side-effect-free, consistent
   with how it works today.
4. Extend the existing Collection Issues row for that source with the note:
   `Failed to scrape today — showing N job(s) from the last successful scrape on <local date>.`
   If `last_success_at` is `None` (a source that has never once succeeded), the note instead reads
   `Failed to scrape — no prior successful data available for this source.` and no jobs are merged
   for it (there's nothing to merge).
5. Merged-in jobs are placed in the normal Strong/For-review/Below-50 sections using whatever
   `prior_assessment` they already carry from SQLite (exactly like any other candidate) — no
   separate section, no per-row badge, per the user's decision in section 3.

### 4.4 Shared helper: move `_active_jobs`-style logic into the installed package

`refilter_archive.py`'s `_active_jobs()`/`_successful_source_scope()` currently live in `scripts/`,
not the installed `job_hunter` package, so `render_radar.py` can't cleanly import them without a
scripts-importing-scripts chain (one already exists — `diff_profile.py` imports from
`render_radar.py` — so it's not unprecedented, but a source-scoped "give me this source's current
active/eligible/filtered jobs" query is generic enough to belong in the package proper, where it's
also directly unit-testable with the rest of `tests/`). Proposed: a new
`src/job_hunter/active_pool.py` with a `source_jobs(database_path, source_key, profile, max_age_days,
*, keywords=None, now=None) -> list[Job]` function, reapplying `passes_prefilter`/`passes_recency`
exactly like `_active_jobs()` does today. `refilter_archive.py` is refactored to call it instead of
its own private copy (behavior-preserving refactor, not a behavior change); `render_radar.py` calls
it for the stale-fallback merge.

### 4.5 `render_radar.py`'s new database dependency

`render_radar.py` is documented today as "pure presentation... never re-derives, adjusts, or
overrides a score" and currently touches only two JSON files, no database. This change is a
deliberate, disclosed exception to that, the same way `docs/SPEC.md` already documents other
deliberate exceptions (e.g. `stealth_html`) rather than pretending the rule has no exceptions —
it still never re-derives a *score*, it only reads already-decided job records for a report. A new
`--no-collection-fallback` flag disables the merge entirely (falls back to today's
note-with-no-jobs behavior), mirroring the escape-hatch style of `cleanup`'s `--no-vacuum`/
`--no-export` flags — default is fallback **on**.

## 5. Skill changes

- `skills/job-hunter/SKILL.md`: rewritten to call `job-hunter pipeline` / `pipeline-status`
  (ability 1), plus a new documented path for `--no-scrape [--review]` (ability 2) — "you edited
  the profile and want the report to reflect it without a new scrape."
- `skills/job-radar/SKILL.md`: step 3 (today's manual `refilter_archive.py` → `render_radar.py`
  chain) gets replaced with a pointer to `job-hunter pipeline --no-scrape`, and a new paragraph
  documenting the stale-source note so the agent knows to mention non-zero collection issues in
  its chat-facing summary, same as it already does for outright failures today.
- `skills/job-scout`/`job-reviewer`/`job-feedback`: core procedures unaffected by abilities 1–3 —
  `job-feedback`'s `diff_profile.py`-based preview-before-you-edit workflow stays a deliberately
  separate tool from ability 2's post-edit reporting (see rejected alternative 6.1) and needs no
  procedural change. All three do get the lighter pass from section 9 (`--project
  "$CLAUDE_PROJECT_DIR"` added to every shown command, plus 9.3's internal rationale trims) as part
  of this same round of edits, since section 9 was audited alongside abilities 1–3 and touches the
  same files.

## 6. Rejected alternatives

### 6.1 Building ability 2 on `diff_profile.py` instead of `refilter_archive.py`

`diff_profile.py` compares against a tracked baseline snapshot with an explicit accept/rollback
step — built for *previewing* a profile edit before committing to it. The user's ask ("if
candidate_profile is updated... generate the radar report") is phrased as *after* the edit already
happened — closer to `refilter_archive.py`'s "rebuild and show me the new picture" model, which
also directly produces something radar-renderable (`diff_profile.py` does not). Keeping these as
two separate tools for two different moments (before/after committing an edit) avoids collapsing
two genuinely different workflows into one.

### 6.2 Fallback logic in the live collector (Option A from the original audit question)

Rejected per the resolved decision in section 3 — highest blast radius for the actual ask, since it
touches the piece of code everything else depends on.

### 6.3 A separate `job-hunter refilter` command instead of `pipeline --no-scrape`

Considered, rejected as needless duplication — a parallel command would re-implement manifest
writing, path resolution, and subprocess supervision that `pipeline` already has, for a flow that's
90% the same as the existing one minus the search stage.

## 7. Testing plan

- Unit tests for `active_pool.source_jobs()` (moved logic — same coverage `refilter_archive.py`'s
  existing tests already have for `_active_jobs`, migrated).
- Unit tests for `render_radar.py`'s stale-fallback merge: a fixture archive with one `failed`
  source, SQLite seeded with that source's old active jobs (some within/some past the recency
  window) — assert only the recency-passing ones are merged, the note text is correct, and a
  `last_success_at IS NULL` source produces the no-data note with zero merged jobs.
- Unit tests for the new `pipeline --no-scrape`/`--review` manifest fields and stage transitions,
  same style as the existing `test_pipeline.py`.
- A live smoke test scoped narrowly (one already-failed or intentionally-misconfigured source, not
  a full 65-source crawl) before calling this done — flagging this up front so it isn't run without
  confirmation, per the lesson from the last live test in this project.

## 8. Open questions for the user

None remaining that block starting implementation — all three original design questions are
resolved in section 3. Flagging one small naming judgment call made unilaterally in 4.2 (the
opposite-default `--review` vs `--skip-review` flags) in case the user wants a different name or
mechanism before implementation starts.

## 9. Skill audit — simplification, token efficiency, and correctness

Requested separately from the three abilities above: audit all five `SKILL.md` files (plus their
two lazily-loaded reference docs) for redundancy, unnecessary token cost, and whether they invoke
the correct, current commands. This section is audit findings only — no skill files have been
edited yet; the rewrites in sections 4.1/4.2 above should incorporate these findings rather than
being done as a separate pass.

### 9.1 Why this matters beyond tidiness

A `SKILL.md`'s full body loads into the agent's context every time that skill fires — unlike
`CLAUDE.md`/`docs/SPEC.md`, which a developer reads occasionally, a skill's prose is a recurring,
per-invocation cost paid on every single job search session. Current sizes (word count, a rough
proxy for token cost):

| File | Words |
|---|---|
| `job-feedback/SKILL.md` | 1406 |
| `job-radar/SKILL.md` | 1163 |
| `job-hunter/SKILL.md` | 778 |
| `job-reviewer/SKILL.md` | 746 |
| `job-scout/SKILL.md` | 501 |
| `job-reviewer/references/scoring.md` | 169 |
| `job-scout/references/troubleshooting.md` | 251 |

The two reference docs are **not** part of this cost the same way: `scoring.md` is read by
`review_with_lm_studio.py` itself and sent to the *local* model as part of its own prompt — it
never enters Claude's context at all, so its size is irrelevant here by design. `troubleshooting.md`
is only pulled in conditionally ("see references/troubleshooting.md if a source is failing") —
already correctly deferred, not a cost on a normal run. Both are fine as-is.

### 9.2 Concrete cross-skill duplication found

1. **The "compile into Strong/For-review tiers, tag [90+]/[80+]/[New]" procedure is duplicated
   near-verbatim** — `job-hunter/SKILL.md` step 7 (`skills/job-hunter/SKILL.md:54`) and
   `job-radar/SKILL.md` step 5 (`skills/job-radar/SKILL.md:55`) are ~200 words of near-identical
   text. This is the single largest duplication in the whole skill set, and it's exactly what
   `docs/agent-runtime-audit.md`'s skill audit flagged generically ("the orchestrator duplicates
   commands from the stage skills") — here it's the same tier-compiling rules restated in full
   rather than the presentation logic itself being wasteful.
2. **The "never invent salary/sponsorship/arrangement/qualifications/dates" disclaimer** is
   restated in both `job-hunter/SKILL.md` step 9 (`:74`) and `job-radar/SKILL.md` step 10 (`:98`) —
   two near-identical sentences, smaller than #1 but the same pattern.
3. **`job-reviewer/SKILL.md` step 6** (`:66`, "Resume changes never affect this step's caching...")
   fully re-explains the assessment-cache-key principle that is now *also* stated as a first-class
   "working principle" in `CLAUDE.md`'s "Working in this repo" section (added this session,
   `CLAUDE.md:503`) — every runtime that loads `CLAUDE.md` as project instructions already has this
   stated once; the skill restating it in full is now genuinely redundant, not just similar.
4. **Archive-resolution rationale** is explained at full length in `job-reviewer/SKILL.md` step 2
   (`:32-41`); `job-radar/SKILL.md`'s version of the same rule (step 2) is already a much terser
   citation ("the same resolution rule as `job-reviewer`... see `docs/skill-split-plan.md` section
   4") — a good example of the target shape, just not applied consistently. `job-reviewer`'s own
   copy is the one that should shrink to match `job-radar`'s style, not the other way around.

### 9.3 Per-skill internal verbosity (not cross-file, just within one file)

- `job-scout/SKILL.md` step 3 re-explains the `--archive` filename convention
  ("same keyword on the same day overwrites... a new day or a different keyword always gets its
  own file...") at a depth that duplicates `search_archive.py`'s own docstring almost exactly —
  the agent needs the one-line operational rule, not the full rationale restated; the code comment
  is the canonical source of the "why."
- `job-feedback/SKILL.md` step 3's six-bucket breakdown (`:52-66`) carries a paragraph of rationale
  per bucket, much of which duplicates `docs/feedback-exclusion-plan.md`, already cited at the end
  of the same step. The buckets themselves (what to relay, in what order) need to stay explicit —
  agents should not skip an empty bucket — but the *why* behind each can shrink to a phrase with
  the existing citation carrying the depth.

### 9.4 Correctness gap: none of the five skills use `--project`

Every skill's step 1 is some version of "work from the project directory containing
`pyproject.toml`" — every shown command is a bare `uv run job-hunter ...`/`uv run python
scripts/...` with no `--project` flag. This is exactly the fragility
`docs/agent-runtime-audit.md` flagged and that this project already fixed at the CLI layer
(`--project`/`JOB_HUNTER_ROOT`, this session) — but the fix was never wired into the one place that
actually issues these commands. The audit's own recommendation
("The agent skills should pass `--project "$CLAUDE_PROJECT_DIR"` for Claude and the active
workspace path for Hermes") was never applied. Every command shown in every skill should gain
`--project "$CLAUDE_PROJECT_DIR"` (Claude Code) with the Hermes-equivalent noted alongside, in
place of relying on "already `cd`'d into the repo."

### 9.5 Recommended approach

- **Deduplicate, don't just trim.** For 9.2's items 1–2, keep one canonical copy (in
  `job-radar/SKILL.md`, since tiering/tagging/disclaiming is that skill's actual subject) and have
  `job-hunter/SKILL.md`'s rewritten procedure (already being rewritten per section 4.1) cite it by
  step number instead of restating it — the same citation pattern `job-hunter` already correctly
  uses for the search and review steps today (e.g. "fully resumable — see `job-reviewer`'s
  `SKILL.md` for why"), just not yet applied to the compile/disclaim steps. No new shared reference
  file needed; a same-repo skill can already cite another skill's file/section directly, which is
  the established pattern here — inventing a third file to hold the shared block was considered and
  rejected as one more file to keep in sync for no real benefit over citing the existing home.
- **Cut restated rationale, keep the citation.** For 9.2 item 3 and 9.3, replace the restated "why"
  with the one-line operational rule plus the existing citation (`CLAUDE.md`, `docs/
  feedback-exclusion-plan.md`, `search_archive.py`'s own docstring) — the depth still exists, just
  not paid for twice.
- **Add `--project "$CLAUDE_PROJECT_DIR"` to every shown command, in all five skills.**
- Net effect: `job-hunter`/`job-radar` shrink the most (the ~200-word tier-compile block collapses
  to a one-line citation in whichever file loses it; `job-reviewer`/`job-scout`/`job-feedback` see
  smaller trims from rationale-cutting). Rough target: 30–40% off `job-hunter`/`job-radar`'s current
  size, 10–15% off the other three, with zero loss of operational instruction — everything cut is
  restated-elsewhere rationale, not a rule the agent needs to act correctly.
- This dedup/trim pass happens **as part of** the section 4.1 (`job-hunter`) and 4.2/section 5
  (`job-radar`) rewrites already planned for abilities 1–2, not as a separate fourth pass — those
  files are being rewritten anyway to call `job-hunter pipeline`/`pipeline --no-scrape`, so this is
  the natural moment. `job-scout`, `job-reviewer`, and `job-feedback` get a lighter, standalone pass
  (9.3's internal trims plus the `--project` fix from 9.4) since their core procedures aren't
  otherwise changing.

## 10. Implementation, review, and fixes (2026-09-18)

Built by one agent, independently reviewed and live-tested by a second (not the same agent —
deliberately, so the review couldn't just repeat the implementer's own assumptions), following
sections 4–9 above essentially as written. The reviewer confirmed correct: the `active_pool.py`
split (section 4.4), the `render_radar.py` stale-fallback merge including the `failed`-only
scoping/recency-filtering/exact note text/never-rewrites-the-archive properties (section 4.3), the
`pipeline --no-scrape`/`--review` flow and new manifest fields (section 4.2), `--no-scrape` +
`--companies` rejection, `collector.py` genuinely untouched, no live crawl run, no commit created,
and that `job-hunter/SKILL.md` no longer restates `job-radar`'s tier-compile block (section 9.5's
dedup actually happened, not just claimed).

The review also found four real problems, all fixed directly afterward (not via a third agent
round — the fixes were specific enough to apply directly):

1. **Critical, undisclosed: `--project` didn't actually work where every rewritten skill showed
   it.** `add_project_argument` was only ever called on the root argparse parser, never on any
   subparser — `job-hunter <command> --project X` (the exact order all 8 affected example
   commands across 4 skill files used) failed with "unrecognized arguments," while only
   `job-hunter --project X <command>` worked. Fixed by calling `add_project_argument` on every
   subparser too (`cli.py`'s `parser()`, iterating `sub.choices.values()`) — argparse's own
   default-handling means a value set by the root parser is never clobbered by a subparser's
   unset default, so both flag positions now work with no namespace-merge special-casing needed.
   Verified live in both positions after the fix.
2. **Performance: the stale-fallback path did a full, unfiltered scan of the `jobs` table (the
   one CLAUDE.md itself documents as 230MB, 98.5% `description` text) per failed source**, in
   Python, discarding everything but one source's rows — `active_pool.raw_active_jobs()` now
   pushes `source_scope` into the SQL `WHERE ... AND source_key IN (...)` clause instead (with an
   empty-scope short-circuit before ever touching SQLite, since a "restrict to nothing" scope is
   a real, valid case per `refilter_archive.py`'s `_successful_source_scope`). Also fixed a
   smaller instance of the same pattern in `render_radar.py`'s fallback: one `Storage` connection
   and one `health_rows()` read for the whole call, not one per failed source.
3. **A disclosed "trim" (section 9.3's `job-feedback` bucket rationale) had barely happened** —
   diffed old vs. new and confirmed the review's finding: mostly single-clause drops, not the
   promised shrink to a phrase-plus-citation. Trimmed further for real this time (each bucket is
   now a short operational phrase; the citation to `docs/feedback-exclusion-plan.md`, which the
   first pass did add, carries the rationale).
4. **An orphaned manifest on archive-resolution failure in `--no-scrape` mode.** The manifest was
   written as `RUNNING` before `resolve_search_path()` ran; if that raised `FileNotFoundError`
   (no archive matches the given keyword — the exact case the review's own live test exercised),
   the exception propagated straight past every other finalize-the-manifest code path in the
   function, leaving it stuck at `RUNNING` forever — `pipeline-status` would report an instantly-
   failed run as still in progress. Fixed by wrapping `run_pipeline`'s body (now split into a
   `_run_pipeline_body` helper) in a try/except that finalizes the manifest as `FAILED` with the
   real exception message on *any* exception the body doesn't already handle itself, then
   re-raises — `cli.py`'s existing top-level handler still prints the same clean error/exit code
   as before, only the manifest file's own content changed. Verified live (reproduced the exact
   failure, confirmed `pipeline-status` now reports `failed` with a real error message instead of
   an eternally-running phantom run) and covered by a new regression test in `test_pipeline.py`.

Post-fix: `uv run pytest -q -m "not live"` → 277 passed (276 from the implementer's round plus one
new regression test for problem 4); `uv run ruff check .` → clean. No live crawl was run to verify
any of this beyond the single-company/no-archive-write commands already described above; the fixes
were verified via the existing fixture-based suite plus targeted live CLI invocations that don't
touch `data/searches/default_*.json`/`data/radar/default_*.html`. `src/job_hunter/collector.py`
remains untouched. No git commit has been created — everything above is sitting in the working
tree for the user's own review before committing.

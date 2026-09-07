# Retention / cleanup mechanism — Plan

Status: **implemented 2026-09-07** (`src/job_hunter/cleanup.py`, `Storage.find_stale_closed_jobs`/
`delete_closed_jobs`/`vacuum`, `job-hunter cleanup`). See section 6 for the resolved decisions and
what actually shipped, following review of this plan.

## 1. Problem

`data/jobs.sqlite3` and the report directories under `data/` never delete anything today (verified:
no `DELETE FROM` anywhere in `storage.py` or any script). Confirmed live on 2026-09-07: the DB is
230 MB after 9 days of use, with the `jobs` table alone at 227 MB (98.5%) across 31,965 rows
(27,145 `active` / 4,820 `closed`) — dominated by the `description` column, which is never trimmed
even for a job closed weeks ago. `data/profile-diff/` (28 files, 2.2 MB) and `data/radar/` (9
files, 2.5 MB) grow more slowly but also never shrink. `runs` (90 rows) grows one row per search
forever but is trivial in size (16 KB).

The user's request: delete a `jobs` row once it's been `closed` for more than N days (proposed
10), and delete `profile-diff`/`radar` HTML reports older than M days (proposed 15) — except keep
the latest 2 "default" runs regardless of age — with N/M/2 all configurable. Asked explicitly for
a critique, not just an implementation of the literal request.

## 2. What's right about the request, confirmed against the actual code

- **Deleting old `closed` jobs is safe for every current tool.** `diff_profile.py`'s
  `_read_only_jobs()` and `refilter_archive.py`'s `_active_jobs()` both filter
  `WHERE status='active' AND us_eligible=1` — a `closed` row is *already* invisible to both,
  whether or not it still physically exists in the table. Deleting it changes nothing about their
  live output today.
- **No new column is needed to know "how long has this job been closed."** `mark_missing()`
  (`storage.py`) only flips `status='closed'` — it never touches `last_seen_at`. Since
  `last_seen_at` is only updated while a job is being actively re-observed, it's already exactly
  "the last time this job was confirmed present," which is the right anchor for "days since
  closed" with zero migration.
- **No conflict with `stale_before`/early-pagination-stop accounting.** That mechanism (§6/§12,
  `docs/SPEC.md`) exists to stop `mark_missing` from *wrongly* closing a job a source's
  early-pagination-stop optimization simply stopped looking at. It only matters for jobs still
  `status='active'`. Once a job has already reached `status='closed'`, that determination is final
  and this cleanup can't re-trigger it.
- **`data/searches/` archives are correctly left out of scope.** They're documented as
  deliberately permanent (`search_archive.py`/CLAUDE.md: "an earlier run's exact candidate
  snapshot survives... stays reachable later"), and deleting one would break
  `resolve_search_path`/`--keyword` resolution for any future `job-reviewer`/`job-radar`/
  `refilter_archive.py` call against it. Good instinct not to include them.

## 3. What's missing or needs a different design

### 3.1 Wrong file for the settings — `candidate_profile.yaml` is the wrong home

`CandidateProfile` (`config.py`) models exactly one thing: resume-matching terms (`target_domains`,
`exclude_terms`, `soft_exclude_terms`, ..., `resume_path`, `minimum_recommendation_score`). It's
personal, gitignored, and — per `CLAUDE.md`'s own stated config split — is not where system/
storage housekeeping knobs live. `Settings` (`config/settings.yaml`, `SearchConfig`,
`CollectionConfig`) is that place; it already holds the closest analog, `search.max_posting_age_days`.
Putting a `closed_job_retention_days` next to `soft_exclude_terms` would mix "how I want jobs
scored" with "how the database prunes itself" in a file whose only documented purpose is the
former. **Recommendation: a new `retention:` section in `config/settings.yaml`**, not
`candidate_profile.yaml`.

### 3.2 `DELETE` alone will not shrink the file — VACUUM is required

This is the part most likely to be missed: SQLite's `DELETE` frees pages *inside* the file for
reuse by future inserts, but does not shrink the file on disk. Given the entire stated goal is
disk growth, a cleanup command that only runs `DELETE` and never `VACUUM` would satisfy "closed
jobs disappear from queries" while doing **nothing** to the 230 MB file size — the freed space
just gets silently reused as new jobs come in, and the file keeps growing at roughly the same
rate. `VACUUM` rewrites the whole file and needs to be a deliberate, separate step (it takes an
exclusive lock; on a database this size that's currently sub-second, but worth calling out for
if/when the DB grows much larger).

### 3.3 Deletion needs an explicit, dry-run-capable trigger — not silent, not automatic

The request doesn't say *when* this runs. Given `DELETE` is irreversible and this project's
consistent pattern elsewhere (`diff_profile.py`'s baseline-accept confirmation, `job-feedback`'s
two explicit stop-and-ask points, never auto-committing or auto-pushing) is to make a
destructive/state-changing action an explicit, visible step — not a side effect of something else
— this should be a **new, separate CLI subcommand** (`job-hunter cleanup`), never wired into the
normal `search` path or a hook. It should default to (or require an explicit flag for) a
**dry-run** that reports exactly what *would* be deleted — counts, and ideally a sample of
company/title — before anything is actually removed. A second, explicit flag commits it.

### 3.4 The "keep latest 2" exception, as literally stated, has three problems

1. **It only protects the literal `"default"` slug.** If reports are mostly keyword-scoped (e.g.
   you mostly run `/job-radar --keyword ADAS`), every one of those is a flat 15-day cutoff with no
   floor at all — your only ADAS reports could all disappear, while two default-search reports
   nobody's looking at are kept. **Suggested fix: keep the latest N per resolved slug** (whatever
   the archive/report's own slug is — `default`, `adas`, etc.), not hardcoded to the string
   `"default"`. If you specifically only care about protecting profile-driven (no-keyword) runs,
   say so and I'll keep it literal — but I'd guess the per-slug version is what you actually want.
2. **It doesn't obviously apply to plain `diff_profile.py` reports at all.** Those live at
   `data/profile-diff/{timestamp}.html` with no slug, no "default" vs. keyword concept — they're
   one global timeline of "here's how the profile itself has changed." The "default" framing
   doesn't map onto them. **Suggested fix:** for this file shape specifically, "keep latest N"
   just means the N most recently generated, full stop — no slug grouping needed since there isn't
   one.
3. **`data/radar/` and `data/profile-diff/` grow at very different rates**, which changes how much
   the "keep latest N" floor actually matters:
   - `render_radar.py` writes to `data/radar/{search-stem}.html` and **overwrites in place** —
     there is naturally at most one radar file per slug per day, re-running just updates it. Over
     9 days of real use this produced only 9 files total.
   - `diff_profile.py`/`refilter_archive.py` write a **new, uniquely-timestamped** file on every
     single invocation — 28 files in the same 9 days, and this will keep outpacing radar's growth
     the more you iterate on profile terms.

   Practically: radar's "keep latest N" floor rarely bites (few files exist per slug to begin
   with); profile-diff's floor is the one that actually matters day-to-day.

### 3.5 A naive age-based glob is unsafe — it could delete a file you deliberately kept

`data/profile-diff/` already contains at least one file that doesn't match either generated
pattern: `soft_exclude_terms_removed_2026-09-05.html` — a descriptively-named file from earlier
work, not one of the two auto-generated shapes. A blind "anything `.html` older than 15 days"
sweep would delete it too. **The cleanup must match only the exact generated filename patterns**
and leave anything else alone, however old:
- `diff_profile.py`: `YYYY-MM-DD-T-HH-MM-SS.html` (current format) — plus the older
  `YYYYMMDDTHHMMSS.html` shape still sitting on disk from before this session's timestamp-format
  change, which should also be recognized (or migrated/renamed once, your call).
- `refilter_archive.py`: `archive-{search_stem}-YYYY-MM-DD-T-HH-MM-SS.html` (+ the same older
  variant).
- `render_radar.py`: `{search_stem}.html` (no timestamp of its own — its "age" for this purpose is
  the file's mtime, or equivalently the date embedded in `search_stem`).

### 3.6 Deleting a `jobs` row: cascade to `assessments`/`job_feedback`, or leave them?

Neither table has a real foreign key to `jobs` (none is declared, and SQLite doesn't enforce one
here by default), so nothing *breaks* either way. But there's a real choice:
- **Leave them (recommended).** They're tiny (`assessments`: 1.3 MB/553 rows; `job_feedback`:
  20 KB/99 rows) — deleting them buys effectively no disk space back. They're also valuable on
  their own: if a deleted job's requisition ID is later reused when a company reposts the same
  role, the assessment cache (keyed on `content_hash`, independent of the `jobs` row) still works
  correctly — deleting `assessments` early would just force a wasted re-review. `job_feedback` in
  particular feeds `suggest_exclusions.py`'s term-suggestion corpus; deleting old labels shrinks
  that signal for no real benefit.
- Cascade-deleting them is possible but I don't see a reason to — flagging only because it's a
  choice, not an oversight if left out.

### 3.7 One real, disclosed side effect of deleting `jobs` rows

If a `jobs` row is deleted and that exact posting later reopens (same `source_key`/`job_id` —
common; many ATS platforms keep a requisition ID stable across a repost), `upsert_job()` will see
no prior row (`get_job()` returns `None`) and treat it as `is_new=True` — losing the fact this is
actually a returning posting (its original `first_seen_at` is gone). This is a minor cosmetic
accuracy loss in `is_new`/`is_changed` reporting, not a functional break (the assessment cache
still works independently, per 3.6) — worth knowing, not worth blocking on.

### 3.8 Optional extra safety: export before delete

Not required, but worth considering given this is genuinely irreversible: the cleanup command
could write whatever it's about to delete to a JSON file first (mirroring the existing
`export`/`export-assessments`/`export-feedback` commands' shape) before running `DELETE`+`VACUUM`.
Cheap insurance, easy to skip with a flag if you don't want it. Your call — listed as an open
question below, not assumed.

## 4. Proposed design

### 4.1 `config/settings.yaml` — new `retention` section

```yaml
retention:
  closed_job_after_days: 10       # delete a `jobs` row this long after last_seen_at, once status='closed'
  report_after_days: 15           # delete a generated profile-diff/radar report this long after generation
  keep_latest_reports_per_slug: 2 # never delete the N most recent reports of a given slug, regardless of age
```

Pydantic model (sibling to `SearchConfig`):

```python
class RetentionConfig(BaseModel):
    closed_job_after_days: int = Field(10, ge=1)
    report_after_days: int = Field(15, ge=1)
    keep_latest_reports_per_slug: int = Field(2, ge=0)
```

Added to `Settings` as `retention: RetentionConfig = RetentionConfig()` — same pattern as every
other section, so an old `settings.yaml` with no `retention:` key still validates fine with these
defaults.

### 4.2 New CLI subcommand: `job-hunter cleanup`

```bash
uv run job-hunter cleanup                 # dry run (default) — reports what would be deleted, deletes nothing
uv run job-hunter cleanup --apply         # actually deletes, then VACUUMs
uv run job-hunter cleanup --apply --no-vacuum   # delete without vacuuming (e.g. to batch several cleanups before one vacuum)
uv run job-hunter cleanup --jobs-only     # only the SQLite side
uv run job-hunter cleanup --reports-only  # only data/profile-diff/ + data/radar/
```

Algorithm:
1. **Jobs**: `SELECT source_key, job_id, company, title FROM jobs WHERE status='closed' AND
   last_seen_at < ?` (cutoff = now − `closed_job_after_days`). Dry run prints the count and a
   handful of examples; `--apply` deletes those rows, `assessments`/`job_feedback` untouched
   (3.6), then `VACUUM` unless `--no-vacuum`.
2. **Reports**: for each of `data/profile-diff/` and `data/radar/`, list files matching only the
   known generated patterns (3.5), group by resolved slug (radar: from the filename itself;
   profile-diff `archive-*`: from the embedded `search_stem`; plain `diff_profile.py` reports: one
   group, no slug), sort each group newest-first, keep the first
   `keep_latest_reports_per_slug` unconditionally, and delete the rest only if older than
   `report_after_days`. Dry run lists exactly which files; `--apply` deletes them.
3. Print a final summary either way: rows/files that qualify, rows/files actually removed (only
   under `--apply`), and (under `--apply`) the DB file size before/after `VACUUM`.

### 4.3 Testing plan

- Pydantic validation: `RetentionConfig` bounds, defaults when `retention:` key is absent.
- `storage.py`: a helper (e.g. `Storage.delete_closed_jobs(before: datetime) -> int`) tested
  against an in-memory/tmp DB — closed-and-old rows removed, closed-but-recent and active rows
  untouched, `assessments`/`job_feedback` rows for a deleted job survive.
- A report-scanning helper tested against a tmp directory with a mix of: old/new timestamp
  formats, an `archive-*` file, a plain diff file, a radar file, and a deliberately-unrelated
  `.html` file (the "don't touch a hand-named file" case from 3.5) — asserting only the intended
  files are selected.
- `cli.py`: dry-run prints counts and deletes nothing (assert row/file counts unchanged);
  `--apply` actually removes them; `--jobs-only`/`--reports-only` scope correctly.

## 5. Open questions for you before I implement anything

1. **Per-slug "keep latest N" vs. literal `"default"` only** — 3.4.1. I'd default to per-slug
   unless you specifically only want `default` runs protected.
2. **Does `keep_latest_reports_per_slug` apply to plain `diff_profile.py` reports too** (as "latest
   N overall, no slug"), per 3.4.2 — or should those be handled some other way?
3. **Export-before-delete** (3.8) — want it, or skip it?
4. **Cascade-delete `assessments`/`job_feedback` for a deleted job, or leave them** (3.6) — I'm
   recommending leave, but it's your data.
5. **Should `job-hunter cleanup` default to dry-run** (safest, my recommendation) **or default to
   applying** (more convenient, riskier)? I'd default to dry-run and require `--apply` to commit.
6. Any interest in the old (pre-EST-fix) timestamp-format files in `data/profile-diff/` being
   recognized/cleaned by the same patterns (3.5), or left alone entirely since they're a small,
   fixed, non-growing set?

## 6. Resolved decisions (2026-09-07) and what shipped

All six open questions resolved in favor of the suggested improvements over the literal original
request:

1. **Per-slug "keep latest N"**, not literal `"default"` — confirmed.
2. **Applies to plain `diff_profile.py` reports too**, as one ungrouped series (group key
   `"(profile-diff)"`) — confirmed.
3. **Export before delete** — confirmed. Implemented as a JSON file written to
   `data/cleanup-exports/{UTC-timestamp}.json` immediately before the actual `DELETE`/`unlink`
   calls, containing the full deleted job/assessment/job_feedback rows and the deleted report
   paths. Only written when something was actually deleted (`--apply` and a non-empty result) —
   no empty export files on a no-op run.
4. **Cascade-delete `assessments`/`job_feedback`** for a deleted job — confirmed, implemented in
   `Storage.delete_closed_jobs`.
5. **Dry-run by default, `--apply` to commit; parameters in `settings.yaml`** — confirmed. New
   `RetentionConfig` (`closed_job_after_days: 10`, `report_after_days: 15`,
   `keep_latest_reports_per_slug: 2`) added to `Settings`, written into `config/settings.yaml`
   with the same defaults, matching the existing `search:`/`collection:` section pattern
   (backward-compatible: an old `settings.yaml` with no `retention:` key still validates against
   these defaults — confirmed by test).
6. **Both old and new timestamp formats count as generated patterns** — confirmed (the user's
   phrasing: "match only the exact generated filename patterns... leave anything else alone,
   however old" was read as including the pre-EST-fix format, since it's genuinely
   script-generated, not hand-named). Both `YYYY-MM-DD-T-HH-MM-SS.html` and the older
   `YYYYMMDDTHHMMSS.html` shape are recognized by `classify_report`.

**What shipped, concretely:**

- `RetentionConfig` in `config.py`, wired into `Settings.retention`.
- `Storage.find_stale_closed_jobs(before)` — read-only, used for both the dry-run preview and
  immediately before `delete_closed_jobs` to know exactly what's about to be deleted (`last_seen_at`
  reused as the "days since closed" anchor, exactly as section 2 anticipated — no migration).
- `Storage.delete_closed_jobs(before)` — deletes qualifying `jobs` rows plus their `assessments`/
  `job_feedback` rows, returning exactly what was removed from each table (for the export).
- `Storage.vacuum()` — a thin `VACUUM` wrapper, called after a job deletion unless `--no-vacuum`.
- `src/job_hunter/cleanup.py` — `classify_report`/`scan_reports`/`select_reports_to_delete`
  (the report-file half, parameterized by directory for testability) and `run_cleanup` (the single
  entry point orchestrating both halves, returning a `CleanupResult` the CLI prints).
- `job-hunter cleanup` subcommand: `--apply` (default off), `--no-vacuum`, `--jobs-only`/
  `--reports-only` (mutually exclusive), `--no-export`.
- Tests: `tests/test_config.py` (bounds/defaults), `tests/test_storage.py` (cascade delete, vacuum,
  eligibility filtering), `tests/test_cleanup.py` (pattern-matching safety — including a regression
  test built directly from the live `soft_exclude_terms_removed_2026-09-05.html` case — grouping,
  keep-latest-floor interaction with the age cutoff, and end-to-end dry-run/apply), `tests/test_cli.py`
  (dry-run-by-default, mutually-exclusive scope flags). 213 tests pass; ruff clean.
- Live-verified in dry-run only (never `--apply`, per this being a real, irreversible operation on
  the user's own data): `job-hunter cleanup` against the actual `data/jobs.sqlite3`/
  `data/profile-diff`/`data/radar` correctly reports 0 eligible jobs and 0 eligible reports — the
  project is only ~9 days old, younger than either default cutoff (10/15 days), so this is the
  expected "nothing to clean yet" result, not a bug. Separately confirmed via a direct
  `scan_reports()` call that the real `soft_exclude_terms_removed_2026-09-05.html` (and another
  hand-named file, `default_2026-08-31_original-116.html`, found live) are both correctly excluded
  from the 35 real files that *do* match a generated pattern.

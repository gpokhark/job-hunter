"""SQLite's currently-active, US-eligible job pool, scoped and filtered for reuse by more than
one caller — lifted out of `scripts/refilter_archive.py`'s originally-private `_active_jobs()`
into the installed `job_hunter` package (see `docs/pipeline-refilter-stale-source-plan.md`
section 4.4) once a *second* caller needed the identical query/filtering logic.

That second caller is `scripts/render_radar.py`'s stale-source-collection fallback (plan section
4.3): when a source fails to scrape on a given run, the radar report still shows that source's
last-known-good jobs rather than silently showing zero, and answering "what does this one failed
source's current picture look like" is exactly the same question `refilter_archive.py` already
asks across every source in an archive's scope, just narrowed to one `source_key` at a time. A
scripts-importing-scripts chain already exists for this kind of reuse in this codebase (
`scripts/diff_profile.py` imports directly from `scripts/render_radar.py`), so reaching into
`refilter_archive.py`'s private internals from `render_radar.py` wouldn't have been unprecedented
— but a query this generic (not tied to either script's own report-rendering concerns) belongs in
`job_hunter` proper instead, where it's directly unit-testable alongside everything else in
`tests/` rather than only reachable by the `sys.path.insert(...)` every `scripts/*.py` test file
already has to do to import its script under test.

Two functions are exported, matching the two different things a caller of the old `_active_jobs()`
actually needed from it:

- `raw_active_jobs()` — every active/US-eligible job in SQLite, optionally scoped to a set of
  source keys, with **no** prefilter/recency applied yet. `refilter_archive.py`'s own `refilter()`
  needs exactly this shape (unfiltered) because it has to count *why* a job didn't survive
  (`stale_excluded` specifically means "failed recency", not "failed prefilter for some other
  reason") — a fully-filtered single-source list can't reconstruct that per-reason breakdown once
  the excluded jobs are already gone from it.
- `source_jobs()` — one source's current active/US-eligible/prefilter-passing/recency-passing
  jobs, built on top of `raw_active_jobs()`. This is the one `render_radar.py`'s stale-source
  fallback actually calls: it never needs `refilter_archive.py`'s per-reason exclusion count, only
  "what would show up for this one source today," so the fully-filtered shape is the right one for
  it to depend on directly instead of re-deriving the same two checks (`passes_prefilter`,
  `passes_recency`) itself a third time.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .config import CandidateProfile
from .models import Job
from .prefilter import passes_prefilter, passes_recency
from .storage import Storage

# jobs.canonical_url -> Job.url is the one required rename; every other column already lines up
# with a Job field by name. Identical allowlist to scripts/diff_profile.py's own `_JOB_COLUMNS`
# (duplicated there, not imported from here) — this project's existing convention for these
# small SQLite-row-shaping helpers is per-module self-containment (see CLAUDE.md's
# scripts/refilter_archive.py section), which this module doesn't change; it only relocates
# refilter_archive.py's own copy so a second, non-script caller can reach it without importing a
# sibling script.
_JOB_COLUMNS = (
    "source_key", "company", "job_id", "source_platform", "title",
    "location_raw", "city", "state", "country", "work_arrangement",
    "us_eligible", "location_confidence", "location_evidence",
    "visa_sponsorship", "sponsorship_evidence", "department",
    "employment_type", "posted_at", "description", "salary_min",
    "salary_max", "salary_currency", "salary_evidence", "content_hash", "first_seen_at",
    "last_seen_at",
)


def _row_to_job(row: sqlite3.Row) -> Job:
    data = {column: row[column] for column in _JOB_COLUMNS}
    data["url"] = row["canonical_url"]
    return Job(**data)


def raw_active_jobs(database_path: Path, source_scope: set[str] | None = None) -> list[Job]:
    """Every currently-active, US-eligible job in SQLite, optionally restricted to a source-key
    scope (`None` means every source currently sitting in the database, not just one company) —
    no prefilter or recency applied. Attaches `prior_assessment` exactly like a live search's
    `collector.py` does, and exactly like the old `_active_jobs()` always has: only when the
    stored assessment's own `content_hash` still matches the job's *current* one, so a job whose
    posting changed since it was last reviewed is treated as unassessed again rather than served
    a stale verdict.

    `source_scope`, when given, is pushed into the SQL `WHERE` clause rather than fetched-then-
    filtered-in-Python — `render_radar.py`'s stale-source fallback calls this once per `failed`
    source (`source_jobs()` below), and this table is the one CLAUDE.md itself documents as
    230MB, 98.5% of it `jobs.description` text; pulling every active row across every source into
    Python only to discard all but one source's worth per call would mean N full-table-plus-
    description scans for N failed sources in a single render, for no reason a `WHERE
    source_key IN (...)` can't avoid outright. An empty (but non-`None`) `source_scope` is a
    genuine "restrict to nothing" (see `refilter_archive.py`'s `_successful_source_scope`
    docstring — every attempted source having failed is a known scope of zero, not an unknown
    one) and short-circuits before touching SQLite at all, since an empty SQL `IN ()` is invalid
    syntax, not merely slow."""
    if source_scope is not None and not source_scope:
        return []
    with Storage(database_path) as storage:
        if source_scope is None:
            rows = storage.connection.execute(
                "SELECT * FROM jobs WHERE status='active' AND us_eligible=1"
            ).fetchall()
        else:
            placeholders = ",".join("?" for _ in source_scope)
            rows = storage.connection.execute(
                "SELECT * FROM jobs WHERE status='active' AND us_eligible=1 "
                f"AND source_key IN ({placeholders})",
                tuple(source_scope),
            ).fetchall()
        assessments = storage.all_assessments()
    jobs = []
    for row in rows:
        job = _row_to_job(row)
        prior = assessments.get((job.source_key, job.job_id))
        if prior and prior.content_hash == job.content_hash:
            job.prior_assessment = prior
        jobs.append(job)
    return jobs


def source_jobs(
    database_path: Path,
    source_key: str,
    profile: CandidateProfile,
    max_age_days: int,
    *,
    keywords: list[str] | None = None,
    now: datetime | None = None,
) -> list[Job]:
    """One source's current active, US-eligible, prefilter-passing, recency-passing jobs — the
    exact pool `render_radar.py`'s stale-source-collection fallback needs to answer "what does
    this one failed source's last-known-good picture look like today" (see
    `docs/pipeline-refilter-stale-source-plan.md` section 4.3), without pulling in every other
    source's jobs the way `raw_active_jobs()`'s unscoped/multi-source shape would. Applies the
    same two checks `scripts/refilter_archive.py`'s own `refilter()` already applies to its
    (multi-source) pool: `passes_prefilter` against the *current* `profile` (or a `keywords`
    override, meaning exactly what it means everywhere else in this project — a full replacement
    of the profile's own positive-match terms, not a narrowing of them), then `passes_recency`
    against `max_age_days`. A source down long enough will eventually show zero fallback jobs
    here (every one of them aged past the recency cutoff) while the caller can still report the
    source itself as failed — confirmed as the intended, consistent behavior, not a bug to guard
    against (see the plan's section 3, decision 2)."""
    now = now or datetime.now(UTC)
    jobs = raw_active_jobs(database_path, {source_key})
    kept = []
    for job in jobs:
        if not passes_prefilter(job, profile, keywords=keywords):
            continue
        if not passes_recency(job, max_age_days, now=now):
            continue
        kept.append(job)
    return kept

#!/usr/bin/env python3
"""Re-apply the *current* candidate_profile.yaml's prefilter/recency logic to an already-collected
search archive — no network, no adapter/scraper invoked. Useful right after adding, removing, or
loosening a filtering term: see its effect on a report you already have, without waiting for (or
risking rate limits from) a fresh live search.

Rebuilds the archive's `candidates` list from scratch out of SQLite's current active/US-eligible
job pool — scoped to the same sources this archive's own `source_health` originally attempted —
rather than narrowing whatever's already sitting in `candidates`. That narrowing-in-place design
was tried first and found to be a one-way ratchet: once a `soft_exclude_terms` edit dropped a job
from `candidates`, that job's data was gone from the archive file, so a *later* loosening edit
meant to rescue it (a new `strong_relevance_terms` override, a removed `soft_exclude_terms` entry)
had nothing left in the file to restore — the rescue silently did nothing. Concrete case that
surfaced this: an Apple "Custom Silicon Validation Engineer - Camera Hardware" posting dropped by
adding `silicon` to `soft_exclude_terms`, meant to be rescued by later adding `camera` to
`strong_relevance_terms` — the rescue had zero effect until the posting was manually pulled back
out of SQLite. Every observed job is persisted there regardless of prefilter outcome
(`collector.py`'s `storage.upsert_job()` runs before prefilter is ever applied), so rebuilding from
it is always possible without a live search — this is the same "stored, `us_eligible`,
recency-passing job" universe `scripts/diff_profile.py` already previews against, using the same
`passes_prefilter`/`passes_recency`.

Restricting to the archive's own `source_health` source set (rather than every source currently in
`companies.yaml`) matters for a dated, non-default archive: onboarding a new company later should
never cause an old keyword archive to silently gain that company's jobs on a re-filter — it was
never part of what that archive searched. A `--search`/`--keyword`-resolved archive with no
`source_health` (an unexpected/older file shape) falls back to no source restriction rather than
raising, since failing shouldn't be the behavior for a merely-missing optional field.

One real behavior change from the old narrowing design: because candidates are rebuilt from
SQLite's *current* state, a job's title/description/location here reflects the latest observed
content for it (across any run, not just this archive's original collection), not a frozen
snapshot from when this archive was first written — matching this tool's existing recency
handling, which already recomputes against wall-clock *now* rather than assuming nothing aged
since collection.

Usage:
    uv run python scripts/refilter_archive.py
    uv run python scripts/refilter_archive.py --keyword ADAS
    uv run python scripts/refilter_archive.py --search data/searches/default_2026-09-05.json --output /tmp/refiltered.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from job_hunter.config import load_profile, load_settings
from job_hunter.models import Job
from job_hunter.prefilter import passes_prefilter, passes_recency
from job_hunter.search_archive import resolve_search_path
from job_hunter.storage import Storage

# jobs.canonical_url -> Job.url is the one required rename; every other column already lines up
# with a Job field by name. Same allowlist as scripts/diff_profile.py's _row_to_job — duplicated
# rather than shared, matching this project's existing per-script self-containment for these
# support scripts.
_JOB_COLUMNS = (
    "source_key", "company", "job_id", "source_platform", "title",
    "location_raw", "city", "state", "country", "work_arrangement",
    "us_eligible", "location_confidence", "location_evidence",
    "visa_sponsorship", "sponsorship_evidence", "department",
    "employment_type", "posted_at", "description", "salary_min",
    "salary_max", "salary_currency", "content_hash", "first_seen_at",
    "last_seen_at",
)


def _row_to_job(row: sqlite3.Row) -> Job:
    data = {column: row[column] for column in _JOB_COLUMNS}
    data["url"] = row["canonical_url"]
    return Job(**data)


def _active_jobs(database_path: Path, source_scope: set[str] | None) -> list[Job]:
    """Every currently-active, US-eligible job in SQLite, optionally restricted to a source-key
    scope. Attaches `prior_assessment` exactly like `collector.py` does for a live search, so a
    rebuilt candidate carries the same fields a fresh search would have produced."""
    with Storage(database_path) as storage:
        rows = storage.connection.execute(
            "SELECT * FROM jobs WHERE status='active' AND us_eligible=1"
        ).fetchall()
        assessments = storage.all_assessments()
    jobs = []
    for row in rows:
        if source_scope is not None and row["source_key"] not in source_scope:
            continue
        job = _row_to_job(row)
        prior = assessments.get((job.source_key, job.job_id))
        if prior and prior.content_hash == job.content_hash:
            job.prior_assessment = prior
        jobs.append(job)
    return jobs


def refilter(
    data: dict[str, Any],
    *,
    keywords: list[str] | None = None,
    now: datetime | None = None,
    database_path: Path | str | None = None,
) -> dict[str, Any]:
    """Returns a new archive dict — `candidates` rebuilt from SQLite's current active/eligible
    job pool (scoped to the sources this archive's own `source_health` originally attempted),
    filtered by the current on-disk profile, summary counts updated to match. Does not mutate the
    input dict."""
    now = now or datetime.now(UTC)
    profile = load_profile()
    settings = load_settings()
    max_age_days = settings.search.max_posting_age_days
    resolved_db_path = Path(database_path) if database_path is not None else settings.database_path

    source_scope = {row["source_key"] for row in data.get("source_health", [])} or None
    jobs = _active_jobs(resolved_db_path, source_scope)

    kept: list[Job] = []
    stale_excluded = 0
    for job in jobs:
        if not passes_prefilter(job, profile, keywords=keywords):
            continue
        if not passes_recency(job, max_age_days, now=now):
            stale_excluded += 1
            continue
        kept.append(job)
    kept.sort(key=lambda job: (-(job.posted_at.timestamp() if job.posted_at else 0), job.title))

    new_data = dict(data)
    new_data["candidates"] = [json.loads(job.model_dump_json()) for job in kept]
    new_summary = dict(data.get("summary", {}))
    new_summary["prefilter_candidates"] = len(kept)
    new_summary["stale_excluded"] = stale_excluded
    new_data["summary"] = new_summary
    return new_data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--search", type=Path, default=None, help="archive to re-filter (default: newest overall)")
    parser.add_argument("--keyword", default=None, help="resolve --search by keyword, and use as the positive-match override (same as job-hunter search --keyword)")
    parser.add_argument("--output", type=Path, default=None, help="write here instead of overwriting the input archive in place")
    args = parser.parse_args()

    search_path = resolve_search_path(search=args.search, keyword=args.keyword)
    keywords = [term.strip() for term in args.keyword.split(",") if term.strip()] if args.keyword else None

    data = json.loads(search_path.read_text(encoding="utf-8"))
    before_keys = {(c["source_key"], c["job_id"]) for c in data["candidates"]}
    before_count = len(data["candidates"])
    new_data = refilter(data, keywords=keywords)
    after_keys = {(c["source_key"], c["job_id"]) for c in new_data["candidates"]}
    after_count = len(new_data["candidates"])
    removed = len(before_keys - after_keys)
    gained = len(after_keys - before_keys)

    output_path = args.output or search_path
    output_path.write_text(json.dumps(new_data, indent=2, default=str, ensure_ascii=False) + "\n", encoding="utf-8")

    print(
        f"Re-filtered {search_path} -> {output_path}: {after_count} candidate(s) "
        f"(was {before_count}, {removed} removed, {gained} gained)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

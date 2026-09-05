#!/usr/bin/env python3
"""Re-apply the *current* candidate_profile.yaml's prefilter/recency logic to an already-collected
search archive — no network, no adapter/scraper invoked. Useful right after adding or removing an
exclude term: see its effect on a report you already have, without waiting for (or risking rate
limits from) a fresh live search.

Rewrites the archive's `candidates` list and `summary.prefilter_candidates`/`stale_excluded` to
match what the current profile would produce from the same already-collected postings — every
other field (`jobs_observed`, `source_health`, `run` metadata) is left untouched, since those
describe the collection itself, not filtering, and nothing here re-collects anything.

Recency is recomputed against *now*, not assumed unchanged from collection time — if enough real
time has passed since the archive was written, a job that was fresh then may have aged out since,
independent of any profile edit.

Usage:
    uv run python scripts/refilter_archive.py
    uv run python scripts/refilter_archive.py --keyword ADAS
    uv run python scripts/refilter_archive.py --search data/searches/default_2026-09-05.json --output /tmp/refiltered.json
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from job_hunter.config import load_profile, load_settings
from job_hunter.models import Job
from job_hunter.prefilter import passes_prefilter, passes_recency
from job_hunter.search_archive import resolve_search_path


def refilter(
    data: dict[str, Any], *, keywords: list[str] | None = None, now: datetime | None = None
) -> dict[str, Any]:
    """Returns a new archive dict — candidates re-filtered by the current on-disk profile,
    summary counts updated to match. Does not mutate the input dict."""
    now = now or datetime.now(UTC)
    profile = load_profile()
    settings = load_settings()
    max_age_days = settings.search.max_posting_age_days

    kept: list[dict[str, Any]] = []
    stale_excluded = 0
    for candidate in data["candidates"]:
        job = Job(**{k: v for k, v in candidate.items() if k != "raw"})
        if not passes_prefilter(job, profile, keywords=keywords):
            continue
        if not passes_recency(job, max_age_days, now=now):
            stale_excluded += 1
            continue
        kept.append(candidate)

    new_data = dict(data)
    new_data["candidates"] = kept
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
    before_count = len(data["candidates"])
    new_data = refilter(data, keywords=keywords)
    after_count = len(new_data["candidates"])

    output_path = args.output or search_path
    output_path.write_text(json.dumps(new_data, indent=2, default=str, ensure_ascii=False) + "\n", encoding="utf-8")

    print(
        f"Re-filtered {search_path} -> {output_path}: {after_count} candidate(s) "
        f"(was {before_count}, {before_count - after_count} removed by the current profile/recency)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

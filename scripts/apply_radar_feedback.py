#!/usr/bin/env python3
"""Ingest a radar-report feedback export (see docs/feedback-exclusion-plan.md) into the
`job_feedback` SQLite table. Upserts by (source_key, job_id) — a later label for the same job
replaces the earlier one rather than creating a second, contradictory row, so relabeling a job
between sessions (e.g. "okay" reconsidered as "irrelevant") is handled correctly. A job omitted
from the export file is never touched: its prior label (if any) survives untouched, and a job
that was never tagged at all never gets a row created for it either.

Refreshes `data/job_feedback.csv` afterward for human browsing, same pattern as
`assessments_to_csv.py`.

With no `--file`, auto-resolves the newest `radar-feedback-*.json` in `--downloads-dir` (default
`~/Downloads`) — the exact filename the radar report's export button already produces (see
`scripts/templates/radar_template.html`). Always prints which file it picked and when it was
last modified, so an auto-pick is never silently the wrong one. If none is found, this exits 0
having done nothing — safe to call unconditionally (e.g. from a skill) without first checking
whether a fresh export actually exists. An explicit `--file` always wins over auto-resolution.

Usage:
    uv run python scripts/apply_radar_feedback.py
    uv run python scripts/apply_radar_feedback.py --file ~/Downloads/radar-feedback-default_2026-09-05.json
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

from job_hunter.config import load_settings
from job_hunter.models import FeedbackLabel, JobFeedback
from job_hunter.rootutil import add_project_argument, chdir_to_project_root
from job_hunter.storage import Storage

_VALID_LABELS = set(get_args(FeedbackLabel))

_CSV_COLUMNS = ["recorded_at", "label", "score", "company", "title", "department", "source_key", "job_id"]

_DEFAULT_DOWNLOADS_DIR = Path.home() / "Downloads"


def resolve_feedback_file(explicit: Path | None, downloads_dir: Path) -> Path | None:
    """An explicit --file always wins, unresolved. Otherwise, the newest
    radar-feedback-*.json in downloads_dir by mtime, or None if there isn't one — the caller
    decides what "nothing to ingest" means for it, this never raises for a missing file."""
    if explicit is not None:
        return explicit
    matches = list(downloads_dir.glob("radar-feedback-*.json")) if downloads_dir.exists() else []
    return max(matches, key=lambda p: p.stat().st_mtime) if matches else None


def _write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in _CSV_COLUMNS})


def export_event_time(path: Path) -> datetime:
    """The export file's mtime as a UTC-aware event time. The static export's entries carry no
    timestamp of their own, so this is the best available "when did this person last click"
    bound: an export can never contain a click newer than the moment it was written. Used so an
    old ~/Downloads file (auto-picked when no --file is given) cannot overwrite a newer live
    relabel or resurrect an untagged job — see docs/live-radar-dashboard-plan.md section 3.4."""
    return datetime.fromtimestamp(path.stat().st_mtime, UTC)


def ingest(
    storage: Storage, payload: list[dict[str, Any]], *, event_at: datetime | None = None
) -> dict[str, int]:
    """Applies every valid entry in payload through `Storage.apply_feedback` at `event_at`
    (the export's mtime when called from `main`). A job omitted from payload is never touched.
    An entry older than the stored label/tombstone is skipped: counted `unchanged` when its
    label equals the stored one, otherwise under a `stale` key that only appears when nonzero.
    `event_at=None` (direct callers/tests) keeps the historical always-apply behavior, stamped
    with the current time. Returns counts: new, changed, unchanged, invalid[, stale]."""
    prior_by_key = {
        (row["source_key"], row["job_id"]): row["label"] for row in storage.export_job_feedback()
    }
    counts = {"new": 0, "changed": 0, "unchanged": 0, "invalid": 0}
    effective_event_at = event_at or datetime.now(UTC)
    for entry in payload:
        label = entry.get("label")
        if label not in _VALID_LABELS:
            print(
                f"job-hunter: skipping {entry.get('source_key')}/{entry.get('job_id')} — "
                f"invalid label {label!r} (expected one of {sorted(_VALID_LABELS)})",
            )
            counts["invalid"] += 1
            continue
        feedback = JobFeedback(
            source_key=entry["source_key"],
            job_id=entry["job_id"],
            company=entry["company"],
            title=entry["title"],
            department=entry.get("department"),
            score=entry.get("score"),
            label=label,
        )
        key = (feedback.source_key, feedback.job_id)
        outcome = storage.apply_feedback(
            feedback, event_at=effective_event_at, force=event_at is None
        )
        if outcome == "stale":
            if prior_by_key.get(key) == label:
                counts["unchanged"] += 1
            else:
                counts["stale"] = counts.get("stale", 0) + 1
            continue
        if key not in prior_by_key:
            counts["new"] += 1
        elif prior_by_key[key] != label:
            counts["changed"] += 1
        else:
            counts["unchanged"] += 1
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--file", type=Path, default=None,
        help="the exported radar-feedback JSON file; if omitted, auto-resolves the newest "
        "radar-feedback-*.json in --downloads-dir",
    )
    parser.add_argument(
        "--downloads-dir", type=Path, default=_DEFAULT_DOWNLOADS_DIR,
        help=f"where to look for an un-specified --file (default: {_DEFAULT_DOWNLOADS_DIR})",
    )
    add_project_argument(parser)
    args = parser.parse_args()
    chdir_to_project_root(args.project)

    resolved = resolve_feedback_file(args.file, args.downloads_dir)
    if resolved is None:
        print(
            f"No radar-feedback-*.json found in {args.downloads_dir} and no --file given — "
            "nothing to ingest."
        )
        return 0
    if not resolved.exists():
        print(f"job-hunter: {resolved} does not exist", file=sys.stderr)
        return 2

    mtime = datetime.fromtimestamp(resolved.stat().st_mtime).isoformat(timespec="seconds")
    print(f"Using {resolved} (last modified {mtime})")

    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        print(f"job-hunter: {resolved} must contain a JSON array of feedback entries", file=sys.stderr)
        return 2

    settings = load_settings()
    with Storage(settings.database_path) as storage:
        counts = ingest(storage, payload, event_at=export_event_time(resolved))
        rows = storage.export_job_feedback()

    csv_path = settings.database_path.parent / "job_feedback.csv"
    _write_csv(rows, csv_path)

    print(
        f"Ingested {len(payload)} feedback entr{'y' if len(payload) == 1 else 'ies'}: "
        f"{counts['new']} new, {counts['changed']} label-changed, {counts['unchanged']} unchanged"
        + (f", {counts['invalid']} invalid" if counts["invalid"] else "")
        + (f", {counts['stale']} stale-skipped (older than a live change)" if counts.get("stale") else "")
        + f". Wrote {csv_path}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

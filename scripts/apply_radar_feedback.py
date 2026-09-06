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
from datetime import datetime
from pathlib import Path
from typing import Any

from job_hunter.config import load_settings
from job_hunter.models import JobFeedback
from job_hunter.storage import Storage

_VALID_LABELS = {"relevant", "okay", "irrelevant"}

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


def ingest(storage: Storage, payload: list[dict[str, Any]]) -> dict[str, int]:
    """Upserts every valid entry in payload into job_feedback. A job omitted from payload is
    never touched — its prior row (if any) survives exactly as it was; a job that never had a
    row at all still doesn't get one. Returns counts: new, changed, unchanged, invalid."""
    prior_by_key = {
        (row["source_key"], row["job_id"]): row["label"] for row in storage.export_job_feedback()
    }
    counts = {"new": 0, "changed": 0, "unchanged": 0, "invalid": 0}
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
        if key not in prior_by_key:
            counts["new"] += 1
        elif prior_by_key[key] != label:
            counts["changed"] += 1
        else:
            counts["unchanged"] += 1
        storage.upsert_job_feedback(feedback)
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
    args = parser.parse_args()

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
        counts = ingest(storage, payload)
        rows = storage.export_job_feedback()

    csv_path = settings.database_path.parent / "job_feedback.csv"
    _write_csv(rows, csv_path)

    print(
        f"Ingested {len(payload)} feedback entr{'y' if len(payload) == 1 else 'ies'}: "
        f"{counts['new']} new, {counts['changed']} label-changed, {counts['unchanged']} unchanged"
        + (f", {counts['invalid']} invalid" if counts["invalid"] else "")
        + f". Wrote {csv_path}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Convert a `job-hunter search --json` output file into a CSV for quick human review
(filtering/sorting in Excel, Numbers, or Google Sheets) — no extra dependency beyond the
stdlib `csv` module, since CSV opens directly in all of them with full sort/filter support."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

_COLUMNS = [
    "company",
    "title",
    "posted_at",
    "location_raw",
    "city",
    "state",
    "country",
    "work_arrangement",
    "us_eligible",
    "location_confidence",
    "employment_type",
    "department",
    "source_key",
    "job_id",
    "url",
    "location_evidence",
    "description",
]


def _row(candidate: dict[str, Any]) -> dict[str, Any]:
    return {column: candidate.get(column, "") for column in _COLUMNS}


def convert(input_path: Path, output_path: Path) -> int:
    data = json.loads(input_path.read_text(encoding="utf-8"))
    candidates = data.get("candidates", [])
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_COLUMNS)
        writer.writeheader()
        for candidate in candidates:
            writer.writerow(_row(candidate))
    return len(candidates)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path, default=Path("data/latest_search.json"), help="search JSON to read"
    )
    parser.add_argument(
        "--output", type=Path, default=None, help="CSV path (default: input path with .csv)"
    )
    args = parser.parse_args()
    output_path = args.output or args.input.with_suffix(".csv")
    count = convert(args.input, output_path)
    print(f"Wrote {count} candidates to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

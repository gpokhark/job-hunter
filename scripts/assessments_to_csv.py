#!/usr/bin/env python3
"""Convert `data/assessments.json` (written by `job-hunter record-assessment`/
`export-assessments`) into a CSV for quick human review — no extra dependency beyond the
stdlib `csv` module, since CSV opens directly in Excel/Numbers/Sheets with full sort/filter
support."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

_COLUMNS = [
    "assessed_at",
    "recommended",
    "score",
    "company",
    "title",
    "matches",
    "gaps",
    "source_key",
    "job_id",
    "url",
    "resume_path",
]


def _row(assessment: dict[str, Any]) -> dict[str, Any]:
    row = {column: assessment.get(column, "") for column in _COLUMNS}
    row["matches"] = "; ".join(assessment.get("matches") or [])
    row["gaps"] = "; ".join(assessment.get("gaps") or [])
    return row


def convert(input_path: Path, output_path: Path) -> int:
    assessments = json.loads(input_path.read_text(encoding="utf-8"))
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_COLUMNS)
        writer.writeheader()
        for assessment in assessments:
            writer.writerow(_row(assessment))
    return len(assessments)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path, default=Path("data/assessments.json"), help="assessments JSON to read"
    )
    parser.add_argument(
        "--output", type=Path, default=None, help="CSV path (default: input path with .csv)"
    )
    args = parser.parse_args()
    output_path = args.output or args.input.with_suffix(".csv")
    count = convert(args.input, output_path)
    print(f"Wrote {count} assessments to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

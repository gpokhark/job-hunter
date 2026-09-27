#!/usr/bin/env python3
"""Append one row to data/output/resume_log.csv after a resume is generated.

Usage:
    uv run python scripts/log_resume.py --file Doe_CV_Acme_ADAS_2026-09-26.html --company Acme \\
        --role "ADAS Engineer" --url https://... --fill 93.8 --pages 1 --iterations 2 --date 2026-09-26

`--fill` is the *last page's* fill percentage from measure_resume.py (`last_page_fill_pct`).
Cells that look like spreadsheet formulas are prefixed with ' so the CSV is safe to open in Excel.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from job_hunter.applications_export import csv_safe
from job_hunter.rootutil import add_project_argument, chdir_to_project_root

HEADERS = ["Date", "Company", "Role", "Job_URL", "Fill_Pct", "Pages", "Iterations", "Resume_File"]
LOG_PATH = Path("data/output/resume_log.csv")


def append_row(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        if write_header:
            writer.writerow(HEADERS)
        writer.writerow([csv_safe(row.get(column, "")) for column in HEADERS])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", required=True)
    parser.add_argument("--company", required=True)
    parser.add_argument("--role", default="Not specified")
    parser.add_argument("--url", default="Not specified")
    parser.add_argument("--fill", default="?")
    parser.add_argument("--pages", default="?")
    parser.add_argument("--iterations", default="?")
    parser.add_argument("--date", required=True)
    add_project_argument(parser)
    args = parser.parse_args()
    chdir_to_project_root(args.project)
    append_row(LOG_PATH, {
        "Date": args.date, "Company": args.company, "Role": args.role, "Job_URL": args.url,
        "Fill_Pct": args.fill, "Pages": args.pages, "Iterations": args.iterations, "Resume_File": args.file,
    })
    print(f"Logged: {args.company} | {args.role} | {args.fill}% | {args.iterations} iteration(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

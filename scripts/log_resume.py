#!/usr/bin/env python3
"""Append one row to data/output/resume_log.csv after a resume is generated.

Usage:
    uv run python scripts/log_resume.py --file Doe_CV_Acme_ADAS_2026-09-26.html \\
        --jd data/output/Acme/JD_Acme_ADAS_Engineer_2026-09-26.txt --fill 93.8 --pages 1 \\
        --iterations 2 --date 2026-09-26
    (or pass --company/--role/--url explicitly; explicit flags win over --jd)

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


def read_jd_fields(path: Path) -> dict[str, str]:
    """Role (line 1), Job URL and Company from an exported JD file, so untrusted posting text never
    has to travel through a shell command line. Missing pieces stay absent."""
    lines = path.read_text(encoding="utf-8").splitlines()
    fields: dict[str, str] = {}
    if lines and lines[0].strip():
        fields["Role"] = lines[0].strip()
    for line in lines:
        if line.startswith("Job URL:") and "Job_URL" not in fields:
            fields["Job_URL"] = line[len("Job URL:"):].strip()
        elif line.startswith("Source:") and "Company" not in fields:
            source = line[len("Source:"):].strip()
            fields["Company"] = source.rsplit(" (", 1)[0].strip() if source.endswith(")") else source
    return fields


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", required=True)
    parser.add_argument("--company")
    parser.add_argument("--jd", type=Path, help="exported JD file: reads role, company and URL from it (explicit flags win)")
    parser.add_argument("--role")
    parser.add_argument("--url")
    parser.add_argument("--fill", default="?")
    parser.add_argument("--pages", default="?")
    parser.add_argument("--iterations", default="?")
    parser.add_argument("--date", required=True)
    add_project_argument(parser)
    args = parser.parse_args()
    chdir_to_project_root(args.project)
    from_jd = read_jd_fields(args.jd) if args.jd else {}
    company = args.company or from_jd.get("Company") or (args.jd.parent.name if args.jd else None)
    if not company:
        parser.error("--company or --jd is required")
    role = args.role or from_jd.get("Role") or "Not specified"
    url = args.url or from_jd.get("Job_URL") or "Not specified"
    append_row(LOG_PATH, {
        "Date": args.date, "Company": company, "Role": role, "Job_URL": url,
        "Fill_Pct": args.fill, "Pages": args.pages, "Iterations": args.iterations, "Resume_File": args.file,
    })
    print(f"Logged: {company} | {role} | {args.fill}% | {args.iterations} iteration(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

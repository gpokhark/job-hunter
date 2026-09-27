"""JSON/CSV exports of tracked applications, shared by `job-hunter export-applications` and the
live radar server (which refreshes both files after every committed application write).

`CSV_COLUMNS` is the fixed, documented column order. Any *string* cell that starts with a
spreadsheet formula trigger (`=`, `+`, `-`, `@`, tab, CR) is prefixed with `'` so opening the CSV
in Excel/Sheets can never execute scraped or hand-typed text as a formula.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .atomic import atomic_write_text

CSV_COLUMNS = [
    "source_key", "job_id", "company", "title", "url", "status", "applied_at", "location",
    "posted_at", "score", "salary_evidence", "notes", "created_at", "updated_at",
]

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return "'" + value if value.startswith(_FORMULA_PREFIXES) else value
    return str(value)


def applications_csv(rows: Iterable[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow([csv_safe(row.get(column)) for column in CSV_COLUMNS])
    return buffer.getvalue()


def applications_json(rows: Iterable[dict[str, Any]]) -> str:
    return json.dumps(list(rows), indent=2, ensure_ascii=False, default=str) + "\n"


def write_applications_exports(directory: Path, rows: list[dict[str, Any]]) -> tuple[Path, Path]:
    """Atomically (re)write `applications.json` then `applications.csv` under `directory`."""
    directory = Path(directory)
    json_path = directory / "applications.json"
    csv_path = directory / "applications.csv"
    atomic_write_text(json_path, applications_json(rows))
    atomic_write_text(csv_path, applications_csv(rows))
    return json_path, csv_path

import csv
import io
import json

from job_hunter.applications_export import (
    CSV_COLUMNS,
    applications_csv,
    applications_json,
    csv_safe,
    write_applications_exports,
)


def _row(**over):
    row = {
        "source_key": "acme", "job_id": "42", "company": "Acme", "title": "Engineer",
        "url": "https://example.com/42", "status": "applied", "applied_at": "2026-09-20",
        "location": "Detroit, MI", "posted_at": "2026-09-01T00:00:00Z", "score": 82,
        "salary_evidence": "$100,000 - $120,000", "notes": "referral",
        "created_at": "2026-09-20T10:00:00+00:00", "updated_at": "2026-09-21T10:00:00+00:00",
    }
    row.update(over)
    return row


def test_csv_columns_are_fixed_and_documented_order():
    assert CSV_COLUMNS == [
        "source_key", "job_id", "company", "title", "url", "status", "applied_at", "location",
        "posted_at", "score", "salary_evidence", "notes", "created_at", "updated_at",
    ]


def test_csv_safe_prefixes_formula_starts_only_for_strings():
    assert csv_safe("=HYPERLINK(\"http://x\")") == "'=HYPERLINK(\"http://x\")"
    for start in ("+1", "-5% pay", "@cmd", "\tx", "\rx"):
        assert csv_safe(start) == "'" + start
    assert csv_safe("plain") == "plain"
    assert csv_safe(None) == ""
    assert csv_safe(-1) == "-1"  # numbers are not text and are left alone
    assert csv_safe(82) == "82"


def test_csv_round_trips_through_a_real_csv_reader_and_guards_hostile_cells():
    rows = [_row(), _row(job_id="43", notes="=1+1", title='He said "hi", ok\nnext line', score=None)]
    parsed = list(csv.DictReader(io.StringIO(applications_csv(rows))))
    assert list(parsed[0]) == CSV_COLUMNS
    assert parsed[0]["company"] == "Acme" and parsed[0]["score"] == "82"
    assert parsed[1]["notes"] == "'=1+1"
    assert parsed[1]["title"] == 'He said "hi", ok\nnext line'
    assert parsed[1]["score"] == ""


def test_empty_export_is_a_header_only_csv_and_an_empty_json_list():
    assert applications_csv([]) == ",".join(CSV_COLUMNS) + "\n"
    assert json.loads(applications_json([])) == []


def test_json_keeps_row_order_and_unicode():
    rows = [_row(job_id="2", notes="café"), _row(job_id="1")]
    text = applications_json(rows)
    assert "café" in text and text.endswith("\n")
    assert [r["job_id"] for r in json.loads(text)] == ["2", "1"]


def test_write_exports_creates_both_files_and_is_idempotent(tmp_path):
    rows = [_row(), _row(job_id="43")]
    json_path, csv_path = write_applications_exports(tmp_path / "data", rows)
    assert json_path.name == "applications.json" and csv_path.name == "applications.csv"
    first = (json_path.read_text(), csv_path.read_text())
    write_applications_exports(tmp_path / "data", rows)
    assert (json_path.read_text(), csv_path.read_text()) == first
    assert not list((tmp_path / "data").glob(".*.tmp"))  # atomic writes leave no temp files

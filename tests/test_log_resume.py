import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from log_resume import HEADERS, append_row  # noqa: E402


def _row(**over):
    row = {"Date": "2026-09-26", "Company": "Acme", "Role": "ADAS Engineer", "Job_URL": "https://x.test/1",
           "Fill_Pct": "93.8", "Pages": "1", "Iterations": "2", "Resume_File": "Doe_CV_Acme_ADAS_2026-09-26.html"}
    row.update(over)
    return row


def test_header_is_written_once_and_rows_append(tmp_path):
    log = tmp_path / "data" / "output" / "resume_log.csv"
    append_row(log, _row())
    append_row(log, _row(Company="Beta", Iterations="1"))
    rows = list(csv.reader(log.open(encoding="utf-8")))
    assert rows[0] == HEADERS == ["Date", "Company", "Role", "Job_URL", "Fill_Pct", "Pages", "Iterations", "Resume_File"]
    assert [r[1] for r in rows[1:]] == ["Acme", "Beta"]


def test_cells_that_look_like_spreadsheet_formulas_are_neutralised(tmp_path):
    log = tmp_path / "resume_log.csv"
    append_row(log, _row(Role="=HYPERLINK(\"http://evil\")", Company="+cmd"))
    row = list(csv.DictReader(log.open(encoding="utf-8")))[0]
    assert row["Role"].startswith("'=") and row["Company"] == "'+cmd"


def test_jd_file_supplies_role_company_and_url_without_a_command_line(tmp_path):
    from log_resume import read_jd_fields

    jd = tmp_path / "JD.txt"
    jd.write_text(
        "Engineer $(touch /tmp/pwned) `id`\nSomewhere\n\nSummary\nPosted: 2026-09-01\nJob ID: 1\n"
        "Job URL: https://x.test/1\nSource: Acme Corp (acme)\n\nDescription\nBody\n",
        encoding="utf-8",
    )
    fields = read_jd_fields(jd)
    assert fields == {"Role": "Engineer $(touch /tmp/pwned) `id`", "Job_URL": "https://x.test/1", "Company": "Acme Corp"}

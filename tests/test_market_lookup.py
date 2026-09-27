import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from market_lookup import fetch_matches, main, render_text  # noqa: E402

from job_hunter.models import Job, LocationConfidence, SponsorshipStatus
from job_hunter.storage import Storage


def make_job(**updates):
    values = dict(
        source_key="gm", source_platform="test", company="General Motors", job_id="1",
        title="Autonomous Vehicle Test Engineer", url="https://example.com/1",
        us_eligible=True, location_confidence=LocationConfidence.HIGH,
        city=None, state="MI", salary_min=105420.0, salary_max=140000.0, salary_currency="USD",
        visa_sponsorship=SponsorshipStatus.UNMENTIONED,
    )
    values.update(updates)
    return Job(**values)


def _seed(db_path: Path) -> None:
    with Storage(db_path) as storage:
        storage.upsert_job(make_job(job_id="1", state="MI", salary_min=105420.0, salary_max=140000.0))
        storage.upsert_job(make_job(
            source_key="gm", job_id="2", state="AZ", salary_min=135420.0, salary_max=165000.0,
        ))
        storage.upsert_job(make_job(
            source_key="ford", company="Ford", job_id="3",
            title="ADAS Help Me Drive Verification & Validation Engineer",
            state="MI", salary_min=66660.0, salary_max=175000.0,
            visa_sponsorship=SponsorshipStatus.AVAILABLE,
        ))
        storage.upsert_job(make_job(
            source_key="rivian", company="Rivian", job_id="4",
            title="Sr. Autonomy Systems Validation Engineer",
            state="AZ", salary_min=117500.0, salary_max=146900.0,
        ))
        storage.upsert_job(make_job(
            source_key="caterpillar", company="Caterpillar", job_id="5",
            title="Software Engineer, Payroll Systems",  # no title-keyword match
            state="IL", salary_min=90000.0, salary_max=120000.0,
        ))
        storage.upsert_job(make_job(
            source_key="caterpillar", company="Caterpillar", job_id="6",
            title="Autonomy Engineer", state="IL", salary_min=None, salary_max=None,
        ))
        storage.upsert_job(make_job(
            source_key="honda", company="Honda", job_id="7",
            title="Autonomous Vehicle Test Engineer", state="OH",
            us_eligible=False, salary_min=100000.0, salary_max=130000.0,
        ))
        storage.connection.execute("UPDATE jobs SET status='closed' WHERE job_id='4'")
        storage.connection.commit()


class TestFetchMatches:
    def test_matches_any_of_multiple_title_keywords(self, tmp_path):
        db = tmp_path / "jobs.sqlite3"
        _seed(db)
        rows = fetch_matches(db, ["Autonomous Vehicle", "ADAS", "Autonomy"])
        titles = {r["title"] for r in rows}
        assert "Autonomous Vehicle Test Engineer" in titles
        assert "ADAS Help Me Drive Verification & Validation Engineer" in titles
        assert "Software Engineer, Payroll Systems" not in titles

    def test_excludes_non_us_eligible_even_on_title_match(self, tmp_path):
        db = tmp_path / "jobs.sqlite3"
        _seed(db)
        rows = fetch_matches(db, ["Autonomous Vehicle"])
        assert all(r["company"] != "Honda" for r in rows)

    def test_requires_salary_by_default(self, tmp_path):
        db = tmp_path / "jobs.sqlite3"
        _seed(db)
        rows = fetch_matches(db, ["Autonomy Engineer"])
        assert rows == []  # job_id=6 has no salary
        rows_incl = fetch_matches(db, ["Autonomy Engineer"], require_salary=False)
        assert len(rows_incl) == 1
        assert rows_incl[0]["salary_min"] is None

    def test_excludes_closed_by_default_includes_when_asked(self, tmp_path):
        db = tmp_path / "jobs.sqlite3"
        _seed(db)
        rows = fetch_matches(db, ["Autonomy Systems Validation"])
        assert rows == []
        rows_incl = fetch_matches(db, ["Autonomy Systems Validation"], include_closed=True)
        assert len(rows_incl) == 1
        assert rows_incl[0]["status"] == "closed"

    def test_filters_by_company_and_state(self, tmp_path):
        db = tmp_path / "jobs.sqlite3"
        _seed(db)
        rows = fetch_matches(db, ["Autonomous Vehicle"], companies=["gm"], states=["AZ"])
        assert len(rows) == 1
        assert rows[0]["state"] == "AZ" and rows[0]["source_key"] == "gm"

    def test_read_only_connection_does_not_write(self, tmp_path):
        db = tmp_path / "jobs.sqlite3"
        _seed(db)
        before = db.stat().st_mtime_ns
        fetch_matches(db, ["Autonomous Vehicle"])
        assert db.stat().st_mtime_ns == before


class TestRenderText:
    def test_no_matches_message(self):
        assert render_text([]) == "No matches."

    def test_includes_company_title_location_salary_and_sponsorship(self, tmp_path):
        db = tmp_path / "jobs.sqlite3"
        _seed(db)
        rows = fetch_matches(db, ["Autonomous Vehicle"], companies=["gm"], states=["AZ"])
        out = render_text(rows)
        assert "General Motors" in out
        assert "$135,420-$165,000" in out
        assert "sponsorship: unmentioned" in out


class TestCli:
    def test_json_output(self, tmp_path, monkeypatch, capsys):
        db = tmp_path / "jobs.sqlite3"
        _seed(db)
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "settings.yaml").write_text(f"database_path: {db}\n")
        monkeypatch.chdir(tmp_path)

        exit_code = main(["--title-like", "Autonomous Vehicle", "--companies", "gm", "--json"])
        assert exit_code == 0
        rows = json.loads(capsys.readouterr().out)
        assert len(rows) == 2

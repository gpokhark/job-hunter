import csv
import io
from datetime import UTC, datetime, timedelta

from job_hunter.config import CandidateProfile
from job_hunter.models import Job, LocationConfidence
from job_hunter.near_miss import (
    GENERIC_STRONG_TERMS,
    html_to_text,
    matched_terms,
    read_last_scan,
    render_csv,
    render_html,
    scan,
    strip_boilerplate,
    write_last_scan,
)
from job_hunter.storage import Storage

NOW = datetime(2026, 10, 1, tzinfo=UTC)
PROFILE = CandidateProfile(
    target_title_terms=["robotics"],
    strong_relevance_terms=["ADAS", "perception", "lidar", "sensor fusion", "vehicle"],
)


def make_job(**updates):
    values = dict(
        source_key="acme", source_platform="test", company="Acme", job_id="1",
        title="Chef de Cuisine", url="https://example.com/1", us_eligible=True,
        location_confidence=LocationConfidence.HIGH, posted_at=datetime(2026, 9, 30, tzinfo=UTC),
        description="",
    )
    values.update(updates)
    return Job(**values)


def seed(tmp_path, jobs, first_seen=None):
    db = tmp_path / "jobs.sqlite3"
    with Storage(db) as storage:
        for job in jobs:
            storage.upsert_job(job)
        for (source, job_id), stamp in (first_seen or {}).items():
            storage.connection.execute(
                "UPDATE jobs SET first_seen_at=? WHERE source_key=? AND job_id=?",
                (stamp.isoformat(), source, job_id),
            )
        storage.connection.commit()
    return db


def test_html_to_text_strips_tags_unescapes_and_keeps_block_breaks():
    text = html_to_text("<div><p>First &amp; second</p><p>Third</p><ul><li>a</li><li>b</li></ul></div>")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    assert lines == ["First & second", "Third", "a", "b"]


def test_matched_terms_use_word_boundaries_and_ignore_case():
    assert matched_terms("Works on ADAS and lidar; NOT adaptive", ["ADAS", "lidar", "adapt"]) == ["ADAS", "lidar"]
    assert matched_terms("sensor-fusion team", ["sensor fusion"]) == []
    assert matched_terms("", ["ADAS"]) == []


def test_default_generic_terms_are_documented():
    assert {"vehicle", "driving", "camera"} <= GENERIC_STRONG_TERMS


def test_strip_boilerplate_removes_paragraphs_recurring_across_a_source():
    blurb = "We build autonomous driving technology with ADAS perception lidar sensor fusion for everyone."
    jobs = [make_job(job_id=str(i), description=f"{blurb}\nUnique line {i} about chefs and kitchens here.") for i in range(6)]
    jobs.append(make_job(job_id="x", description=f"{blurb}\nThis role works on perception for real, with a long sentence."))
    cleaned = strip_boilerplate(jobs)
    assert "autonomous driving technology" not in cleaned[("acme", "x")]
    assert "works on perception" in cleaned[("acme", "x")]


def test_strip_boilerplate_keeps_everything_for_small_sources():
    blurb = "We build autonomous driving technology with ADAS perception lidar sensor fusion for everyone."
    jobs = [make_job(job_id=str(i), description=blurb) for i in range(3)]
    assert all(blurb in text for text in strip_boilerplate(jobs).values())


def test_scan_lists_only_rejected_jobs_with_enough_distinct_strong_terms(tmp_path):
    db = seed(tmp_path, [
        make_job(job_id="a", title="Chef de Cuisine", description="perception lidar sensor fusion ADAS vehicle vehicle"),
        make_job(job_id="b", title="Accountant", description="perception only"),
        make_job(job_id="c", title="Robotics Engineer", description="perception lidar sensor fusion ADAS"),
        make_job(job_id="d", title="Driver", description="vehicle vehicle vehicle driving"),
    ])
    result = scan(db, PROFILE, 30, now=NOW)
    assert [r.job_id for r in result.rows] == ["a"]
    assert result.rows[0].terms == ("ADAS", "perception", "lidar", "sensor fusion")
    assert result.rows[0].score == 4 and result.rows[0].occurrences == 4   # 'vehicle' is generic
    assert [r.job_id for r in scan(db, PROFILE, 30, min_terms=1, now=NOW).rows] == ["a", "b"]
    assert result.pool_size == 3                       # a, b, d rejected; c passed the gate


def test_scan_ignore_terms_are_configurable(tmp_path):
    db = seed(tmp_path, [make_job(job_id="a", description="perception vehicle")])
    # by default 'vehicle' is generic, so only one term counts and min_terms=2 is not met
    assert scan(db, PROFILE, 30, min_terms=2, now=NOW).rows == []
    # an explicit ignore set replaces the generic default: now 'vehicle' counts
    result = scan(db, PROFILE, 30, min_terms=2, ignore_terms=frozenset({"lidar"}), now=NOW)
    assert [r.terms for r in result.rows] == [("perception", "vehicle")]


def test_scan_since_and_limit(tmp_path):
    jobs = [make_job(job_id=str(i), title=f"Role {i}", description="perception lidar ADAS") for i in range(4)]
    stamps = {("acme", str(i)): NOW - timedelta(days=10 - 3 * i) for i in range(4)}
    db = seed(tmp_path, jobs, first_seen=stamps)
    since = NOW - timedelta(days=5)
    assert sorted(r.job_id for r in scan(db, PROFILE, 30, since=since, now=NOW).rows) == ["2", "3"]
    assert len(scan(db, PROFILE, 30, limit=2, now=NOW).rows) == 2


def test_scan_without_strong_terms_returns_no_rows_not_an_error(tmp_path):
    db = seed(tmp_path, [make_job(description="perception lidar ADAS")])
    result = scan(db, CandidateProfile(target_title_terms=["robotics"]), 30, now=NOW)
    assert result.rows == [] and result.pool_size == 1


def test_scan_reports_department_coverage_by_source(tmp_path):
    db = seed(tmp_path, [
        make_job(job_id="1", department="Engineering"),
        make_job(job_id="2", department=None),
        make_job(job_id="3", source_key="bigco", department=None),
        make_job(job_id="4", source_key="bigco", department=""),
    ])
    result = scan(db, PROFILE, 30, now=NOW)
    assert (result.eligible_recent, result.empty_department) == (4, 3)
    assert [(c.source_key, c.total, c.empty_department) for c in result.coverage] == [("bigco", 2, 2), ("acme", 2, 1)]


def test_scan_vocabulary_hints_rank_new_title_terms_by_near_miss_frequency(tmp_path):
    desc = "perception lidar ADAS sensor fusion"
    db = seed(tmp_path, [
        make_job(job_id="1", title="Calibration Specialist", description=desc),
        make_job(job_id="2", title="Camera Calibration Lead", description=desc),
        make_job(job_id="3", title="Pastry Chef", description=desc),
        make_job(job_id="4", title="Pastry Chef II", description=desc),
        make_job(job_id="5", title="Pastry Chef Assistant", description="nothing relevant"),
    ])
    hints = scan(db, PROFILE, 30, now=NOW).hints
    by_term = {h.term: h for h in hints}
    assert by_term["calibration"].near_miss_jobs == 2 and by_term["calibration"].gain == 2
    # in two near-misses; it would also admit the non-near-miss 'Pastry Chef Assistant'
    assert by_term["pastry chef"].near_miss_jobs == 2 and by_term["pastry chef"].gain == 3
    assert "camera calibration" not in by_term       # only one near-miss: below the frequency floor
    assert hints[0].near_miss_jobs >= hints[-1].near_miss_jobs


def test_hints_no_longer_hide_a_word_that_is_only_part_of_a_longer_profile_term(tmp_path):
    profile = CandidateProfile(target_title_terms=["vehicle test"], strong_relevance_terms=PROFILE.strong_relevance_terms)
    desc = "perception lidar ADAS sensor fusion"
    db = seed(tmp_path, [
        make_job(job_id="1", title="Test Lead", description=desc),
        make_job(job_id="2", title="Test Analyst II", description=desc),
    ])
    by_term = {h.term: h for h in scan(db, profile, 30, now=NOW).hints}
    assert by_term["test"].near_miss_jobs == 2 and by_term["test"].gain >= 2


def test_hints_skip_phrases_that_would_admit_nothing_new(tmp_path):
    profile = CandidateProfile(
        target_title_terms=["robotics"], strong_relevance_terms=PROFILE.strong_relevance_terms,
        soft_exclude_terms=["supervisor"],
    )
    desc = "perception lidar ADAS sensor fusion"
    db = seed(tmp_path, [
        make_job(job_id="1", title="Pastry Supervisor", description=desc),
        make_job(job_id="2", title="Pastry Supervisor", description=desc),
        make_job(job_id="3", title="Pastry Chef", description=desc),
        make_job(job_id="4", title="Pastry Chef", description=desc),
    ])
    by_term = {h.term: h for h in scan(db, profile, 30, now=NOW).hints}
    assert "pastry chef" in by_term
    assert "supervisor" not in by_term and "pastry supervisor" not in by_term


def _result(tmp_path, title="Perception <script>alert(1)</script> & Co"):
    db = seed(tmp_path, [make_job(job_id="a", title=title, description="perception lidar ADAS sensor fusion")])
    return scan(db, PROFILE, 30, now=NOW)


def test_render_html_escapes_titles_and_shows_coverage_and_hints(tmp_path):
    page = render_html(_result(tmp_path), generated_at=NOW, since=None)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "&amp; Co" in page
    assert "title is the only gate signal" in page


def test_render_csv_has_a_header_and_one_row_per_near_miss(tmp_path):
    rows = list(csv.reader(io.StringIO(render_csv(_result(tmp_path).rows))))
    assert rows[0] == ["rank", "source", "title", "score", "terms", "posted", "first_seen", "url"]
    assert len(rows) == 2 and rows[1][0] == "1" and rows[1][3] == "4"


def test_state_round_trip_and_missing_or_corrupt_state_means_no_since(tmp_path):
    state = tmp_path / "near-miss" / "state.json"
    assert read_last_scan(state) is None
    write_last_scan(state, NOW)
    assert read_last_scan(state) == NOW
    state.write_text("not json")
    assert read_last_scan(state) is None

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from job_hunter.cli import main, parser
from job_hunter.models import Job, LocationConfidence
from job_hunter.storage import Storage

PROFILE_YAML = """\
target_title_terms: [vehicle test, robotics]
target_domains: [validation, ADAS]
strong_relevance_terms: [ADAS, perception, lidar, "sensor fusion", vehicle]
"""


def make_job(**updates):
    values = dict(
        source_key="ford", source_platform="test", company="Ford", job_id="71202",
        title="Vehicle Calibration & Test Supervisor",
        url="https://efds.example/hcmUI/job/71202", us_eligible=True,
        location_confidence=LocationConfidence.HIGH, location_evidence="structured U.S. country",
        posted_at=datetime.now(UTC), description="validation verification vehicle",
    )
    values.update(updates)
    return Job(**values)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text("{}\n")
    (tmp_path / "config" / "candidate_profile.yaml").write_text(PROFILE_YAML)
    (tmp_path / "data" / "searches").mkdir(parents=True)
    with Storage(tmp_path / "data" / "jobs.sqlite3") as storage:
        storage.upsert_job(make_job())
        storage.upsert_job(make_job(job_id="2", source_key="apple", title="Camera Calibration and Test Engineer"))
        storage.upsert_job(make_job(job_id="3", source_key="abb", title="Test Supervisor"))
    archive = {
        "summary": {}, "candidates": [],
        "source_health": [{"source_key": "ford", "company": "Ford", "status": "ok", "job_count": 842}],
    }
    (tmp_path / "data" / "searches" / "default_2026-09-30.json").write_text(json.dumps(archive))
    return tmp_path


def test_why_missed_explains_the_ford_case_end_to_end(project, capsys):
    exit_code = main(["why-missed", "ford:71202"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "Stopped at: prefilter" in out and "no_positive_match" in out
    assert '"test supervisor"' in out and '"calibration"' in out
    assert "default_2026-09-30.json" in out
    assert "Nothing was changed" in out


def test_why_missed_accepts_a_bare_job_id_when_only_one_source_has_it(project, capsys):
    assert main(["why-missed", "71202", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["source_key"] == "ford"


def test_why_missed_by_title_and_json_shape(project, capsys):
    exit_code = main(["why-missed", "Vehicle Calibration & Test", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert data["source_key"] == "ford" and data["job_id"] == "71202"
    assert [s["name"] for s in data["stages"]][3] == "prefilter"
    assert {s["term"] for s in data["suggestions"]} >= {"test supervisor", "calibration"}


def test_the_real_ford_careers_url_is_not_found_with_a_helpful_hint(project, capsys):
    exit_code = main(["why-missed", "https://www.careers.ford.com/job/-/-/48560/101370456832"])
    err = capsys.readouterr().err
    assert exit_code == 2
    assert "no stored job matches" in err and "job id" in err and "source-test" in err


def test_several_matches_ask_for_source_and_id(project, capsys):
    exit_code = main(["why-missed", "test supervisor"])
    err = capsys.readouterr().err
    assert exit_code == 2
    assert "ford:71202" in err and "abb:3" in err


def test_keyword_override_suppresses_suggestions(project, capsys):
    assert main(["why-missed", "ford:71202", "--keyword", "ADAS", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["suggestions"] == []


def test_missing_archive_is_reported_not_fatal(project, capsys):
    (project / "data" / "searches" / "default_2026-09-30.json").unlink()
    assert main(["why-missed", "ford:71202", "--json"]) == 0
    stages = {s["name"]: s for s in json.loads(capsys.readouterr().out)["stages"]}
    assert stages["in archive"]["ok"] is None


def test_project_flag_works_after_the_new_subcommands():
    assert parser().parse_args(["why-missed", "ford:1", "--project", "/x"]).project == Path("/x")
    assert parser().parse_args(["--project", "/y", "why-missed", "ford:1"]).project == Path("/y")


def test_missing_database_exits_2_with_a_hint(project, capsys):
    for path in (project / "data").glob("jobs.sqlite3*"):
        path.unlink()
    assert main(["why-missed", "ford:71202"]) == 2
    assert "no database at" in capsys.readouterr().err


def test_database_missing_a_table_exits_2_not_a_traceback(project, capsys):
    with Storage(project / "data" / "jobs.sqlite3") as storage:
        storage.connection.execute("DROP TABLE source_health")
        storage.connection.commit()
    assert main(["why-missed", "ford:71202"]) == 2
    assert "could not read the database" in capsys.readouterr().err


def test_explicit_missing_search_path_exits_2(project, capsys):
    assert main(["why-missed", "ford:71202", "--search", "data/searches/nope.json"]) == 2
    assert "nope.json" in capsys.readouterr().err


NEAR_DESC = "perception lidar ADAS sensor fusion"


def _near_miss_project(project):
    with Storage(project / "data" / "jobs.sqlite3") as storage:
        storage.upsert_job(make_job(job_id="9", source_key="acme", title="Pastry Chef", description=NEAR_DESC))
    return project


def test_near_misses_writes_reports_and_advances_state(project, tmp_path, capsys):
    _near_miss_project(project)
    out = tmp_path / "reports"
    assert main(["near-misses", "--output-dir", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "Near-misses: 1 new job(s)" in printed
    assert len(list(out.glob("*.html"))) == 1 and len(list(out.glob("*.csv"))) == 1
    assert "last_scan_at" in json.loads((out / "state.json").read_text())


def test_second_run_lists_only_new_jobs_and_all_lists_everything_again(project, tmp_path, capsys):
    _near_miss_project(project)
    out = tmp_path / "reports"
    main(["near-misses", "--output-dir", str(out)])
    capsys.readouterr()
    assert main(["near-misses", "--output-dir", str(out)]) == 0
    assert "Near-misses: 0 new job(s)" in capsys.readouterr().out
    assert main(["near-misses", "--output-dir", str(out), "--all"]) == 0
    assert "Near-misses: 1 new job(s)" in capsys.readouterr().out


def test_no_state_flag_leaves_state_untouched(project, tmp_path):
    _near_miss_project(project)
    out = tmp_path / "reports"
    assert main(["near-misses", "--output-dir", str(out), "--no-state"]) == 0
    assert not (out / "state.json").exists()


def test_an_unwritable_output_location_exits_2_and_never_advances_state(project, tmp_path, capsys):
    _near_miss_project(project)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    assert main(["near-misses", "--output-dir", str(blocker / "sub")]) == 2
    assert "could not write the near-miss report" in capsys.readouterr().err
    assert not (blocker / "sub").exists()


def test_first_run_is_capped_at_100_rows_without_a_limit(project, tmp_path, capsys):
    with Storage(project / "data" / "jobs.sqlite3") as storage:
        for i in range(120):
            storage.upsert_job(make_job(job_id=f"n{i}", title=f"Pastry Chef {i}", description=NEAR_DESC))
    assert main(["near-misses", "--output-dir", str(tmp_path / "r")]) == 0
    assert "Near-misses: 100 new job(s)" in capsys.readouterr().out


def test_near_misses_project_flag_and_options_parse():
    args = parser().parse_args(["near-misses", "--project", "/z", "--min-terms", "2", "--ignore-term", "lidar", "--all"])
    assert args.project == Path("/z") and args.min_terms == 2 and args.ignore_term == ["lidar"] and args.all


def test_second_file_write_failure_exits_2_and_leaves_state_byte_identical(project, tmp_path, capsys, monkeypatch):
    from job_hunter import near_miss

    _near_miss_project(project)
    out = tmp_path / "reports"
    out.mkdir()
    state = out / "state.json"
    state.write_text('{"last_scan_at": "2026-01-01T00:00:00+00:00"}\n')
    before = state.read_bytes()
    real, calls = near_miss.atomic_write_text, []

    def flaky(path, text):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("disk full")
        return real(path, text)

    monkeypatch.setattr(near_miss, "atomic_write_text", flaky)
    assert main(["near-misses", "--output-dir", str(out)]) == 2
    assert "could not write the near-miss report" in capsys.readouterr().err
    assert state.read_bytes() == before


def test_naive_state_timestamp_does_not_crash_the_command(project, tmp_path, capsys):
    _near_miss_project(project)
    out = tmp_path / "reports"
    out.mkdir()
    (out / "state.json").write_text('{"last_scan_at": "2026-10-01T00:00:00"}')
    assert main(["near-misses", "--output-dir", str(out)]) == 0
    assert "Near-misses: 1 new job(s)" in capsys.readouterr().out


def test_near_misses_with_a_missing_database_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text("{}\n")
    (tmp_path / "config" / "candidate_profile.yaml").write_text(PROFILE_YAML)
    assert main(["near-misses", "--output-dir", str(tmp_path / "r")]) == 2
    assert "no database at" in capsys.readouterr().err

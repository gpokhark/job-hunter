import json
import os
from datetime import UTC, datetime, timedelta

from job_hunter.cleanup import classify_report, run_cleanup, scan_reports, select_reports_to_delete
from job_hunter.config import Settings
from job_hunter.models import Job, LocationConfidence
from job_hunter.normalizer import description_hash
from job_hunter.storage import Storage


def _touch(path, *, age_days: float, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    mtime = (datetime.now(UTC) - timedelta(days=age_days)).timestamp()
    os.utime(path, (mtime, mtime))


def make_job(description="first", **updates):
    values = dict(
        source_key="acme",
        source_platform="test",
        company="Acme",
        job_id="42",
        title="Engineer",
        url="https://example.com/42",
        us_eligible=True,
        location_confidence=LocationConfidence.HIGH,
        description=description,
        content_hash=description_hash(description),
    )
    values.update(updates)
    return Job(**values)


# --- classify_report: exact-pattern safety (docs/retention-cleanup-plan.md section 3.5) ---


def test_classify_report_matches_new_and_old_plain_diff_timestamp_formats(tmp_path):
    new = tmp_path / "2026-09-06-T-23-30-04.html"
    old = tmp_path / "20260906T145247.html"
    new.write_text("x")
    old.write_text("x")
    assert classify_report(new, role="profile-diff").kind == "profile_diff"
    assert classify_report(old, role="profile-diff").kind == "profile_diff"


def test_classify_report_matches_archive_diff_and_extracts_slug(tmp_path):
    path = tmp_path / "archive-default_2026-09-06-2026-09-06-T-23-30-05.html"
    path.write_text("x")
    result = classify_report(path, role="profile-diff")
    assert result.kind == "archive_diff"
    assert result.group == "default"


def test_classify_report_matches_radar_and_extracts_slug(tmp_path):
    path = tmp_path / "adas_2026-09-04.html"
    path.write_text("x")
    result = classify_report(path, role="radar")
    assert result.kind == "radar"
    assert result.group == "adas"


def test_classify_report_rejects_hand_named_files(tmp_path):
    """Regression for the exact live case that surfaced this requirement:
    data/profile-diff/soft_exclude_terms_removed_2026-09-05.html must never be treated as a
    generated report just because it lives in the same directory and ends in .html."""
    path = tmp_path / "soft_exclude_terms_removed_2026-09-05.html"
    path.write_text("x")
    assert classify_report(path, role="profile-diff") is None


def test_classify_report_rejects_a_diff_shaped_name_in_the_radar_role(tmp_path):
    """A plain timestamp is a valid profile-diff report shape but not a radar one — role
    matters, not just the filename in isolation."""
    path = tmp_path / "2026-09-06-T-23-30-04.html"
    path.write_text("x")
    assert classify_report(path, role="radar") is None


# --- select_reports_to_delete: per-group "keep latest N" + age cutoff ---


def test_select_reports_to_delete_keeps_latest_n_per_group_regardless_of_age(tmp_path):
    now = datetime.now(UTC)
    old1 = tmp_path / "adas_2026-01-01.html"
    old2 = tmp_path / "adas_2026-01-02.html"
    _touch(old1, age_days=100)
    _touch(old2, age_days=99)
    files = [classify_report(old1, role="radar"), classify_report(old2, role="radar")]
    to_delete = select_reports_to_delete(files, cutoff=now - timedelta(days=15), keep_latest_per_group=2)
    assert to_delete == []  # both are within the latest-2 floor for this slug, despite being ancient


def test_select_reports_to_delete_deletes_beyond_the_floor_only_if_also_old(tmp_path):
    now = datetime.now(UTC)
    newest = tmp_path / "adas_2026-09-06.html"
    middle = tmp_path / "adas_2026-09-01.html"
    oldest_but_recent = tmp_path / "adas_2026-08-25.html"
    _touch(newest, age_days=1)
    _touch(middle, age_days=20)
    _touch(oldest_but_recent, age_days=5)  # 3rd-newest by mtime, but not old enough itself
    files = [
        classify_report(newest, role="radar"),
        classify_report(middle, role="radar"),
        classify_report(oldest_but_recent, role="radar"),
    ]
    to_delete = select_reports_to_delete(files, cutoff=now - timedelta(days=15), keep_latest_per_group=2)
    # newest + oldest_but_recent are the 2 most-recently-modified -> protected by the floor.
    # middle is 3rd by mtime (outside the floor) AND older than the 15-day cutoff -> deleted.
    assert [f.path for f in to_delete] == [middle]


def test_select_reports_to_delete_groups_independently_by_kind_and_slug(tmp_path):
    now = datetime.now(UTC)
    adas = tmp_path / "adas_2026-01-01.html"
    default = tmp_path / "default_2026-01-01.html"
    _touch(adas, age_days=100)
    _touch(default, age_days=100)
    files = [classify_report(adas, role="radar"), classify_report(default, role="radar")]
    # keep_latest=1 per group: each slug has only 1 file, so each is "the latest" in its own
    # group and neither is deleted, even though both are ancient.
    to_delete = select_reports_to_delete(files, cutoff=now - timedelta(days=15), keep_latest_per_group=1)
    assert to_delete == []


def test_select_reports_to_delete_keep_latest_zero_disables_the_floor(tmp_path):
    now = datetime.now(UTC)
    path = tmp_path / "adas_2026-01-01.html"
    _touch(path, age_days=100)
    files = [classify_report(path, role="radar")]
    to_delete = select_reports_to_delete(files, cutoff=now - timedelta(days=15), keep_latest_per_group=0)
    assert [f.path for f in to_delete] == [path]


def test_scan_reports_ignores_files_outside_known_directories_by_construction(tmp_path):
    profile_diff_dir = tmp_path / "profile-diff"
    radar_dir = tmp_path / "radar"
    _touch(profile_diff_dir / "2026-09-06-T-23-30-04.html", age_days=1)
    _touch(profile_diff_dir / "hand-named.html", age_days=1)
    _touch(radar_dir / "default_2026-09-06.html", age_days=1)
    files = scan_reports(profile_diff_dir=profile_diff_dir, radar_dir=radar_dir)
    assert {f.path.name for f in files} == {"2026-09-06-T-23-30-04.html", "default_2026-09-06.html"}


# --- run_cleanup: end-to-end, dry-run vs --apply, jobs + reports together ---


def _settings(tmp_path, **retention_overrides) -> Settings:
    settings = Settings(database_path=tmp_path / "jobs.sqlite3")
    for key, value in retention_overrides.items():
        setattr(settings.retention, key, value)
    return settings


def test_run_cleanup_dry_run_deletes_nothing(tmp_path):
    now = datetime.now(UTC)
    settings = _settings(tmp_path, closed_job_after_days=10, report_after_days=15, keep_latest_reports_per_slug=0)
    with Storage(settings.database_path) as storage:
        storage.upsert_job(make_job(job_id="stale", last_seen_at=now - timedelta(days=30)))
        storage.connection.execute("UPDATE jobs SET status='closed' WHERE job_id='stale'")
        storage.connection.commit()

    profile_diff_dir = tmp_path / "profile-diff"
    radar_dir = tmp_path / "radar"
    _touch(profile_diff_dir / "20260101T000000.html", age_days=100)

    result = run_cleanup(
        settings, apply=False, now=now, profile_diff_dir=profile_diff_dir, radar_dir=radar_dir,
        export_dir=tmp_path / "cleanup-exports",
    )

    assert result.closed_jobs_eligible == 1
    assert result.closed_jobs_deleted == 0
    assert len(result.reports_eligible) == 1
    assert result.reports_deleted == []
    assert result.export_path is None
    with Storage(settings.database_path) as storage:
        assert storage.stats()["closed"] == 1
    assert (profile_diff_dir / "20260101T000000.html").exists()


def test_run_cleanup_apply_deletes_and_writes_export(tmp_path):
    now = datetime.now(UTC)
    settings = _settings(tmp_path, closed_job_after_days=10, report_after_days=15, keep_latest_reports_per_slug=0)
    with Storage(settings.database_path) as storage:
        storage.upsert_job(make_job(job_id="stale", last_seen_at=now - timedelta(days=30)))
        storage.connection.execute("UPDATE jobs SET status='closed' WHERE job_id='stale'")
        storage.connection.commit()

    profile_diff_dir = tmp_path / "profile-diff"
    radar_dir = tmp_path / "radar"
    stale_report = profile_diff_dir / "20260101T000000.html"
    _touch(stale_report, age_days=100)
    export_dir = tmp_path / "cleanup-exports"

    result = run_cleanup(
        settings, apply=True, now=now, profile_diff_dir=profile_diff_dir, radar_dir=radar_dir,
        export_dir=export_dir,
    )

    assert result.closed_jobs_deleted == 1
    assert result.reports_deleted == [stale_report]
    assert not stale_report.exists()
    with Storage(settings.database_path) as storage:
        assert storage.get_job("acme", "stale") is None

    assert result.export_path is not None
    payload = json.loads(result.export_path.read_text())
    assert payload["deleted_jobs"][0]["job_id"] == "stale"
    assert payload["deleted_reports"] == [str(stale_report)]


def test_run_cleanup_keep_latest_protects_reports_apply_mode(tmp_path):
    now = datetime.now(UTC)
    settings = _settings(tmp_path, keep_latest_reports_per_slug=1, report_after_days=15)
    profile_diff_dir = tmp_path / "profile-diff"
    radar_dir = tmp_path / "radar"
    newest = radar_dir / "default_2026-09-06.html"
    oldest = radar_dir / "default_2026-01-01.html"
    _touch(newest, age_days=1)
    _touch(oldest, age_days=100)

    result = run_cleanup(
        settings, apply=True, jobs_only=False, reports_only=True, now=now,
        profile_diff_dir=profile_diff_dir, radar_dir=radar_dir, export_dir=tmp_path / "cleanup-exports",
    )

    assert result.reports_deleted == [oldest]
    assert newest.exists()
    assert not oldest.exists()


def test_run_cleanup_jobs_only_skips_reports(tmp_path):
    now = datetime.now(UTC)
    settings = _settings(tmp_path, keep_latest_reports_per_slug=0, report_after_days=15)
    profile_diff_dir = tmp_path / "profile-diff"
    radar_dir = tmp_path / "radar"
    stale_report = profile_diff_dir / "20260101T000000.html"
    _touch(stale_report, age_days=100)

    result = run_cleanup(
        settings, apply=True, jobs_only=True, now=now,
        profile_diff_dir=profile_diff_dir, radar_dir=radar_dir, export_dir=tmp_path / "cleanup-exports",
    )

    assert result.reports_eligible == []
    assert stale_report.exists()  # untouched — reports scope was skipped entirely


def test_run_cleanup_no_export_flag_skips_writing_export(tmp_path):
    now = datetime.now(UTC)
    settings = _settings(tmp_path)
    with Storage(settings.database_path) as storage:
        storage.upsert_job(make_job(job_id="stale", last_seen_at=now - timedelta(days=30)))
        storage.connection.execute("UPDATE jobs SET status='closed' WHERE job_id='stale'")
        storage.connection.commit()

    result = run_cleanup(
        settings, apply=True, now=now, write_export=False,
        profile_diff_dir=tmp_path / "profile-diff", radar_dir=tmp_path / "radar",
        export_dir=tmp_path / "cleanup-exports",
    )
    assert result.closed_jobs_deleted == 1
    assert result.export_path is None
    assert not (tmp_path / "cleanup-exports").exists()
